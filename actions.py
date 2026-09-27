"""The action framework: the agent PROPOSES, the user APPROVES, only then does anything happen.
One module for every action type (Google Calendar, Gmail). No channel code here.

Rules enforced here in code (not only in prompts):
- The agent can only create pending proposals. execute() is reached only through approve(), which
  bot.py calls for the user's own button tap or explicit "yes" reply to that card, or through the
  user's own "calendar auto" setting for low-risk calendar adds. Content inside emails, web pages
  or documents can never approve anything: it never reaches approve().
- Proposals expire after 24 hours and execute at most once (atomic status change).
- Only the action types registered below exist: no Brightspace submissions, no purchases, no payments.
- Emails go only to addresses the user gave (never guessed), at most 10 sent per day, never
  coursework to submit as the user's own.
"""

import asyncio
import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
import base64

import fmt
import gaccount
import llm
import schedule
import selfmodel
from fmt import View, esc
from schedule import TZ

log = logging.getLogger("actions")

EXPIRE_AFTER = timedelta(hours=24)
SEND_DELAY = 10  # seconds between tapping Send and the email leaving, with Undo
MAX_EMAILS_PER_DAY = 10
MARKER = "Added by Violet"  # in descriptions of events Violet creates (the iCal feed carries it)

DB_PATH = Path(__file__).resolve().parent / "data" / "violet.db"
MSG_DB = Path(__file__).resolve().parent / "data" / "messages.db"
_db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
_db.row_factory = sqlite3.Row
_db.executescript("""
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,             -- calendar_add, email
    payload TEXT NOT NULL,          -- JSON
    summary TEXT NOT NULL,
    status TEXT NOT NULL,           -- pending, executing, sending, done, failed, cancelled, expired, undone
    origin TEXT,                    -- agent, going, schedule, followup
    op TEXT,                        -- what was approved: do, send, draft
    approved_via TEXT,              -- button, text, setting
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    executed_at TEXT,
    result TEXT,                    -- JSON
    link TEXT,
    card_message_id INTEGER
);
CREATE TABLE IF NOT EXISTS violet_gcal (
    ical_uid TEXT PRIMARY KEY,      -- how the Google Calendar iCal feed names the event
    event_id TEXT NOT NULL,
    action_id TEXT,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
""")
_db.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Refused(Exception):
    """A hard limit said no. The message is safe to show the user."""


# ---------- Types ----------

ICON = {"event": "🎟", "meeting": "🤝", "deadline": "📌", "exam": "📝", "class": "🎓"}


def _when_text(p: dict) -> str:
    start = datetime.fromisoformat(p["start"])
    if p.get("all_day"):
        return f"{start:%a %b} {start.day}, all day"
    return f"{start:%a %b} {start.day}, {fmt.fmt_time(start.time())}"


def _reminder_text(minutes: int) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"reminder {minutes // 1440} day{'s' if minutes > 1440 else ''} before"
    if minutes >= 60 and minutes % 60 == 0:
        return f"reminder {minutes // 60}h before"
    return f"reminder {minutes} min before"


@dataclass
class Kind:
    name: str
    question: str  # card header
    ops: list[tuple[str, str]]  # (button label, op)


KINDS = {
    "calendar_add": Kind("calendar_add", "Add to Google Calendar?", [("✅ Do it", "do"), ("✏️ Edit", "edit"), ("✖ Cancel", "cancel")]),
    "email": Kind("email", "Send this email?", [("📤 Send", "send"), ("📝 Save as draft", "draft"), ("✏️ Edit", "edit"), ("✖ Cancel", "cancel")]),
}
EXECUTE_OPS = {"calendar_add": {"do"}, "email": {"send", "draft"}}


# ---------- Hard limits ----------

_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
_COURSEWORK = re.compile(
    r"\b(attached is my|please find (attached )?my|here is my|here's my|my submission|submitting my)\s+"
    r"(assignment|essay|paper|homework|hw|problem set|report|project|lab|quiz|exam|answers?)\b", re.I)
_SUBMISSION_HOSTS = re.compile(r"brightspace|d2l|gradescope|turnitin", re.I)


def email_known(user_id, address: str) -> bool:
    """An address may be used only if the user gave it: in their own messages or saved on a person."""
    a = address.strip().lower()
    for f in selfmodel.get_profile(user_id)["people"]:
        if str(f.get("attrs", {}).get("email", "")).lower() == a:
            return True
    try:
        m = sqlite3.connect(MSG_DB, timeout=10)
        hit = m.execute("SELECT 1 FROM messages WHERE user_id = ? AND direction = 'in' AND lower(text) LIKE ? LIMIT 1",
                        (int(user_id), f"%{a}%")).fetchone()
        m.close()
        if hit:
            return True
    except Exception:
        pass
    return bool(_db.execute("SELECT 1 FROM conversation WHERE user_id = ? AND role = 'user' AND lower(content) LIKE ? LIMIT 1",
                            (str(user_id), f"%{a}%")).fetchone())


def emails_sent_today(user_id) -> int:
    start = datetime.combine(datetime.now(TZ).date(), time(0, 0), tzinfo=TZ).astimezone(timezone.utc).isoformat()
    return _db.execute("SELECT COUNT(*) FROM actions WHERE user_id = ? AND type = 'email' AND op = 'send' "
                       "AND status IN ('done', 'sending', 'executing') AND executed_at >= ?", (str(user_id), start)).fetchone()[0]


def validate(user_id, type_: str, p: dict) -> dict:
    """Check and normalize a payload. Raises Refused with a reason the user can read."""
    if type_ not in KINDS:
        raise Refused("I can only add calendar events and write emails for you, nothing else.")
    blob = json.dumps(p)
    if type_ == "calendar_add":
        if not p.get("title") or not p.get("start"):
            raise Refused("A calendar event needs a title and a start time.")
        start = datetime.fromisoformat(str(p["start"]))
        start = start if start.tzinfo else start.replace(tzinfo=TZ)
        p["start"] = start.isoformat()
        if p.get("end"):
            end = datetime.fromisoformat(str(p["end"]))
            p["end"] = (end if end.tzinfo else end.replace(tzinfo=TZ)).isoformat()
        p["reminder_minutes"] = int(p.get("reminder_minutes") or 60)
        p["kind"] = p.get("kind") if p.get("kind") in ICON else "event"
        return p
    # email
    to = str(p.get("to_email") or "").strip()
    if not _EMAIL.match(to):
        raise Refused("I need his or her real email address first. What is it?")
    if not email_known(user_id, to):
        raise Refused(f"I won't use {to} unless you give it to me. What's the right address?")
    if _SUBMISSION_HOSTS.search(to) or _COURSEWORK.search(f"{p.get('subject', '')} {p.get('body', '')}"):
        raise Refused("I can't send coursework or submissions for you. That part has to be yours.")
    if not str(p.get("subject") or "").strip() or not str(p.get("body") or "").strip():
        raise Refused("An email needs a subject and a message.")
    p["body"] = fmt.nodash(str(p["body"])).strip()
    p["subject"] = fmt.nodash(str(p["subject"])).strip()
    return p


# ---------- Proposals and cards ----------


@dataclass
class Action:
    id: str
    user_id: str
    type: str
    payload: dict
    summary: str
    status: str
    link: str | None = None
    result: dict | None = None
    op: str | None = None
    created_at: str = ""
    expires_at: str = ""
    card_message_id: int | None = None


def _row(r) -> Action:
    return Action(r["id"], r["user_id"], r["type"], json.loads(r["payload"]), r["summary"], r["status"], r["link"],
                  json.loads(r["result"]) if r["result"] else None, r["op"], r["created_at"], r["expires_at"],
                  r["card_message_id"])


def get(action_id: str) -> Action | None:
    r = _db.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    return _row(r) if r else None


def propose(user_id, type_: str, payload: dict, summary: str, origin: str = "agent") -> Action:
    """Store a pending proposal (after the hard limits). Nothing is executed here."""
    p = validate(user_id, type_, dict(payload))
    now = _now()
    a = Action(secrets.token_hex(5), str(user_id), type_, p, fmt.nodash(summary)[:200] or type_, "pending")
    _db.execute("INSERT INTO actions (id, user_id, type, payload, summary, status, origin, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (a.id, a.user_id, type_, json.dumps(p), a.summary, origin, now.isoformat(), (now + EXPIRE_AFTER).isoformat()))
    _db.commit()
    log.info("proposed %s %s (%s)", type_, a.id, origin)
    return a


def set_card_message(action_id: str, message_id: int) -> None:
    _db.execute("UPDATE actions SET card_message_id = ? WHERE id = ?", (message_id, action_id))
    _db.commit()


def card(a: Action) -> View:
    """The approval card, rendered by code."""
    k = KINDS[a.type]
    p = a.payload
    lines = [f"⚡ <b>{esc(k.question)}</b>"]
    if a.type == "calendar_add":
        lines.append(f"{ICON.get(p.get('kind'), '🎟')} {esc(fmt.short_title(p['title']))} · {esc(_when_text(p))}")
        lines.append(fmt.sub(p.get("location"), _reminder_text(p["reminder_minutes"])))
        if p.get("link"):
            lines.append(fmt.sub_html(fmt.link(p["link"], "Event page")))
    else:
        lines.append(f"✉️ To: {esc(p.get('to_name') or '')} &lt;{esc(p['to_email'])}&gt;".replace("To:  &lt;", "To: &lt;"))
        lines.append(f"<b>Subject:</b> {esc(p['subject'])}")
        lines.append("")
        lines.append(esc(p["body"]))
    buttons = [[(label, f"act:{op}:{a.id}") for label, op in k.ops]]
    return View("\n".join(l for l in lines if l is not None), buttons)


def pending_for(user_id) -> list[Action]:
    rows = _db.execute("SELECT * FROM actions WHERE user_id = ? AND status = 'pending' AND expires_at > ? ORDER BY created_at DESC",
                       (str(user_id), _now().isoformat())).fetchall()
    return [_row(r) for r in rows]


# ---------- Approval and execution ----------


def _claim(a: Action, frm: str, to: str) -> bool:
    """Atomic status change: the one thing that makes every action run at most once."""
    if frm == "pending":  # a pending card can only be approved before it expires
        cur = _db.execute("UPDATE actions SET status = ? WHERE id = ? AND status = 'pending' AND expires_at > ?",
                          (to, a.id, _now().isoformat()))
    else:
        cur = _db.execute("UPDATE actions SET status = ? WHERE id = ? AND status = ?", (to, a.id, frm))
    _db.commit()
    return cur.rowcount == 1


def _finish(a: Action, status: str, result: dict | None = None, link: str | None = None) -> None:
    _db.execute("UPDATE actions SET status = ?, result = ?, link = ?, executed_at = COALESCE(executed_at, ?) WHERE id = ?",
                (status, json.dumps(result) if result is not None else None, link, _now().isoformat(), a.id))
    _db.commit()


@dataclass
class Outcome:
    text: str  # a short confirmation or explanation (plain facts; bot voices or sends it)
    link: str | None = None
    undo: bool = False  # offer [↩️ Undo]
    sending: bool = False  # email queued, will leave after SEND_DELAY unless undone


def _status_text(a: Action) -> str:
    return {"done": "That's already done.", "sending": "That's already on its way.", "cancelled": "That one was cancelled.",
            "expired": "That card expired (after 24 hours). Ask me again if you still want it.",
            "undone": "That was undone.", "executing": "Working on it already.", "failed": "That one failed earlier."}.get(a.status, "")


async def approve(user_id, action_id: str, op: str, via: str) -> Outcome:
    """The user's own approval (via = button, text or setting). Runs the action at most once."""
    a = get(action_id)
    if a is None or a.user_id != str(user_id):
        return Outcome("I can't find that card.")
    if a.status == "pending" and a.expires_at <= _now().isoformat():
        _db.execute("UPDATE actions SET status = 'expired' WHERE id = ? AND status = 'pending'", (a.id,))
        _db.commit()
        a.status = "expired"
    if op == "cancel":
        if _claim(a, "pending", "cancelled"):
            return Outcome("Cancelled, nothing was done.")
        return Outcome(_status_text(a) or "Nothing to cancel.")
    if op not in EXECUTE_OPS.get(a.type, set()):
        return Outcome("That button doesn't fit this card.")
    if via not in ("button", "text", "setting") or (via == "setting" and a.type != "calendar_add"):
        return Outcome("Only you can approve this.")
    target = "sending" if (a.type == "email" and op == "send") else "executing"
    if a.type == "email" and op == "send" and emails_sent_today(user_id) >= MAX_EMAILS_PER_DAY:
        return Outcome(f"That would be more than {MAX_EMAILS_PER_DAY} emails today, so I'm holding it. "
                       "Save it as a draft instead, or send it tomorrow.")
    if not _claim(a, "pending", target):
        return Outcome(_status_text(get(a.id)) or "That card isn't pending anymore.")
    _db.execute("UPDATE actions SET op = ?, approved_via = ?, executed_at = ? WHERE id = ?", (op, via, _now().isoformat(), a.id))
    _db.commit()
    if target == "sending":
        return Outcome(f"Sending to {a.payload['to_email']} in {SEND_DELAY} seconds.", undo=True, sending=True)
    try:
        if not gaccount.connected():
            raise gaccount.NotConnected("not connected")
        result = await (_do_calendar(a) if a.type == "calendar_add" else _do_draft(a))
    except gaccount.NotConnected:
        _db.execute("UPDATE actions SET status = 'pending' WHERE id = ?", (a.id,))  # nothing happened: keep the card
        _db.commit()
        return Outcome("Google isn't connected yet. Send /connect_google first, then tap the button again.")
    except Exception as e:
        log.warning("action %s failed (%s)", a.id, type(e).__name__)
        _finish(a, "failed", {"error": type(e).__name__})
        return Outcome(f"That didn't go through ({type(e).__name__}). Nothing was changed.")
    _finish(a, "done", result, result.get("link"))
    return Outcome(result["confirm"], result.get("link"), undo=True)


async def finish_send(action_id: str) -> Outcome | None:
    """After the Undo window: send the email for real (unless it was undone)."""
    a = get(action_id)
    if a is None or not _claim(a, "sending", "executing"):
        return None  # undone during the window
    try:
        result = await _do_send(a)
    except Exception as e:
        log.warning("send %s failed (%s)", a.id, type(e).__name__)
        _finish(a, "failed", {"error": type(e).__name__})
        return Outcome(f"The email didn't send ({type(e).__name__}). Nothing went out.")
    _finish(a, "done", result, result.get("link"))
    _after_email(a)
    return Outcome(result["confirm"], result.get("link"))


async def undo(user_id, action_id: str) -> Outcome:
    a = get(action_id)
    if a is None or a.user_id != str(user_id):
        return Outcome("I can't find that.")
    if a.status == "sending" and _claim(a, "sending", "cancelled"):
        return Outcome("Stopped. The email was not sent.")
    if a.status != "done" or not a.result:
        return Outcome(_status_text(a) or "There's nothing to undo there.")
    try:
        if a.type == "calendar_add":
            ok = await _undo_calendar(a)
        elif a.type == "email" and a.op == "draft":
            await asyncio.to_thread(lambda: gaccount.service("gmail", "v1").users().drafts().delete(
                userId="me", id=a.result["draft_id"]).execute())
            ok = True
        else:
            return Outcome("A sent email can't be unsent after the 10 seconds.")
    except Exception as e:
        return Outcome(f"Couldn't undo that ({type(e).__name__}).")
    if not ok:
        return Outcome("I only remove events I created myself, and that one isn't mine anymore.")
    _finish(a, "undone", a.result, a.link)
    return Outcome("Undone.")


def expire_stale_sends() -> int:
    """On startup: a send interrupted by a restart never went out; mark it failed, don't send late."""
    cur = _db.execute("UPDATE actions SET status = 'failed', result = ? WHERE status = 'sending'",
                      (json.dumps({"error": "interrupted before sending"}),))
    _db.commit()
    return cur.rowcount


# ---------- Google Calendar ----------


def _event_body(a: Action) -> dict:
    p = a.payload
    desc = "\n\n".join(x for x in [p.get("description"), p.get("link"), MARKER] if x)
    body = {
        "summary": p["title"],
        "location": p.get("location") or None,
        "description": desc,
        "extendedProperties": {"private": {"violet_id": a.id}},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": p["reminder_minutes"]}]},
    }
    start = datetime.fromisoformat(p["start"])
    if p.get("all_day"):
        body["start"] = {"date": start.date().isoformat()}
        body["end"] = {"date": (start.date() + timedelta(days=1)).isoformat()}
    else:
        end = datetime.fromisoformat(p["end"]) if p.get("end") else start + timedelta(hours=1)
        body["start"] = {"dateTime": start.isoformat(), "timeZone": "America/New_York"}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": "America/New_York"}
    return {k: v for k, v in body.items() if v is not None}


async def _do_calendar(a: Action) -> dict:
    ev = await asyncio.to_thread(lambda: gaccount.service("calendar", "v3").events().insert(
        calendarId="primary", body=_event_body(a)).execute())
    _db.execute("INSERT OR REPLACE INTO violet_gcal (ical_uid, event_id, action_id, user_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (ev.get("iCalUID", ""), ev["id"], a.id, a.user_id, _now().isoformat()))
    _db.commit()
    return {"event_id": ev["id"], "ical_uid": ev.get("iCalUID"), "link": ev.get("htmlLink"),
            "confirm": f"Added to Google Calendar: {a.payload['title']}, {_when_text(a.payload)}."}


async def _undo_calendar(a: Action) -> bool:
    svc = gaccount.service("calendar", "v3")
    ev = await asyncio.to_thread(lambda: svc.events().get(calendarId="primary", eventId=a.result["event_id"]).execute())
    if (ev.get("extendedProperties", {}).get("private", {}) or {}).get("violet_id") != a.id:
        return False  # never delete an event Violet didn't create
    await asyncio.to_thread(lambda: svc.events().delete(calendarId="primary", eventId=a.result["event_id"]).execute())
    _db.execute("DELETE FROM violet_gcal WHERE event_id = ?", (a.result["event_id"],))
    _db.commit()
    return True


def is_violet_event(uid: str, description: str = "") -> bool:
    """For the Google Calendar feed import: events Violet created itself are skipped."""
    return MARKER in (description or "") or bool(_db.execute("SELECT 1 FROM violet_gcal WHERE ical_uid = ?", (uid,)).fetchone())


# ---------- Gmail ----------


def _mime(a: Action) -> str:
    p = a.payload
    m = EmailMessage()
    m["To"] = f"{p.get('to_name')} <{p['to_email']}>" if p.get("to_name") else p["to_email"]
    m["Subject"] = p["subject"]
    m.set_content(p["body"])
    return base64.urlsafe_b64encode(m.as_bytes()).decode()


async def _do_draft(a: Action) -> dict:
    d = await asyncio.to_thread(lambda: gaccount.service("gmail", "v1").users().drafts().create(
        userId="me", body={"message": {"raw": _mime(a)}}).execute())
    return {"draft_id": d["id"], "link": "https://mail.google.com/mail/u/0/#drafts",
            "confirm": f"Saved as a Gmail draft to {a.payload['to_email']}: \"{a.payload['subject']}\"."}


async def _do_send(a: Action) -> dict:
    m = await asyncio.to_thread(lambda: gaccount.service("gmail", "v1").users().messages().send(
        userId="me", body={"raw": _mime(a)}).execute())
    return {"message_id": m["id"], "link": f"https://mail.google.com/mail/u/0/#sent/{m['id']}",
            "confirm": f"Sent to {a.payload['to_email']}: \"{a.payload['subject']}\"."}


def _after_email(a: Action) -> None:
    """Update the person: last contact today, follow-up done; close matching follow-up items."""
    name = (a.payload.get("to_name") or "").strip()
    if not name:
        return
    data = selfmodel.get_profile(a.user_id)
    for f in data["people"]:
        if name.split()[0].lower() in f["text"].lower():
            f.setdefault("attrs", {}).update(last_contact=datetime.now(TZ).date().isoformat(), followup="done",
                                             email=a.payload["to_email"])
    selfmodel.save_profile(a.user_id, data)
    first = name.split()[0].lower()
    for it in schedule.existing_items(a.user_id):
        if not it.done and schedule.FOLLOW_UP.search(it.title) and first in it.title.lower():
            schedule.mark_done_id(it.id)  # the follow-up with this person is done


def save_contact_email(user_id, name: str, email: str) -> str:
    """Save an address the user gave on the person's profile entry."""
    if not _EMAIL.match(email.strip()) or not email_known(user_id, email):
        raise Refused("I'll only save an address you've typed to me yourself.")
    data = selfmodel.get_profile(user_id)
    for f in data["people"]:
        if name.split()[0].lower() in f["text"].lower():
            f.setdefault("attrs", {}).update(email=email.strip())
            selfmodel.save_profile(user_id, data)
            return f"Saved {email} for {name}."
    selfmodel.add_facts(user_id, [{"section": "people", "text": f"{name}", "attrs": {"name": name, "email": email.strip()}}],
                        "chat", stated=True)
    return f"Saved {email} for {name}."


def person_email(user_id, name: str) -> str | None:
    for f in selfmodel.get_profile(user_id)["people"]:
        if name.split()[0].lower() in f["text"].lower() and f.get("attrs", {}).get("email"):
            return f["attrs"]["email"]
    return None


# ---------- Edits ----------

EDIT_TASK = """Update this {kind} per the user's change. Keep everything else as is. Return the full
updated JSON object only (same keys). Times are ISO 8601 in America/New_York. No em dashes.
For emails keep his style: casual, short, value-first, signed Chirag.

Current: {payload}
Change: "{change}" """


async def edit(user_id, action_id: str, change: str) -> Action:
    a = get(action_id)
    if a is None or a.user_id != str(user_id) or a.status != "pending":
        raise Refused(_status_text(a) if a else "I can't find that card.")
    data = await llm.chat_json([{"role": "user", "content": EDIT_TASK.format(
        kind="calendar event" if a.type == "calendar_add" else "email", payload=json.dumps(a.payload), change=change)}])
    p = validate(user_id, a.type, {**a.payload, **{k: v for k, v in data.items() if k in a.payload or k in ("end", "location", "description")}})
    _db.execute("UPDATE actions SET payload = ?, expires_at = ? WHERE id = ?",
                (json.dumps(p), (_now() + EXPIRE_AFTER).isoformat(), a.id))
    _db.commit()
    return get(a.id)


# ---------- Builders for automatic proposals ----------


def calendar_from_event(e) -> dict:
    """From a discovered event (NYU Events, Luma) he's going to."""
    import events

    return {"title": e.title, "start": e.start.isoformat(), "end": e.end.isoformat() if e.end and not e.all_day else None,
            "all_day": e.all_day, "location": events.where(e), "link": e.url, "reminder_minutes": 120, "kind": "event",
            "description": f"{e.organizer or ''}".strip() or None}


def calendar_from_item(it: schedule.Item) -> dict:
    """From a deadline, exam or event he added by chat."""
    kind = "exam" if it.kind == "exam" else ("event" if it.kind == "event" else "deadline")
    if it.due_time and kind != "event":
        start = datetime.combine(it.due_date, it.due_time, tzinfo=TZ) - timedelta(minutes=30)
        end = datetime.combine(it.due_date, it.due_time, tzinfo=TZ)
        title = f"Due: {it.shown}"
    elif it.due_time:
        start = datetime.combine(it.due_date, it.due_time, tzinfo=TZ)
        end = datetime.combine(it.due_date, it.end_time, tzinfo=TZ) if it.end_time else start + timedelta(hours=1)
        title = it.shown
    else:
        start, end, title = datetime.combine(it.due_date, time(0, 0), tzinfo=TZ), None, (f"Due: {it.shown}" if kind != "event" else it.shown)
    return {"title": title, "start": start.isoformat(), "end": end.isoformat() if end else None,
            "all_day": it.due_time is None, "location": it.room, "reminder_minutes": 1440 if kind in ("deadline", "exam") else 60,
            "kind": kind, "description": it.course or None}


def calendar_mode(user_id) -> str:
    return (selfmodel.meta(user_id).get("settings") or {}).get("calendar", "ask")


def set_calendar_mode(user_id, mode: str) -> None:
    s = selfmodel.meta(user_id).get("settings") or {}
    s["calendar"] = mode
    selfmodel.set_meta(user_id, settings=s)


# ---------- Log ----------

_TYPE_LABEL = {"calendar_add": "Calendar", "email": "Email"}
_STATUS_ICON = {"done": "✅", "pending": "⏳", "sending": "📤", "cancelled": "✖", "expired": "⌛", "undone": "↩️",
                "failed": "⚠️", "executing": "⏳"}


def log_view(user_id, n: int = 15) -> View:
    rows = _db.execute("SELECT * FROM actions WHERE user_id = ? ORDER BY created_at DESC LIMIT ?", (str(user_id), n)).fetchall()
    if not rows:
        return View("⚡ <b>ACTIONS</b>\n\nNothing yet. When I offer to add something to your calendar or write an email, it shows up here.")
    lines = ["⚡ <b>ACTIONS</b> · last " + str(len(rows))]
    for r in rows:
        a = _row(r)
        when = datetime.fromisoformat(r["created_at"]).astimezone(TZ)
        if a.type == "email" and a.status in ("done", "undone"):
            verb = {"send": "Sent", "draft": "Drafted"}.get(a.op or "", "Email")
        else:
            verb = _TYPE_LABEL.get(a.type, a.type)
        head = f"{_STATUS_ICON.get(a.status, '•')} <b>{esc(verb)}</b> · {esc(fmt.short_title(a.summary, limit=50))}"
        detail = f"{when:%a %b} {when.day}, {fmt.fmt_time(when.time())} · {a.status}"
        lines.append(head)
        lines.append(fmt.sub_html(esc(detail) + (f" · {fmt.link(a.link, 'Open')}" if a.link else "")))
    return fmt.view("\n".join(lines))


def done_yesterday(user_id) -> int:
    today = datetime.combine(datetime.now(TZ).date(), time(0, 0), tzinfo=TZ).astimezone(timezone.utc)
    return _db.execute("SELECT COUNT(*) FROM actions WHERE user_id = ? AND status = 'done' AND executed_at >= ? AND executed_at < ?",
                       (str(user_id), (today - timedelta(days=1)).isoformat(), today.isoformat())).fetchone()[0]
