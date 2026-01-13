import os
from io import BytesIO
from PIL import Image
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1
import aiohttp
from .utils import run_in_threadpool, remove_duplicate_artists


async def fetch_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.read()


def process_cover_and_tags(audio_path: str, title: str, artist: str, thumb: bytes) -> bytes:
    img = Image.open(BytesIO(thumb))
    if img.mode == "RGBA":
        img = img.convert("RGB")

    # Center crop to square
    w, h = img.size
    m = min(w, h)
    img = img.crop(((w - m)//2, (h - m)//2, (w + m)//2, (h + m)//2))

    # Smaller centered crop (346/461)
    new_dim = int(m * (346 / 461))
    offset = (m - new_dim)//2
    img = img.crop((offset, offset, offset + new_dim, offset + new_dim))

    # Save to bytes
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    thumb_bytes = buf.read()

    audio = MP3(audio_path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    if "APIC:" in audio.tags:
        del audio.tags["APIC:"]

    audio.tags.add(APIC(encoding=3, mime="image/PNG", type=3, desc="Cover", data=thumb_bytes))
    audio.tags.add(TIT2(encoding=3, text=title))
    audio.tags.add(TPE1(encoding=3, text=remove_duplicate_artists(artist)))
    audio.save()
    return thumb_bytes


async def process_audio(audio_path, title, artist, thumb_url):
    thumb_data = await fetch_bytes(thumb_url)
    return await run_in_threadpool(process_cover_and_tags, audio_path, title, artist, thumb_data)


def cleanup_file(path: str | None):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass