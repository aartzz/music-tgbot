import asyncio
import os
import re
from typing import Optional, Dict, List, Any

from aiogram import Router, Bot, F
from aiogram.enums import ChatAction, ChatType
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile, BufferedInputFile, LinkPreviewOptions
from aiogram.exceptions import TelegramBadRequest

from db.db import Music, Analytics
from modules.downloader import (
    yt_extract,
    make_ydl_opts,
    build_paths,
    rename_with_collision_avoidance,
)
from modules.processing import process_audio, cleanup_file
from modules.utils import safe_edit_text, remove_duplicate_artists
from modules.progress import animate_ellipsis, animate_download_progress, animate_countdown, animate_starting, format_download_text

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

# Single dictionary for progress tracking
download_progress: Dict[str, float] = {}

YOUTUBE_REGEX = (
    r"(?:https?://)?(?:www\.)?(?:m\.)?"
    r"(?:youtube\.com|youtu\.be)/(?:watch\?v=|embed/|v/|playlist\?list=|)"
    r"([\w-]{11}|list=[\w-]{34})(?:\S+)?"
)

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
async def send_cached_audio(msg: Message, bot: Bot, vid_id: str, file_id: str, progress_msg: Message) -> bool:
    try:
        await progress_msg.delete()
    except Exception:
        pass
    try:
        await bot.send_audio(chat_id=msg.chat.id, audio=file_id, disable_notification=True)
        return True
    except Exception:
        db.remove_data(vid_id)
        return False


async def send_processed_audio(
    bot: Bot,
    msg: Message,
    vid_id: str,
    audio_path: str,
    title: str,
    artist: str,
    thumb_data: bytes,
) -> str:
    sent = await bot.send_audio(
        chat_id=msg.chat.id,
        audio=FSInputFile(audio_path),
        title=title,
        performer=artist,
        thumbnail=BufferedInputFile(thumb_data, filename=f"{vid_id}_thumb.jpg"),
        disable_notification=True,
    )
    db.add_data(vid_id, sent.audio.file_id)
    return sent.audio.file_id

# ------------------------------------------------------------------------------
# Video processing pipeline
# ------------------------------------------------------------------------------
async def process_single_video(
    msg: Message,
    bot: Bot,
    original_url: str,
    info: Dict[str, Any],
    progress_msg: Message,
    user_id: int
):
    vid_id = info.get("id")
    title = info.get("title", "<unknown>")
    artist = remove_duplicate_artists(info.get("artist", info.get("uploader", "<unknown>")))
    thumb_url = info.get("thumbnail")

    cached_id = db.get_file_id(vid_id)
    if cached_id:
        sent = await send_cached_audio(msg, bot, vid_id, cached_id, progress_msg)
        if sent:
            return

    temp_path, final_path = build_paths(vid_id, title)
    download_progress[vid_id] = 0.0

    await safe_edit_text(
        progress_msg,
        f"<blockquote>{original_url}</blockquote>\n⬇️ скачивание...",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        parse_mode="HTML",
    )

    anim_task = asyncio.create_task(
        animate_download_progress(
            progress_msg, original_url, vid_id, bot, download_progress, is_playlist=("list=" in original_url)
        )
    )
    track_task(user_id, anim_task)

    try:
        ydl_opts = make_ydl_opts(vid_id, download_progress)
        await yt_extract(info.get("webpage_url", original_url), ydl_opts, download=True)

        if not (os.path.exists(temp_path) and thumb_url):
            raise RuntimeError(f"404 - видео '{title}' не существует")

        try:
            final_path = rename_with_collision_avoidance(temp_path, final_path)
        except Exception:
            final_path = temp_path

        anim_task.cancel()
        await progress_msg.edit_text(
            f"<blockquote>{original_url}</blockquote>\n✴️ обработка...",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode="HTML",
        )
        anim_task = asyncio.create_task(animate_ellipsis(progress_msg, original_url, "✴️ обработка", "", bot, ChatAction.UPLOAD_PHOTO))
        track_task(user_id, anim_task)

        thumb_data = await process_audio(final_path, title, artist, thumb_url)

        anim_task.cancel()
        await progress_msg.edit_text(
            f"<blockquote>{original_url}</blockquote>\n❇️ отправка...",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            parse_mode="HTML",
        )
        anim_task = asyncio.create_task(animate_ellipsis(progress_msg, original_url, "❇️ отправка", "", bot, ChatAction.UPLOAD_VOICE))
        track_task(user_id, anim_task)

        await bot.send_chat_action(chat_id=msg.chat.id, action=ChatAction.UPLOAD_VOICE)
        await send_processed_audio(bot, msg, vid_id, final_path, title, artist, thumb_data)

        if not anim_task.done():
            anim_task.cancel()

    except asyncio.CancelledError:
        anim_task.cancel()
        cleanup_file(temp_path)
        cleanup_file(final_path)
        raise
    except Exception as e:
        anim_task.cancel()
        err_txt = f"⛔️ ошибка обработки\n{e}"
        try:
            await progress_msg.edit_text(
                f"<blockquote>{original_url}</blockquote>\n{err_txt} <i>15</i>\n```\n{e}```",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )
        except Exception:
            progress_msg = await msg.answer(
                f"<blockquote>{original_url}</blockquote>\n{err_txt} <i>15</i>\n```\n{e}```",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )
            track_message(user_id, progress_msg)
        await animate_countdown(progress_msg, err_txt, 15, original_url, str(e))
    finally:
        download_progress.pop(vid_id, None)
        cleanup_file(temp_path)
        cleanup_file(final_path)
        try:
            anim_task.cancel()
        except Exception:
            pass

# ------------------------------------------------------------------------------
# Entry point for URLs
# ------------------------------------------------------------------------------
async def handle_url(msg: Message, bot: Bot, original_url: str, user_id: int):
    is_playlist = "list=" in original_url or "/playlist" in original_url
    progress_msg = await msg.answer(
        f"<blockquote>{original_url}</blockquote>\n🛜 достаю {'плейлист' if is_playlist else 'видео'}...",
        link_preview_options=LinkPreviewOptions(is_disabled=True),
        parse_mode="HTML",
    )
    track_message(user_id, progress_msg)

    db_analytics.add_user(msg.from_user.id)
    db_analytics.increment_use_count()

    start_anim = asyncio.create_task(animate_starting(progress_msg, original_url, bot, is_playlist))
    track_task(user_id, start_anim)

    async with download_semaphore:
        try:
            info = await yt_extract(original_url, make_ydl_opts(), download=False)
        except Exception:
            start_anim.cancel()
            err_txt = "⛔️ не удалось получить информацию о видео"
            await animate_countdown(progress_msg, err_txt, 15, original_url)
            return

        start_anim.cancel()

        # Playlist
        if info.get("_type") == "playlist":
            entries = info.get("entries", []) or []
            if not entries:
                await animate_countdown(progress_msg, "⛔️ плейлист пуст", 15, original_url)
                return

            total = len(entries)
            await safe_edit_text(
                progress_msg,
                f"<blockquote>{original_url}</blockquote>\n📋 скачиваем плейлист <i>(0/{total})</i>",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )

            for idx, entry in enumerate(entries, start=1):
                if asyncio.current_task().cancelled():
                    raise asyncio.CancelledError()
                if not entry or not entry.get("webpage_url"):
                    continue

                await safe_edit_text(
                    progress_msg,
                    f"<blockquote>{original_url}</blockquote>\n📋 скачиваем плейлист <i>({idx}/{total} - отменить /cancel)</i>",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    parse_mode="HTML",
                )

                video_msg = await msg.answer(
                    f"<blockquote>{entry['webpage_url']}</blockquote>\n⬇️ скачивание...",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    parse_mode="HTML",
                )
                track_message(user_id, video_msg)
                await process_single_video(msg, bot, entry["webpage_url"], entry, video_msg, user_id)

            try:
                await progress_msg.delete()
            except Exception:
                pass
            done = await msg.answer("✅ готово, плейлист полностью скачан <i>15</i>", parse_mode="HTML")
            await animate_countdown(done, "✅ готово, плейлист полностью скачан", 15)

        else:
            await process_single_video(msg, bot, original_url, info, progress_msg, user_id)
            try:
                await progress_msg.delete()
            except Exception:
                pass

# ------------------------------------------------------------------------------
# Commands
# ------------------------------------------------------------------------------
@router.message(Command(commands=["start"]))
async def start(msg: Message):
    await msg.answer(
        """<b><u>lostya's youtube music downloader</u></b>
этот бот сделан специально для @lostyawolfer но ты им тоже можешь пользоваться

<b>РАБОТАЕТ ТОЛЬКО ЮТУБ!</b>

<b><i>КАК ПОЛЬЗОВАТЬСЯ:</i></b>
<blockquote>- скинь ссылку на видео или плейлист ютуб
- бот скачаeт звук, добавит превью, исполнителя и название
- можно отправлять несколько ссылок
- /cancel отменяет все загрузки
- сообщения прогресса анимированы
- бот автоматически очищает командные сообщения
- используется кеш, повторные загрузки мгновенны!</blockquote>""",
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
        m = await msg.answer("✅ нечего отменять <i>5</i>", parse_mode="HTML")
        await animate_countdown(m, "✅ нечего отменять", 5)
        return
    await cancel_user_tasks_and_messages(user_id)
    m = await msg.answer("✅ отменено! <i>5</i>", parse_mode="HTML")
    await animate_countdown(m, "✅ отменено!", 5)

# ------------------------------------------------------------------------------
# Main handler
# ------------------------------------------------------------------------------
@router.message(F.chat.type.in_({ChatType.SUPERGROUP, ChatType.GROUP, ChatType.CHANNEL, ChatType.PRIVATE}))
async def main(msg: Message, bot: Bot):
    if not msg.audio:
        try:
            await msg.delete()
        except Exception:
            pass

    if not msg.text:
        return

    print(f"{msg.from_user.id} (@{msg.from_user.username}) requested {msg.text}")
    match = re.search(YOUTUBE_REGEX, msg.text)
    if not match:
        return

    user_id = msg.from_user.id
    user_tasks.setdefault(user_id, [])
    user_messages.setdefault(user_id, [])

    url = match.group(0)
    task = asyncio.create_task(handle_url(msg, bot, url, user_id))
    track_task(user_id, task)
    task.add_done_callback(lambda t: untrack_task(user_id, t))