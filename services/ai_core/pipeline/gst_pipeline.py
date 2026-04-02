"""
GStreamer ingest for vision pipeline.

Two ingress modes:
- **RTSP** — `playbin` with video-only flags (avoids decodebin failing on obscure audio codecs; see NVR/VLC warnings).
- **Other URIs** — `uridecodebin` (HTTP file, YouTube-resolved URL, local file).

Downstream: fixed RGB size → tee → appsink (numpy inference) + jpegenc → MJPEG queue.

Errors on the bus trigger recovery with backoff; see `shared.errors.ErrorCode.GST_PIPELINE_ERROR` for log correlation.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from shared.schemas.config import AppConfig, ModelConfig
from shared.schemas.telemetry import PipelineState

from services.ai_core.source_resolve import resolve_playback_uri, sanitize_uri
from services.ai_core.pipeline.rtsp_probe import rtsp_describe_ok
from services.ai_core.pipeline.state import PipelineController

if TYPE_CHECKING:
    pass

logger = logging.getLogger("ai_core.gst")

_GST_AVAILABLE = False
try:
    import gi  # noqa: PLC0415

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst  # noqa: PLC0415

    _GST_AVAILABLE = True
except Exception as e:
    logger.warning("gstreamer_not_available", extra={"extra_data": {"err": str(e)}})
    Gst = None  # type: ignore[misc, assignment]
    GLib = None  # type: ignore[misc, assignment]


def gst_init() -> bool:
    if not _GST_AVAILABLE or Gst is None:
        return False
    Gst.init(None)
    return True


# playbin: pouze video — bez audio větve (kamery často posílají G.711/PCM, které rozbijí decodebin)
_GST_PLAY_FLAG_VIDEO = 1


def _rtsp_use_playbin_video_only() -> bool:
    return os.environ.get("RPY_RTSP_USE_URIDECODEBIN", "").lower() not in (
        "1",
        "true",
        "yes",
    )


class GstVisionPipeline:
    def __init__(
        self,
        app_config: AppConfig,
        controller: PipelineController,
        on_frame: Callable[..., Any],
        on_jpeg: Callable[[bytes], None],
        on_state: Callable[[PipelineState, str | None], None],
    ) -> None:
        self._cfg = app_config
        self._controller = controller
        self._on_frame = on_frame
        self._on_jpeg = on_jpeg
        self._on_state = on_state
        self._pipeline: Any = None
        self._main_loop: Any = None
        self._thread: threading.Thread | None = None
        self._frame_id = 0
        self._last_fps_t = time.monotonic()
        self._fps_frames = 0
        self._fps_value = 0.0
        self._lat_samples: deque[float] = deque(maxlen=60)
        self._recover_stop = threading.Event()
        self._recover_thread: threading.Thread | None = None
        self._width = 640
        self._height = 480
        self._last_playback_uri: str | None = None
        self._last_resolution_error: str | None = None
        self._last_gst_error: str | None = None
        self._recovery_cycles = 0
        self._rtsp_mode: str | None = None

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "configured_uri": sanitize_uri(self._cfg.source.uri),
            "playback_uri": sanitize_uri(self._last_playback_uri or self._cfg.source.uri),
            "resolution_error": self._last_resolution_error,
            "last_gst_error": self._last_gst_error,
            "recovery_cycles": self._recovery_cycles,
            "rtsp_mode": self._rtsp_mode,
        }

    def _create_vsink_bin(self) -> tuple[Any, Any, Any]:
        """Bin: queue → RGB tee → (appsink infer + jpeg). Vrací (vsink_bin, asink, jsink)."""
        assert Gst is not None
        vsink_bin = Gst.Bin.new("vsink")
        q1 = Gst.ElementFactory.make("queue", "vq1")
        q1.set_property("max-size-buffers", 2)
        conv = Gst.ElementFactory.make("videoconvert", "conv")
        caps = Gst.ElementFactory.make("capsfilter", "caps")
        caps.set_property(
            "caps",
            Gst.Caps.from_string(f"video/x-raw,format=RGB,width={self._width},height={self._height}"),
        )
        tee = Gst.ElementFactory.make("tee", "t")

        q2 = Gst.ElementFactory.make("queue", "q2")
        q2.set_property("max-size-buffers", 2)
        asink = Gst.ElementFactory.make("appsink", "asink")
        asink.set_property("emit-signals", True)
        asink.set_property("sync", False)
        asink.set_property("max-buffers", 2)
        asink.set_property("drop", True)

        q3 = Gst.ElementFactory.make("queue", "q3")
        q3.set_property("max-size-buffers", 2)
        jconv = Gst.ElementFactory.make("videoconvert", "jconv")
        jenc = Gst.ElementFactory.make("jpegenc", "jenc")
        jsink = Gst.ElementFactory.make("appsink", "jsink")
        jsink.set_property("emit-signals", True)
        jsink.set_property("sync", False)
        jsink.set_property("max-buffers", 2)
        jsink.set_property("drop", True)

        for el in (q1, conv, caps, tee, q2, asink, q3, jconv, jenc, jsink):
            if el is None:
                raise RuntimeError("missing GStreamer element in vsink bin")
            vsink_bin.add(el)

        if not q1.link(conv) or not conv.link(caps) or not caps.link(tee):
            raise RuntimeError("vsink bin link failed (tee)")

        def _request_tee_pad(t: Any) -> Any:
            if hasattr(t, "request_pad_simple"):
                return t.request_pad_simple("src_%u")
            return t.get_request_pad("src_%u")

        tee_src0 = _request_tee_pad(tee)
        sink_pad_a = q2.get_static_pad("sink")
        if tee_src0 and sink_pad_a:
            tee_src0.link(sink_pad_a)
        q2.link(asink)

        tee_src1 = _request_tee_pad(tee)
        sink_pad_j = q3.get_static_pad("sink")
        if tee_src1 and sink_pad_j:
            tee_src1.link(sink_pad_j)
        q3.link(jconv)
        jconv.link(jenc)
        jenc.link(jsink)

        q1_sink = q1.get_static_pad("sink")
        ghost = Gst.GhostPad.new("sink", q1_sink)
        ghost.set_active(True)
        vsink_bin.add_pad(ghost)

        return vsink_bin, asink, jsink

    def _wire_decode_bus_and_play(
        self,
        asink: Any,
        jsink: Any,
    ) -> None:
        assert Gst is not None
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)
        asink.connect("new-sample", self._on_new_sample)
        jsink.connect("new-sample", self._on_jpeg_sample)
        self._main_loop = GLib.MainLoop()
        self._thread = threading.Thread(target=self._main_loop.run, daemon=True)
        self._controller.force(PipelineState.RUNNING)
        self._on_state(PipelineState.RUNNING, None)
        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self._on_state(PipelineState.FAILED, "PLAYING failed")
            return
        self._thread.start()

    def start(self) -> bool:
        if not _GST_AVAILABLE or not gst_init():
            self._on_state(PipelineState.FAILED, "GStreamer unavailable")
            return False
        self._controller.transition(PipelineState.RECOVERING)
        self._build_and_run()
        return True

    def _build_and_run(self) -> None:
        assert Gst is not None
        resolved, rerr = resolve_playback_uri(self._cfg.source.uri)
        self._last_resolution_error = rerr
        self._last_playback_uri = resolved if not rerr else None
        self._rtsp_mode = None
        if rerr:
            logger.error(
                "playback_resolve_failed",
                extra={"extra_data": {"err": rerr, "uri": sanitize_uri(self._cfg.source.uri)}},
            )
            self._last_gst_error = None
            self._on_state(PipelineState.RECOVERING, rerr)
            self._start_recovery(f"resolve:{rerr}")
            return

        self._pipeline = Gst.Pipeline.new("vision")
        try:
            vsink_bin, asink, jsink = self._create_vsink_bin()
        except RuntimeError as e:
            self._on_state(PipelineState.FAILED, str(e))
            return

        use_rtsp_playbin = (
            resolved.lower().startswith("rtsp://")
            and _rtsp_use_playbin_video_only()
        )

        if use_rtsp_playbin:
            playbin = Gst.ElementFactory.make("playbin", "play")
            if playbin is None:
                self._on_state(PipelineState.FAILED, "playbin missing")
                return
            playbin.set_property("uri", resolved)
            playbin.set_property("flags", _GST_PLAY_FLAG_VIDEO)
            playbin.set_property("video-sink", vsink_bin)
            self._pipeline.add(playbin)
            self._pipeline.add(vsink_bin)
            self._rtsp_mode = "playbin_video_only"
            logger.info(
                "rtsp_pipeline",
                extra={
                    "extra_data": {
                        "mode": self._rtsp_mode,
                        "hint": "bez audio stopy — kamery často posílají kodek, který decodebin nezvládne",
                    },
                },
            )
        else:
            decode = Gst.ElementFactory.make("uridecodebin", "decode")
            if decode is None:
                self._on_state(PipelineState.FAILED, "uridecodebin missing")
                return
            decode.set_property("uri", resolved)
            self._pipeline.add(decode)
            self._pipeline.add(vsink_bin)
            self._rtsp_mode = (
                "uridecodebin_forced"
                if resolved.lower().startswith("rtsp://")
                else "uridecodebin"
            )

            bin_sink = vsink_bin.get_static_pad("sink")
            _linked = {"done": False}

            def on_pad_added(_dbin: Any, pad: Any) -> None:
                if _linked["done"] or bin_sink is None:
                    return
                caps_p = pad.get_current_caps()
                struct = caps_p.get_structure() if caps_p else None
                name = struct.get_name() if struct else ""
                if not name.startswith("video"):
                    return
                if not pad.is_linked():
                    pad.link(bin_sink)
                    _linked["done"] = True

            decode.connect("pad-added", on_pad_added)

        self._wire_decode_bus_and_play(asink, jsink)

    def _on_bus_message(self, bus: Any, message: Any) -> None:
        assert Gst is not None
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            self._last_gst_error = str(err)
            logger.error("gst_error", extra={"extra_data": {"err": str(err), "dbg": dbg}})
            self._controller.transition(PipelineState.PAUSED)
            detail = f"{err}" + (f" | {dbg}" if dbg else "")
            self._on_state(PipelineState.RECOVERING, detail[:1200])
            self._pipeline.set_state(Gst.State.NULL)
            self._start_recovery(str(err))
        elif t == Gst.MessageType.EOS:
            logger.warning("gst_eos")
            self._last_gst_error = "EOS (konec streamu nebo odpojení)"
            self._controller.transition(PipelineState.PAUSED)
            self._on_state(PipelineState.RECOVERING, self._last_gst_error)
            self._pipeline.set_state(Gst.State.NULL)
            self._start_recovery("EOS")
        elif t == Gst.MessageType.WARNING:
            warn, dbg = message.parse_warning()
            logger.warning("gst_warn", extra={"extra_data": {"w": str(warn), "dbg": dbg}})

    def _on_new_sample(self, sink: Any) -> Any:
        assert Gst is not None
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        caps = sample.get_caps()
        struct = caps.get_structure(0) if caps else None
        w = self._width
        h = self._height
        if struct:
            w = struct.get_int("width")[1]
            h = struct.get_int("height")[1]
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            arr = np.frombuffer(mapinfo.data, dtype=np.uint8).reshape((h, w, 3))
            frame = np.ascontiguousarray(arr)
        finally:
            buf.unmap(mapinfo)
        self._frame_id += 1
        self._fps_frames += 1
        now = time.monotonic()
        if now - self._last_fps_t >= 1.0:
            self._fps_value = self._fps_frames / (now - self._last_fps_t)
            self._fps_frames = 0
            self._last_fps_t = now
        t0 = time.perf_counter()
        self._on_frame(frame, self._frame_id, time.time_ns(), self._cfg.source.uri, self._cfg.model)
        dt = (time.perf_counter() - t0) * 1000
        self._lat_samples.append(dt)
        return Gst.FlowReturn.OK

    def _on_jpeg_sample(self, sink: Any) -> Any:
        assert Gst is not None
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            data = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)
        self._on_jpeg(data)
        return Gst.FlowReturn.OK

    def _start_recovery(self, reason: str) -> None:
        if self._recover_thread and self._recover_thread.is_alive():
            return
        self._recovery_cycles += 1
        self._recover_stop.clear()
        self._recover_thread = threading.Thread(target=self._recovery_loop, args=(reason,), daemon=True)
        self._recover_thread.start()

    def _recovery_loop(self, reason: str) -> None:
        logger.info(
            "recovery_started",
            extra={"extra_data": {"reason": reason[:500], "cycle": self._recovery_cycles}},
        )
        time.sleep(1.5)
        backoff = 3.0
        while not self._recover_stop.is_set():
            uri = self._cfg.source.uri
            if uri.lower().startswith("rtsp://") and not rtsp_describe_ok(uri):
                logger.warning(
                    "rtsp_probe_backoff",
                    extra={"extra_data": {"uri": sanitize_uri(uri), "sleep_s": min(backoff, 30.0)}},
                )
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 1.45, 30.0)
                continue
            time.sleep(min(backoff, 25.0))
            backoff = min(backoff * 1.2, 30.0)
            self._controller.transition(PipelineState.RUNNING)
            self._on_state(PipelineState.RECOVERING, None)
            time.sleep(0.2)
            self._rebuild()
            return
        logger.info("recovery_stopped")

    def _rebuild(self) -> None:
        if self._main_loop:
            self._main_loop.quit()
        self._thread = None
        self._main_loop = None
        self._pipeline = None
        self._build_and_run()

    def stop(self) -> None:
        self._recover_stop.set()
        if self._main_loop:
            self._main_loop.quit()
        if self._pipeline and _GST_AVAILABLE:
            self._pipeline.set_state(Gst.State.NULL)

    def get_fps(self) -> float:
        return self._fps_value

    def get_latency_ms(self) -> float | None:
        if not self._lat_samples:
            return None
        return sum(self._lat_samples) / len(self._lat_samples)

    def apply_model_config(self, model: ModelConfig) -> None:
        self._cfg.model = model

    def apply_source_uri(self, uri: str) -> None:
        self._cfg.source.uri = uri
        self._controller.transition(PipelineState.RECONFIGURING)
        self._on_state(PipelineState.RECONFIGURING, None)
        self.stop()
        time.sleep(0.3)
        self.start()
