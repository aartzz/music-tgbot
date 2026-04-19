[EN](README.md) | [UK](README-uk.md) | [RU](README-ru.md)

<div align="center">
  <img src="images/logo.jpeg" width="128" alt="logo">
  <h3>завантажувач музики aartzz</h3>
  завантажує аудіо з музичних платформ, вставляє метадані, надсилає в телеграм
</div>

## що робить

кидаєш посилання — бот завантажує трек, вставляє обкладинку / виконавця / назву / альбом / синхронізований текст, і надсилає назад. однаковий досвід для всіх джерел.

## джерела

| пріоритет | джерело | формат |
|-----------|---------|--------|
| 1 | **tidal** | flac (hi-res 24bit/96 або lossless 16bit/44.1) |
| 2 | **youtube** | mp3 (найкраща якість через yt-dlp) |
| 3 | **soundcloud** | mp3 (через yt-dlp) |
| 4 | **odesli** | знаходить tidal/youtube за посиланнями з 20+ платформ |

odesli обробляє: spotify, apple music, deezer, amazon music, yandex, audius, anghami, boomplay, audiomack, bandcamp, pandora, napster та інші. якщо є tidal — flac, якщо youtube — mp3, якщо нічого — пробує оригінальне посилання через yt-dlp.

## можливості

- **альбоми та плейлисти** — альбоми/плейлисти tidal, плейлисти youtube. всі треки за раз
- **синхронізований текст** — таймстемпи на рівні слів (enhanced LRC). вбудовується як SYLT+USLT (mp3) або Vorbis LYRICS (flac)
- **пошук** — надішли текст → пошук tidal → вибирай з поігинованих результатів (5 на сторінці)
- **мульти-мовність** — `/lang` перемикає мову бота (🇷🇺 🇺🇦 🇬🇧)
- **кешування** — повторні завантаження миттєві (telegram file_id)
- **dash hi-res** — ffmpeg обробляє tidal DASH MPD манифести для 24bit треків
- `/cancel` — скасовує поточні завантаження

## встановлення

```bash
git clone https://github.com/aartzz/music-tgbot.git
cd music-tgbot
pip install -r requirements.txt
```

встанови [ffmpeg](https://ffmpeg.org/download.html) — має бути в PATH.

скопіюй `.env.example` у `.env`, встав свій токен бота:

```
TOKEN=123456:ABC-DEF...
```

запуск:

```bash
python main.py
```

## примітки

- інстанси tidal api беруться з community uptime монітора. якщо tidal лежить — бот працює тільки з youtube
- odesli має ліміт запитів (10/хв). при 429 обертає 3 проксі
- старі youtube ключі кешу (11-символьні id) працюють поряд з новими з префіксом (`youtube:...`, `tidal:...`)
