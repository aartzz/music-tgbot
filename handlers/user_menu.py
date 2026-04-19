import asyncio
import os
import logging
from typing import Optional, Dict, List

from aiogram import Router, Bot, F
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile, BufferedInputFile, LinkPreviewOptions, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.exceptions import TelegramBadRequest

from db.db import Music, Analytics, get_cache_lock
from modules.sources import classify_url, resolve, ResolvedItem
from modules.tidal import get_stream_info, StreamInfo, search as tidal_search
from modules.i18n import t, get_lang, set_lang
from modules.downloader import (
    yt_extract,
    make_ydl_opts,
    build_paths,
    build_flac_paths,
    rename_with_collision_avoidance,
    head_content_length,
    download_stream,
    download_dash,
)
from modules.processing import process_audio, cleanup_file
from modules.lyrics import fetch_lrc
from modules.utils import safe_edit_text, remove_duplicate_artists
from modules.progress import (
    animate_ellipsis,
    animate_download_progress,
    animate_countdown,
    animate_starting,
)
import html
import re
from io import BytesIO
from PIL import Image

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Router & Database
# ------------------------------------------------------------------------------
router = Router()

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

db = Music()
db_analytics = Analytics()

# Track per-user running tasks and messages
user_tasks: Dict[int, List[asyncio.Task]] = {}
user_messages: Dict[int, List[Message]] = {}

# Global concurrent limit
MAX_CONCURRENT_DOWNLOADS = 5
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# Single dictionary for progress tracking — keys are now "{source}:{id}"
download_progress: Dict[str, float] = {}


# ------------------------------------------------------------------------------
# Task tracking
# ------------------------------------------------------------------------------
def track_task(uid: int, task: asyncio.Task):
    user_tasks.setdefault(uid, []).append(task)


def untrack_task(uid: int, task: asyncio.Task):
    try:
        user_tasks[uid].remove(task)
    except Exception:
        pass


def track_message(uid: int, message: Message):
    user_messages.setdefault(uid, []).append(message)


async def cancel_user_tasks_and_messages(uid: int):
    for t in user_tasks.get(uid, []):
        if not t.done() and not t.cancelled():
            t.cancel()

    for m in user_messages.get(uid, []):
        try:
            await m.delete()
        except Exception:
            pass

    user_tasks[uid] = []
    user_messages[uid] = []


# ------------------------------------------------------------------------------
# Core file sending
# ------------------------------------------------------------------------------
def _resize_thumb(thumb_data: bytes, max_px: int = 320, max_kb: int = 200) -> bytes:
    """Resize thumbnail for Telegram: ≤max_px pixels, ≤max_kb kilobytes."""
    img = Image.open(BytesIO(thumb_data))
    if img.mode == "RGBA":
        img = img.convert("RGB")
    img.thumbnail((max_px, max_px))
    quality = 85
    while quality >= 30:
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        size = buf.tell()
        if size <= max_kb * 1024:
            buf.seek(0)
            return buf.read()
        quality -= 10
    # Fallback: return whatever we have
    buf.seek(0)
    return buf.read()


async def send_cached_audio(
    msg: Message, bot: Bot, cache_key: str, file_id: str, progress_msg: Message, silent: bool = False
) -> bool:
    """Resend a cached file_id. Returns True on success."""
    if progress_msg:
        try:
            await progress_msg.delete()
        except Exception:
            pass
    try:
        await bot.send_audio(
            chat_id=msg.chat.id, audio=file_id, disable_notification=silent
        )
        return True
    except Exception:
        db.remove_data(cache_key)
        return False


async def send_processed_audio(
    bot: Bot,
    msg: Message,
    cache_key: str,
    audio_path: str,
    title: str,
    artist: str,
    thumb_data: bytes,
    duration: int | None = None,
    silent: bool = False,
) -> str:
    """Upload a local file via send_audio, cache the resulting file_id."""
    filename = os.path.basename(audio_path)
    # Resize thumbnail for Telegram (≤320px, <200KB)
    tg_thumb = _resize_thumb(thumb_data)
    # Extract duration from file if not provided (needed for FLAC — Telegram won't auto-detect)
    if duration is None and audio_path.endswith(".flac"):
        try:
            from mutagen.flac import FLAC as FLACMutagen
            flac_info = FLACMutagen(audio_path)
            duration = int(flac_info.info.length)
        except Exception:
            pass
    kwargs = dict(
        chat_id=msg.chat.id,
        audio=FSInputFile(audio_path, filename=filename),
        title=title,
        performer=artist,
        thumbnail=BufferedInputFile(tg_thumb, filename=f"{cache_key}_thumb.jpg"),
        disable_notification=silent,
    )
    if duration:
        kwargs["duration"] = duration
    sent = await bot.send_audio(**kwargs)
    db.add_data(cache_key, sent.audio.file_id)
    return sent.audio.file_id


# ------------------------------------------------------------------------------
# Single-track processing pipeline
# ------------------------------------------------------------------------------
async def process_single_track(
    msg: Message,
    bot: Bot,
    item: ResolvedItem,
    progress_msg: Optional[Message],
    user_id: int,
    skip_db_check: bool = False,
    is_from_album: bool = False,
):
    """
    Download, tag, and send a single ResolvedItem (track).
    Handles both Tidal (FLAC) and YouTube (MP3) paths.
    """
    cache_key = item.cache_key
    display_name = f'<blockquote><a href="{item.original_url}">{item.title}</a></blockquote>'

    # --- Cache check (unless skipped, e.g. already checked by caller) ---
    if not skip_db_check:
        cached_id = db.get_file_id(cache_key)
        if cached_id:
            try:
                await bot.send_audio(
                    chat_id=msg.chat.id,
                    audio=cached_id,
                    disable_notification=is_from_album,
                )
                if progress_msg:
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass
                return
            except Exception:
                db.remove_data(cache_key)

    # --- Acquire per-key lock to prevent concurrent same-track downloads ---
    lock = get_cache_lock(cache_key)
    async with lock:
        # Re-check cache after acquiring lock (another task may have downloaded it)
        if not skip_db_check:
            cached_id = db.get_file_id(cache_key)
            if cached_id:
                try:
                    await bot.send_audio(
                        chat_id=msg.chat.id,
                        audio=cached_id,
                        disable_notification=is_from_album,
                    )
                    if progress_msg:
                        try:
                            await progress_msg.delete()
                        except Exception:
                            pass
                    return
                except Exception:
                    db.remove_data(cache_key)

        # Create progress message if it doesn't exist
        if not progress_msg:
            progress_msg = await msg.answer(
                f"{display_name}\n⬇️ {t(user_id, 'downloading')}...",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )
            track_message(user_id, progress_msg)

        # --- Tidal FLAC path ---
        if item.source == "tidal":
            await _process_tidal_track(
                msg, bot, item, cache_key, display_name, progress_msg, user_id, is_from_album
            )
        # --- YouTube MP3 path ---
        else:
            await _process_youtube_track(
                msg, bot, item, cache_key, display_name, progress_msg, user_id, is_from_album
            )


async def _process_tidal_track(
    msg, bot, item, cache_key, display_name, progress_msg, user_id, is_from_album
):
    """Download FLAC from Tidal, tag, and send. Handles BTS (direct) and DASH (ffmpeg)."""
    # Get stream info (handles both BTS and DASH manifests)
    try:
        stream_info = await get_stream_info(item.id)
    except Exception as e:
        raise RuntimeError(f"Tidal stream failed: {e}")

    temp_path, final_path = build_flac_paths(item.id, item.title)
    download_progress[cache_key] = 0.0

    # Update progress message
    quality_label = ""
    if stream_info.bit_depth and stream_info.sample_rate:
        quality_label = f" ({stream_info.bit_depth}bit/{stream_info.sample_rate // 1000}kHz)"
    await safe_edit_text(
        progress_msg,
        f"{display_name}\n⬇️ {t(user_id, 'downloading')}{quality_label}...",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        parse_mode="HTML",
    )

    anim_task = asyncio.create_task(
        animate_download_progress(
            progress_msg, display_name, cache_key, bot, download_progress, user_id=user_id, is_playlist=is_from_album
        )
    )
    track_task(user_id, anim_task)

    try:
        # --- Download phase ---
        if stream_info.type == "bts":
            await download_stream(stream_info.url, temp_path, cache_key, download_progress)
        else:
            # DASH: use ffmpeg to download from MPD manifest
            await download_dash(stream_info.mpd_xml, temp_path, cache_key, download_progress,
                                size_estimate=stream_info.size_estimate or 0)

        try:
            final_path = rename_with_collision_avoidance(temp_path, final_path)
        except Exception:
            final_path = temp_path

        anim_task.cancel()
        await progress_msg.edit_text(
            f"{display_name}\n✴️ {t(user_id, 'processing')}...",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode="HTML",
        )
        anim_task = asyncio.create_task(
            animate_ellipsis(
                progress_msg, display_name, f"✴️ {t(user_id, 'processing')}", "", bot, ChatAction.UPLOAD_PHOTO
            )
        )
        track_task(user_id, anim_task)

        # Fetch lyrics in parallel with tagging
        lrc, lrc_lang = await fetch_lrc(item.title, item.artist, item.duration or 0, tidal_id=item.id, isrc=item.isrc or "", album=item.album or "")

        thumb_data = await process_audio(
            final_path, item.title, item.artist, item.cover_url,
            album=item.album, lrc=lrc, lrc_lang=lrc_lang,
        )

        anim_task.cancel()
        await progress_msg.edit_text(
            f"{display_name}\n❇️ {t(user_id, 'sending')}...",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode="HTML",
        )

        await send_processed_audio(
            bot, msg, cache_key, final_path, item.title, item.artist, thumb_data,
            duration=item.duration, silent=is_from_album,
        )

        try:
            await progress_msg.delete()
        except Exception:
            pass

    except asyncio.CancelledError:
        try:
            anim_task.cancel()
        except Exception:
            pass
        cleanup_file(temp_path)
        cleanup_file(final_path)
        raise
    except Exception as e:
        try:
            anim_task.cancel()
        except Exception:
            pass
        err_txt = f"⛔️ {t(user_id, 'error_processing')}"
        error_details = html.escape(str(e))
        try:
            await progress_msg.edit_text(
                f"{display_name}\n{err_txt} <i>15</i>\n<pre><code>{error_details}</code></pre>",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )
        except Exception:
            progress_msg = await msg.answer(
                f"{display_name}\n{err_txt} <i>15</i>\n<pre><code>{error_details}</code></pre>",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )
            track_message(user_id, progress_msg)
        await animate_countdown(progress_msg, err_txt, 15, display_name, error_details)
    finally:
        download_progress.pop(cache_key, None)
        cleanup_file(temp_path)
        cleanup_file(final_path)


async def _process_youtube_track(
    msg, bot, item, cache_key, display_name, progress_msg, user_id, is_from_album
):
    """Download MP3 via yt-dlp, tag, and send (existing path)."""
    temp_path, final_path = build_paths(item.id, item.title)
    download_progress[cache_key] = 0.0

    await safe_edit_text(
        progress_msg,
        f"{display_name}\n⬇️ {t(user_id, 'downloading')}...",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        parse_mode="HTML",
    )

    anim_task = asyncio.create_task(
        animate_download_progress(
            progress_msg, display_name, cache_key, bot, download_progress, user_id=user_id, is_playlist=is_from_album
        )
    )
    track_task(user_id, anim_task)

    try:
        ydl_opts = make_ydl_opts(cache_key, download_progress)
        await yt_extract(item.original_url, ydl_opts, download=True)

        if not (os.path.exists(temp_path) and item.cover_url):
            raise RuntimeError(f"404 - {t(user_id, 'error_video_404', item.title)}")

        try:
            final_path = rename_with_collision_avoidance(temp_path, final_path)
        except Exception:
            final_path = temp_path

        anim_task.cancel()
        await progress_msg.edit_text(
            f"{display_name}\n✴️ {t(user_id, 'processing')}...",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode="HTML",
        )
        anim_task = asyncio.create_task(
            animate_ellipsis(
                progress_msg, display_name, f"✴️ {t(user_id, 'processing')}", "", bot, ChatAction.UPLOAD_PHOTO
            )
        )
        track_task(user_id, anim_task)

        # Fetch lyrics (no tidal_id for YouTube source)
        lrc, lrc_lang = await fetch_lrc(item.title, item.artist, item.duration or 0, isrc=item.isrc or "", album=item.album or "")

        thumb_data = await process_audio(
            final_path, item.title, item.artist, item.cover_url,
            album=item.album, lrc=lrc, lrc_lang=lrc_lang,
        )

        anim_task.cancel()
        await progress_msg.edit_text(
            f"{display_name}\n❇️ {t(user_id, 'sending')}...",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode="HTML",
        )

        await send_processed_audio(
            bot, msg, cache_key, final_path, item.title, item.artist, thumb_data,
            duration=item.duration, silent=is_from_album,
        )

        try:
            await progress_msg.delete()
        except Exception:
            pass

    except asyncio.CancelledError:
        try:
            anim_task.cancel()
        except Exception:
            pass
        cleanup_file(temp_path)
        cleanup_file(final_path)
        raise
    except Exception as e:
        try:
            anim_task.cancel()
        except Exception:
            pass
        err_txt = f"⛔️ {t(user_id, 'error_processing')}"
        error_details = html.escape(str(e))
        try:
            await progress_msg.edit_text(
                f"{display_name}\n{err_txt} <i>15</i>\n<pre><code>{error_details}</code></pre>",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )
        except Exception:
            progress_msg = await msg.answer(
                f"{display_name}\n{err_txt} <i>15</i>\n<pre><code>{error_details}</code></pre>",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )
            track_message(user_id, progress_msg)
        await animate_countdown(progress_msg, err_txt, 15, display_name, error_details)
    finally:
        download_progress.pop(cache_key, None)
        cleanup_file(temp_path)
        cleanup_file(final_path)


# ------------------------------------------------------------------------------
# Entry point for URLs
# ------------------------------------------------------------------------------
async def handle_url(msg: Message, bot: Bot, original_url: str, user_id: int):
    """Resolve a URL and download/send the audio."""

    progress_msg = await msg.answer(
        f"<blockquote>{original_url}</blockquote>\n🛜 {t(user_id, 'fetching_track')}...",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        parse_mode="HTML",
    )
    track_message(user_id, progress_msg)

    db_analytics.add_user(msg.from_user.id)
    db_analytics.increment_use_count()

    start_anim = asyncio.create_task(animate_starting(progress_msg, original_url, bot, user_id=user_id))
    track_task(user_id, start_anim)

    async with download_semaphore:
        try:
            resolved = await resolve(original_url)
        except Exception as e:
            start_anim.cancel()
            err_txt = f"⛔️ {t(user_id, 'error_resolve')}"
            error_details = html.escape(str(e))
            try:
                await progress_msg.edit_text(
                    f"<blockquote>{original_url}</blockquote>\n{err_txt} <i>15</i>\n<pre><code>{error_details}</code></pre>",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await animate_countdown(progress_msg, err_txt, 15, original_url, error_details)
            return

        if resolved is None:
            start_anim.cancel()
            err_txt = f"⛔️ {t(user_id, 'error_not_found')}"
            await animate_countdown(progress_msg, err_txt, 15, original_url)
            return

        start_anim.cancel()

        # --- Album / Playlist: iterate items ---
        if resolved.kind in ("album", "playlist"):
            await _handle_collection(msg, bot, resolved, progress_msg, user_id)

        # --- Single track ---
        else:
            # Cache check BEFORE creating progress message (for instant replay)
            cache_key = resolved.cache_key
            cached_id = db.get_file_id(cache_key)
            if cached_id:
                try:
                    await bot.send_audio(
                        chat_id=msg.chat.id,
                        audio=cached_id,
                        disable_notification=False,
                    )
                    try:
                        await progress_msg.delete()
                    except Exception:
                        pass
                    return
                except Exception:
                    db.remove_data(cache_key)

            display_name = f'<blockquote><a href="{resolved.original_url}">{resolved.title}</a></blockquote>'
            await safe_edit_text(
                progress_msg,
                f"{display_name}\n⬇️ {t(user_id, 'downloading')}...",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )

            await process_single_track(
                msg, bot, resolved, progress_msg, user_id, skip_db_check=True, is_from_album=False
            )
            try:
                await progress_msg.delete()
            except Exception:
                pass


async def _handle_collection(
    msg: Message, bot: Bot, resolved: ResolvedItem, progress_msg: Message, user_id: int
):
    """Handle album or playlist: iterate tracks, send each."""
    is_album = resolved.kind == "album"
    collection_title = resolved.title or "??????"
    collection_display = f'<blockquote><b><a href="{resolved.original_url}">{collection_title}</a></b></blockquote>'

    items = resolved.items or []
    if not items:
        await animate_countdown(progress_msg, f"⛔️ {t(user_id, 'empty_collection')}", 15, collection_display)
        return

    total = len(items)

    # Update text with collection info
    collection_label = t(user_id, "album") if is_album else t(user_id, "playlist")
    await safe_edit_text(
        progress_msg,
        f"{collection_display}\n📋 {t(user_id, 'downloading_collection_short', collection_label, 0, total)}",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        parse_mode="HTML",
    )

    # Pin progress message in groups
    if msg.chat.type in (ChatType.SUPERGROUP, ChatType.GROUP):
        try:
            await bot.pin_chat_message(
                chat_id=msg.chat.id,
                message_id=progress_msg.message_id,
                disable_notification=True,
            )
        except Exception:
            pass

    for idx, item in enumerate(items, start=1):
        if asyncio.current_task().cancelled():
            raise asyncio.CancelledError()

        await safe_edit_text(
            progress_msg,
            f"{collection_display}\n📋 {t(user_id, 'downloading_collection', collection_label, idx, total)}",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode="HTML",
        )

        # Check DB before creating message
        cached_id = db.get_file_id(item.cache_key)
        if cached_id:
            try:
                await bot.send_audio(
                    chat_id=msg.chat.id,
                    audio=cached_id,
                    disable_notification=True,
                )
                continue
            except Exception:
                db.remove_data(item.cache_key)

        track_display = f'<blockquote><a href="{item.original_url}">{item.title}</a></blockquote>'
        track_msg = await msg.answer(
            f"{track_display}\n⬇️ {t(user_id, 'downloading')}...",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode="HTML",
        )
        track_message(user_id, track_msg)
        await process_single_track(
            msg, bot, item, track_msg, user_id, skip_db_check=True, is_from_album=True
        )

    try:
        await progress_msg.delete()
    except Exception:
        pass

    # Final message with sound notification
    done = await msg.answer(
        f"✅ {t(user_id, 'collection_done', collection_label)} <i>15</i>",
        parse_mode="HTML",
        disable_notification=False,
    )
    await animate_countdown(done, f"✅ {t(user_id, 'collection_done', collection_label)}", 30)


# ------------------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------------------
@router.message(Command(commands=["start"]))
async def start(msg: Message):
    uid = msg.from_user.id
    await msg.answer(
        f"""<b><u>{t(uid, 'start_title')}</u></b>
{t(uid, 'start_desc')}

<b><i>{t(uid, 'start_how')}:</i></b>
<blockquote>- {t(uid, 'start_send_link')}
- {t(uid, 'start_auto_tags')}
- {t(uid, 'start_multi_link')}
- {t(uid, 'start_cancel')}
- {t(uid, 'start_animated')}
- {t(uid, 'start_instant')}
- {t(uid, 'start_lang')}</blockquote>""",
        parse_mode="HTML",
    )


@router.message(Command(commands=["analytics"]))
async def send_analytics(msg: Message):
    if msg.from_user.id != 653632008:
        await msg.delete()
        return
    await msg.delete()
    a = await msg.answer(
        f"бот скачал {db_analytics.get_total_use_count()} файлов по запросам от {db_analytics.get_user_count()} пользователей"
    )
    await asyncio.sleep(5)
    await a.delete()


@router.message(Command(commands=["cancel"]))
async def cancel_downloads(msg: Message):
    await msg.delete()
    user_id = msg.from_user.id
    if not user_tasks.get(user_id):
        m = await msg.answer(f"✅ {t(user_id, 'nothing_to_cancel')} <i>5</i>", parse_mode="HTML")
        await animate_countdown(m, f"✅ {t(user_id, 'nothing_to_cancel')}", 5)
        return
    await cancel_user_tasks_and_messages(user_id)
    m = await msg.answer(f"✅ {t(user_id, 'cancelled')} <i>5</i>", parse_mode="HTML")
    await animate_countdown(m, f"✅ {t(user_id, 'cancelled')}", 5)


@router.message(Command(commands=["lang"]))
async def lang_command(msg: Message):
    """Set language directly or show picker."""
    uid = msg.from_user.id
    # If user provided a lang code (e.g. /lang uk), set it directly
    arg = (msg.text or "").split(maxsplit=1)
    if len(arg) == 2 and arg[1].strip().lower() in ("ru", "uk", "en"):
        lang = arg[1].strip().lower()
        set_lang(uid, lang)
        await msg.answer(t(uid, "lang_set", lang))
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=t(uid, "lang_name_ru"), callback_data="l:ru"),
            InlineKeyboardButton(text=t(uid, "lang_name_uk"), callback_data="l:uk"),
            InlineKeyboardButton(text=t(uid, "lang_name_en"), callback_data="l:en"),
        ]
    ])
    await msg.answer(t(uid, "lang_pick"), reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("l:"))
async def lang_callback(cb: CallbackQuery):
    """Handle language selection callback."""
    lang = cb.data[2:]  # strip "l:"
    uid = cb.from_user.id
    set_lang(uid, lang)
    # Re-fetch with new language
    await cb.answer(t(uid, "lang_set", lang), show_alert=False)
    try:
        await cb.message.delete()
    except Exception:
        pass


# ------------------------------------------------------------------------------
# Search + inline buttons + pagination
# ------------------------------------------------------------------------------
_SEARCH_RESULTS: Dict[int, List[Dict]] = {}  # msg_id → list of track dicts
_SEARCH_PER_PAGE = 5


def _track_label(track: Dict) -> tuple[str, str]:
    """Return (display_label, callback_data) for a search result track."""
    title = track.get("title", "???")
    artist = (track.get("artist") or {}).get("name", "")
    version = track.get("version")
    if version:
        title = f"{title} ({version})"
    label = f"{artist} — {title}"[:60]
    track_id = str(track.get("id", ""))
    return label, f"t:{track_id}"


def _build_search_kb(results: List[Dict], page: int, msg_id: int) -> InlineKeyboardMarkup:
    """Build inline keyboard for search page: track buttons + nav row."""
    total = len(results)
    total_pages = max(1, (total + _SEARCH_PER_PAGE - 1) // _SEARCH_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start = page * _SEARCH_PER_PAGE
    page_items = results[start:start + _SEARCH_PER_PAGE]

    rows = []
    for track in page_items:
        label, cb_data = _track_label(track)
        rows.append([InlineKeyboardButton(text=label, callback_data=cb_data)])

    # Navigation row: ◀️ 1/10 ▶️
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"sp:{msg_id}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="sp:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"sp:{msg_id}:{page + 1}"))
    rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _search_and_show(msg: Message, bot: Bot, query: str, user_id: int):
    """Run Tidal search and display paginated results."""
    progress = await msg.answer(
        f"🔍 {t(user_id, 'searching')} <b>{html.escape(query)}</b>...",
        parse_mode="HTML",
    )
    track_message(user_id, progress)

    try:
        results = await tidal_search(query, limit=50)
    except Exception as e:
        await safe_edit_text(
            progress,
            f"⛔️ {t(user_id, 'search_failed')}\n<pre><code>{html.escape(str(e))}</code></pre>",
            parse_mode="HTML",
        )
        await animate_countdown(progress, f"⛔️ {t(user_id, 'search_failed')}", 15)
        return

    if not results:
        await safe_edit_text(progress, f"🔍 {t(user_id, 'search_nothing')} 😔", parse_mode="HTML")
        await animate_countdown(progress, f"🔍 {t(user_id, 'search_nothing')}", 10)
        return

    # Store results for pagination callbacks
    _SEARCH_RESULTS[progress.message_id] = results

    kb = _build_search_kb(results, 0, progress.message_id)
    await safe_edit_text(
        progress,
        f"🔍 {t(user_id, 'search_results_for')} <b>{html.escape(query)}</b>:",
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(lambda c: c.data and c.data.startswith("sp:"))
async def search_page_callback(cb: CallbackQuery):
    """Handle pagination navigation (◀️/▶️) in search results."""
    _, _, payload = cb.data.partition(":")  # "sp:{msg_id}:{page}" or "sp:noop"
    if payload == "noop":
        await cb.answer()
        return

    parts = payload.split(":")
    if len(parts) != 2:
        await cb.answer(t(cb.from_user.id, "error"), show_alert=True)
        return

    try:
        msg_id = int(parts[0])
        page = int(parts[1])
    except ValueError:
        await cb.answer(t(cb.from_user.id, "error"), show_alert=True)
        return

    results = _SEARCH_RESULTS.get(msg_id)
    if not results:
        await cb.answer(t(cb.from_user.id, "search_stale"), show_alert=True)
        return

    kb = _build_search_kb(results, page, msg_id)
    try:
        await cb.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    await cb.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("t:"))
async def search_callback(cb: CallbackQuery, bot: Bot):
    """Handle search result button click — download the chosen track."""
    track_id = cb.data[2:]  # strip "t:"
    if not track_id:
        await cb.answer(t(cb.from_user.id, "error"), show_alert=True)
        return

    # Acknowledge the button press
    await cb.answer()

    msg = cb.message
    user_id = cb.from_user.id
    user_tasks.setdefault(user_id, [])
    user_messages.setdefault(user_id, [])

    # Delete the search results message
    try:
        await msg.delete()
    except Exception:
        pass

    # Resolve and download the track
    url = f"https://tidal.com/track/{track_id}"
    task = asyncio.create_task(handle_url(msg, bot, url, user_id))
    track_task(user_id, task)
    task.add_done_callback(lambda t, uid=user_id: untrack_task(uid, t))


# ------------------------------------------------------------------------------
# Main handler
# ------------------------------------------------------------------------------
# Universal URL regex — match any HTTP/HTTPS URL, then classify via sources module.
_URL_RE = r"https?://\S+"


@router.message(F.chat.type.in_({ChatType.SUPERGROUP, ChatType.GROUP, ChatType.CHANNEL, ChatType.PRIVATE}))
async def main(msg: Message, bot: Bot):
    if not msg.audio:
        try:
            await msg.delete()
        except Exception:
            pass

    if not msg.text:
        return

    # Find all URLs in the message text
    urls = re.findall(_URL_RE, msg.text)
    if not urls:
        # No URLs found → treat as search query
        # Skip short messages and commands
        text = msg.text.strip()
        if len(text) >= 2 and not text.startswith("/"):
            user_id = msg.from_user.id
            user_tasks.setdefault(user_id, [])
            user_messages.setdefault(user_id, [])
            task = asyncio.create_task(_search_and_show(msg, bot, text, user_id))
            track_task(user_id, task)
            task.add_done_callback(lambda t, uid=user_id: untrack_task(uid, t))
        return

    # Try each URL — classify_url will filter out non-music URLs
    for url in urls:
        classified = classify_url(url)
        if not classified:
            continue

        print(f"{msg.from_user.id} (@{msg.from_user.username}) requested {url} [{classified[0]}/{classified[1]}]")

        user_id = msg.from_user.id
        user_tasks.setdefault(user_id, [])
        user_messages.setdefault(user_id, [])

        task = asyncio.create_task(handle_url(msg, bot, url, user_id))
        track_task(user_id, task)
        task.add_done_callback(lambda t, uid=user_id: untrack_task(uid, t))
