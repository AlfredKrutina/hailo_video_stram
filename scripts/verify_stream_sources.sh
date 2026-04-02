#!/bin/sh
# Ověření dostupnosti YouTube (yt-dlp) a volitelně RTSP z ai_core image / kontejneru.
# Použití na Pi: cd docker && docker compose exec ai_core sh /app/scripts/verify_stream_sources.sh
# Nebo: RTSP_TEST_URI='rtsp://user:pass@host:8554/path' sh scripts/verify_stream_sources.sh

set -e

YOUTUBE_URL="${YOUTUBE_URL:-https://www.youtube.com/watch?v=3nyPER2kzqk}"

echo "=== yt-dlp -F (formáty) ==="
yt-dlp -F --no-warnings "$YOUTUBE_URL" || true

echo ""
echo "=== yt-dlp krátký test stažení metadat ==="
yt-dlp --no-warnings --print title --skip-download "$YOUTUBE_URL" || true

echo ""
echo "=== ffprobe RTSP (nastavte RTSP_TEST_URI) ==="
if [ -n "${RTSP_TEST_URI}" ]; then
  ffprobe -v error -rtsp_transport tcp -show_streams -select_streams v:0 \
    -of default=nw=1:nk=1 "$RTSP_TEST_URI" || true
else
  echo "Přeskočeno: proměnná RTSP_TEST_URI není nastavena."
  echo "Příklad: export RTSP_TEST_URI='rtsp://user:pass@192.168.1.33:8554/stream_id'"
fi

echo ""
echo "Hotovo."
