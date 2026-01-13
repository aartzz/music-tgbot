import asyncio
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import LinkPreviewOptions
from modules.utils import safe_edit_text

# ------------------------------------------------------------------------------
# Download progress formatting
# ------------------------------------------------------------------------------
def format_download_text(percentage: float, animation: str) -> str:
    """Format download text with underlined progress"""
    word = "скачивание"
    underline_len = int((percentage / 100.0) * len(word))

    if underline_len == 0:
        return f"⬇️ {word}{animation}"
    elif underline_len >= len(word):
        return f"⬇️ <u>{word}</u>{animation}"
    else:
        return f"⬇️ <u>{word[:underline_len]}</u>{word[underline_len:]}{animation}"

# ------------------------------------------------------------------------------
# Countdown messages
# ------------------------------------------------------------------------------
# ------------------------------------------------------------------------------
# Countdown messages
# ------------------------------------------------------------------------------
async def animate_countdown(
    message,
    info: str,
    seconds: int = 15,
    original_url: str | None = None,
    error_details: str | None = None,
):
    """
    Countdown animation before deleting a message.

    - Countdown appears right after the main line.
    - If detailed error text is provided, it is shown below as a code block.
    """
    await asyncio.sleep(1)
    for i in range(seconds - 1, 0, -1):
        try:
            # Build the visible message body
            if original_url:
                text = f"<blockquote>{original_url}</blockquote>\n"

                # Add main info and countdown on same line
                text += f"{info} <i>{i}</i>"

                # If detailed error text exists, put it below in a code block
                if error_details:
                    text += f"\n<pre><code>{error_details}</code></pre>"
            else:
                text = f"{info} <i>{i}</i>"
                if error_details:
                    text += f"\n<pre><code>{error_details}</code></pre>"

            await message.edit_text(
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )

            await asyncio.sleep(1)
        except Exception:
            break

    try:
        await message.delete()
    except Exception:
        pass

# ------------------------------------------------------------------------------
# Simple ellipsis animation (e.g. for 'обработка...', 'отправка...')
# ------------------------------------------------------------------------------
async def animate_ellipsis(progress_msg, original_url: str, prefix: str, suffix: str, bot, action: ChatAction):
    """Animated ellipsis ('...', '..', etc.) for processing/sending states."""
    animations = [".", "..", "..."]
    count = 1
    while True:
        try:
            await bot.send_chat_action(chat_id=progress_msg.chat.id, action=action)
            dots = animations[count % len(animations)]
            text = f"<blockquote>{original_url}</blockquote>\n{prefix}{dots}{suffix}"
            try:
                await safe_edit_text(
                    progress_msg,
                    text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    parse_mode="HTML",
                )
            except TelegramBadRequest as e:
                if "too many requests" in str(e).lower() or "retry after" in str(e).lower():
                    pass
                else:
                    raise
            count += 1
            await asyncio.sleep(1)
        except Exception:
            break

# ------------------------------------------------------------------------------
# Starting animation (fetching video/playlist info)
# ------------------------------------------------------------------------------
async def animate_starting(progress_msg, original_url: str, bot, is_playlist: bool = False):
    """Display 'fetching video/playlist' animation."""
    animations = [".", "..", "..."]
    count = 0
    search_text = "🛜 достаю плейлист" if is_playlist else "🛜 достаю видео"

    while True:
        try:
            await bot.send_chat_action(chat_id=progress_msg.chat.id, action=ChatAction.CHOOSE_STICKER)
            dots = animations[count % len(animations)]
            if count < 15:
                text = f"<blockquote>{original_url}</blockquote>\n{search_text}{dots}"
            else:
                wait_text = "⏳ больше видео = дольше обработка" if is_playlist else "⏳ терпение..."
                text = (
                    f"<blockquote>{original_url}</blockquote>\n"
                    f"{wait_text} <i>(прошло {count}с | отменить /cancel)</i>"
                )
            await safe_edit_text(
                progress_msg,
                text,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                parse_mode="HTML",
            )
            count += 1
            await asyncio.sleep(1)
        except Exception:
            break

# ------------------------------------------------------------------------------
# Download progress animation itself
# ------------------------------------------------------------------------------
async def animate_download_progress(progress_msg, original_url: str, video_id: str, bot, download_progress: dict, is_playlist: bool = False):
    """Continuously update download progress with animation."""
    animations = [".", "..", "..."]
    i = -1
    last_switch = 0.0
    update_interval = 0.5 if not is_playlist else 1
    ellipsis_interval = 1.0
    next_update = asyncio.get_event_loop().time()

    try:
        while True:
            await bot.send_chat_action(chat_id=progress_msg.chat.id, action=ChatAction.RECORD_VIDEO)

            now = asyncio.get_event_loop().time()
            if now - last_switch >= ellipsis_interval:
                i = (i + 1) % len(animations)
                last_switch = now

            percentage = download_progress.get(video_id, 0.0)
            text = f"<blockquote>{original_url}</blockquote>\n" \
                   f"{format_download_text(percentage, animations[i])}"

            try:
                await safe_edit_text(
                    progress_msg,
                    text,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    parse_mode="HTML",
                )
            except TelegramBadRequest as e:
                if "too many requests" in str(e).lower() or "retry after" in str(e).lower():
                    pass  # Let animation freeze, continue after
                else:
                    raise

            next_update += update_interval
            await asyncio.sleep(max(0.0, next_update - asyncio.get_event_loop().time()))
    except asyncio.CancelledError:
        pass
    except Exception:
        pass