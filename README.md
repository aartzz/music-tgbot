[EN](README.md) | [UK](README-uk.md) | [RU](README-ru.md)

<div align="center">
  <img src="images/logo.jpeg" width="128" alt="logo">
  <h3>aartzz's music downloader</h3>
  downloads audio from music platforms, embeds metadata, sends to telegram
</div>

## what it does

paste a link — bot downloads the track, slaps on cover art / artist / title / album / synced lyrics, and sends it back. same experience for every source.

## sources

| priority | source | format |
|----------|--------|--------|
| 1 | **tidal** | flac (hi-res 24bit/96 or lossless 16bit/44.1) |
| 2 | **youtube** | mp3 (best audio via yt-dlp) |
| 3 | **soundcloud** | mp3 (via yt-dlp) |
| 4 | **odesli** | resolves links from 20+ platforms to tidal/youtube |

odesli handles: spotify, apple music, deezer, amazon music, yandex, audius, anghami, boomplay, audiomack, bandcamp, pandora, napster, and more. if tidal link exists → flac, if youtube → mp3, if neither → tries the original url via yt-dlp.

## features

- **albums & playlists** — tidal albums/playlists, youtube playlists. all tracks in one go
- **synced lyrics** — word-by-word timestamps (enhanced LRC). embedded as SYLT+USLT (mp3) or Vorbis LYRICS (flac)
- **search** — send text → tidal search → pick from paginated results (5 per page)
- **multi-language** — `/lang` switches bot language (🇷🇺 🇺🇦 🇬🇧)
- **caching** — repeat downloads are instant (telegram file_id)
- **dash hi-res** — ffmpeg handles tidal DASH MPD manifests for 24bit tracks
- `/cancel` — aborts current downloads

## setup

```bash
git clone https://github.com/aartzz/music-tgbot.git
cd music-tgbot
pip install -r requirements.txt
```

install [ffmpeg](https://ffmpeg.org/download.html) — must be in PATH.

copy `.env.example` to `.env`, put your bot token:

```
TOKEN=123456:ABC-DEF...
```

run:

```bash
python main.py
```

## notes

- tidal api instances come from a community uptime monitor. if tidal is down, bot falls back to youtube-only
- odesli is rate-limited (10 req/min). rotates through 3 proxies on 429
- legacy youtube cache keys (11-char ids) work alongside new prefixed keys (`youtube:...`, `tidal:...`)
