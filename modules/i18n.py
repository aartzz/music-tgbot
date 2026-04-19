"""
Simple i18n module. Stores per-user language preference (ru/uk/en).

Public API:
    get_lang(user_id) → str  ('ru', 'uk', or 'en')
    set_lang(user_id, lang) → None
    t(user_id, key) → str    (translated string)
"""
from __future__ import annotations

import sqlite3
import os
import logging
from typing import Optional

log = logging.getLogger(__name__)

_DB_PATH = os.path.join("db", "langs.db")
_DEFAULT = "ru"
_AVAILABLE = ("ru", "uk", "en")

# ---- DB layer ----

def _ensure_db():
    con = sqlite3.connect(_DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_langs(
            user_id INTEGER PRIMARY KEY,
            lang TEXT NOT NULL
        )
    """)
    con.commit()
    con.close()

_ensure_db()


def get_lang(user_id: int) -> str:
    """Return the user's language, or the default."""
    con = sqlite3.connect(_DB_PATH)
    row = con.execute("SELECT lang FROM user_langs WHERE user_id=?", (user_id,)).fetchone()
    con.close()
    if row and row[0] in _AVAILABLE:
        return row[0]
    return _DEFAULT


def set_lang(user_id: int, lang: str) -> None:
    """Set the user's language preference."""
    if lang not in _AVAILABLE:
        return
    con = sqlite3.connect(_DB_PATH)
    con.execute(
        "INSERT OR REPLACE INTO user_langs(user_id, lang) VALUES(?, ?)",
        (user_id, lang),
    )
    con.commit()
    con.close()


# ---- Translations ----

_T: dict[str, dict[str, str]] = {
    # ─── /start ────────────────────────────────────────────────────
    "start_title": {
        "ru": "aartzz's music downloader",
        "uk": "aartzz's music downloader",
        "en": "aartzz's music downloader",
    },
    "start_desc": {
        "ru": "скачивает аудио с разных платформ",
        "uk": "завантажує аудіо з різних платформ",
        "en": "downloads audio from various platforms",
    },
    "start_how": {
        "ru": "КАК ПОЛЬЗОВАТЬСЯ",
        "uk": "ЯК КОРИСТУВАТИСЯ",
        "en": "HOW TO USE",
    },
    "start_send_link": {
        "ru": "скинь ссылку на трек, альбом или плейлист",
        "uk": "кинь посилання на трек, альбом або плейлист",
        "en": "send a link to a track, album, or playlist",
    },
    "start_auto_tags": {
        "ru": "бот скачает звук, добавит превью, исполнителя и название",
        "uk": "бот завантажить звук, додасть превью, виконавця і назву",
        "en": "bot downloads audio, adds cover, artist and title",
    },
    "start_multi_link": {
        "ru": "можно отправлять несколько ссылок",
        "uk": "можна надсилати кілька посилань",
        "en": "you can send multiple links at once",
    },
    "start_cancel": {
        "ru": "/cancel отменяет все загрузки",
        "uk": "/cancel скасовує всі завантаження",
        "en": "/cancel cancels all downloads",
    },
    "start_animated": {
        "ru": "сообщения прогресса анимированы",
        "uk": "повідомлення прогресу анімовані",
        "en": "progress messages are animated",
    },
    "start_instant": {
        "ru": "повторные загрузки ранее загружавшихся треков мгновенны",
        "uk": "повторні завантаження раніше завантажуваних треків миттєві",
        "en": "re-downloads of previously cached tracks are instant",
    },
    "start_lang": {
        "ru": "/lang меняет язык (ru, uk, en)",
        "uk": "/lang змінює мову (ru, uk, en)",
        "en": "/lang changes language (ru, uk, en)",
    },

    # ─── Progress states ────────────────────────────────────────────
    "downloading": {
        "ru": "скачивание",
        "uk": "завантаження",
        "en": "downloading",
    },
    "processing": {
        "ru": "обработка",
        "uk": "обробка",
        "en": "processing",
    },
    "sending": {
        "ru": "отправка",
        "uk": "відправка",
        "en": "sending",
    },
    "fetching_track": {
        "ru": "достаю трек",
        "uk": "дістаю трек",
        "en": "fetching track",
    },
    "fetching_video": {
        "ru": "достаю видео",
        "uk": "дістаю відео",
        "en": "fetching video",
    },
    "fetching_playlist": {
        "ru": "достаю плейлист",
        "uk": "дістаю плейлист",
        "en": "fetching playlist",
    },

    # ─── Wait messages ──────────────────────────────────────────────
    "patience": {
        "ru": "терпение...",
        "uk": "терпіння...",
        "en": "patience...",
    },
    "more_videos_wait": {
        "ru": "больше видео = дольше обработка",
        "uk": "більше відео = довша обробка",
        "en": "more videos = longer processing",
    },
    "elapsed_cancel": {
        "ru": "прошло {0}с | отменить /cancel",
        "uk": "минув {0}с | скасувати /cancel",
        "en": "{0}s elapsed | cancel /cancel",
    },

    # ─── Collection (album/playlist) ────────────────────────────────
    "album": {
        "ru": "альбом",
        "uk": "альбом",
        "en": "album",
    },
    "playlist": {
        "ru": "плейлист",
        "uk": "плейлист",
        "en": "playlist",
    },
    "downloading_collection": {
        "ru": "скачиваем {0} ({1}/{2} - отменить /cancel)",
        "uk": "завантажуємо {0} ({1}/{2} - скасувати /cancel)",
        "en": "downloading {0} ({1}/{2} - cancel /cancel)",
    },
    "downloading_collection_short": {
        "ru": "скачиваем {0} ({1}/{2})",
        "uk": "завантажуємо {0} ({1}/{2})",
        "en": "downloading {0} ({1}/{2})",
    },
    "collection_done": {
        "ru": "готово, {0} полностью скачан",
        "uk": "готово, {0} повністю завантажено",
        "en": "done, {0} fully downloaded",
    },
    "empty_collection": {
        "ru": "пусто — нет треков",
        "uk": "порожньо — немає треків",
        "en": "empty — no tracks",
    },

    # ─── Errors ─────────────────────────────────────────────────────
    "error_processing": {
        "ru": "ошибка обработки",
        "uk": "помилка обробки",
        "en": "processing error",
    },
    "error_resolve": {
        "ru": "не удалось получить информацию о треке",
        "uk": "не вдалося отримати інформацію про трек",
        "en": "failed to get track info",
    },
    "error_not_found": {
        "ru": "трек не найден",
        "uk": "трек не знайдено",
        "en": "track not found",
    },
    "error_video_404": {
        "ru": "видео '{0}' не существует",
        "uk": "відео '{0}' не існує",
        "en": "video '{0}' does not exist",
    },

    # ─── Search ─────────────────────────────────────────────────────
    "searching": {
        "ru": "ищу",
        "uk": "шукаю",
        "en": "searching",
    },
    "search_failed": {
        "ru": "поиск не удался",
        "uk": "пошук не вдався",
        "en": "search failed",
    },
    "search_nothing": {
        "ru": "ничего не найдено",
        "uk": "нічого не знайдено",
        "en": "nothing found",
    },
    "search_results_for": {
        "ru": "результаты для",
        "uk": "результати для",
        "en": "results for",
    },
    "search_stale": {
        "ru": "результаты устарели",
        "uk": "результати застаріли",
        "en": "results expired",
    },
    "error": {
        "ru": "ошибка",
        "uk": "помилка",
        "en": "error",
    },

    # ─── /cancel ────────────────────────────────────────────────────
    "nothing_to_cancel": {
        "ru": "нечего отменять",
        "uk": "нічого скасовувати",
        "en": "nothing to cancel",
    },
    "cancelled": {
        "ru": "отменено!",
        "uk": "скасовано!",
        "en": "cancelled!",
    },

    # ─── /lang ──────────────────────────────────────────────────────
    "lang_set": {
        "ru": "язык установлен: {0}",
        "uk": "мову встановлено: {0}",
        "en": "language set: {0}",
    },
    "lang_pick": {
        "ru": "выберите язык:",
        "uk": "виберіть мову:",
        "en": "choose language:",
    },
    "lang_name_ru": {
        "ru": "🇷🇺 Русский",
        "uk": "🇷🇺 Російська",
        "en": "🇷🇺 Russian",
    },
    "lang_name_uk": {
        "ru": "🇺🇦 Українська",
        "uk": "🇺🇦 Українська",
        "en": "🇺🇦 Ukrainian",
    },
    "lang_name_en": {
        "ru": "🇬🇧 English",
        "uk": "🇬🇧 Англійська",
        "en": "🇬🇧 English",
    },
}


def t(user_id: int, key: str, *fmt_args) -> str:
    """Return the translated string for the user's language.

    Supports positional format args: t(uid, "elapsed_cancel", 15)
    → "прошло 15с | отменить /cancel" for ru.
    """
    lang = get_lang(user_id)
    entry = _T.get(key)
    if not entry:
        return key
    text = entry.get(lang) or entry.get(_DEFAULT) or key
    if fmt_args:
        text = text.format(*fmt_args)
    return text
