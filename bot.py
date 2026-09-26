"""Violet Memory: Telegram channel. All Telegram logic lives here; memory is in brain.py."""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import brain  # loads .env
import docs
import gcal
import ingest
import schedule

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

WELCOME = (
    "Hi, I'm Violet Memory 💜 Text me anything to remember it: people you meet, "
    "events, ideas, deadlines. Ask anything ending with ? and I'll recall it."
)
HELP = (
    WELCOME
    + "\n\nSend me a PDF, Word file, text file or photo (like a syllabus or a class "
    "schedule screenshot) and I'll save it and add its deadlines. Add a caption like "
    "\"OM syllabus\" so I know what it is.\n\n"
    "/week shows your next 7 days.\n/undo removes the deadlines from your last file."
)
FILE_TYPES = "I can read PDF, Word (.docx), .txt, .md and photos for now 💜"
TOO_BIG = "That file is over 20 MB, which is more than Telegram lets me download. Could you send a smaller version? 💜"
FILES_DIR = Path(__file__).resolve().parent / "data" / "files"
SAVED = "💜 Saved"
TEXT_ONLY = "I can only read text for now 💜"

# ---------- SQLite message log ----------

DB_PATH = Path(__file__).resolve().parent / "data" / "messages.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.execute(
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        direction TEXT NOT NULL,   -- 'in' or 'out'
        user_id INTEGER,
        chat_id INTEGER,
        text TEXT
    )"""
)
_db.commit()


def log_message(direction: str, update: Update, text: str | None) -> None:
    user = update.effective_user
    chat = update.effective_chat
    log_raw(direction, user.id if user else None, chat.id if chat else None, text)


def log_raw(direction: str, user_id, chat_id, text: str | None) -> None:
    _db.execute(
        "INSERT INTO messages (ts, direction, user_id, chat_id, text) VALUES (?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            direction,
            user_id,
            chat_id,
            text,
        ),
    )
    _db.commit()


async def reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text)
    log_message("out", update, text)


def inbound_text(update: Update) -> str:
    msg = update.effective_message
    if msg.document or msg.photo:
        name = msg.document.file_name if msg.document else "photo"
        return f"<{_kind(msg)}: {name}>" + (f" {msg.caption}" if msg.caption else "")
    return msg.text or msg.caption or f"<{_kind(msg)}>"


def _kind(msg) -> str:
    for attr in ("photo", "voice", "audio", "video", "document", "sticker", "location", "contact"):
        if getattr(msg, attr, None):
            return attr
    return "non-text"


# ---------- Handlers ----------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await reply(update, WELCOME)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await reply(update, HELP)


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await reply(update, schedule.week_view(update.effective_user.id))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    undone = schedule.undo_last_file(update.effective_user.id)
    if undone is None:
        await reply(update, "There's nothing from a file to undo 💜")
        return
    label, count = undone
    await reply(update, f"Removed {count} item{'s' if count != 1 else ''} added from {label} 💜")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text.strip()
    user_id = update.effective_user.id
    log_message("in", update, text)

    if text.endswith("?"):
        await update.effective_chat.send_action(ChatAction.TYPING)
        answer = await _with_typing(update, context, brain.recall(user_id, text))
        await reply(update, answer)
        return

    try:
        await brain.save(user_id, text)
    except Exception:
        log.exception("save failed")
        await reply(update, "Sorry, I couldn't save that. Please try again 💜")
        return
    await reply(update, SAVED)
    context.application.create_task(brain.connect(user_id), update=update)
    context.application.create_task(_chat_schedule(update, context, user_id, text), update=update)


async def _chat_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, text: str) -> None:
    """Pick deadlines or class times out of a saved chat message, in the background."""
    try:
        note = await ingest.process_chat(user_id, text)
    except Exception:
        log.exception("chat schedule extraction failed")
        return
    if note:
        await reply(update, note)


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user_id = update.effective_user.id
    log_message("in", update, inbound_text(update))

    if msg.document:
        tg_file, size = msg.document, msg.document.file_size
        file_name = msg.document.file_name or "file"
        ext = Path(file_name).suffix.lower()
        if not ext and (msg.document.mime_type or "").startswith("image/"):
            file_name += "." + msg.document.mime_type.split("/")[1]
        if ext == ".doc":
            await reply(update, "I can't read old .doc files. Could you save it as .docx and send it again? 💜")
            return
        if not docs.is_supported(file_name):
            await reply(update, FILE_TYPES)
            return
    else:
        tg_file = msg.photo[-1]  # largest size
        size = tg_file.file_size
        file_name = f"photo_{msg.date:%Y%m%d_%H%M%S}.jpg"

    if size and size > docs.MAX_BYTES:
        await reply(update, TOO_BIG)
        return

    await update.effective_chat.send_action(ChatAction.TYPING)
    user_dir = FILES_DIR / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"{msg.date:%Y%m%d_%H%M%S}_{Path(file_name).name}"
    try:
        f = await tg_file.get_file()
        await f.download_to_drive(path)
    except Exception:
        log.exception("download failed")
        await reply(update, "Sorry, I couldn't download that file. Please try again 💜")
        return

    caption = (msg.caption or "").strip() or None
    try:
        summary = await _with_typing(update, context, ingest.process_file(user_id, path, file_name, caption))
    except Exception:
        log.exception("file processing failed")
        await reply(update, "Sorry, I couldn't read that file. Please try again 💜")
        return
    await reply(update, summary)
    context.application.create_task(brain.connect(user_id), update=update)


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await reply(update, TEXT_ONLY)


async def _with_typing(update: Update, context: ContextTypes.DEFAULT_TYPE, coro):
    """Run coro while keeping Telegram's typing indicator alive (it expires after ~5s)."""
    task = asyncio.ensure_future(coro)
    while not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=4)
        except asyncio.TimeoutError:
            await update.effective_chat.send_action(ChatAction.TYPING)
    return task.result()


async def send_reminders(app: Application) -> None:
    """Every 30 seconds, send reminders that are due. Private chat id == user id."""
    while True:
        try:
            for user_id, text in schedule.pop_due_reminders():
                await app.bot.send_message(chat_id=int(user_id), text=text)
                log_raw("out", int(user_id), int(user_id), text)
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(30)


async def sync_google(app: Application) -> None:
    """Sync the Google Calendar feed now and every 3 hours. Never logs the feed URL."""
    while True:
        cfg = gcal.configured()
        if cfg:
            url, user_id = cfg
            try:
                result = await gcal.sync(user_id, url)
                if result.added:
                    app.create_task(brain.connect(user_id))
            except gcal.FeedError as e:
                log.warning("google sync failed: %s", e)
            except Exception as e:
                log.warning("google sync failed: %s", type(e).__name__)
        await asyncio.sleep(gcal.SYNC_EVERY.total_seconds())


async def post_init(app: Application) -> None:
    app.create_task(send_reminders(app))
    app.create_task(sync_google(app))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await reply(update, "Oops, something went wrong. Please try again 💜")
        except Exception:
            pass


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Put it in .env")

    app = Application.builder().token(token).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, on_other))
    app.add_error_handler(on_error)

    log.info("Violet Memory is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
