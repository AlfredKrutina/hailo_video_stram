# YouTube / portály přes yt-dlp na Raspberry Pi

## Shrnutí

YouTube není primární use-case pro Pi v Dockeru: řetězec **yt-dlp → (ffmpeg MPEG-TS) → GStreamer** je náchylný k:

- změnám extractorů a formátů na straně YouTube;
- síťovým timeoutům z kontejneru;
- **OOM** při velkých frontách (`RPY_YTDLP_QUEUE_MB`).

Doporučení: pro produkci používejte **RTSP / lokální soubor / přímé HTTP MP4**.

## Doporučené proměnné

- `RPY_YTDLP_FFMPEG_TS=1` (výchozí) — stabilnější typefind než surový stdout z yt-dlp.
- `RPY_YTDLP_QUEUE_MB` — na Pi držet rozumně (např. 8–12).
- Staging: `RPY_YTDLP_LOG_STDERR=1` pro logy yt-dlp.

## Volitelný compose overlay

Soubor [docker/docker-compose.youtube-dev.yml](../docker/docker-compose.youtube-dev.yml) nastaví **limit paměti 2G** pro `ai_core` — vhodné jen pro vývoj / experimenty s YouTube, ne pro běžný RTSP provoz na slabém Pi.

Spuštění z adresáře `docker/`:

```bash
docker compose -f docker-compose.yml -f docker-compose.youtube-dev.yml up -d
```
