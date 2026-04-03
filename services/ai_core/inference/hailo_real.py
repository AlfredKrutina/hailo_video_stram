"""
Hailo NPU inference přes hailo_platform (HailoRT): HEF, VDevice, InferVStreams.

Vyžaduje shodnou verzi HailoRT wheelu s ovladačem na hostu. Výchozí postprocess: YOLOv8 hlava
jako u ONNX ([1, 4+nc, N]). Více výstupních vstreamů: bere se první (nebo RPY_HAILO_OUTPUT_NAME).
"""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

from shared.schemas.config import ModelConfig
from shared.schemas.detections import DetectionFrame

from services.ai_core.inference.yolo_postprocess import postprocess_yolov8_head

logger = logging.getLogger("ai_core.inference.hailo_real")


def _device_path() -> Path:
    return Path(os.environ.get("RPY_HAILO_DEVICE", "/dev/hailo0").strip() or "/dev/hailo0")


class HailoBackend:
    """Inference na Hailo zařízení přes HailoRT Python API."""

    infer_implemented: bool = True

    def __init__(self) -> None:
        dev = _device_path()
        if not dev.exists():
            raise RuntimeError(
                f"Hailo: chybí {dev} — namapujte zařízení v docker-compose (devices) a ověřte driver na hostu.",
            )
        hef = os.environ.get("RPY_HAILO_HEF_PATH", "").strip()
        if not hef:
            raise RuntimeError(
                "Hailo: nastavte RPY_HAILO_HEF_PATH na .hef model (nebo použijte RPY_INFER_BACKEND=onnx / stub).",
            )
        if not Path(hef).is_file():
            raise RuntimeError(f"Hailo: RPY_HAILO_HEF_PATH neexistuje: {hef}")

        try:
            import hailo_platform as hpf  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                "Hailo: nainstalujte hailo_platform / HailoRT wheel odpovídající vašemu image (pip v Dockerfile).",
            ) from e

        try:
            import cv2  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError(
                "Hailo backend potřebuje opencv-python-headless pro resize snímku (stejně jako ONNX).",
            ) from e

        self._cv2 = cv2
        self._hpf = hpf
        self._hef_path = hef
        self._hef = hpf.HEF(hef)
        self._vdevice_cm = hpf.VDevice()
        self._vdevice = self._vdevice_cm.__enter__()
        self._network_group = self._configure_group(hpf, self._hef, self._vdevice)
        self._ng_params = self._network_group.create_params()

        in_infos = self._hef.get_input_vstream_infos()
        out_infos = self._hef.get_output_vstream_infos()
        if not in_infos:
            raise RuntimeError("Hailo: HEF neobsahuje vstupní vstream.")
        if not out_infos:
            raise RuntimeError("Hailo: HEF neobsahuje výstupní vstream.")

        self._in_info = in_infos[0]
        self._out_infos = out_infos
        self._input_name = self._in_info.name

        quant_in = os.environ.get("RPY_HAILO_QUANTIZED_INPUT", "0").lower() in ("1", "true", "yes")
        fmt = hpf.FormatType.UINT8 if quant_in else hpf.FormatType.FLOAT32

        try:
            in_params = hpf.InputVStreamParams.make_from_network_group(
                self._network_group,
                quantized=quant_in,
                format_type=fmt,
            )
            out_params = hpf.OutputVStreamParams.make_from_network_group(
                self._network_group,
                quantized=False,
                format_type=hpf.FormatType.FLOAT32,
            )
        except Exception:
            in_params = hpf.InputVStreamParams.make_from_network_group(
                self._network_group,
                quantized=False,
                format_type=hpf.FormatType.FLOAT32,
            )
            out_params = hpf.OutputVStreamParams.make_from_network_group(
                self._network_group,
                quantized=False,
                format_type=hpf.FormatType.FLOAT32,
            )

        self._activate_ctx = self._network_group.activate(self._ng_params)
        self._activate_ctx.__enter__()
        self._infer_pipeline = hpf.InferVStreams(self._network_group, in_params, out_params)
        self._infer_pipeline.__enter__()

        self._in_w, self._in_h = self._resolve_input_hw(self._in_info)
        self._last_det_count = 0
        self._logged_once = False

        atexit.register(self._atexit_cleanup)

        logger.info(
            "hailo_backend_init_ok",
            extra={
                "extra_data": {
                    "hef": hef,
                    "device": str(dev),
                    "in_hw": (self._in_w, self._in_h),
                    "outputs": [o.name for o in self._out_infos],
                },
            },
        )

    def _resolve_input_hw(self, in_info: Any) -> tuple[int, int]:
        w_env = os.environ.get("RPY_HAILO_INPUT_WIDTH", "").strip()
        h_env = os.environ.get("RPY_HAILO_INPUT_HEIGHT", "").strip()
        if w_env.isdigit() and h_env.isdigit():
            return int(w_env), int(h_env)
        shape = getattr(in_info, "shape", None)
        if shape is not None:
            try:
                dims = list(shape) if not hasattr(shape, "__iter__") else [int(x) for x in shape]
            except Exception:
                dims = []
            if len(dims) >= 2:
                a, b = dims[-2], dims[-1]
                if len(dims) >= 3 and dims[0] in (1, 3):
                    return int(b), int(dims[1])
                return int(b), int(a)
        return 640, 640

    def _configure_group(self, hpf: Any, hef: Any, vdevice: Any) -> Any:
        iface_env = (os.environ.get("RPY_HAILO_STREAM_INTERFACE") or "").strip().upper()
        iface_cls = getattr(hpf, "HailoStreamInterface", None)
        candidates: list[Any] = []
        if iface_env and iface_cls:
            mapped = getattr(iface_cls, iface_env, None)
            if mapped is not None:
                candidates.append(mapped)
        if iface_cls:
            for name in ("PCIe", "INTEGRATED", "ETH", "MIPI"):
                v = getattr(iface_cls, name, None)
                if v is not None and v not in candidates:
                    candidates.append(v)

        last_err: Exception | None = None
        for interface in candidates or [None]:
            try:
                if interface is not None:
                    cp = hpf.ConfigureParams.create_from_hef(hef, interface=interface)
                else:
                    cp = hpf.ConfigureParams.create_from_hef(hef)
                ngs = vdevice.configure(hef, cp)
                if not ngs:
                    raise RuntimeError("configure returned no network groups")
                return ngs[0]
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(
            f"Hailo: configure selhal (zkuste RPY_HAILO_STREAM_INTERFACE=PCIe|INTEGRATED): {last_err}",
        ) from last_err

    def _atexit_cleanup(self) -> None:
        try:
            if getattr(self, "_infer_pipeline", None) is not None:
                self._infer_pipeline.__exit__(None, None, None)
                self._infer_pipeline = None
            if getattr(self, "_activate_ctx", None) is not None:
                self._activate_ctx.__exit__(None, None, None)
                self._activate_ctx = None
            if getattr(self, "_vdevice_cm", None) is not None:
                self._vdevice_cm.__exit__(None, None, None)
                self._vdevice_cm = None
        except Exception:
            pass

    def telemetry_extra(self) -> dict[str, Any]:
        return {
            "hailo_infer_implemented": True,
            "hailo_last_det_count": self._last_det_count,
            "hailo_hef_path": self._hef_path,
        }

    def _prepare_tensor(self, rgb: np.ndarray) -> dict[str, np.ndarray]:
        img = self._cv2.resize(rgb, (self._in_w, self._in_h), interpolation=self._cv2.INTER_LINEAR)
        chw = np.transpose(img, (2, 0, 1)).astype(np.float32) / 255.0
        batch = np.expand_dims(chw, 0)
        return {self._input_name: batch}

    def _pick_output(self, results: dict[str, Any]) -> np.ndarray:
        name_pick = os.environ.get("RPY_HAILO_OUTPUT_NAME", "").strip()
        names = [o.name for o in self._out_infos]
        key = name_pick if name_pick and name_pick in results else names[0]
        return np.asarray(results[key])

    def infer(
        self,
        rgb: np.ndarray,
        frame_id: int,
        timestamp_ns: int,
        source_uri: str,
        model: ModelConfig,
    ) -> DetectionFrame:
        h, w = rgb.shape[:2]
        input_data = self._prepare_tensor(rgb)
        results = self._infer_pipeline.infer(input_data)
        out = self._pick_output(results)
        if not self._logged_once:
            self._logged_once = True
            logger.info(
                "hailo_infer_output_shape",
                extra={"extra_data": {"shape": getattr(out, "shape", None)}},
            )
        detections = postprocess_yolov8_head(out, w, h, model, self._in_w, self._in_h)
        self._last_det_count = len(detections)
        return DetectionFrame(
            frame_id=frame_id,
            timestamp_ns=timestamp_ns,
            width=w,
            height=h,
            source_uri=source_uri,
            detections=detections,
        )
