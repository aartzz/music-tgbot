import os
import asyncio
import logging
from yt_dlp import YoutubeDL
from typing import Any, Dict, Optional
from .utils import run_in_threadpool, sanitize_filename

yt_dlp_logger = logging.getLogger("yt_dlp")
yt_dlp_logger.setLevel(logging.ERROR)


def create_progress_hook(video_id: str, progress_dict: dict):
    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            if total:
                progress_dict[video_id] = (d["downloaded_bytes"] / total) * 100
        elif d.get("status") == "finished":
            progress_dict[video_id] = 100.0
    return hook


def make_ydl_opts(video_id=None, progress_dict=None) -> Dict[str, Any]:
    opts = {
        "format": "bestaudio",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }
        ],
        "outtmpl": os.path.join("downloads", "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
    }
    if video_id and progress_dict is not None:
        opts["progress_hooks"] = [create_progress_hook(video_id, progress_dict)]
    return opts


async def yt_extract(url: str, ydl_opts: Dict[str, Any], download=True):
    with YoutubeDL(ydl_opts) as ydl:
        return await run_in_threadpool(ydl.extract_info, url, download)


def build_paths(video_id: str, title: str) -> tuple[str, str]:
    tmp = os.path.join("downloads", f"{video_id}.mp3")
    final = os.path.join("downloads", f"{sanitize_filename(title)}.mp3")
    return tmp, final


def rename_with_collision_avoidance(src: str, desired: str) -> str:
    if not os.path.exists(desired):
        os.rename(src, desired)
        return desired
    base, ext = os.path.splitext(desired)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    dst = f"{base}_{i}{ext}"
    os.rename(src, dst)
    return dst