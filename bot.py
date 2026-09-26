"""Violet Memory: Telegram channel. All Telegram logic lives here; memory is in brain.py."""

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import assistant
import brain  # loads .env
import docs
import feeds
import ingest
import onboarding
import schedule
import selfmodel

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
    "/week shows your next 7 days.\n/undo removes the deadlines from your last file.\n"
    "/demo_time 2026-09-29 13:30 shows the reminders that would fire at that time.\n\n"
    "/me shows what I know about you. /me setup to tell me about yourself, /me edit to fix "
    "something, /me reflect for a quick check-in. /forget <text> removes something."
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
if "classification" not in {r[1] for r in _db.execute("PRAGMA table_info(messages)")}:
    _db.execute("ALTER TABLE messages ADD COLUMN classification TEXT")  # question, schedule, memory
_db.commit()


def log_message(direction: str, update: Update, text: str | None, classification: str | None = None) -> int:
    user = update.effective_user
    chat = update.effective_chat
    return log_raw(direction, user.id if user else None, chat.id if chat else None, text, classification)


def log_raw(direction: str, user_id, chat_id, text: str | None, classification: str | None = None) -> int:
    cur = _db.execute(
        "INSERT INTO messages (ts, direction, user_id, chat_id, text, classification) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), direction, user_id, chat_id, text, classification),
    )
    _db.commit()
    return cur.lastrowid


def set_classification(row_ids: list[int], label: str) -> None:
    _db.executemany("UPDATE messages SET classification = ? WHERE id = ?", [(label, r) for r in row_ids])
    _db.commit()


def last_reply(user_id) -> str | None:
    row = _db.execute(
        "SELECT text FROM messages WHERE user_id = ? AND direction = 'out' ORDER BY id DESC LIMIT 1", (int(user_id),)
    ).fetchone()
    return row[0] if row else None


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


# Telegram splits long pastes into several messages. Messages from one user that arrive
# within this many seconds of each other are joined and handled as one.
BUFFER_SECONDS = 3.0
_buffers: dict[int, dict] = {}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.effective_message.text
    user_id = update.effective_user.id
    row = log_message("in", update, text)
    await update.effective_chat.send_action(ChatAction.TYPING)
    buf = _buffers.setdefault(user_id, {"parts": [], "rows": []})
    buf["parts"].append(text)
    buf["rows"].append(row)
    buf["update"] = update
    if buf.get("task"):
        buf["task"].cancel()  # still waiting: restart the quiet window
    buf["task"] = context.application.create_task(_flush_later(user_id, context), update=update)


async def _flush_later(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await asyncio.sleep(BUFFER_SECONDS)
    except asyncio.CancelledError:
        return
    buf = _buffers.pop(user_id, None)  # no await between the sleep and here: nothing can slip in
    if not buf:
        return
    text = "\n".join(p.strip() for p in buf["parts"]).strip()
    if len(buf["parts"]) > 1:
        log.info("joined %d messages (%d chars) from %s", len(buf["parts"]), len(text), user_id)
    await handle_text(buf["update"], context, text, buf["rows"])


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, rows: list[int]) -> None:
    user_id = update.effective_user.id
    await update.effective_chat.send_action(ChatAction.TYPING)

    # Conversations in progress about the profile take the message first.
    if onboarding.is_active(user_id):
        set_classification(rows, "onboarding")
        answer, finished = await _with_typing(update, context, onboarding.handle(user_id, text))
        await reply(update, answer)
        if finished:
            context.application.create_task(sync_profile(user_id))
        return
    m = selfmodel.meta(user_id)
    if m.get("awaiting_edit"):
        selfmodel.set_meta(user_id, awaiting_edit=False)
        set_classification(rows, "profile_edit")
        await reply(update, await _with_typing(update, context, _apply_edit(user_id, text)))
        return
    if m.get("awaiting_answer") and selfmodel.next_question(user_id):
        set_classification(rows, "reflection_answer")
        await reply(update, await _reflection_reply(update, context, user_id, text))
        return

    c = await assistant.classify(text)
    label = c.type + ("+schedule" if c.about_schedule else "") + ("+about_user" if c.about_user else "")
    log.info("classified %r as %s", text[:80], label)
    set_classification(rows, label)

    if c.type == assistant.CORRECTION:
        answer = await _with_typing(update, context, assistant.correct(user_id, text, last_reply(user_id)))
        await reply(update, answer)
        context.application.create_task(brain.connect(user_id), update=update)
        context.application.create_task(sync_profile(user_id))
        return

    # Learn lasting facts in the background; never slows the reply.
    context.application.create_task(_learn(user_id, text))

    if c.type == assistant.QUESTION:
        answer = await _with_typing(
            update, context, assistant.answer(user_id, text, c.about_schedule, about_user=c.about_user)
        )
        await reply(update, answer)
        return

    try:
        await brain.save(user_id, text)
    except Exception:
        log.exception("save failed")
        await reply(update, "Sorry, I couldn't save that. Please try again 💜")
        return
    context.application.create_task(brain.connect(user_id), update=update)

    if c.type == assistant.SCHEDULE:
        try:
            note = await _with_typing(update, context, ingest.process_chat(user_id, text))
        except Exception:
            log.exception("chat schedule extraction failed")
            note = None
        await reply(update, note or SAVED)
        return
    await reply(update, SAVED)


async def _learn(user_id, text: str) -> None:
    try:
        await selfmodel.learn(user_id, text)
    except Exception as e:
        log.warning("learn failed (%s)", type(e).__name__)


async def sync_profile(user_id) -> None:
    """Write the profile into Cognee as the core "me" document and connect it."""
    try:
        if await selfmodel.sync_to_memory(user_id):
            await brain.connect(user_id)
    except Exception as e:
        log.warning("profile sync failed (%s)", type(e).__name__)


async def _apply_edit(user_id, instruction: str) -> str:
    changes = await selfmodel.edit(user_id, instruction)
    if not changes:
        return "I couldn't tell what to change. Try something like: /me edit I'm in the MS MOT program, not MBA 💜"
    asyncio.get_running_loop().create_task(sync_profile(user_id))
    return "Done 💜\n" + "\n".join(changes)


async def _reflection_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, text: str) -> str:
    q = selfmodel.next_question(user_id)
    if q is None:
        selfmodel.set_meta(user_id, awaiting_answer=False)
        return "Thanks 💜"
    outcome, fact = selfmodel.answer_question(user_id, text)
    if outcome == "confirmed":
        head = "Thanks, noted 💜"
    elif outcome == "deleted":
        head = "Got it, I've dropped that 💜"
    else:
        changes = await _with_typing(update, context, selfmodel.edit(user_id, f'About "{q["question"]}": {text}'))
        head = ("Thanks, updated 💜\n" + "\n".join(changes)) if changes else "Thanks 💜"
    context.application.create_task(sync_profile(user_id))
    nxt = selfmodel.next_question(user_id)
    if nxt:
        return f"{head}\n\n{nxt['question']}"
    selfmodel.set_meta(user_id, awaiting_answer=False)
    return head


async def me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/me, /me setup, /me edit <text>, /me reflect"""
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    sub = (context.args[0].lower() if context.args else "")
    rest = " ".join(context.args[1:]).strip()
    if sub == "setup":
        await reply(update, onboarding.start(user_id))
    elif sub == "edit":
        if rest:
            await reply(update, await _with_typing(update, context, _apply_edit(user_id, rest)))
        else:
            selfmodel.set_meta(user_id, awaiting_edit=True)
            await reply(update, "What should I change? For example: my career goal is product management, not consulting.")
    elif sub == "reflect":
        await reply(update, await _with_typing(update, context, run_reflection(user_id)))
    else:
        shown = selfmodel.format_profile(user_id)
        await reply(update, shown or "I don't know much about you yet. Send /me setup and I'll ask a few quick questions 💜")


async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    text = " ".join(context.args).strip()
    if not text:
        await reply(update, "Tell me what to forget, like: /forget Sam")
        return
    removed = selfmodel.forget_facts(user_id, text)
    memories = await _with_typing(update, context, brain.forget_text(user_id, text))
    context.application.create_task(sync_profile(user_id))
    if not removed and not memories:
        await reply(update, f"I couldn't find anything about \"{text}\" to forget 💜")
        return
    parts = []
    if removed:
        parts.append(f"{len(removed)} profile fact{'s' if len(removed) != 1 else ''}")
    if memories:
        parts.append(f"{memories} saved message{'s' if memories != 1 else ''}")
    await reply(update, f"Forgotten: {' and '.join(parts)} about \"{text}\" 💜")


def _recent_messages(user_id, days: int = 7) -> list[str]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = _db.execute(
        "SELECT text, classification FROM messages WHERE user_id = ? AND direction = 'in' AND ts >= ? "
        "AND text NOT LIKE '/%' ORDER BY id",
        (int(user_id), since),
    ).fetchall()
    return [f"[{c or 'message'}] {t}" for t, c in rows if t]


async def run_reflection(user_id) -> str:
    """Review the week, update patterns, and start asking up to 2 confirmation questions."""
    today = datetime.now(schedule.TZ).date()
    agenda = "\n".join(schedule.agenda_lines(user_id, today - timedelta(days=7), today + timedelta(days=7)))
    questions = await selfmodel.reflect(user_id, _recent_messages(user_id), agenda)
    asyncio.get_running_loop().create_task(sync_profile(user_id))
    patterns = [f for f in selfmodel.get_profile(user_id)["patterns"]]
    head = "🪞 Weekly check-in done."
    if patterns:
        head += " Patterns I see:\n" + "\n".join(f"• {f['text']}" for f in patterns[-4:])
    if questions:
        selfmodel.set_meta(user_id, awaiting_answer=True)
        return f"{head}\n\nQuick question: {questions[0]['question']}"
    return head


async def demo_time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/demo_time 2026-09-29 13:30: show the reminders that would fire at that moment."""
    log_message("in", update, inbound_text(update))
    try:
        at = datetime.strptime(" ".join(context.args[:2]), "%Y-%m-%d %H:%M").replace(tzinfo=schedule.TZ)
    except ValueError:
        await reply(update, "Try it like this: /demo_time 2026-09-29 13:30")
        return
    await reply(update, await _with_typing(update, context, demo_time(update.effective_user.id, at)))


async def demo_time(user_id, at: datetime) -> str:
    head = f"🕒 Pretending it's {schedule.fmt_day(at.date())}, {schedule.fmt_time(at.time())}"
    due = schedule.preview_reminders(user_id, at)
    if not due:
        return head + "\n\nNo reminders at that time 💜"
    texts = [await assistant.reminder_message(user_id, it, lead, at) for it, lead in due]
    return head + "\n\n" + "\n\n".join(texts)


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
    """Every 30 seconds, send reminders that are due. Private chat id == user id.
    Each reminder is marked sent before sending, so restarts never repeat one."""
    while True:
        try:
            schedule.ensure_class_reminders()
            now = datetime.now(schedule.TZ)
            for user_id, item, lead in schedule.pop_due_reminders(now, quiet_until=selfmodel.quiet_until):
                text = await assistant.reminder_message(user_id, item, lead, now)
                await app.bot.send_message(chat_id=int(user_id), text=text)
                log_raw("out", int(user_id), int(user_id), text)
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(30)


async def sync_feeds(app: Application) -> None:
    """Sync Brightspace and Google Calendar now and every 3 hours. Never logs feed URLs."""
    while True:
        results = await feeds.sync_all()
        if any(not isinstance(r, str) and r.added for r in results.values()):
            app.create_task(brain.connect(feeds.calendar_user()))
        await asyncio.sleep(feeds.SYNC_EVERY.total_seconds())


async def profile_loop(app: Application) -> None:
    """Every 15 minutes: write changed profiles to Cognee. Sunday from 6 PM (New York):
    the weekly reflection, once per user, with its first question sent as a message."""
    while True:
        await asyncio.sleep(15 * 60)
        for user_id in selfmodel.all_user_ids():
            await sync_profile(user_id)
        now = datetime.now(schedule.TZ)
        if now.weekday() == 6 and now.hour >= 18:
            for user_id in selfmodel.all_user_ids():
                if not user_id.isdigit() or selfmodel.meta(user_id).get("last_reflection") == now.date().isoformat():
                    continue
                try:
                    text = await run_reflection(user_id)
                    await app.bot.send_message(chat_id=int(user_id), text=text)
                    log_raw("out", int(user_id), int(user_id), text)
                except Exception as e:
                    log.warning("weekly reflection failed (%s)", type(e).__name__)


async def post_init(app: Application) -> None:
    schedule.plan_all_reminders()
    app.create_task(send_reminders(app))
    app.create_task(sync_feeds(app))
    app.create_task(profile_loop(app))


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
    app.add_handler(CommandHandler("demo_time", demo_time_cmd))
    app.add_handler(CommandHandler("me", me_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, on_other))
    app.add_error_handler(on_error)

    log.info("Violet Memory is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
