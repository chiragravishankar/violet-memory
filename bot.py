"""Violet Memory: Telegram channel. All Telegram logic lives here; memory is in brain.py."""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import contextvars
import secrets
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import actions
import agent
import assistant
import brain  # loads .env
import brief
import docs
import events
import feeds
import gaccount
import inbox
import ingest
import onboarding
import schedule
import selfmodel
import speech
import transcribe
import views
import voice
import fmt
from fmt import View

logging.basicConfig(format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# Reference list shown word for word under /help (not a reply; the reply is voiced).
COMMANDS = (
    "/today  today at a glance\n"
    "/week  your next 7 days\n"
    "/brief  your morning brief (/brief tomorrow, /brief week, /brief off, /brief on, /brief time 7:30)\n"
    "/events  best events this week, NYU and Luma (/events all, /events refresh, /events add_luma <url>)\n"
    "/undo  remove what your last file added\n"
    "/me  what I know about you (/me setup, /me edit, /me reflect)\n"
    "/forget <text>  make me forget something\n"
    "/connect_google  let me add calendar events and write emails (always with your OK)\n"
    "/actions  what I've done or offered to do\n"
    "/inbox  email that needs you (/email alerts on|off, /email quiet)\n"
    "/voice mirror|on|off  voice replies: when you send voice (default), always, or never\n"
    "/settings calendar auto|ask  add calendar events without asking, or ask first\n"
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


_db.execute("""CREATE TABLE IF NOT EXISTS voice_transcripts (
    token TEXT PRIMARY KEY, user_id INTEGER, text TEXT NOT NULL, created_at TEXT NOT NULL)""")
_db.commit()

# Set while a voice note is being handled: the first reply gets the 🎙 quote on top and a
# [📝 Full transcript] button, whichever path (answer, save, view) produces it.
_voice_note: contextvars.ContextVar[dict | None] = contextvars.ContextVar("voice_note", default=None)


# Set while handling a message whose reply should be spoken (per the user's /voice mode). The first
# reply that goes out takes it: a voice note for conversation, a short spoken summary after a view.
_speak: contextvars.ContextVar[dict | None] = contextvars.ContextVar("speak", default=None)
VOICE_TIMINGS: list[float] = []


def _take_speak() -> dict | None:
    sp = _speak.get()
    if sp is None or sp.get("done"):
        return None
    sp["done"] = True
    return sp


def _store_text(user_id, text: str) -> str:
    token = uuid.uuid4().hex[:12]
    _db.execute("INSERT INTO voice_transcripts (token, user_id, text, created_at) VALUES (?, ?, ?, ?)",
                (token, int(user_id), text, datetime.now(timezone.utc).isoformat()))
    _db.commit()
    return token


async def _voice_reply(send_voice, send_text, chat_id: int, user_id, text: str, structured: bool, note: dict | None,
                       extra: list[tuple[str, str]] | None = None, also_text: bool = False) -> bool:
    """Speak a reply. Returns False (nothing sent) if speech fails or is too slow: the caller sends text."""
    try:
        path, spoken, cut, took = await speech.make(user_id, text, structured)
    except Exception as e:
        log.info("voice reply fell back to text (%s)", type(e).__name__)
        return False
    VOICE_TIMINGS.append(took)
    buttons = [] if structured else [[InlineKeyboardButton("📝 Show text", callback_data=f"tx:{_store_text(user_id, text)}")]]
    if extra:
        buttons.append([InlineKeyboardButton(label, callback_data=action[:64]) for label, action in extra])
    if note is not None:
        buttons = buttons or [[]]
        buttons[0].append(InlineKeyboardButton("📝 Full transcript", callback_data=f"tr:{note['token']}"))
    try:
        with open(path, "rb") as f:
            msg = await send_voice(voice=f, caption=note["quote"] if note else None, parse_mode=ParseMode.HTML if note else None,
                                   reply_markup=InlineKeyboardMarkup(buttons) if buttons and buttons[0] else None)
        _track(chat_id, msg)
    except Exception as e:
        log.info("sending voice failed (%s)", type(e).__name__)
        return False
    finally:
        path.unlink(missing_ok=True)  # temp audio never kept
    log_raw("out", chat_id, chat_id, f"[voice] {spoken}")
    if (cut or also_text) and not structured:
        await send_text(text)  # the rest (or the links) is in the chat
    return True


def _take_voice_note() -> dict | None:
    note = _voice_note.get()
    if note is not None:
        _voice_note.set(None)
    return note


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


_last_out: dict[int, int] = {}  # chat id -> id of the last message the bot sent there


def _track(chat_id: int, msg) -> None:
    if msg is not None and getattr(msg, "message_id", None):
        _last_out[chat_id] = msg.message_id


async def reply(update: Update, text: str) -> None:
    """Send text that is already in Violet's voice (or speak it, if this reply should be spoken)."""
    sp = _take_speak()
    if sp is not None:
        await update.effective_chat.send_action(ChatAction.RECORD_VOICE)
        note = _voice_note.get()

        async def text_too(t):
            _track(update.effective_chat.id, await update.effective_message.reply_text(t))

        if await _voice_reply(update.effective_message.reply_voice, text_too, update.effective_chat.id,
                              update.effective_user.id, text, False, note):
            _take_voice_note()
            log_message("out", update, text)
            return
    note = _take_voice_note()
    if note is None:
        _track(update.effective_chat.id, await update.effective_message.reply_text(text))
    else:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("📝 Full transcript", callback_data=f"tr:{note['token']}")]])
        try:
            _track(update.effective_chat.id, await update.effective_message.reply_text(
                f"{note['quote']}\n\n{fmt.esc(text)}", parse_mode=ParseMode.HTML, reply_markup=markup,
                disable_web_page_preview=True))
        except BadRequest:
            _track(update.effective_chat.id, await update.effective_message.reply_text(
                f"{fmt.to_plain(note['quote'])}\n\n{text}", reply_markup=markup))
    log_message("out", update, text)


_more_pages: dict[str, list[str]] = {}


def _markup(v: View) -> InlineKeyboardMarkup | None:
    rows = [[InlineKeyboardButton(label, callback_data=action[:64]) for label, action in row] for row in v.buttons]
    if v.more:
        token = secrets.token_hex(4)
        _more_pages[token] = v.more
        rows.append([InlineKeyboardButton("Show more", callback_data=f"more:{token}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def send_view(bot, chat_id: int, v: View, edit_message=None, speak: bool = True):
    """Send (or edit) a structured view as HTML; if Telegram rejects the formatting, resend plain.
    If this reply should be spoken, a short spoken summary follows the view (never for action cards)."""
    sp = _take_speak() if speak and edit_message is None else None
    msg = await _send_view(bot, chat_id, v, edit_message)
    if sp is not None:
        await bot.send_chat_action(chat_id, ChatAction.RECORD_VOICE)

        async def no_text(t):
            return None

        await _voice_reply(lambda **k: bot.send_voice(chat_id, **k), no_text, chat_id, chat_id, v.plain, True, None)
    return msg


async def _send_view(bot, chat_id: int, v: View, edit_message=None):
    note = _take_voice_note() if edit_message is None else None
    if note is not None:
        v = View(f"{note['quote']}\n\n{v.html}", v.buttons + [[("📝 Full transcript", f"tr:{note['token']}")]], v.more)
    markup = _markup(v)
    try:
        if edit_message is not None:
            await edit_message.edit_text(v.html, parse_mode=ParseMode.HTML, reply_markup=markup,
                                         disable_web_page_preview=True)
        else:
            msg = await bot.send_message(chat_id, v.html, parse_mode=ParseMode.HTML, reply_markup=markup,
                                         disable_web_page_preview=True)
            _track(chat_id, msg)
            log_raw("out", chat_id, chat_id, v.plain)
            return msg
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        log.warning("HTML send failed (%s), sending plain text", e)
        msg = await bot.send_message(chat_id, v.plain, reply_markup=markup, disable_web_page_preview=True)
        _track(chat_id, msg)
        log_raw("out", chat_id, chat_id, v.plain)
        return msg
    log_raw("out", chat_id, chat_id, v.plain)
    return None


# ---------- Actions: cards, approvals, undo ----------


async def offer_card(bot, chat_id: int, user_id, action_id: str) -> None:
    """Show an action card, or run it straight away for a calendar add in "calendar auto" mode."""
    a = actions.get(action_id)
    if a is None:
        return
    if str(user_id) != str(feeds.calendar_user()):
        return  # the Google account is the owner's: nobody else gets cards that would act on it
    if a.type == "calendar_add" and actions.calendar_mode(user_id) == "auto" and gaccount.connected():
        await _send_outcome(bot, chat_id, a.id, await actions.approve(user_id, a.id, "do", "setting"))
        return
    msg = await send_view(bot, chat_id, actions.card(a), speak=False)  # cards are never voiced
    if msg is not None:
        actions.set_card_message(a.id, msg.message_id)


async def _send_outcome(bot, chat_id: int, action_id: str, o) -> None:
    text = fmt.esc(o.text) + (f" {fmt.link(o.link, 'Open')}" if o.link else "")
    buttons = [[("↩️ Undo", f"act:undo:{action_id}")]] if o.undo else []
    await send_view(bot, chat_id, View(text, buttons), speak=False)
    if o.sending:
        asyncio.get_running_loop().create_task(_send_after_delay(bot, chat_id, action_id))


async def _send_after_delay(bot, chat_id: int, action_id: str) -> None:
    """The Undo window: the email leaves only if nobody tapped Undo in the meantime."""
    await asyncio.sleep(actions.SEND_DELAY)
    o = await actions.finish_send(action_id)
    if o is not None:
        await _send_outcome(bot, chat_id, action_id, o)


async def act_on_card(bot, chat_id: int, user_id, action_id: str, op: str, via: str, card_message=None) -> None:
    """His decision on a card: do / send / draft / cancel / undo."""
    if op == "undo":
        o = await actions.undo(user_id, action_id)
        await _send_outcome(bot, chat_id, action_id, o)
        return
    o = await actions.approve(user_id, action_id, op, via)
    if card_message is not None and op in ("do", "send", "draft", "cancel"):
        a = actions.get(action_id)
        if a and a.status not in ("pending",):
            try:
                await card_message.edit_reply_markup(reply_markup=None)  # the card is decided: no more buttons
            except Exception:
                pass
    await _send_outcome(bot, chat_id, action_id, o)


_YES = re.compile(r"^\s*(yes|yeah|yep|yup|ok|okay|sure|do it|go ahead|add it|please do|sounds good)\b", re.I)
_SEND = re.compile(r"\b(send it|send now|yes,? send|go ahead and send|send)\b", re.I)
_DRAFT = re.compile(r"\b(draft|save it|save as)\b", re.I)
_NO = re.compile(r"^\s*(no|nope|cancel|don'?t|stop|never ?mind|nah)\b", re.I)


def _card_being_answered(update: Update, user_id):
    """The pending card this text answers: a Telegram reply to the card, or the card being the last
    thing sent in the chat. Anything else is not an approval."""
    replied = getattr(update.effective_message, "reply_to_message", None)
    target = replied.message_id if replied is not None else _last_out.get(update.effective_chat.id)
    for a in actions.pending_for(user_id):
        if a.card_message_id and a.card_message_id == target:
            return a
    return None


async def reply_any(update: Update, context: ContextTypes.DEFAULT_TYPE, answer) -> None:
    """Send a reply that may be plain text or a structured view."""
    if isinstance(answer, View):
        await send_view(context.bot, update.effective_chat.id, answer)
    else:
        await reply(update, answer)


async def speak(update: Update, draft: str, kind: str = "info", user_message: str | None = None,
                attach: str | None = None) -> None:
    """Every reply goes through the voice layer: the draft says what to get across."""
    user_id = update.effective_user.id
    text = await voice.say(user_id, draft, kind=kind, user_message=user_message, attach=attach,
                           recent=recent_replies(user_id))
    await reply(update, text)


_FEED_URL = re.compile(r"https?://\S*(?:ics|ical)\S*", re.I)


def inbound_text(update: Update) -> str:
    """What gets logged for an inbound message. Calendar feed URLs are secrets: redacted."""
    return _FEED_URL.sub("<feed url>", _inbound_text(update))


def _inbound_text(update: Update) -> str:
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
    await speak(update, "First hello. You're Violet: he can text or voice-note you anything to keep (people he "
                        "meets, events, ideas, deadlines), ask you anything about it, and send files like a syllabus. "
                        "Two short sentences.", kind="info")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await speak(update, "Quick how-to: text or send a voice note (up to 5 minutes) with anything to keep, ask "
                        "anything in plain words, send a PDF, "
                        "Word file or photo (a caption like 'OM syllabus' helps). Commands are listed below.",
                kind="info", attach=COMMANDS)


async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await send_view(context.bot, update.effective_chat.id, views.week_view(update.effective_user.id))


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await send_view(context.bot, update.effective_chat.id, views.today_view(update.effective_user.id))


async def brief_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/brief (now), /brief tomorrow, /brief week, /brief off, /brief on, /brief time 7:30"""
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    said = inbound_text(update)
    sub = context.args[0].lower() if context.args else "now"
    now = datetime.now(schedule.TZ)
    if sub in ("off", "on"):
        brief.set_settings(user_id, enabled=sub == "on")
        await speak(update, f"Morning brief turned {sub}.", kind="confirmation", user_message=said)
    elif sub == "time":
        t = brief.parse_time(" ".join(context.args[1:]))
        if not t:
            await speak(update, "Show the format: /brief time 7:30", kind="error", user_message=said)
            return
        brief.set_settings(user_id, time=t.strftime("%H:%M"))
        await speak(update, f"Morning brief moved to {schedule.fmt_time(t)} New York time (Sundays stay the "
                            "7 PM weekly preview).", kind="confirmation", user_message=said)
    elif sub == "week":
        await send_view(context.bot, update.effective_chat.id, await _with_typing(update, context, brief.weekly(user_id, now)))
    elif sub == "tomorrow":
        v = await _with_typing(update, context, brief.daily(user_id, now.date() + timedelta(days=1), now))
        await send_view(context.bot, update.effective_chat.id, v)
    else:
        v = await _with_typing(update, context, brief.daily(user_id, now.date(), now))
        await _deliver_brief(context.bot, user_id, now.date(), v)


async def _deliver_brief(bot, user_id, day, v: View) -> None:
    """Send a daily brief, record it (never twice a day), and mark its event pick suggested."""
    await send_view(bot, int(user_id), v)
    brief.mark_sent(user_id, day, "daily", v.plain)
    pick = getattr(v, "pick", None)
    if pick is not None:
        # Suggested once only: the 9 AM picks won't repeat it.
        events.mark_suggested(user_id, pick.event.id)
        events.remember_list(user_id, [pick.event.id])


async def connect_google_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run Google's sign-in in the browser on this computer and save the token."""
    log_message("in", update, inbound_text(update))
    if not gaccount.CLIENT_FILE.exists():
        await reply(update, "credentials.json is missing from the project, so I can't start the Google login.")
        return
    await reply(update, "Opening Google sign-in in your browser on this computer. Allow Calendar and Gmail, then come back here.")
    try:
        email = await gaccount.login()
    except Exception as e:
        log.warning("google login failed (%s)", type(e).__name__)
        await reply(update, "Couldn't finish the Google login. If Google said \"access blocked\", use a personal Gmail "
                            "that's added as a test user on the app's consent screen, then send /connect_google again.")
        return
    await reply(update, f"Connected to Google as {email}. I'll always show you a card before adding events or sending anything.")


async def inbox_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await send_view(context.bot, update.effective_chat.id, inbox.inbox_view(update.effective_user.id))


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/voice mirror|on|off"""
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    arg = (context.args[0].lower() if context.args else "")
    if arg in speech.MODES:
        speech.set_mode(user_id, arg)
        await reply(update, {"mirror": "Voice notes get a voice reply, texts get text.",
                             "on": "I'll answer everything with a voice note (lists still come as text too).",
                             "off": "Text only from now on."}[arg])
    else:
        await reply(update, f"Voice replies: {speech.mode(user_id)}. Use /voice mirror, /voice on or /voice off.")


async def email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/email alerts on|off, /email quiet (brief only)"""
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    args = [a.lower() for a in context.args]
    if args[:1] == ["quiet"]:
        inbox.set_alerts(user_id, "quiet")
        await reply(update, "Okay, no instant email alerts. I'll keep important mail for your morning brief.")
    elif len(args) == 2 and args[0] == "alerts" and args[1] in ("on", "off"):
        inbox.set_alerts(user_id, args[1])
        await reply(update, "Email alerts on. Only the important ones, at most 5 a day." if args[1] == "on"
                    else "Email alerts off. /inbox still shows what needs you.")
    else:
        await reply(update, f"Email alerts: {inbox.settings(user_id)['alerts']}. Use /email alerts on, /email alerts off or /email quiet.")


async def actions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log_message("in", update, inbound_text(update))
    await send_view(context.bot, update.effective_chat.id, actions.log_view(update.effective_user.id))


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/settings calendar auto|ask"""
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    args = [a.lower() for a in context.args]
    if len(args) == 2 and args[0] == "calendar" and args[1] in ("auto", "ask"):
        actions.set_calendar_mode(user_id, args[1])
        await reply(update, "Got it. I'll add low-risk calendar events without asking (they still show up in /actions)."
                    if args[1] == "auto" else "Got it. I'll ask before adding anything to your calendar.")
        return
    mode = actions.calendar_mode(user_id)
    await reply(update, f"Calendar adds: {mode}. Change it with /settings calendar auto or /settings calendar ask.")


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
    brain.touch("user is messaging")  # start waking hosted memory while we work on the reply
    text = update.effective_message.text
    user_id = update.effective_user.id
    row = log_message("in", update, _FEED_URL.sub("<feed url>", text))
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


_SAY_IT = re.compile(r"^\s*(say (that|it) out loud|read (that|it) (out|to me)|send (that|it) as (a )?voice( note)?|voice (that|it))\b", re.I)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, rows: list[int]) -> None:
    user_id = update.effective_user.id
    await update.effective_chat.send_action(ChatAction.TYPING)
    was_voice = _voice_note.get() is not None
    _speak.set({"done": False} if speech.wants_voice(user_id, was_voice) else None)
    if _SAY_IT.match(text):
        last = last_reply(user_id)
        set_classification(rows, "say_it")
        if not last:
            await reply(update, "There's nothing to read out yet.")
            return
        _speak.set({"done": False})
        await reply(update, re.sub(r"^\[voice\]\s*", "", last))
        return

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

    # A card being edited, or a reply to the card that was just shown.
    editing = selfmodel.meta(user_id).get("awaiting_action_edit")
    if editing:
        selfmodel.set_meta(user_id, awaiting_action_edit=None)
        set_classification(rows, "action_edit")
        try:
            a = await _with_typing(update, context, actions.edit(user_id, editing, text))
        except actions.Refused as r:
            await reply(update, str(r))
            return
        await speak(update, "Updated the card. Here it is again.", kind="confirmation", user_message=text)
        await offer_card(context.bot, update.effective_chat.id, user_id, a.id)
        return
    card = _card_being_answered(update, user_id)
    if card is not None:
        op = None
        if _NO.match(text):
            op = "cancel"
        elif card.type == "calendar_add" and (_YES.match(text) or _SEND.search(text)):
            op = "do"
        elif card.type == "email" and _DRAFT.search(text):
            op = "draft"
        elif card.type == "email" and _SEND.search(text):
            op = "send"
        elif card.type == "email" and _YES.match(text):
            await reply(update, "Send it now, or save it as a draft? (Reply \"send\" or \"draft\", or tap a button.)")
            _track(update.effective_chat.id, None)
            _last_out[update.effective_chat.id] = card.card_message_id  # keep the card as the one being answered
            return
        if op:
            set_classification(rows, f"action_{op}")
            await act_on_card(context.bot, update.effective_chat.id, user_id, card.id, op, "text")
            return

    # "I am Chirag Ravishankar": set the name in code, no model involved.
    m_name = assistant._NAME.match(text)
    if m_name and assistant._looks_like_name(m_name.group(1)):
        set_classification(rows, "correction")
        answer = await _with_typing(update, context, assistant.correct(user_id, text, last_reply(user_id), recent_replies(user_id)))
        agent.remember_turn(user_id, "user", text)
        agent.remember_turn(user_id, "assistant", answer)
        await reply(update, answer)
        context.application.create_task(brain.connect(user_id), update=update)
        context.application.create_task(sync_profile(user_id))
        return

    # Everything else: the conversational agent decides what he wants and which tools to use.
    set_classification(rows, "agent")
    try:
        r = await _with_typing(update, context, agent.respond(user_id, text))
    except Exception:
        log.exception("agent failed")
        await reply(update, "Sorry, my head's a bit foggy right now. Try me again in a moment?")
        return
    set_classification(rows, "agent:" + (",".join(dict.fromkeys(r.tools)) or "chat"))
    await _send_agent_reply(update, context, r)
    if r.connect:
        context.application.create_task(brain.connect(user_id), update=update)
    # Learn lasting facts about him in the background; never slows the reply.
    context.application.create_task(_learn(user_id, text))


async def _send_agent_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, r) -> None:
    """Text, then any formatted views (the text becomes the first view's lead-in), buttons last."""
    chat_id = update.effective_chat.id
    buttons = [list(r.buttons)] if r.buttons else []
    if r.views:
        for n, v in enumerate(r.views):
            html = v.html
            if n == 0 and r.text:
                html = f"{fmt.esc(r.text)}\n\n{html}"
            last = n == len(r.views) - 1
            await send_view(context.bot, chat_id, View(html, v.buttons + (buttons if last else []), v.more))
    elif buttons or _URL.search(r.text or ""):
        # A spoken reply keeps its buttons; links also go out as text (you can't tap a voice note).
        if _speak.get() and not _speak.get().get("done") and r.text:
            _take_speak()
            await update.effective_chat.send_action(ChatAction.RECORD_VOICE)

            async def linked_text(t):
                await send_view(context.bot, chat_id, View(_linkify(t)), speak=False)

            if await _voice_reply(update.effective_message.reply_voice, linked_text, chat_id, update.effective_user.id,
                                  r.text, False, _voice_note.get(), extra=list(r.buttons), also_text=bool(_URL.search(r.text))):
                _take_voice_note()
                log_message("out", update, r.text)
                for action_id in r.cards:
                    await offer_card(context.bot, chat_id, update.effective_user.id, action_id)
                return
        # Raw links become tappable text; buttons need an HTML message anyway.
        await send_view(context.bot, chat_id, View(_linkify(r.text or "Here you go."), buttons))
    else:
        await reply(update, r.text or "Hmm, I lost my train of thought there. Say that again?")
    for action_id in r.cards:
        await offer_card(context.bot, chat_id, update.effective_user.id, action_id)


_URL = re.compile(r"https?://[^\s<>()]+[^\s<>().,!?;:'\"]")


def _linkify(text: str) -> str:
    """Escape text and turn raw URLs into tappable "Link" text."""
    out, last = [], 0
    for m in _URL.finditer(text):
        out.append(fmt.esc(text[last:m.start()]))
        out.append(fmt.link(m.group(0), "Link"))
        last = m.end()
    out.append(fmt.esc(text[last:]))
    return "".join(out)


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
    if action == "going":
        for e in chosen:
            await _propose_going(context.bot, update.effective_chat.id, user_id, e)
    agent.remember_turn(user_id, "user", text)
    agent.remember_turn(user_id, "assistant", last_reply(user_id) or " ".join(drafts))


async def events_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/events: top 5 in the next 7 days. /events all: everything relevant this week by day.
    /events refresh: fetch now."""
    log_message("in", update, inbound_text(update))
    user_id = update.effective_user.id
    sub = context.args[0].lower() if context.args else ""
    said = inbound_text(update)
    now = datetime.now(schedule.TZ)
    if sub == "add_luma":
        url = (context.args[1] if len(context.args) > 1 else "").strip()
        if not re.match(r"^https://(api\.)?(luma\.com|lu\.ma)/\S+$", url) or "ics" not in url:
            await speak(update, "Needs a Luma iCal feed link, like /events add_luma https://api.luma.com/ics/get?entity=...",
                        kind="error", user_message=said)
            return
        import luma
        if url in luma.urls():
            await speak(update, "That Luma feed is already added.", kind="confirmation", user_message=said)
            return
        try:
            async with events.eventsources.client() as c:
                resp = await c.get(url)
                resp.raise_for_status()
                n = len(luma.parse(resp.content, now))
        except Exception as e:
            await speak(update, f"Couldn't read that Luma feed ({type(e).__name__}). Ask him to check the link.",
                        kind="error", user_message=said)
            return
        _add_env_list("LUMA_ICS_URLS", url)
        report = await _with_typing(update, context, events.refresh(now))
        scored = await _with_typing(update, context, events.score_new(user_id, now))
        await speak(update, f"Added the Luma feed ({n} upcoming events in it). Refreshed everything and scored "
                            f"{scored} new events for him.", kind="confirmation", user_message=said)
        return
    if sub == "refresh":
        report = await _with_typing(update, context, events.refresh(now))
        scored = await _with_typing(update, context, events.score_new(user_id, now))
        got = "; ".join(f"{k}: {v.get('fetched', 0)} upcoming, {v.get('new', 0)} new" if "error" not in v
                        else f"{k}: failed ({v['error']})" for k, v in report.items())
        await speak(update, f"Refreshed the event feeds. {got}. Scored {scored} new ones for him.",
                    kind="confirmation", user_message=said)
        return
    await send_view(context.bot, update.effective_chat.id, _events_view(user_id, sub == "all", now))


def _events_view(user_id, all_of_them: bool, now: datetime) -> View:
    top = events.matches(user_id, now, now + timedelta(days=7))
    if not top:
        return View("🎟 <b>EVENTS</b>\n\nNo strong matches in the next 7 days.")
    if all_of_them:
        events.remember_list(user_id, [s.event.id for s in top])
        return views.events_by_day_view(top, now)
    top = top[:5]
    events.remember_list(user_id, [s.event.id for s in top])
    return views.events_view(top, now, title="Top events", lead="Best matches for the next 7 days.")


def _add_env_list(key: str, value: str) -> None:
    """Append a value to a comma-separated .env setting (and the running process). Never logged."""
    env = Path(__file__).resolve().parent / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    current = next((l.split("=", 1)[1] for l in lines if l.startswith(f"{key}=")), "")
    merged = ",".join([v for v in current.split(",") if v.strip()] + [value])
    lines = [l for l in lines if not l.startswith(f"{key}=")] + [f"{key}={merged}"]
    env.write_text("\n".join(lines) + "\n")
    os.environ[key] = merged


async def _email_button(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, op: str, ref: str) -> None:
    chat_id = update.effective_chat.id
    if op in ("reply", "nudge"):
        try:
            a = await _with_typing(update, context, inbox.draft_reply(user_id, ref, nudge=op == "nudge"))
        except actions.Refused as r:
            await reply(update, str(r))
            return
        except Exception as e:
            log.warning("draft reply failed (%s)", type(e).__name__)
            await reply(update, "Couldn't write that draft right now. Try again in a moment?")
            return
        await offer_card(context.bot, chat_id, user_id, a.id)
    elif op == "sched":
        e = inbox.get_email(ref)
        if e is None:
            await reply(update, "I can't find that email anymore.")
            return
        told, new_ids = inbox.apply_items(user_id, e)
        await reply(update, "Updated your schedule:\n" + "\n".join(f"• {t}" for t in told) if told
                    else "That's already on your schedule.")
        for item_id in new_ids[:3]:
            it = schedule.get_item(item_id)
            if it:
                try:
                    a = actions.propose(user_id, "calendar_add", actions.calendar_from_item(it), it.title, "email")
                    await offer_card(context.bot, chat_id, user_id, a.id)
                except actions.Refused:
                    pass
    elif op == "ok":
        await reply(update, "👍")
    elif op in ("snooze1", "wait3"):
        kind = "dropped" if op == "snooze1" else "waiting"
        inbox.set_nudge(user_id, ref, kind, "snoozed", snooze=timedelta(days=1 if op == "snooze1" else 3))
        await reply(update, "I'll bring it up again tomorrow." if op == "snooze1" else "I'll check again in 3 days.")
    elif op in ("noreply", "letgo"):
        inbox.set_nudge(user_id, ref, "dropped" if op == "noreply" else "waiting", "dismissed")
        await reply(update, "Got it, I'll leave that thread alone.")


async def _propose_going(bot, chat_id: int, user_id, e) -> None:
    try:
        a = actions.propose(user_id, "calendar_add", actions.calendar_from_event(e), e.title, "going")
    except actions.Refused:
        return
    await offer_card(bot, chat_id, user_id, a.id)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tap buttons: week toggle, events, going / later / not for me, show more."""
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = q.data or ""
    log_raw("in", user_id, update.effective_chat.id, f"[button] {data}", "button")
    now = datetime.now(schedule.TZ)
    if data.startswith("week:"):
        parts = data.split(":")
        v = views.week_view(user_id, int(parts[1]), now)
        # Toggling inside /week edits the message; "Full week" from the brief sends a new one.
        await send_view(context.bot, update.effective_chat.id, v,
                        edit_message=None if len(parts) > 2 else q.message)
    elif data == "inbox":
        await send_view(context.bot, update.effective_chat.id, inbox.inbox_view(user_id))
    elif data == "events":
        await send_view(context.bot, update.effective_chat.id, _events_view(user_id, False, now))
    elif data.startswith("ev:"):
        _, action, event_id = data.split(":", 2)
        e = events.get_event(event_id)
        if e is None:
            await speak(update, "That event isn't in the list anymore.", kind="error")
            return
        draft = await events.act(user_id, {"going": "going", "later": "later", "hide": "hide"}[action], e)
        if action == "going":
            context.application.create_task(brain.connect(user_id))
        await speak(update, draft, kind="confirmation", user_message=f"(tapped {action} on {e.title})")
        if action == "going":
            await _propose_going(context.bot, update.effective_chat.id, user_id, e)
    elif data.startswith("act:"):
        _, op, action_id = data.split(":", 2)
        if op == "edit":
            a = actions.get(action_id)
            if a is None or a.status != "pending":
                await reply(update, "That card isn't open anymore.")
                return
            selfmodel.set_meta(user_id, awaiting_action_edit=action_id)
            await reply(update, "What should I change? Tell me in plain words (a different time, a shorter email...).")
            return
        await act_on_card(context.bot, update.effective_chat.id, user_id, action_id, op, "button", q.message)
    elif data.startswith("em:"):
        _, op, ref = data.split(":", 2)
        await _email_button(update, context, user_id, op, ref)
    elif data.startswith("draft:"):
        it = next((i for i in schedule.existing_items(user_id) if str(i.id) == data[6:]), None)
        if it is None:
            await reply(update, "I can't find that follow-up anymore.")
            return
        r = await agent.respond(user_id, f"Draft an email for my follow-up \"{it.title}\" ({schedule.fmt_due(it)}). "
                                         "Use what you know about the person. If you don't have their email address, ask me for it.")
        await _send_agent_reply(update, context, r)
    elif data.startswith("tx:"):
        row = _db.execute("SELECT text FROM voice_transcripts WHERE token = ?", (data[3:],)).fetchone()
        await send_view(context.bot, update.effective_chat.id, View(fmt.esc(row[0]) if row else "That text isn't around anymore."), speak=False)
    elif data.startswith("tr:"):
        row = _db.execute("SELECT text FROM voice_transcripts WHERE token = ?", (data[3:],)).fetchone()
        text = f"📝 <b>TRANSCRIPT</b>\n\n<i>{fmt.esc(row[0])}</i>" if row else "That transcript isn't around anymore."
        await send_view(context.bot, update.effective_chat.id, View(text))
    elif data.startswith("more:"):
        pages = _more_pages.pop(data[5:], None)
        if not pages:
            await speak(update, "That list expired. Ask again and you'll get a fresh one.", kind="error")
            return
        await send_view(context.bot, update.effective_chat.id, View(pages[0], [], pages[1:]))


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
        "AND text NOT LIKE '/%' AND text NOT LIKE '[button]%' ORDER BY id",
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
    for part in await _with_typing(update, context, demo_time(update.effective_user.id, at)):
        if isinstance(part, View):
            await send_view(context.bot, update.effective_chat.id, part)
        else:
            await reply(update, part)


async def demo_time(user_id, at: datetime) -> list:
    """What would go out at that moment: reminders and (at 9 AM) event picks. Returns views/texts."""
    out: list = [f"🕒 Pretending it's {schedule.fmt_day(at.date())}, {schedule.fmt_time(at.time())}"]
    if at.time() == events.DELIVERY_TIME:
        picks = events.picks_for_day(user_id, at.date(), demo=True)
        if picks:
            v = await assistant.picks_message(user_id, picks, at, recent_replies(user_id))
            events.record_picks(user_id, at.date(), picks, v.plain, demo=True)
            out.append(v)
    for it, lead in schedule.preview_reminders(user_id, at):
        out.append(await assistant.reminder_message(user_id, it, lead, at, recent_replies(user_id)))
    if len(out) == 1:
        out.append("Nothing would go out at that moment.")
    return out


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    brain.touch("user is messaging")  # start waking hosted memory while we work on the reply
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


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Voice notes, audio files and video notes: transcribe, then handle exactly like typed text."""
    brain.touch("user is messaging")  # start waking hosted memory while we work on the reply
    msg = update.effective_message
    media = msg.voice or msg.audio or msg.video_note
    user_id = update.effective_user.id
    duration = getattr(media, "duration", 0) or 0
    if duration > transcribe.MAX_SECONDS:
        log_message("in", update, f"[voice {duration}s, too long]")
        await speak(update, f"That note is {duration // 60} minutes long; you can take up to 5. Ask him for a "
                            "shorter one or to split it.", kind="error")
        return
    await update.effective_chat.send_action(ChatAction.TYPING)
    if msg.voice:
        ext = ".ogg"
    elif msg.video_note:
        ext = ".mp4"
    else:
        ext = Path(msg.audio.file_name or "").suffix.lower() or {"audio/mpeg": ".mp3", "audio/mp4": ".m4a"}.get(msg.audio.mime_type or "", ".ogg")
    tmp = FILES_DIR.parent / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / f"{uuid.uuid4().hex}{ext}"
    try:
        f = await media.get_file()
        await f.download_to_drive(path)
        text = await _with_typing(update, context, transcribe.transcribe(path, user_id))
    except Exception as e:
        log.warning("voice note failed (%s)", type(e).__name__)
        text = ""
    finally:
        path.unlink(missing_ok=True)  # keep the transcript, never the audio
    if not text:
        log_message("in", update, "[voice] (nothing transcribed)")
        await reply(update, "Couldn't catch that, mind trying again or typing it?")
        return
    row = log_message("in", update, f"[voice] {text}")
    token = uuid.uuid4().hex[:12]
    _db.execute("INSERT INTO voice_transcripts (token, user_id, text, created_at) VALUES (?, ?, ?, ?)",
                (token, user_id, text, datetime.now(timezone.utc).isoformat()))
    _db.commit()
    _voice_note.set({"quote": f"🎙 <i>\"{fmt.esc(transcribe.preview(text))}\"</i>", "token": token})
    try:
        await handle_text(update, context, text, [row])
    finally:
        _voice_note.set(None)


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
                v = await assistant.reminder_message(user_id, item, lead, now, recent_replies(user_id))
                await send_view(app.bot, int(user_id), v)
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(30)


async def sync_feeds(app: Application) -> None:
    """Sync Brightspace and Google Calendar now and every 3 hours. Never logs feed URLs."""
    while True:
        results = await feeds.sync_all()
        if any(not isinstance(r, str) and r.added for r in results.values()):
            app.create_task(brain.connect(feeds.calendar_user()))
        # Clean up new or changed titles once ("30 Min Meeting between ..." -> "Meeting with ...").
        await schedule.normalize_titles(owner=selfmodel.owner_name(feeds.calendar_user()) if feeds.calendar_user() else None)
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


async def email_loop(app: Application) -> None:
    """Every 10 minutes: new mail since last time (read-only). Then schedule updates from email,
    important alerts (max 5 a day, held in quiet hours) and, hourly, dropped-thread and waiting-on nudges."""
    told_reconnect, last_nudges = set(), None
    while True:
        now = datetime.now(schedule.TZ)
        # One Google connection, and it belongs to the owner: never read it on anyone else's behalf.
        for user_id in [u for u in [feeds.calendar_user()] if u]:
            try:
                if not gaccount.connected():
                    continue
                if not gaccount.can_read_mail():
                    if user_id not in told_reconnect:
                        log.info("email awareness needs read access: /connect_google again")
                        told_reconnect.add(user_id)
                    continue
                if inbox.due(user_id):
                    started = datetime.now(timezone.utc).isoformat()
                    rep = await inbox.sync(user_id)
                    if rep.first and not rep.error:
                        await send_view(app.bot, int(user_id), View(
                            f"📬 <b>INBOX CONNECTED</b>\n↳ Read the last {inbox.FIRST_SYNC_DAYS} days: {rep.fetched} emails, "
                            f"{rep.skipped} skipped (promotions, social, newsletters), {rep.classified} sorted.\n"
                            "↳ I only read. I never delete, archive or mark anything, and I never send without your OK.",
                            [[("📬 Open inbox", "inbox")]]))
                    await _after_sync(app, user_id, started, first=rep.first)
                quiet = selfmodel.quiet_until(user_id, now)
                if inbox.settings(user_id)["alerts"] == "on" and not quiet:
                    for e in inbox.pending_alerts(user_id):
                        await send_view(app.bot, int(user_id), inbox.alert_view(e))
                        inbox.mark_alerted(e.id)
                if (last_nudges is None or now - last_nudges >= timedelta(hours=1)) and not quiet:
                    for n in inbox.nudges_due(user_id)[:2]:
                        await send_view(app.bot, int(user_id), inbox.nudge_view(n))
                        inbox.set_nudge(user_id, n["email"].thread_id if n["kind"] == "dropped" else n["thread_id"], n["kind"], "sent",
                                        email_id=n["email"].id if n["kind"] == "dropped" else n["id"])
            except Exception:
                log.exception("email loop error")
        if last_nudges is None or now - last_nudges >= timedelta(hours=1):
            last_nudges = now
        await asyncio.sleep(60)


async def _after_sync(app: Application, user_id, since: str, first: bool) -> None:
    """New mail: deadlines and changes onto the schedule (telling him), calendar cards for new ones,
    and important facts into memory."""
    rows = inbox._db.execute("SELECT id FROM emails WHERE user_id = ? AND classified_at >= ? AND mine = 0", (str(user_id), since)).fetchall()
    told_all, cards = [], 0
    for r in rows:
        e = inbox.get_email(r["id"])
        if e is None:
            continue
        inbox.remember_important(user_id, e)
        if e.items and not e.applied and (datetime.now(timezone.utc) - e.date) < timedelta(days=inbox.FIRST_SYNC_DAYS):
            told, new_ids = inbox.apply_items(user_id, e)
            told_all += told
            for item_id in new_ids:
                it = schedule.get_item(item_id)
                if it and cards < 3 and it.due_date and it.due_date >= datetime.now(schedule.TZ).date():
                    try:
                        a = actions.propose(user_id, "calendar_add", actions.calendar_from_item(it), it.title, "email")
                        cards += 1
                        await offer_card(app.bot, int(user_id), user_id, a.id)
                    except actions.Refused:
                        pass
    if told_all:
        await send_view(app.bot, int(user_id), fmt.view("📅 <b>FROM YOUR EMAIL</b>\n" + "\n".join(f"• {fmt.esc(t)}" for t in told_all[:8])))
    if rows:
        app.create_task(brain.connect(user_id))


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
                    v = await assistant.picks_message(user_id, picks, now, recent_replies(user_id)) if picks else None
                    events.record_picks(user_id, now.date(), picks, v.plain if v else None)
                    if v:
                        events.remember_list(user_id, [p.event.id for p in picks])
                        await send_view(app.bot, int(user_id), v)
        except Exception:
            log.exception("events loop error")
        await asyncio.sleep(60)


async def brief_loop(app: Application) -> None:
    """Morning brief at each user's time (default 8 AM New York); Sundays the 7 PM weekly preview
    instead. Never twice for the same day; skipped in quiet hours or if the bot was off too long."""
    while True:
        now = datetime.now(schedule.TZ)
        for user_id in event_users():
            try:
                kind = brief.due(user_id, now)
                if kind == "daily":
                    await _deliver_brief(app.bot, user_id, now.date(), await brief.daily(user_id, now.date(), now))
                elif kind == "weekly":
                    v = await brief.weekly(user_id, now)
                    await send_view(app.bot, int(user_id), v)
                    brief.mark_sent(user_id, now.date(), "weekly", v.plain)
            except Exception:
                log.exception("brief loop error")
        await asyncio.sleep(60)


WARM_AHEAD = timedelta(minutes=4)


async def warm_loop(app: Application) -> None:
    """Wake hosted memory a few minutes before jobs that read it (the morning brief or weekly preview,
    class reminders), so they don't hit a cold start. Only then; user messages wake it on their own."""
    while True:
        try:
            now = datetime.now(schedule.TZ)
            soon = now + WARM_AHEAD
            if any(brief.due(u, soon) and not brief.due(u, now) for u in event_users()):
                brain.touch("brief in a few minutes")
            nxt = schedule.next_reminder_at()
            if nxt and now <= nxt <= soon:
                brain.touch("class reminder in a few minutes")
        except Exception:
            log.exception("warm loop error")
        await asyncio.sleep(60)


async def post_init(app: Application) -> None:
    await brain.start()  # Cognee Cloud: connect on this loop before any memory operation
    n = actions.expire_stale_sends()  # a send cut off by a restart never goes out late
    if n:
        log.warning("%d interrupted email send(s) marked failed", n)
    schedule.plan_all_reminders()
    app.create_task(send_reminders(app))
    app.create_task(sync_feeds(app))
    app.create_task(profile_loop(app))
    app.create_task(events_loop(app))
    app.create_task(brief_loop(app))
    app.create_task(email_loop(app))
    app.create_task(warm_loop(app))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await speak(update, "Something went wrong on your side. Ask him to try again.", kind="error")
        except Exception:
            pass


def build_app(token: str) -> Application:
    app = (
        Application.builder().token(token).concurrent_updates(True).post_init(post_init)
        .connect_timeout(20).read_timeout(30).get_updates_connect_timeout(20).get_updates_read_timeout(40)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("demo_time", demo_time_cmd))
    app.add_handler(CommandHandler("me", me_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(CommandHandler("events", events_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("brief", brief_cmd))
    app.add_handler(CommandHandler("connect_google", connect_google_cmd))
    app.add_handler(CommandHandler("actions", actions_cmd))
    app.add_handler(CommandHandler("inbox", inbox_cmd))
    app.add_handler(CommandHandler("email", email_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, on_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, on_voice))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND, on_other))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Put it in .env")
    from telegram.error import NetworkError

    # A brief network drop while starting (reaching Telegram) used to kill the bot. Retry instead.
    for attempt in range(1, 7):
        try:
            log.info("Violet Memory is running. Press Ctrl+C to stop.")
            build_app(token).run_polling(allowed_updates=Update.ALL_TYPES)
            return
        except NetworkError as e:  # includes TimedOut
            wait = min(60, 5 * attempt)
            log.warning("could not reach Telegram at startup (%s), retrying in %ds", type(e).__name__, wait)
            import time as _time
            _time.sleep(wait)
            asyncio.set_event_loop(asyncio.new_event_loop())
    raise SystemExit("Could not reach Telegram after several tries.")


if __name__ == "__main__":
    main()
