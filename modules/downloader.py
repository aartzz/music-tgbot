import os
import asyncio
import logging
from yt_dlp import YoutubeDL
from typing import Any, Dict, Optional
import aiohttp
from .utils import run_in_threadpool, sanitize_filename

log = logging.getLogger(__name__)
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


# ---- Tidal FLAC streaming ----------------------------------------------------

_TIMEOUT = aiohttp.ClientTimeout(total=120, sock_read=30)


async def head_content_length(url: str) -> Optional[int]:
    """HEAD the stream URL and return Content-Length, or None if unknown."""
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.head(url, allow_redirects=True) as resp:
                if resp.status == 200:
                    return int(resp.headers.get("Content-Length", 0)) or None
    except (aiohttp.ClientError, asyncio.TimeoutError):
        log.debug("head_content_length failed for %s", url[:80])
    return None


async def download_stream(
    url: str,
    dest_path: str,
    progress_key: str = "",
    progress_dict: Optional[Dict[str, float]] = None,
) -> int:
    """Download a FLAC stream to *dest_path* with optional progress tracking.

    Returns the total bytes written. Raises RuntimeError if the file exceeds
    ``_MAX_SIZE`` mid-download (avoids wasting time on oversized tracks).
    """
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    total_bytes = 0

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise RuntimeError(f"stream download failed: HTTP {resp.status}")
            content_length = int(resp.headers.get("Content-Length", 0)) or None

            with open(dest_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    f.write(chunk)
                    total_bytes += len(chunk)

                    # Progress update.
                    if progress_key and progress_dict is not None:
                        if content_length:
                            progress_dict[progress_key] = (total_bytes / content_length) * 100
                        else:
                            # No length — pulse between 0..90 so UI shows activity.
                            progress_dict[progress_key] = min(
                                90.0, progress_dict.get(progress_key, 0) + 0.5
                            )

    if progress_key and progress_dict is not None:
        progress_dict[progress_key] = 100.0

    return total_bytes


def build_flac_paths(track_id: str, title: str) -> tuple[str, str]:
    """Build temp and final paths for a Tidal FLAC download."""
    tmp = os.path.join("downloads", f"{track_id}.flac")
    final = os.path.join("downloads", f"{sanitize_filename(title)}.flac")
    return tmp, final


# ---- DASH (Hi-Res) download via ffmpeg ---------------------------------------

_FFMPEG_PROTOCOL_WHITELIST = "tcp,http,https,tls,file,crypto"


async def download_dash(
    mpd_xml: str,
    dest_path: str,
    progress_key: str = "",
    progress_dict: Optional[Dict[str, float]] = None,
    size_estimate: int = 0,
    timeout: int = 180,
) -> int:
    """Download a DASH stream via ffmpeg from raw MPD XML.

    Writes MPD to a temp file, runs ``ffmpeg -i <mpd> -acodec copy <dest>``,
    and cleans up. Returns the total bytes written.
    """
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    mpd_path = dest_path + ".mpd"
    with open(mpd_path, "w", encoding="utf-8") as f:
        f.write(mpd_xml)

    cmd = [
        "ffmpeg", "-y",
        "-protocol_whitelist", _FFMPEG_PROTOCOL_WHITELIST,
        "-i", mpd_path,
        "-acodec", "copy",
        "-loglevel", "error",
        dest_path,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Poll progress while ffmpeg runs
    try:
        while proc.returncode is None:
            await asyncio.sleep(0.5)
            if progress_key and progress_dict is not None:
                if os.path.exists(dest_path):
                    current_size = os.path.getsize(dest_path)
                    if size_estimate and size_estimate > 0:
                        pct = min(95.0, (current_size / size_estimate) * 100)
                    else:
                        # No estimate — pulse slowly
                        pct = min(90.0, progress_dict.get(progress_key, 0) + 0.5)
                    progress_dict[progress_key] = pct
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        _cleanup_pair(mpd_path, dest_path)
        raise

    # ffmpeg finished
    stderr = await proc.stderr.read()
    _cleanup_pair(mpd_path)  # remove .mpd only, keep output

    if proc.returncode != 0:
        _cleanup_pair(dest_path)
        err = stderr.decode(errors="replace")[:200]
        raise RuntimeError(f"ffmpeg DASH download failed (code={proc.returncode}): {err}")

    if not os.path.exists(dest_path):
        raise RuntimeError("ffmpeg DASH download produced no output file")

    total_bytes = os.path.getsize(dest_path)

    if progress_key and progress_dict is not None:
        progress_dict[progress_key] = 100.0

    return total_bytes


def _cleanup_pair(*paths: str) -> None:
    """Silently remove one or more files if they exist."""
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass