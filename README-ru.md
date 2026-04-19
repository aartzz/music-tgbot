[EN](README.md) | [UK](README-uk.md) | [RU](README-ru.md)

<div align="center">
  <img src="images/logo.jpeg" width="128" alt="logo">
  <h3>загрузчик музыки aartzz</h3>
  скачивает аудио с музыкальных платформ, вставляет метаданные, отправляет в телеграм
</div>

## что делает

кидаешь ссылку — бот скачивает трек, вставляет обложку / исполнителя / название / альбом / синхронизированный текст, и отправляет обратно. одинаковый опыт для всех источников.

## источники

| приоритет | источник | формат |
|-----------|----------|--------|
| 1 | **tidal** | flac (hi-res 24bit/96 или lossless 16bit/44.1) |
| 2 | **youtube** | mp3 (лучшее аудио через yt-dlp) |
| 3 | **soundcloud** | mp3 (через yt-dlp) |
| 4 | **odesli** | находит tidal/youtube по ссылкам с 20+ платформ |

odesli обрабатывает: spotify, apple music, deezer, amazon music, yandex, audius, anghami, boomplay, audiomack, bandcamp, pandora, napster и другие. если есть tidal — flac, если youtube — mp3, если ничего — пробует оригинальную ссылку через yt-dlp.

## возможности

- **альбомы и плейлисты** — альбомы/плейлисты tidal, плейлисты youtube. все треки за раз
- **синхронизированный текст** — таймстемпы на уровне слов (enhanced LRC). встраивается как SYLT+USLT (mp3) или Vorbis LYRICS (flac)
- **поиск** — отправь текст → поиск tidal → выбирай из постраничных результатов (5 на странице)
- **мульти-язычность** — `/lang` переключает язык бота (🇷🇺 🇺🇦 🇬🇧)
- **кэширование** — повторные загрузки мгновенные (telegram file_id)
- **dash hi-res** — ffmpeg обрабатывает tidal DASH MPD манифесты для 24bit треков
- `/cancel` — отменяет текущие загрузки

## установка

```bash
git clone https://github.com/aartzz/music-tgbot.git
cd music-tgbot
pip install -r requirements.txt
```

установи [ffmpeg](https://ffmpeg.org/download.html) — должен быть в PATH.

скопируй `.env.example` в `.env`, вставь свой токен бота:

```
TOKEN=123456:ABC-DEF...
```

запуск:

```bash
python main.py
```

## заметки

- инстансы tidal api берутся из community uptime монитора. если tidal лежит — бот работает только с youtube
- odesli имеет лимит запросов (10/мин). при 429 вращает 3 прокси
- старые youtube ключи кэша (11-символьные id) работают наряду с новыми с префиксом (`youtube:...`, `tidal:...`)
