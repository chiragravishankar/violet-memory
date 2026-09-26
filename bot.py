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
import events
import feeds
import ingest
import onboarding
import schedule
import selfmodel
import voice

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# Reference list shown word for word under /help (not a reply; the reply is voiced).
COMMANDS = (
    "/week  your next 7 days\n"
    "/events  best NYU events this week (/events all, /events refresh)\n"
    "/undo  remove what your last file added\n"
    "/me  what I know about you (/me setup, /me edit, /me reflect)\n"
    "/forget <text>  make me forget something\n"
    "/demo_time 2026-09-29 13:30  preview reminders at a moment"
)
FILES_DIR = Path(__file__).resolve().parent / "data" / "files"

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


def recent_replies(user_id, n: int = 5) -> list[str]:
    rows = _db.execute(
        "SELECT text FROM messages WHERE user_id = ? AND direction = 'out' ORDER BY id DESC LIMIT ?", (int(user_id), n)
    ).fetchall()
    return [r[0] for r in reversed(rows) if r[0]]


async def reply(update: Update, text: str) -> None:
    """Send text that is already in Violet's voice."""
    await update.effective_message.reply_text(text)
    log_message("out", update, text)


async def speak(update: Update, draft: str, kind: str = "info", user_message: str | None = None,
                attach: str | None = None) -> None:
    """Every reply goes through the voice layer: the draft says what to get across."""
    user_id = update.effective_user.id
    text = await voice.say(user_id, draft, kind=kind, user_message=user_message, attach=attach,
                           recent=recent_replies(user_id))
    await reply(update, text)


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
    await speak(update, "First hello. You're Violet: he can text you anything to keep (people he meets, "
                        "events, ideas, deadlines), ask you anything about it, and send files like a syllabus. "
                        "Two short sentences.", kind="info")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await speak(update, "Quick how-to: text you anything to keep it, ask anything in plain words, send a PDF, "
                        "Word file or photo (a caption like 'OM syllabus' helps). Commands are listed below.",
                kind="info", attach=COMMANDS)


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    now = datetime.now(schedule.TZ)
    lines = schedule.agenda_lines(user_id, now.date(), now.date() + timedelta(days=6), now)
    if not lines:
        await speak(update, "Nothing on his calendar for the next 7 days.", kind="answer", user_message="/week")
        return
    await speak(update, "His next 7 days are below. Lead in with a few words, pointing out what stands out "
                        "(an exam, a busy day) if anything.", kind="answer", user_message="/week", attach="\n".join(lines))


async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    undone = schedule.undo_last_file(update.effective_user.id)
    if undone is None:
        await speak(update, "Nothing from a file to undo.", kind="confirmation", user_message="/undo")
        return
    label, count = undone
    await speak(update, f"Removed the {count} calendar item{'s' if count != 1 else ''} that came from {label}.",
                kind="confirmation", user_message="/undo")


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
        draft, attach, finished = await _with_typing(update, context, onboarding.handle(user_id, text))
        await speak(update, draft, kind="question", user_message=text, attach=attach)
        if finished:
            context.application.create_task(sync_profile(user_id))
        return
    m = selfmodel.meta(user_id)
    if m.get("awaiting_edit"):
        selfmodel.set_meta(user_id, awaiting_edit=False)
        set_classification(rows, "profile_edit")
        await speak(update, await _with_typing(update, context, _apply_edit(user_id, text)),
                    kind="confirmation", user_message=text)
        return
    if m.get("awaiting_answer") and selfmodel.next_question(user_id):
        set_classification(rows, "reflection_answer")
        await speak(update, await _reflection_reply(update, context, user_id, text), kind="confirmation", user_message=text)
        return

    action = events.parse_action(text)
    if action and action[0] and events.last_list(user_id):
        set_classification(rows, f"event_{action[0]}")
        await _event_reply(update, context, user_id, text, *action)
        return

    recent = recent_replies(user_id)
    c = await assistant.classify(text, user_id=user_id, recent=recent)
    label = c.type + ("+schedule" if c.about_schedule else "") + ("+about_user" if c.about_user else "")
    log.info("classified %r as %s", text[:80], label)
    set_classification(rows, label)

    if c.type == assistant.CORRECTION:
        answer = await _with_typing(update, context, assistant.correct(user_id, text, last_reply(user_id), recent))
        await reply(update, answer)
        context.application.create_task(brain.connect(user_id), update=update)
        context.application.create_task(sync_profile(user_id))
        return

    # Learn lasting facts in the background; never slows the reply.
    context.application.create_task(_learn(user_id, text))

    if c.type == assistant.QUESTION and assistant.is_event_question(text):
        await reply(update, await _with_typing(update, context, assistant.answer_events(user_id, text, recent=recent)))
        return
    if c.type == assistant.QUESTION:
        answer = await _with_typing(
            update, context, assistant.answer(user_id, text, c.about_schedule, about_user=c.about_user, recent=recent)
        )
        await reply(update, answer)
        return

    # Reply first; the save runs in the background. cognee.add waits for any graph build
    # already running on this dataset, which could hold the reply up for many seconds.
    context.application.create_task(_remember(user_id, text))

    if c.type == assistant.SCHEDULE:
        try:
            draft = await _with_typing(update, context, ingest.process_chat(user_id, text))
        except Exception:
            log.exception("chat schedule extraction failed")
            draft = None
        if draft:
            await speak(update, draft.text, kind=draft.kind, user_message=text)
        else:
            await speak(update, "Kept it, but there was no clear date or time to put on his calendar. "
                                "Ask when, if it matters.", kind="save", user_message=text)
        return
    if c.ack:
        await reply(update, c.ack)  # written in the same call as the classification
    else:
        await speak(update, "Kept it. Acknowledge the specific thing.", kind="save", user_message=text)


async def _event_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, text: str,
                       action: str, ref: str) -> None:
    chosen, cands = events.resolve(user_id, ref)
    if not chosen:
        names = "; ".join(f"{n}. {e.title} ({events.when(e)})" for n, e in enumerate(cands, 1))
        await speak(update, f"Not sure which event he means. Ask which one, by name or number: {names}",
                    kind="question", user_message=text)
        return
    drafts = [await events.act(user_id, action, e) for e in chosen]
    if action == "going":
        context.application.create_task(brain.connect(user_id))
    await speak(update, " ".join(drafts), kind="confirmation", user_message=text)


async def events_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/events: top 5 in the next 7 days. /events all: everything relevant this week by day.
    /events refresh: fetch now."""
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    sub = context.args[0].lower() if context.args else ""
    said = inbound_text(update)
    now = datetime.now(schedule.TZ)
    if sub == "refresh":
        report = await _with_typing(update, context, events.refresh(now))
        scored = await _with_typing(update, context, events.score_new(user_id, now))
        got = "; ".join(f"{k}: {v.get('fetched', 0)} upcoming, {v.get('new', 0)} new" if "error" not in v
                        else f"{k}: failed ({v['error']})" for k, v in report.items())
        await speak(update, f"Refreshed the event feeds. {got}. Scored {scored} new ones for him.",
                    kind="confirmation", user_message=said)
        return
    top = events.matches(user_id, now, now + timedelta(days=7))
    if sub == "all":
        if not top:
            await speak(update, "No relevant NYU events in the next 7 days.", kind="answer", user_message=said)
            return
        by_day: dict = {}
        for s in sorted(top, key=lambda s: s.event.start):
            by_day.setdefault(schedule.fmt_day(s.event.start.date()), []).append(s)
        lines = []
        for day, items in by_day.items():
            lines.append(day)
            for s in items:
                t = "all day" if s.event.all_day else schedule.fmt_time(s.event.start.time())
                lines.append(f"  {t} {s.event.title}, {events.where(s.event)}\n  {s.event.url}")
        events.remember_list(user_id, [s.event.id for s in top])
        await speak(update, f"All {len(top)} relevant NYU events in the next 7 days are below, by day. Short lead-in.",
                    kind="answer", user_message=said, attach="\n".join(lines))
        return
    top = top[:5]
    if not top:
        await speak(update, "No strong NYU event matches in the next 7 days.", kind="answer", user_message=said)
        return
    events.remember_list(user_id, [s.event.id for s in top])
    await speak(update, "His top NYU event matches for the next 7 days are below, best first. Short lead-in; "
                        "mention he can reply 'going', 'not for me', 'more like this' or 'remind me later'.",
                kind="answer", user_message=said, attach="\n\n".join(events.list_line(s) for s in top))


async def _remember(user_id, text: str) -> None:
    """Save a message to memory (retry once), then connect it into the graph."""
    for attempt in (1, 2):
        try:
            await brain.save(user_id, text)
            break
        except Exception as e:
            log.warning("save failed (attempt %d, %s)", attempt, type(e).__name__)
            if attempt == 2:
                return
            await asyncio.sleep(5)
    await brain.connect(user_id)


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
        return ("Couldn't tell what to change. Give an example like: /me edit I'm in the MS MOT program, not MBA.")
    asyncio.get_running_loop().create_task(sync_profile(user_id))
    return "Changed: " + "; ".join(changes)


async def _reflection_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, text: str) -> str:
    q = selfmodel.next_question(user_id)
    if q is None:
        selfmodel.set_meta(user_id, awaiting_answer=False)
        return "Thanks for answering."
    outcome, fact = selfmodel.answer_question(user_id, text)
    if outcome == "confirmed":
        head = "He confirmed it; you'll treat it as a fact now."
    elif outcome == "deleted":
        head = "He said no; you dropped it."
    else:
        changes = await _with_typing(update, context, selfmodel.edit(user_id, f'About "{q["question"]}": {text}'))
        head = ("Updated: " + "; ".join(changes)) if changes else "Took his answer on board."
    context.application.create_task(sync_profile(user_id))
    nxt = selfmodel.next_question(user_id)
    if nxt:
        return f"{head} Then ask exactly this: {nxt['question']}"
    selfmodel.set_meta(user_id, awaiting_answer=False)
    return head


async def me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/me, /me setup, /me edit <text>, /me reflect"""
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    sub = (context.args[0].lower() if context.args else "")
    rest = " ".join(context.args[1:]).strip()
    said = inbound_text(update)
    if sub == "setup":
        draft, attach = onboarding.start(user_id)
        await speak(update, draft, kind="question", user_message=said, attach=attach)
    elif sub == "edit":
        if rest:
            await speak(update, await _with_typing(update, context, _apply_edit(user_id, rest)),
                        kind="confirmation", user_message=said)
        else:
            selfmodel.set_meta(user_id, awaiting_edit=True)
            await speak(update, "Ask what he wants changed about him, with an example like: my career goal is "
                                "product management, not consulting.", kind="question", user_message=said)
    elif sub == "reflect":
        draft, attach = await _with_typing(update, context, run_reflection(user_id))
        await speak(update, draft, kind="question", user_message=said, attach=attach)
    else:
        shown = selfmodel.format_profile(user_id)
        if not shown:
            await speak(update, "You don't know much about him yet. Suggest /me setup for a few quick questions.",
                        kind="info", user_message=said)
        else:
            await speak(update, "Here's everything you know about him (below). Lead in with a few words; mention "
                                "/me edit to fix anything.", kind="info", user_message=said,
                        attach=shown.split("\n", 2)[-1].strip())


async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    text = " ".join(context.args).strip()
    if not text:
        await speak(update, "Ask what to forget, with an example: /forget Sam", kind="question", user_message="/forget")
        return
    removed = selfmodel.forget_facts(user_id, text)
    memories = await _with_typing(update, context, brain.forget_text(user_id, text))
    context.application.create_task(sync_profile(user_id))
    if not removed and not memories:
        await speak(update, f'Found nothing about "{text}" to forget.', kind="confirmation", user_message=f"/forget {text}")
        return
    what = [f["text"] for f in removed]
    extra = f" Also dropped {memories} message{'s' if memories != 1 else ''} he'd sent that mentioned it." if memories else ""
    await speak(update, f'Forgot what you knew about "{text}": {"; ".join(what) or "nothing else"}.{extra}',
                kind="confirmation", user_message=f"/forget {text}")


def _recent_messages(user_id, days: int = 7) -> list[str]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = _db.execute(
        "SELECT text, classification FROM messages WHERE user_id = ? AND direction = 'in' AND ts >= ? "
        "AND text NOT LIKE '/%' ORDER BY id",
        (int(user_id), since),
    ).fetchall()
    return [f"[{c or 'message'}] {t}" for t, c in rows if t]


async def run_reflection(user_id) -> tuple[str, str | None]:
    """Review the week, update patterns, and start asking up to 2 confirmation questions.
    Returns a draft for the voice layer and the patterns to show below it."""
    today = datetime.now(schedule.TZ).date()
    agenda = "\n".join(schedule.agenda_lines(user_id, today - timedelta(days=7), today + timedelta(days=7)))
    questions = await selfmodel.reflect(user_id, _recent_messages(user_id), agenda)
    asyncio.get_running_loop().create_task(sync_profile(user_id))
    patterns = [f["text"] for f in selfmodel.get_profile(user_id)["patterns"]][-4:]
    draft = "Weekly check-in: you looked back at his week."
    draft += " Patterns you noticed are listed below." if patterns else " Nothing new stood out."
    if questions:
        selfmodel.set_meta(user_id, awaiting_answer=True)
        draft += f" Then ask exactly this: {questions[0]['question']}"
    return draft, "\n".join(patterns) or None


async def demo_time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/demo_time 2026-09-29 13:30: show the reminders that would fire at that moment."""
    log_message("in", update, inbound_text(update))
    try:
        at = datetime.strptime(" ".join(context.args[:2]), "%Y-%m-%d %H:%M").replace(tzinfo=schedule.TZ)
    except ValueError:
        await speak(update, "Show the format: /demo_time 2026-09-29 13:30", kind="info", user_message=inbound_text(update))
        return
    await reply(update, await _with_typing(update, context, demo_time(update.effective_user.id, at)))


async def demo_time(user_id, at: datetime) -> str:
    head = f"🕒 Pretending it's {schedule.fmt_day(at.date())}, {schedule.fmt_time(at.time())}"
    due = schedule.preview_reminders(user_id, at)
    parts = [head]
    if at.time() == events.DELIVERY_TIME:
        picks = events.picks_for_day(user_id, at.date(), demo=True)
        if picks:
            msg = await assistant.picks_message(user_id, picks, at, recent_replies(user_id))
            events.record_picks(user_id, at.date(), picks, msg, demo=True)
            parts.append(msg)
    if not due:
        if len(parts) == 1:
            parts.append(await voice.say(user_id, "Nothing would go out at that moment: no reminders and no event "
                                                  "picks that day.", kind="info"))
        return "\n\n".join(parts)
    head = "\n\n".join(parts)
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
            await speak(update, "Can't read old .doc files. Ask him to save it as .docx and resend.", kind="error",
                        user_message=inbound_text(update))
            return
        if not docs.is_supported(file_name):
            await speak(update, "Can only read PDF, Word (.docx), .txt, .md and photos for now.", kind="error",
                        user_message=inbound_text(update))
            return
    else:
        tg_file = msg.photo[-1]  # largest size
        size = tg_file.file_size
        file_name = f"photo_{msg.date:%Y%m%d_%H%M%S}.jpg"

    if size and size > docs.MAX_BYTES:
        await speak(update, "That file is over 20 MB, more than Telegram lets you download. Ask for a smaller version.",
                    kind="error", user_message=inbound_text(update))
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
        await speak(update, "Couldn't download that file. Ask him to try again.", kind="error",
                    user_message=inbound_text(update))
        return

    caption = (msg.caption or "").strip() or None
    try:
        draft = await _with_typing(update, context, ingest.process_file(user_id, path, file_name, caption))
    except Exception:
        log.exception("file processing failed")
        await speak(update, "Couldn't read that file. Ask him to try again.", kind="error", user_message=inbound_text(update))
        return
    await speak(update, draft.text, kind=draft.kind, user_message=inbound_text(update), attach=draft.attach)
    context.application.create_task(brain.connect(user_id), update=update)


async def on_other(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await speak(update, "You can only read text, files and photos for now, not this kind of message.", kind="error",
                user_message=inbound_text(update))


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
                text = await assistant.reminder_message(user_id, item, lead, now, recent_replies(user_id))
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
                    draft, attach = await run_reflection(user_id)
                    text = await voice.say(user_id, draft, kind="question", attach=attach, recent=recent_replies(user_id))
                    await app.bot.send_message(chat_id=int(user_id), text=text)
                    log_raw("out", int(user_id), int(user_id), text)
                except Exception as e:
                    log.warning("weekly reflection failed (%s)", type(e).__name__)


def event_users() -> list[str]:
    ids = {feeds.calendar_user()} | {u for u in selfmodel.all_user_ids() if u.isdigit()}
    return sorted(u for u in ids if u)


async def events_loop(app: Application) -> None:
    """Fetch event sources every 6 hours (and score what's new). At 9:00 AM New York, send each
    user one message with up to 3 picks, unless it's their quiet hours (then it waits)."""
    last_fetch = None
    while True:
        now = datetime.now(schedule.TZ)
        try:
            if last_fetch is None or now - last_fetch >= events.eventsources.FETCH_EVERY:
                last_fetch = now
                await events.refresh(now)
                for user_id in event_users():
                    await events.score_new(user_id, now)
            if events.DELIVERY_TIME <= now.time() < events.DELIVERY_UNTIL:
                for user_id in event_users():
                    if events.picked_today(user_id, now.date()) or selfmodel.quiet_until(user_id, now):
                        continue
                    picks = events.picks_for_day(user_id, now.date())
                    msg = await assistant.picks_message(user_id, picks, now, recent_replies(user_id)) if picks else None
                    events.record_picks(user_id, now.date(), picks, msg)
                    if msg:
                        events.remember_list(user_id, [p.event.id for p in picks])
                        await app.bot.send_message(chat_id=int(user_id), text=msg)
                        log_raw("out", int(user_id), int(user_id), msg)
        except Exception:
            log.exception("events loop error")
        await asyncio.sleep(60)


async def post_init(app: Application) -> None:
    schedule.plan_all_reminders()
    app.create_task(send_reminders(app))
    app.create_task(sync_feeds(app))
    app.create_task(profile_loop(app))
    app.create_task(events_loop(app))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await speak(update, "Something went wrong on your side. Ask him to try again.", kind="error")
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
    app.add_handler(CommandHandler("events", events_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, on_other))
    app.add_error_handler(on_error)

    log.info("Violet Memory is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
