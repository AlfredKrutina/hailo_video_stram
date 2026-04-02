"""
GStreamer ingest for vision pipeline.

Ingress modes:
- **RTSP** — `playbin` video-only (optional `uridecodebin` via env).
- **Direct URI** — `uridecodebin` (file, http mp4, RTSP when forced).
- **Portal (YouTube, …)** — `yt-dlp -o -` → `fdsrc` → `decodebin` (avoids googlevideo + souphttpsrc 403).

Downstream: fixed RGB → tee → appsink + jpegenc.

Errors trigger recovery with backoff; repeated HTTP 403 stops recovery (FAILED).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from shared.agent_debug_ndjson import agent_debug_log
from shared.schemas.config import AppConfig, ModelConfig
from shared.schemas.telemetry import PipelineState

from services.ai_core.source_resolve import resolve_playback, sanitize_uri
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


# Odkazy z yt-dlp (googlevideo.com) bez hlaviček často končí 403 Forbidden u souphttpsrc.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def _apply_browser_like_headers(source: Any) -> None:
    """
    Nastaví User-Agent a Referer na HTTP(S) zdroji (souphttpsrc / curlhttpsrc).
    Bez toho YouTube CDN a některé buckety vracejí 403 i při „platné“ URL z yt-dlp.

    Pro souphttpsrc: **is-live=true** — některé servery (např. samplelib) neakceptují HTTP Range;
    výchozí chování posílá Range kvůli seekům a spadne s gst-resource-error „Server does not
    support seeking“. Sekvenční režim Range nepoužívá.
    """
    try:
        factory = source.get_factory()
        name = (factory.get_name() or "").lower()
    except Exception:
        return
    if "souphttp" not in name and "curlhttp" not in name:
        return
    for prop in ("user-agent", "user_agent"):
        try:
            source.set_property(prop, _BROWSER_UA)
            break
        except Exception:
            continue
    for prop in ("referer", "referrer"):
        try:
            source.set_property(prop, "https://www.youtube.com/")
            break
        except Exception:
            continue
    if "souphttp" in name:
        try:
            source.set_property("is-live", True)
            # region agent log
            agent_debug_log(
                "H1",
                "gst_pipeline.py:_apply_browser_like_headers",
                "souphttpsrc_is_live_true",
                {"element": name},
            )
            # endregion
        except Exception as e:
            logger.debug("souphttpsrc_is_live_skipped", extra={"extra_data": {"err": str(e)}})
    logger.debug("http_source_browser_headers", extra={"extra_data": {"element": name}})


# GST_RTSP_LOWER_TRANS_TCP — z Dockeru/LAN často nefunguje UDP kameře; TCP je spolehlivější.
_RTSPSRC_PROTOCOL_TCP = 0x00000004


def _configure_rtspsrc_if_needed(element: Any) -> None:
    """
    Nastaví rtspsrc pro reálné kamery: latence + (výchozí) pouze TCP.

    Env:
    - RPY_RTSP_LATENCY_MS (ms, default 300)
    - RPY_RTSP_FORCE_TCP: 1/true (default) = jen TCP; 0 = výchozí výběr GStreameru
    """
    assert Gst is not None
    try:
        fn = (element.get_factory().get_name() or "").lower()
    except Exception:
        return
    if fn != "rtspsrc":
        return
    latency_ms = int(os.environ.get("RPY_RTSP_LATENCY_MS", "300"))
    try:
        element.set_property("latency", latency_ms)
    except Exception as e:
        logger.debug("rtspsrc_latency_skip", extra={"extra_data": {"err": str(e)}})
    force_tcp = os.environ.get("RPY_RTSP_FORCE_TCP", "1").lower() not in ("0", "false", "no")
    if not force_tcp:
        logger.debug("rtspsrc_tcp_disabled", extra={"extra_data": {}})
        return
    try:
        element.set_property("protocols", _RTSPSRC_PROTOCOL_TCP)
    except Exception as e:
        logger.warning(
            "rtspsrc_tcp_protocols_failed",
            extra={"extra_data": {"err": str(e)}},
        )
        return
    if os.environ.get("ENVIRONMENT", "").lower() == "staging":
        logger.info(
            "rtspsrc_tuned",
            extra={
                "extra_data": {
                    "latency_ms": latency_ms,
                    "protocols": "TCP",
                },
            },
        )


def _ytdlp_stderr_to_log() -> bool:
    """
    Staging: logovat řádky stderr z yt-dlp (lepší diagnostika než DEVNULL).
    Vypnuto: RPY_YTDLP_LOG_STDERR=0. Zapnuto i v produkci: RPY_YTDLP_LOG_STDERR=1.
    """
    ex = os.environ.get("RPY_YTDLP_LOG_STDERR", "").strip().lower()
    if ex in ("0", "false", "no"):
        return False
    if ex in ("1", "true", "yes"):
        return True
    return os.environ.get("ENVIRONMENT", "").lower() == "staging"


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
        self._ingress_mode: str | None = None
        self._ffmpeg_remux_proc: subprocess.Popen | None = None
        self._ytdlp_proc: subprocess.Popen | None = None
        self._ytdlp_stderr_thread: threading.Thread | None = None
        self._ytdlp_stderr_tail: deque[str] = deque(maxlen=120)
        self._forbidden_streak: int = 0

    def get_diagnostics(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if _ytdlp_stderr_to_log() and self._ytdlp_stderr_tail:
            extra["ytdlp_stderr_tail"] = list(self._ytdlp_stderr_tail)[-30:]
        return {
            "configured_uri": sanitize_uri(self._cfg.source.uri),
            "playback_uri": sanitize_uri(self._last_playback_uri or self._cfg.source.uri),
            "resolution_error": self._last_resolution_error,
            "last_gst_error": self._last_gst_error,
            "recovery_cycles": self._recovery_cycles,
            "ingress_mode": self._ingress_mode,
            # legacy key for older UIs
            "rtsp_mode": self._ingress_mode,
            **extra,
        }

    def _create_vsink_bin(self) -> tuple[Any, Any, Any]:
        """Bin: queue → videoscale → videoconvert → RGB caps → tee → (appsink infer + jpeg).

        Bez videoscale selže vyjednávání u 1080p/4K zdrojů: videoconvert nemění rozlišení,
        fixed caps 640×480 pak končí not-negotiated (v logu často jako chyba u qtdemux).
        """
        assert Gst is not None
        vsink_bin = Gst.Bin.new("vsink")
        q1 = Gst.ElementFactory.make("queue", "vq1")
        q1.set_property("max-size-buffers", 2)
        scale = Gst.ElementFactory.make("videoscale", "vscale")
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

        for el in (q1, scale, conv, caps, tee, q2, asink, q3, jconv, jenc, jsink):
            if el is None:
                raise RuntimeError("missing GStreamer element in vsink bin")
            vsink_bin.add(el)

        if not q1.link(scale) or not scale.link(conv) or not conv.link(caps) or not caps.link(tee):
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

    def _drain_ytdlp_stderr(self, proc: subprocess.Popen) -> None:
        err = proc.stderr
        if err is None:
            return
        try:
            for line in iter(err.readline, b""):
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                text = line.decode("utf-8", errors="replace").rstrip("\n\r")
                if not text:
                    continue
                self._ytdlp_stderr_tail.append(text)
                if _ytdlp_stderr_to_log():
                    logger.info(
                        "ytdlp_stderr",
                        extra={"extra_data": {"line": text[:2000]}},
                    )
        except Exception as e:
            logger.debug("ytdlp_stderr_reader_exc", extra={"extra_data": {"err": str(e)}})
        # Neuzavírat proc.stderr zde — uzavření read-endu za živého yt-dlp může při dalším zápisu
        # na stderr dát child procesu SIGPIPE/EPIPE a shodit pipe do GStreameru (502 / prázdný MJPEG).

    def _kill_ytdlp_child(self) -> None:
        """Nejdřív ffmpeg (čte z yt-dlp), pak yt-dlp — uvolní se pipe."""
        thr = self._ytdlp_stderr_thread
        self._ytdlp_stderr_thread = None
        ff = self._ffmpeg_remux_proc
        yp = self._ytdlp_proc
        self._ffmpeg_remux_proc = None
        self._ytdlp_proc = None
        for proc in (ff, yp):
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if thr is not None and thr.is_alive():
            thr.join(timeout=2.0)

    @staticmethod
    def _is_forbidden_http_error(text: str) -> bool:
        t = text.lower()
        return "403" in t or "forbidden" in t

    def _connect_decodebin_video(
        self,
        decode: Any,
        bin_sink: Any,
        linked_flag: dict[str, bool],
    ) -> None:
        """Link first video/x-* pad from decodebin/uridecodebin to vsink_bin (handles late caps)."""
        assert Gst is not None

        def try_link_pad(pad: Any) -> None:
            if linked_flag.get("done") or bin_sink is None:
                return
            caps = pad.get_current_caps()
            if caps is None:
                return
            struct = caps.get_structure(0)
            name = struct.get_name() if struct else ""
            if not name.startswith("video"):
                return
            ret = pad.link(bin_sink)
            if ret == Gst.PadLinkReturn.OK:
                linked_flag["done"] = True

        def on_pad_added(_el: Any, pad: Any) -> None:
            if pad.get_direction() != Gst.PadDirection.SRC:
                return
            try_link_pad(pad)
            if not linked_flag.get("done"):
                pad.connect(
                    "notify::caps",
                    lambda p, _pspec: try_link_pad(p),
                )

        decode.connect("pad-added", on_pad_added)

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
        self._kill_ytdlp_child()

        spec, rerr = resolve_playback(self._cfg.source.uri)
        self._last_resolution_error = rerr
        if spec and spec.kind == "direct":
            self._last_playback_uri = spec.uri
        elif spec and spec.kind == "ytdlp_pipe":
            self._last_playback_uri = spec.ytdlp_page_url
        else:
            self._last_playback_uri = None

        self._ingress_mode = None
        if rerr or not spec:
            logger.error(
                "playback_resolve_failed",
                extra={"extra_data": {"err": rerr, "uri": sanitize_uri(self._cfg.source.uri)}},
            )
            self._last_gst_error = None
            self._on_state(PipelineState.RECOVERING, rerr or "Neznámý zdroj")
            self._start_recovery(f"resolve:{rerr}")
            return

        self._pipeline = Gst.Pipeline.new("vision")
        try:
            vsink_bin, asink, jsink = self._create_vsink_bin()
        except RuntimeError as e:
            self._on_state(PipelineState.FAILED, str(e))
            return

        if spec.kind == "ytdlp_pipe":
            page = (spec.ytdlp_page_url or "").strip()
            ytdlp = shutil.which("yt-dlp")
            if not ytdlp or not page:
                self._on_state(PipelineState.FAILED, "yt-dlp nebo URL chybí")
                return
            # Jednosouborový progressive MP4/WebM lépe snáší fdsrc→decodebin než čisté DASH/HLS
            # (jinak častá chyba typefind: „Stream doesn't contain enough data“).
            fmt = os.environ.get(
                "RPY_YTDLP_FORMAT",
                "best[height<=720][ext=mp4]/best[ext=mp4]/best[height<=720]/best/worst",
            )
            pl = page.lower()
            cmd = [
                ytdlp,
                "-f",
                fmt,
                "-o",
                "-",
                "--no-playlist",
                "--no-warnings",
                "--no-part",
                "--retries",
                "8",
                "--fragment-retries",
                "8",
            ]
            if "youtu.be" in pl or "youtube.com" in pl:
                # Web klient často vrací fragmentované streamy špatně čitelné z pipe bez obřího bufferu.
                cmd.extend(["--extractor-args", "youtube:player_client=android"])
            cmd.append(page)
            self._ytdlp_stderr_tail.clear()
            stderr_dest = subprocess.PIPE if _ytdlp_stderr_to_log() else subprocess.DEVNULL
            use_ffmpeg_ts = os.environ.get("RPY_YTDLP_FFMPEG_TS", "1").lower() not in (
                "0",
                "false",
                "no",
            )
            ffmpeg_bin = shutil.which("ffmpeg") if use_ffmpeg_ts else None
            try:
                if ffmpeg_bin:
                    yp = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=stderr_dest,
                        bufsize=0,
                    )
                    assert yp.stdout is not None
                    if stderr_dest == subprocess.PIPE and yp.stderr is not None:
                        self._ytdlp_stderr_thread = threading.Thread(
                            target=self._drain_ytdlp_stderr,
                            args=(yp,),
                            daemon=True,
                            name="ytdlp-stderr",
                        )
                        self._ytdlp_stderr_thread.start()
                    ff_cmd = [
                        ffmpeg_bin,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-fflags",
                        "+genpts+discardcorrupt",
                        "-probesize",
                        "32M",
                        "-analyzeduration",
                        "30M",
                        "-i",
                        "-",
                        "-map",
                        "0:v:0",
                        "-c",
                        "copy",
                        "-f",
                        "mpegts",
                        "-",
                    ]
                    try:
                        ffp = subprocess.Popen(
                            ff_cmd,
                            stdin=yp.stdout,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            bufsize=0,
                        )
                    except OSError:
                        yp.terminate()
                        try:
                            yp.wait(timeout=2.0)
                        except Exception:
                            try:
                                yp.kill()
                            except Exception:
                                pass
                        raise
                    yp.stdout.close()
                    self._ytdlp_proc = yp
                    self._ffmpeg_remux_proc = ffp
                    assert ffp.stdout is not None
                    fd = ffp.stdout.fileno()
                else:
                    self._ffmpeg_remux_proc = None
                    self._ytdlp_proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=stderr_dest,
                        bufsize=0,
                    )
                    assert self._ytdlp_proc.stdout is not None
                    if stderr_dest == subprocess.PIPE and self._ytdlp_proc.stderr is not None:
                        self._ytdlp_stderr_thread = threading.Thread(
                            target=self._drain_ytdlp_stderr,
                            args=(self._ytdlp_proc,),
                            daemon=True,
                            name="ytdlp-stderr",
                        )
                        self._ytdlp_stderr_thread.start()
                    fd = self._ytdlp_proc.stdout.fileno()
            except OSError as e:
                self._on_state(PipelineState.FAILED, f"yt-dlp/ffmpeg nelze spustit: {e}")
                return
            fdsrc = Gst.ElementFactory.make("fdsrc", "fdsrc")
            q0 = Gst.ElementFactory.make("queue", "qpipe")
            # Buffer před decode — typefind potřebuje souvislý úvod MP4; příliš velké hodnoty
            # na Raspberry snadno OOM → pád procesu → Nginx 502 na MJPEG.
            buf_mb = int(os.environ.get("RPY_YTDLP_QUEUE_MB", "12"))
            buf_bytes = max(4, buf_mb) * 1024 * 1024
            try:
                q0.set_property("max-size-buffers", 0)
                q0.set_property("max-size-bytes", buf_bytes)
            except Exception:
                q0.set_property("max-size-buffers", 256)
            q0.set_property("max-size-time", 12 * 10**9)
            # decodebin3 umí u fdsrc/stdout vyvolat podivné chyby typu „parse … pipe“ na některých
            # verzích pluginů — výchozí je klasický decodebin; decodebin3 jen přes RPY_USE_DECODEBIN3=1.
            use_db3 = os.environ.get("RPY_USE_DECODEBIN3", "").lower() in ("1", "true", "yes")
            decode = (
                Gst.ElementFactory.make("decodebin3", "decode")
                if use_db3
                else Gst.ElementFactory.make("decodebin", "decode")
            ) or Gst.ElementFactory.make("decodebin", "decode")
            if fdsrc is None or q0 is None or decode is None:
                self._on_state(PipelineState.FAILED, "fdsrc/decodebin missing")
                self._kill_ytdlp_child()
                return
            fdsrc.set_property("fd", fd)
            try:
                fdsrc.set_property("blocksize", 262144)
            except Exception:
                pass
            try:
                fdsrc.set_property("is-live", True)
            except Exception:
                pass
            self._pipeline.add(fdsrc)
            self._pipeline.add(q0)
            self._pipeline.add(decode)
            self._pipeline.add(vsink_bin)
            if not fdsrc.link(q0) or not q0.link(decode):
                self._on_state(PipelineState.FAILED, "fdsrc → decodebin link selhal")
                self._kill_ytdlp_child()
                return
            bin_sink = vsink_bin.get_static_pad("sink")
            _linked: dict[str, bool] = {"done": False}
            self._connect_decodebin_video(decode, bin_sink, _linked)
            self._ingress_mode = "ytdlp_pipe_ffmpeg_ts" if ffmpeg_bin else "ytdlp_pipe"
            logger.info(
                "ingress_ytdlp_pipe",
                extra={
                    "extra_data": {
                        "page": sanitize_uri(page),
                        "format": fmt,
                        "decode": (decode.get_factory().get_name() or "decodebin"),
                        "remux": "mpegts_via_ffmpeg" if ffmpeg_bin else "raw_ytdlp_stdout",
                    },
                },
            )
            self._wire_decode_bus_and_play(asink, jsink)
            return

        resolved = spec.uri or ""

        def on_source_setup(_bin: Any, src: Any) -> None:
            _apply_browser_like_headers(src)
            _configure_rtspsrc_if_needed(src)
            try:
                child_name = (src.get_factory().get_name() or "").lower()
            except Exception:
                return
            if child_name == "uridecodebin":
                src.connect("source-setup", on_source_setup)

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
            # Headless Docker: bez audio-sink playsink zkusí ALSA → assert v gstplaysink.c
            fake_audio = Gst.ElementFactory.make("fakesink", "playbin_audio_sink")
            if fake_audio is not None:
                try:
                    fake_audio.set_property("sync", False)
                except Exception:
                    pass
                playbin.set_property("audio-sink", fake_audio)
            playbin.connect("source-setup", on_source_setup)
            self._pipeline.add(playbin)
            self._pipeline.add(vsink_bin)
            self._ingress_mode = "playbin_video_only"
            logger.info(
                "rtsp_pipeline",
                extra={
                    "extra_data": {
                        "mode": self._ingress_mode,
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
            decode.connect("source-setup", on_source_setup)
            self._pipeline.add(decode)
            self._pipeline.add(vsink_bin)
            self._ingress_mode = (
                "uridecodebin_forced"
                if resolved.lower().startswith("rtsp://")
                else "uridecodebin"
            )

            bin_sink = vsink_bin.get_static_pad("sink")
            _linked: dict[str, bool] = {"done": False}
            self._connect_decodebin_video(decode, bin_sink, _linked)

        self._wire_decode_bus_and_play(asink, jsink)

    def _try_loop_seek_after_eos(self) -> bool:
        """
        Po EOS u konečného média znovu přehrát od začátku (seek), místo rebuild pipeline.

        Pouze **file://** — lokální soubor podporuje seek bez HTTP Range.

        U **http(s)://** seek na pipeline znovu aktivuje souphttpsrc s Range; servery bez
        Range (samplelib + is-live sekvenční režim) pak hlásí „Server does not support
        seeking“. Proto u HTTP(S) necháváme EOS na běžné recovery (nové uridecodebin).
        RTSP / yt-dlp pipe také bez seek zde.
        """
        assert Gst is not None
        uri = (self._last_playback_uri or self._cfg.source.uri or "").strip()
        ul = uri.lower()
        if ul.startswith("rtsp://") or self._ingress_mode == "ytdlp_pipe":
            return False
        if not ul.startswith("file://"):
            return False
        if not self._pipeline:
            return False
        try:
            self._pipeline.set_state(Gst.State.PAUSED)
            ok = self._pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                0,
            )
            if not ok:
                logger.warning("eos_seek_simple_returned_false", extra={"extra_data": {"uri": sanitize_uri(uri)}})
                # region agent log
                agent_debug_log(
                    "H1",
                    "gst_pipeline.py:_try_loop_seek_after_eos",
                    "eos_seek_simple_false",
                    {"uri": sanitize_uri(uri)},
                )
                # endregion
                self._pipeline.set_state(Gst.State.NULL)
                return False
            ret = self._pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                logger.warning("eos_seek_playing_failed", extra={"extra_data": {"uri": sanitize_uri(uri)}})
                self._pipeline.set_state(Gst.State.NULL)
                return False
        except Exception as e:
            logger.warning("eos_seek_loop_exc", extra={"extra_data": {"err": str(e), "uri": sanitize_uri(uri)}})
            try:
                self._pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            return False
        self._last_gst_error = None
        self._controller.transition(PipelineState.RUNNING)
        self._on_state(PipelineState.RUNNING, None)
        logger.info("eos_seek_loop_ok", extra={"extra_data": {"uri": sanitize_uri(uri)}})
        # region agent log
        agent_debug_log(
            "H1",
            "gst_pipeline.py:_try_loop_seek_after_eos",
            "eos_seek_loop_ok",
            {"uri": sanitize_uri(uri)},
        )
        # endregion
        return True

    def _on_bus_message(self, bus: Any, message: Any) -> None:
        assert Gst is not None
        t = message.type
        if t == Gst.MessageType.ERROR:
            err, dbg = message.parse_error()
            # region agent log
            agent_debug_log(
                "H1",
                "gst_pipeline.py:_on_bus_message:ERROR",
                "gst_bus_error",
                {
                    "err": str(err)[:500],
                    "dbg": (str(dbg) if dbg else "")[:400],
                    "ingress": self._ingress_mode,
                    "uri": sanitize_uri(self._cfg.source.uri),
                },
            )
            # endregion
            self._last_gst_error = str(err)
            logger.error("gst_error", extra={"extra_data": {"err": str(err), "dbg": dbg}})
            if self._ingress_mode == "ytdlp_pipe" and self._ytdlp_stderr_tail:
                logger.error(
                    "gst_error_ytdlp_stderr_tail",
                    extra={"extra_data": {"tail": list(self._ytdlp_stderr_tail)[-20:]}},
                )
            self._controller.transition(PipelineState.PAUSED)
            detail = f"{err}" + (f" | {dbg}" if dbg else "")
            if self._is_forbidden_http_error(detail):
                self._forbidden_streak += 1
            else:
                self._forbidden_streak = 0

            if self._forbidden_streak >= 5:
                fail_msg = (
                    "Opakovaný HTTP 403 / Forbidden — použijte Demo soubor v image, přímý HTTP MP4 nebo RTSP. "
                    "YouTube přes yt-dlp pipe může stále blokovat CDN."
                )
                self._on_state(PipelineState.FAILED, fail_msg[:1200])
                if self._pipeline:
                    self._pipeline.set_state(Gst.State.NULL)
                self._kill_ytdlp_child()
                return

            self._on_state(PipelineState.RECOVERING, detail[:1200])
            if self._pipeline:
                self._pipeline.set_state(Gst.State.NULL)
            self._kill_ytdlp_child()
            self._start_recovery(str(err))
        elif t == Gst.MessageType.EOS:
            # region agent log
            agent_debug_log(
                "H1",
                "gst_pipeline.py:_on_bus_message:EOS",
                "gst_eos_received",
                {"ingress": self._ingress_mode, "uri": sanitize_uri(self._cfg.source.uri)},
            )
            # endregion
            logger.warning("gst_eos")
            # Konečné soubory / krátké HTTP MP4: po dohrání přijde EOS. Původní chování
            # (NULL + recovery) neustále restartovalo pipeline → málo snímků, prázdný MJPEG,
            # „AI nic nedělá“. Seek na 0 udrží ingest v chodu bez zbytečných cyklů.
            if self._try_loop_seek_after_eos():
                return
            self._last_gst_error = "EOS (konec streamu nebo odpojení)"
            self._controller.transition(PipelineState.PAUSED)
            self._on_state(PipelineState.RECOVERING, self._last_gst_error)
            if self._pipeline:
                self._pipeline.set_state(Gst.State.NULL)
            self._kill_ytdlp_child()
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
        self._forbidden_streak = 0
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
        uri0 = self._cfg.source.uri.strip()
        ul0 = uri0.lower()
        # Krátké HTTP(S) MP4 (samplelib …) skončí EOS každých pár sekund. Původní prodleva
        # 1.5 s + 3 s backoff před každým rebuildem způsobovala dlouhé „odpojení“ a cykly
        # chyb v UI; u čistého EOS znovu otevřeme zdroj téměř hned (bez Range seeku).
        if reason == "EOS" and (ul0.startswith("http://") or ul0.startswith("https://")):
            time.sleep(0.12)
            if not self._recover_stop.is_set():
                self._controller.transition(PipelineState.RUNNING)
                self._on_state(PipelineState.RECOVERING, None)
                time.sleep(0.05)
                self._rebuild()
            return

        # RTSP EOS (výpadek / odpojení): stejně jako HTTP rychlý rebuild místo 1.5 s + backoff.
        if reason == "EOS" and ul0.startswith("rtsp://"):
            time.sleep(0.12)
            if not self._recover_stop.is_set():
                self._controller.transition(PipelineState.RUNNING)
                self._on_state(PipelineState.RECOVERING, None)
                time.sleep(0.05)
                self._rebuild()
            return

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
        self._kill_ytdlp_child()
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
