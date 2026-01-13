import os
import unicodedata
import re
import asyncio
import concurrent.futures
from aiogram.exceptions import TelegramBadRequest

thread_pool = concurrent.futures.ThreadPoolExecutor()


async def run_in_threadpool(func, *args, **kwargs):
    return await asyncio.get_event_loop().run_in_executor(
        thread_pool, lambda: func(*args, **kwargs)
    )


def remove_duplicate_artists(artist_string: str) -> str:
    if not artist_string:
        return ""
    normalized = artist_string.replace(" and ", ", ").replace(" & ", ", ")
    parts = [a.strip() for a in normalized.split(", ") if a.strip()]
    seen = set()
    result = []
    for a in parts:
        if a not in seen:
            seen.add(a)
            result.append(a)
    return ", ".join(result)


async def safe_edit_text(message, *args, **kwargs):
    try:
        return await message.edit_text(*args, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        raise


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("utf-8")
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = name.replace(" ", "_")
    return name[:100]