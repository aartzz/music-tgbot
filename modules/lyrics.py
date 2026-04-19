"""
Lyrics fetching with timestamp-synced LRC output.

Sources (in priority order):
  1. LyricsPlus v2 (line-synced, multi-source)
  2. LyricsPlus v1 (syllable-synced → collapsed to lines)
  3. Tidal /lyrics/ (plain text → unsynced LRC)

Public API:
    fetch_lrc(title, artist, duration_ms, tidal_id='', isrc='', album='') → str
        Returns LRC string with [mm:ss.xx] timestamps, or "" on failure.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from . import tidal

log = logging.getLogger(__name__)

_LP_BASE = "https://lyricsplus.prjktla.my.id"
_LP_SOURCES = "apple,lyricsplus,qq,musixmatch,musixmatch-word"
_TIMEOUT = aiohttp.ClientTimeout(total=12)

# ISO 639-1 → ISO 639-2/B mapping for ID3 lang codes
_ISO_639_MAP = {
    "ja": "jpn", "en": "eng", "ru": "rus", "uk": "ukr", "de": "ger",
    "fr": "fre", "es": "spa", "it": "ita", "pt": "por", "ko": "kor",
    "zh": "zho", "ar": "ara", "hi": "hin", "tr": "tur", "pl": "pol",
    "nl": "nld", "sv": "swe", "no": "nor", "da": "dan", "fi": "fin",
    "cs": "cze", "el": "gre", "he": "heb", "th": "tha", "vi": "vie",
    "id": "ind", "ms": "may", "hu": "hun", "ro": "ron", "bg": "bul",
    "hr": "hrv", "sk": "slo", "sl": "slv", "lt": "lit", "lv": "lav",
    "et": "est", "ca": "cat", "eu": "baq", "gl": "glg", "eu": "baq",
}


def _lang_to_iso639_2(code: str) -> str:
    """Convert ISO 639-1 (2-letter) to ISO 639-2/B (3-letter) for ID3."""
    if not code:
        return "eng"
    mapped = _ISO_639_MAP.get(code.lower())
    if mapped:
        return mapped
    # Already 3-letter? Use as-is
    if len(code) == 3:
        return code.lower()
    return "eng"


def _ms_to_lrc_ts(ms: int) -> str:
    """Convert milliseconds to [mm:ss.xx] LRC timestamp."""
    total_s = ms / 1000.0
    m = int(total_s // 60)
    s = total_s - m * 60
    return f"[{m:02d}:{s:05.2f}]"


def _has_real_sync(lyrics: list) -> bool:
    """Check if lyrics entries have any non-zero timestamps."""
    return any(entry.get("time", 0) != 0 for entry in lyrics)


def _lpv2_to_lrc(lyrics: list) -> str:
    """Convert LyricsPlus v2 line-synced data to Enhanced LRC.

    Each line entry has 'syllabus': [{time, duration, text}, ...] with word-level timing.
    Enhanced LRC format: [mm:ss.xx]word1<mm:ss.xx>word2<mm:ss.xx>word3
    (First word has no angle-bracket timestamp — it inherits the line timestamp.)
    Falls back to plain line text if no syllabus data.
    Returns '' if no real sync timestamps exist (all time=0).
    """
    # If no entry has real timestamps, this is plain text — skip
    if not _has_real_sync(lyrics):
        return ""

    lines = []
    for entry in lyrics:
        ms = entry.get("time", 0)
        syllabus = entry.get("syllabus") or []
        line_text = entry.get("text", "")

        if syllabus and ms != 0:
            # Build Enhanced LRC with per-word timestamps
            # First word inherits the line [mm:ss.xx], no <mm:ss.xx> needed
            parts = [_ms_to_lrc_ts(ms)]
            for idx, syl in enumerate(syllabus):
                word_ms = syl.get("time", 0)
                word_text = syl.get("text", "")
                if not word_text:
                    continue
                if idx == 0:
                    parts.append(word_text)
                else:
                    parts.append(f"<{_ms_to_lrc_ts(word_ms)[1:-1]}>{word_text}")
            lines.append("".join(parts))
        elif ms != 0 and line_text:
            # No syllabus — plain synced line
            lines.append(f"{_ms_to_lrc_ts(ms)}{line_text}")

    return "\n".join(lines)


def _lpv1_to_lrc(lyrics: list) -> str:
    """Convert LyricsPlus v1 syllable-synced data to Enhanced LRC.

    v1 gives per-syllable entries: [{time, text, duration, isLineEnding}, ...].
    We group syllables into lines by isLineEnding flag, and output Enhanced LRC
    with per-word <mm:ss.xx> timestamps.
    Returns '' if no real sync timestamps exist (all time=0).
    """
    if not lyrics:
        return ""

    # If no entry has real timestamps, this is plain text — skip
    if not _has_real_sync(lyrics):
        return ""

    lines = []
    current_words: list[tuple[int, str]] = []  # [(ms, word), ...]
    current_line_ms = 0

    for i, entry in enumerate(lyrics):
        ms = entry.get("time", 0)
        text = entry.get("text", "")
        is_end = entry.get("isLineEnding", 0)

        if not current_words:
            current_line_ms = ms

        if text:
            current_words.append((ms, text))

        # isLineEnding=1 means this syllable ends the line
        if is_end and current_words:
            parts = [_ms_to_lrc_ts(current_line_ms)]
            for idx, (w_ms, w_text) in enumerate(current_words):
                if idx == 0:
                    parts.append(w_text)
                else:
                    parts.append(f"<{_ms_to_lrc_ts(w_ms)[1:-1]}>{w_text}")
            lines.append("".join(parts))
            current_words = []
            current_line_ms = 0

    # Flush remaining
    if current_words:
        parts = [_ms_to_lrc_ts(current_line_ms)]
        for idx, (w_ms, w_text) in enumerate(current_words):
            if idx == 0:
                parts.append(w_text)
            else:
                parts.append(f"<{_ms_to_lrc_ts(w_ms)[1:-1]}>{w_text}")
        lines.append("".join(parts))

    return "\n".join(lines)


def _plain_to_lrc(text: str) -> str:
    """Convert plain text lyrics to unsynced LRC format.

    Uses [00:00.00] timestamp for all lines (no sync data available).
    """
    lines = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if line:
            lines.append(f"[00:00.00]{line}")
    return "\n".join(lines)


def _build_lp_params(title: str, artist: str, duration_ms: int = 0,
                     isrc: str = "", album: str = "") -> dict:
    """Build common LyricsPlus query params."""
    params: dict = {
        "title": title,
        "artist": artist,
        "source": _LP_SOURCES,
        "forceReload": "true",
    }
    if duration_ms:
        params["duration"] = str(duration_ms)
    if isrc:
        params["isrc"] = isrc
    if album:
        params["album"] = album
    return params


def _extract_lang(data: dict) -> str:
    """Extract language from LyricsPlus response, convert to ISO 639-2."""
    lang = (data.get("metadata") or {}).get("language", "")
    return _lang_to_iso639_2(lang)


async def _fetch_lp_v2(session: aiohttp.ClientSession, title: str, artist: str,
                       duration_ms: int = 0, isrc: str = "", album: str = "") -> tuple[str, str]:
    """Try LyricsPlus v2 (line-synced). Returns (LRC, lang) or ('', '')."""
    params = _build_lp_params(title, artist, duration_ms, isrc, album)
    try:
        async with session.get(f"{_LP_BASE}/v2/lyrics/get", params=params, timeout=_TIMEOUT) as r:
            if r.status != 200:
                return "", ""
            data = await r.json(content_type=None)
            lyrics = data.get("lyrics") or []
            if not lyrics:
                return "", ""
            lrc = _lpv2_to_lrc(lyrics)
            if not lrc:
                return "", ""
            return lrc, _extract_lang(data)
    except Exception as e:
        log.debug("lyricsplus v2 failed: %s", e)
        return "", ""


async def _fetch_lp_v1(session: aiohttp.ClientSession, title: str, artist: str,
                       duration_ms: int = 0, isrc: str = "", album: str = "") -> tuple[str, str]:
    """Try LyricsPlus v1 (syllable-synced → collapse to lines). Returns (LRC, lang) or ('', '')."""
    params = _build_lp_params(title, artist, duration_ms, isrc, album)
    try:
        async with session.get(f"{_LP_BASE}/v1/lyrics/get", params=params, timeout=_TIMEOUT) as r:
            if r.status != 200:
                return "", ""
            data = await r.json(content_type=None)
            lyrics = data.get("lyrics") or []
            if not lyrics:
                return "", ""
            lrc = _lpv1_to_lrc(lyrics)
            if not lrc:
                return "", ""
            return lrc, _extract_lang(data)
    except Exception as e:
        log.debug("lyricsplus v1 failed: %s", e)
        return "", ""


async def _fetch_tidal_lyrics(tidal_id: str) -> str:
    """Try Tidal /lyrics/ endpoint. Returns plain-text or ''."""
    if not tidal_id:
        return ""
    try:
        text = await tidal.get_lyrics(tidal_id)
        if text:
            return _plain_to_lrc(text)
        return ""
    except Exception as e:
        log.debug("tidal lyrics failed: %s", e)
        return ""


async def fetch_lrc(title: str, artist: str, duration_ms: int = 0,
                    tidal_id: str = "", isrc: str = "", album: str = "") -> tuple[str, str]:
    """Fetch synced lyrics as LRC string with language code.

    Tries LyricsPlus v2 → v1 → Tidal plain text.
    Returns (lrc_string, iso_639_2_lang) or ("", "eng") on total failure.
    """
    async with aiohttp.ClientSession() as session:
        # Try v2 and v1 in parallel (v2 is preferred, v1 as backup)
        v2_task = asyncio.create_task(_fetch_lp_v2(session, title, artist, duration_ms, isrc, album))
        v1_task = asyncio.create_task(_fetch_lp_v1(session, title, artist, duration_ms, isrc, album))

        v2_result, v1_result = await asyncio.gather(v2_task, v1_task)

        # v2 (line-synced) is best
        if v2_result[0]:
            log.info("lyrics: got synced LRC from LyricsPlus v2 for %s - %s", artist, title)
            return v2_result

        # v1 (syllable → line) is acceptable
        if v1_result[0]:
            log.info("lyrics: got synced LRC from LyricsPlus v1 for %s - %s", artist, title)
            return v1_result

    # Tidal fallback (unsynced plain text)
    tidal_result = await _fetch_tidal_lyrics(tidal_id)
    if tidal_result:
        log.info("lyrics: got plain text from Tidal for %s - %s", artist, title)
        return tidal_result, "eng"

    log.info("lyrics: no lyrics found for %s - %s", artist, title)
    return "", "eng"
