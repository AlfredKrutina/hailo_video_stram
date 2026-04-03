"""
Hobby Pi stream: GStreamer (+ optional Hailo) -> JPEG + detections -> WebSocket -> browser canvas.
Run on Raspberry Pi with Hailo stack: `python main.py`
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from uvicorn import Config, Server

# --- env ---------------------------------------------------------------------------

DEBUG = os.environ.get("DEBUG", "").strip() in ("1", "true", "yes", "on")
SOURCE_URI = os.environ.get("SOURCE_URI", "file:///usr/share/hailo-rpi5-examples/resources/video/example.mp4").strip()
HAILO_HEF_PATH = os.environ.get("HAILO_HEF_PATH", "").strip()
HAILO_FILTER_SO = os.environ.get("HAILO_FILTER_SO", "").strip()
MODEL_W = int(os.environ.get("MODEL_WIDTH", "640"))
MODEL_H = int(os.environ.get("MODEL_HEIGHT", "640"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

_frame_id = 0
_frame_id_lock = threading.Lock()

_clients: set[WebSocket] = set()
_clients_lock = asyncio.Lock()

# Drop old frames: (jpeg_bytes, meta_dict)
_frame_queue: queue.Queue[tuple[bytes, dict[str, Any]]] = queue.Queue(maxsize=2)

# Probe runs before appsink for the same frame — preserve order
_pending_detections: deque[list[dict[str, Any]]] = deque(maxlen=64)
_pending_lock = threading.Lock()


def _next_frame_id() -> int:
    global _frame_id
    with _frame_id_lock:
        _frame_id += 1
        return _frame_id


def _dbg(msg: str, **kw: Any) -> None:
    if DEBUG:
        extra = f" {kw}" if kw else ""
        print(f"[hailo_ws] {msg}{extra}", flush=True)


def detections_from_hailo_buffer(buffer: Any) -> list[dict[str, Any]]:
    """Parse HAILO_DETECTION metadata using TAPPAS `hailo` Python API (same as hailo-rpi5-examples)."""
    try:
        import hailo  # type: ignore[import-untyped]

        roi = hailo.get_roi_from_buffer(buffer)
        if roi is None:
            return []
        out: list[dict[str, Any]] = []
        for detection in roi.get_objects_typed(hailo.HAILO_DETECTION):
            bb = detection.get_bbox()
            out.append(
                {
                    "class": detection.get_label(),
                    "confidence": float(detection.get_confidence()),
                    "bbox": [
                        float(bb.xmin()),
                        float(bb.ymin()),
                        float(bb.width()),
                        float(bb.height()),
                    ],
                }
            )
        return out
    except Exception as e:
        _dbg("detections_parse_failed", err=str(e))
        return []


# --- GStreamer ---------------------------------------------------------------------

def _run_gst_loop() -> None:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    Gst.init(None)

    try:
        pipeline = _build_pipeline(Gst)
    except Exception as e:
        print(f"[hailo_ws] pipeline build failed: {e}", flush=True)
        return

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()

    def on_bus_message(_bus: Any, message: Any, user_data: Any) -> None:
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            print(f"[hailo_ws] GStreamer ERROR: {err} ({dbg})", flush=True)
            user_data.quit()
        elif t == Gst.MessageType.EOS:
            _dbg("eos")
            user_data.quit()

    bus.connect("message", on_bus_message, loop)

    appsink = pipeline.get_by_name("outsink")

    def on_new_sample(sink: Any) -> Any:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        if buf is None:
            return Gst.FlowReturn.OK
        ok, mapinfo = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            jpeg_bytes = bytes(mapinfo.data)
        finally:
            buf.unmap(mapinfo)

        with _pending_lock:
            try:
                dets = _pending_detections.popleft()
            except IndexError:
                dets = []

        meta = {
            "frame_id": _next_frame_id(),
            "source": SOURCE_URI,
            "detections": dets,
        }
        try:
            _frame_queue.put_nowait((jpeg_bytes, meta))
        except queue.Full:
            try:
                _frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                _frame_queue.put_nowait((jpeg_bytes, meta))
            except queue.Full:
                pass

        return Gst.FlowReturn.OK

    appsink.connect("new-sample", on_new_sample)

    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        print("[hailo_ws] PLAYING failed", flush=True)
        return

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)


def _pad_probe_callback(
    _pad: Any, info: Any, *_args: Any
) -> Any:
    from gi.repository import Gst

    buf = info.get_buffer()
    if buf is None:
        return Gst.PadProbeReturn.OK
    dets = detections_from_hailo_buffer(buf)
    with _pending_lock:
        _pending_detections.append(dets)
    return Gst.PadProbeReturn.OK


def _on_decode_pad_added(
    decodebin: Any,
    pad: Any,
    user_data: dict[str, Any],
) -> None:
    from gi.repository import Gst

    caps = pad.get_current_caps()
    if caps is None:
        return
    struct = caps.get_structure(0)
    name = struct.get_name()
    if name.startswith("video"):
        sinkpad = user_data["vqueue"].get_static_pad("sink")
        if sinkpad and not pad.is_linked():
            pad.link(sinkpad)
    else:
        pipeline = user_data["pipeline"]
        fake = Gst.ElementFactory.make("fakesink", f"fake_{pad.get_name()}")
        if fake is None:
            return
        pipeline.add(fake)
        fake.sync_state_with_parent()
        sinkpad = fake.get_static_pad("sink")
        if sinkpad and not pad.is_linked():
            pad.link(sinkpad)


def _build_pipeline(Gst: Any) -> Any:
    use_hailo = bool(HAILO_HEF_PATH and HAILO_FILTER_SO)

    pipeline = Gst.Pipeline.new("pipe")
    decode = Gst.ElementFactory.make("uridecodebin", "decode")
    if decode is None:
        raise RuntimeError("uridecodebin missing")
    decode.set_property("uri", SOURCE_URI)

    vqueue = Gst.ElementFactory.make("queue", "vqueue")
    vqueue.set_property("max-size-buffers", 2)
    conv1 = Gst.ElementFactory.make("videoconvert", "conv1")
    scale = Gst.ElementFactory.make("videoscale", "scale")
    caps_f = Gst.ElementFactory.make("capsfilter", "caps_f")
    caps = Gst.Caps.from_string(
        f"video/x-raw,format=RGB,width={MODEL_W},height={MODEL_H}"
    )
    caps_f.set_property("caps", caps)

    tail_elements: list[Any] = []
    if use_hailo:
        hailonet = Gst.ElementFactory.make("hailonet", "hailonet")
        hailofilter = Gst.ElementFactory.make("hailofilter", "hailofilter")
        if hailonet is None or hailofilter is None:
            raise RuntimeError("hailonet/hailofilter not available (install Hailo GStreamer plugins)")
        hailonet.set_property("hef-path", HAILO_HEF_PATH)
        hailofilter.set_property("so-path", HAILO_FILTER_SO)
        try:
            hailofilter.set_property("qos", False)
        except Exception:
            pass
        q2 = Gst.ElementFactory.make("queue", "q2")
        conv2 = Gst.ElementFactory.make("videoconvert", "conv2")
        jpegenc = Gst.ElementFactory.make("jpegenc", "jpegenc")
        appsink = Gst.ElementFactory.make("appsink", "outsink")
        appsink.set_property("emit-signals", True)
        appsink.set_property("sync", False)
        appsink.set_property("max-buffers", 1)
        appsink.set_property("drop", True)
        tail_elements = [hailonet, hailofilter, q2, conv2, jpegenc, appsink]
    else:
        q2 = Gst.ElementFactory.make("queue", "q2")
        conv2 = Gst.ElementFactory.make("videoconvert", "conv2")
        jpegenc = Gst.ElementFactory.make("jpegenc", "jpegenc")
        appsink = Gst.ElementFactory.make("appsink", "outsink")
        appsink.set_property("emit-signals", True)
        appsink.set_property("sync", False)
        appsink.set_property("max-buffers", 1)
        appsink.set_property("drop", True)
        tail_elements = [q2, conv2, jpegenc, appsink]
        _dbg("hailo_disabled", hint="set HAILO_HEF_PATH and HAILO_FILTER_SO for inference")

    for el in [decode, vqueue, conv1, scale, caps_f, *tail_elements]:
        pipeline.add(el)

    if not vqueue.link(conv1) or not conv1.link(scale) or not scale.link(caps_f):
        raise RuntimeError("link failed (vqueue/conv/scale/caps)")

    if use_hailo:
        hailonet = pipeline.get_by_name("hailonet")
        hailofilter = pipeline.get_by_name("hailofilter")
        assert hailonet is not None and hailofilter is not None
        if not caps_f.link(hailonet) or not hailonet.link(hailofilter):
            raise RuntimeError("link failed (hailonet chain)")
        pad = hailofilter.get_static_pad("src")
        if pad:
            pad.add_probe(Gst.PadProbeType.BUFFER, _pad_probe_callback, None)
        rest = tail_elements[2:]  # q2 ...
        prev = hailofilter
        for el in rest:
            if not prev.link(el):
                raise RuntimeError("link failed tail")
            prev = el
    else:
        prev = caps_f
        for el in tail_elements:
            if not prev.link(el):
                raise RuntimeError("link failed (no hailo tail)")
            prev = el

    decode.connect(
        "pad-added",
        _on_decode_pad_added,
        {"vqueue": vqueue, "pipeline": pipeline},
    )

    return pipeline


# --- FastAPI -----------------------------------------------------------------------

app = FastAPI()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    async with _clients_lock:
        _clients.add(websocket)
    _dbg("ws_connected", n=len(_clients))
    try:
        while True:
            await websocket.receive()
    except WebSocketDisconnect:
        pass
    finally:
        async with _clients_lock:
            _clients.discard(websocket)
        _dbg("ws_disconnected", n=len(_clients))


def _try_get_frame() -> tuple[bytes, dict[str, Any]] | None:
    try:
        return _frame_queue.get(timeout=0.2)
    except queue.Empty:
        return None


async def _broadcast_loop() -> None:
    while True:
        item = await asyncio.to_thread(_try_get_frame)
        if item is None:
            await asyncio.sleep(0.01)
            continue
        jpeg_bytes, meta = item
        text = json.dumps(meta)
        async with _clients_lock:
            dead: list[WebSocket] = []
            for ws in _clients:
                try:
                    await ws.send_text(text)
                    await ws.send_bytes(jpeg_bytes)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _clients.discard(ws)


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_broadcast_loop())
    t = threading.Thread(target=_run_gst_loop, name="gstreamer", daemon=True)
    t.start()
    _dbg("startup", source=SOURCE_URI, hailo=bool(HAILO_HEF_PATH and HAILO_FILTER_SO))


def main() -> None:
    if not STATIC_DIR.is_dir():
        raise SystemExit(f"Missing {STATIC_DIR}")
    config = Config(app, host=HOST, port=PORT, log_level="info")
    server = Server(config)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
