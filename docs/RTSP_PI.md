# RTSP na Raspberry Pi (Docker)

## Výchozí stack

- **GStreamer** `playbin` pouze video, **TCP** pro RTSP (`RPY_RTSP_FORCE_TCP=1`), latence `RPY_RTSP_LATENCY_MS`.
- **Audio** z kamery se nebere (fakesink / video-only caps u `uridecodebin`) — viz [context/video_pipeline_notes.txt](../context/video_pipeline_notes.txt).

## HW dekód H.264

Obraz z Debian `bookworm-slim` image často jde **softwarově** (`avdec_h264` / libav). Na Pi 4/5 lze zkusit **V4L2 stateless** dekodér:

1. Na hostu musí být dostupné `/dev/video10` (nebo jiné decode device — záleží na kernelu).
2. Do `docker-compose` u `ai_core` přidejte mapování zařízení, např.:
   ```yaml
   devices:
     - /dev/hailo0:/dev/hailo0
     - /dev/video10:/dev/video10
   ```
3. Zvýšte prioritu pluginu (experimentální):
   ```yaml
   environment:
     GST_PLUGIN_FEATURE_RANK: "v4l2h264dec:MAX"
   ```
4. Proměnná **`RPY_GST_PREFER_V4L2_H264=1`** se propisuje do telemetrie (`gst_hw_decode_hint`) jako připomínka — samotný výběr dekodéru řídí GStreamer podle pluginů a ranku.

Ověření v kontejneru: `gst-inspect-1.0 v4l2h264dec` (pokud prvek existuje).

## Diagnostika

- UI: **Diagnostika stacku** — kontrola `ai_infer_stack`, `ingress_mode`, `last_gst_error`.
- Logy: `docker compose logs ai_core` při pádu pipeline.
