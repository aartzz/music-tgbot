import os
import re
from io import BytesIO
from PIL import Image
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, SYLT, USLT
from mutagen.flac import FLAC as FLACMutagen, Picture
import aiohttp
from .utils import run_in_threadpool, remove_duplicate_artists


async def fetch_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()


def _parse_lrc(lrc: str) -> list[tuple[int, str]]:
    """Parse LRC string into [(milliseconds, text), ...] list.

    Supports both standard LRC ([mm:ss.xx]text) and Enhanced LRC
    ([mm:ss.xx]word1 <mm:ss.xx>word2 ...). For Enhanced LRC, returns
    word-level entries (each word with its own timestamp).
    """
    entries = []
    for line in lrc.strip().split("\n"):
        line = line.strip()
        # Match line timestamp
        m = re.match(r"\[(\d{2}):(\d{2})\.(\d{2,3})](.*)", line)
        if not m:
            continue
        mins = int(m.group(1))
        secs = int(m.group(2))
        frac = m.group(3)
        ms_frac = int(frac) * (10 if len(frac) == 2 else 1)
        line_start_ms = mins * 60_000 + secs * 1000 + ms_frac
        rest = m.group(4).strip()

        if not rest:
            continue

        # Check for Enhanced LRC: contains <mm:ss.xx> word timestamps
        word_matches = list(re.finditer(r"<(\d{2}):(\d{2})\.(\d{2,3})>([^<]+)", rest))
        if word_matches:
            for wm in word_matches:
                w_mins = int(wm.group(1))
                w_secs = int(wm.group(2))
                w_frac = wm.group(3)
                w_ms_frac = int(w_frac) * (10 if len(w_frac) == 2 else 1)
                w_ms = w_mins * 60_000 + w_secs * 1000 + w_ms_frac
                w_text = wm.group(4).strip()
                if w_text:
                    entries.append((w_ms, w_text))
        else:
            # Standard LRC — line-level entry
            entries.append((line_start_ms, rest))

    return entries


def process_cover_and_tags(
    audio_path: str, title: str, artist: str, thumb: bytes,
    album: str | None = None, lrc: str | None = None,
    lrc_lang: str = "eng",
) -> bytes:
    """Embed cover art + tags + lyrics into an audio file. Branches on codec:
    - .mp3 → ID3 (APIC, TIT2, TPE1, TALB, SYLT, USLT)
    - .flac → Vorbis comments + Picture block + LYRICS
    """
    img = Image.open(BytesIO(thumb))
    if img.mode == "RGBA":
        img = img.convert("RGB")

    # Save cover to JPEG bytes (works for both ID3 and FLAC; PNG also fine but JPEG smaller).
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    thumb_bytes = buf.read()

    ext = os.path.splitext(audio_path)[1].lower()

    if ext == ".flac":
        audio = FLACMutagen(audio_path)
        audio["TITLE"] = title
        audio["ARTIST"] = remove_duplicate_artists(artist)
        if album:
            audio["ALBUM"] = album
        if lrc:
            audio["LYRICS"] = lrc

        # Clear existing pictures, add one.
        audio.clear_pictures()
        pic = Picture()
        pic.data = thumb_bytes
        pic.type = 3  # Cover (front)
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        audio.add_picture(pic)
        audio.save()

    else:  # .mp3 and anything else → ID3
        audio = MP3(audio_path, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()
        if "APIC:" in audio.tags:
            del audio.tags["APIC:"]
        # Remove existing lyrics frames
        for fid in ("SYLT:", "USLT:"):
            if fid in audio.tags:
                del audio.tags[fid]

        audio.tags.add(APIC(encoding=3, mime="image/JPEG", type=3, desc="Cover", data=thumb_bytes))
        audio.tags.add(TIT2(encoding=3, text=title))
        audio.tags.add(TPE1(encoding=3, text=remove_duplicate_artists(artist)))
        if album:
            audio.tags.add(TALB(encoding=3, text=album))

        # Embed lyrics
        if lrc:
            synced = _parse_lrc(lrc)
            if synced:
                # SYLT: word-level synced lyrics for karaoke-style playback.
                # Mutagen SynchronizedTextSpec expects (text, time) tuples.
                audio.tags.add(SYLT(
                    encoding=3, lang=lrc_lang, format=2, type=1,
                    text=[(txt, ms) for ms, txt in synced],
                ))
            # USLT: full LRC text with timestamps (not stripped).
            # Most players read this field — keeping timestamps lets
            # LRC-compatible players show synced lyrics.
            audio.tags.add(USLT(encoding=3, lang=lrc_lang, desc="", text=lrc))

        audio.save()

    return thumb_bytes


async def process_audio(audio_path, title, artist, thumb_url, album=None, lrc=None, lrc_lang="eng"):
    thumb_data = await fetch_bytes(thumb_url)
    return await run_in_threadpool(process_cover_and_tags, audio_path, title, artist, thumb_data, album, lrc, lrc_lang)


def cleanup_file(path: str | None):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass