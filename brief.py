"""Daily brief (8 AM) and weekly preview (Sunday 7 PM). Data is gathered in code; the model only
writes short lines (the day's tone, a tip, one memory line per class or meeting, the week's
priorities). Layout comes from fmt.py. No channel code here."""

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import brain
import events
import fmt
import llm
import schedule
import selfmodel
import views
import voice
from fmt import View, esc
from schedule import TZ, Entry

log = logging.getLogger("brief")

DEFAULT_TIME = time(8, 0)
WEEKLY_TIME = time(19, 0)  # Sundays: the weekly preview instead of the morning brief
SEND_WINDOW = timedelta(hours=2)  # if the bot was off longer than this past the time, skip the day
MEMORY_TIMEOUT = 6.0  # seconds for the per-class/meeting memory lookups
PICK_MIN = 80
LIGHT_MAX = 2  # a day with this many items or fewer gets the light layout
_ADVICE = re.compile(r"^(prepare|prep|be |focus|stay|remember|make sure|don't|dont|get ready|review|bring|consider|try)"
                     r"|\b(be ready|stay focused|good luck|don't forget)\b", re.I)
BRIEF_MAX_LINES = 28  # a busy morning gets a little more room before "Show more"
_FOLLOW_UP = re.compile(r"\b(follow[- ]?up|reach out|email|text|call|ping|reply to|get back to|send .* to)\b", re.I)

DB_PATH = Path(__file__).resolve().parent / "data" / "violet.db"
_db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
_db.execute("""CREATE TABLE IF NOT EXISTS briefs (
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,
    kind TEXT NOT NULL,        -- daily, weekly
    message TEXT,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (user_id, day, kind)
)""")
_db.commit()


# ---------- Settings ----------


def settings(user_id) -> dict:
    s = selfmodel.meta(user_id).get("brief") or {}
    return {"enabled": s.get("enabled", True), "time": s.get("time", DEFAULT_TIME.strftime("%H:%M"))}


def set_settings(user_id, **changes) -> dict:
    s = {**settings(user_id), **changes}
    selfmodel.set_meta(user_id, brief=s)
    return s


def parse_time(text: str) -> time | None:
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", text, re.I)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return time(h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def sent(user_id, day: date, kind: str) -> bool:
    return bool(_db.execute("SELECT 1 FROM briefs WHERE user_id = ? AND day = ? AND kind = ?",
                            (str(user_id), day.isoformat(), kind)).fetchone())


def mark_sent(user_id, day: date, kind: str, message: str) -> None:
    _db.execute("INSERT OR REPLACE INTO briefs (user_id, day, kind, message, sent_at) VALUES (?, ?, ?, ?, ?)",
                (str(user_id), day.isoformat(), kind, message, datetime.now(timezone.utc).isoformat()))
    _db.commit()


def due(user_id, now: datetime) -> str | None:
    """Which brief is due right now ("daily", "weekly") or None. Sundays get the weekly preview
    at 7 PM instead of the morning brief. Never twice for the same day."""
    s = settings(user_id)
    if not s["enabled"] or selfmodel.quiet_until(user_id, now):
        return None
    if now.weekday() == 6:
        at, kind = WEEKLY_TIME, "weekly"
    else:
        at, kind = parse_time(s["time"]) or DEFAULT_TIME, "daily"
    start = datetime.combine(now.date(), at, tzinfo=TZ)
    if start <= now < start + SEND_WINDOW and not sent(user_id, now.date(), kind):
        return kind
    return None


# ---------- Data (all gathered in code) ----------


@dataclass
class BriefData:
    day: date
    name: str
    agenda: list[Entry]  # today's classes, meetings, events
    due_soon: list[Entry]  # deadlines today + next 3 days (LockDown ones marked)
    exams: list[Entry]  # next 7 days
    follow_ups: list[Entry]
    pick: object | None  # events.Scored
    profile: str
    notes: dict = field(default_factory=dict)  # id(entry) -> calendar facts for the memory line


def gather(user_id, day: date, now: datetime) -> BriefData:
    as_of = max(now, datetime.combine(day, time(0, 0), tzinfo=TZ))
    ents = schedule.agenda_entries(user_id, day, day, as_of)
    agenda = [e for e in ents if e.kind in ("class", "meeting", "event")]
    soon = schedule.agenda_entries(user_id, day, day + timedelta(days=3), as_of, schedule.DEADLINE_KINDS)
    due_soon = [e for e in soon if e.kind == "deadline" and not _FOLLOW_UP.search(e.title)]
    exams = [e for e in schedule.agenda_entries(user_id, day, day + timedelta(days=6), as_of, {"exam"})]
    follow_ups = [e for e in ents if e.kind == "deadline" and _FOLLOW_UP.search(e.title)]
    pick = None
    for s in events.picks_for_day(user_id, day, demo=True):
        if s.score >= PICK_MIN and not s.conflicts:
            pick = s
            break
    name = (selfmodel.owner_name(user_id) or "").split(" ")[0] or "there"
    data = BriefData(day, name, agenda, due_soon, exams, follow_ups, pick,
                     selfmodel.profile_summary(user_id, max_words=120) or "")
    for e in agenda:
        facts = []
        if e.kind == "class":
            nxt = schedule.upcoming_for_course(user_id, e.course or e.title, as_of, limit=2)
            facts += [f"due for this course: {i.title}, {schedule.fmt_due(i)}" for i in nxt]
        data.notes[id(e)] = facts
    return data


async def _memory(user_id, e: Entry) -> str:
    q = (f"Who's in my {e.course or e.title} class or group, what's due next for it, and what did I note about it?"
         if e.kind == "class" else f"What do I know about {e.title}: who's involved, what I promised, what it's for?")
    try:
        return (await asyncio.wait_for(brain.context_for(user_id, q), MEMORY_TIMEOUT))[:1200]
    except (asyncio.TimeoutError, Exception) as ex:
        log.info("memory lookup skipped (%s)", type(ex).__name__)
        return ""


LINES_TASK = """Write the short lines for his {what}. Return JSON only.

Date: {date}
Today's agenda:
{agenda}
Deadlines soon: {deadlines}
Exams this week: {exams}
{extra}
Return {{"tone": "2 to 3 words for the day, like Busy day, Light day, Exam week, Big day",
"tip": "one concrete tip for {when} in under 20 words that names a specific item and when to do it (not a general reminder to focus)",
"context": {{"<n>": "one short line (max 12 words) for agenda item n that adds something NOT already in
its title or place: who's involved, what's due for that course, what he promised someone. Only facts
from its notes. Never restate the title, never generic advice. Empty string if there's nothing new."}}{prio}}}"""


async def _lines(user_id, what: str, when: str, day: date, agenda_items: list[tuple[str, str]],
                 deadlines: str, exams: str, extra: str = "", priorities: bool = False) -> dict:
    agenda = "\n".join(f"{n}. {line}\n   notes: {notes or 'none'}" for n, (line, notes) in enumerate(agenda_items, 1)) or "(nothing)"
    prio = ',\n"priorities": ["three short priorities for the week (max 12 words each), from the deadlines, exams and his goals"]' if priorities else ""
    task = LINES_TASK.format(what=what, when=when, date=f"{day:%A %b} {day.day}", agenda=agenda,
                             deadlines=deadlines or "none", exams=exams or "none", extra=extra, prio=prio)
    try:
        data = await llm.chat_json([{"role": "system", "content": voice.system_prompt(user_id)},
                                    {"role": "user", "content": task}], model_name=voice.voice_model(), temperature=0.5)
    except Exception as e:
        log.warning("brief lines failed (%s)", type(e).__name__)
        data = {}
    clean = lambda t: voice.polish(str(t or ""), 0).strip()
    def adds_something(n: int, line: str) -> bool:
        if _ADVICE.search(line):
            return False  # generic advice ("Prepare for ...", "be ready to network") isn't context
        # Drop lines that mostly repeat their item ("Kick Off & Info Session for Entrepreneurs Challenge").
        if not 1 <= n <= len(agenda_items):
            return False
        item = set(re.findall(r"[a-z0-9]+", agenda_items[n - 1][0].lower()))
        words = [w for w in re.findall(r"[a-z0-9]+", line.lower()) if len(w) > 3]
        return bool(words) and sum(w in item for w in words) / len(words) < 0.6

    ctx = {int(k): clean(v) for k, v in (data.get("context") or {}).items() if str(k).isdigit() and clean(v)}
    return {
        "tone": clean(data.get("tone"))[:30] or "Here's your day",
        "tip": clean(data.get("tip")),
        "context": {k: v for k, v in ctx.items() if adds_something(k, v)},
        "priorities": [clean(p) for p in (data.get("priorities") or []) if clean(p)][:3],
    }


def _entry_text(e: Entry) -> str:
    return f"{e.time_text} {e.title}" + (f" ({e.course})" if e.course else "") + (f" at {e.place}" if e.place else "")


# ---------- Daily brief ----------


async def daily(user_id, day: date, now: datetime | None = None) -> View:
    now = now or datetime.now(TZ)
    d = gather(user_id, day, now)
    memories = await asyncio.gather(*(_memory(user_id, e) for e in d.agenda)) if d.agenda else []
    items = [(_entry_text(e), "; ".join(d.notes[id(e)] + ([m] if m else []))[:1500]) for e, m in zip(d.agenda, memories)]
    lines = await _lines(
        user_id, "morning brief", "today", day, items,
        "; ".join(f"{e.title} ({e.course}) {e.day:%a} {e.time_text}" + (" [LockDown]" if e.note else "") for e in d.due_soon),
        "; ".join(f"{e.title} {e.day:%a} {e.time_text}" for e in d.exams),
        extra=f"Event worth his time: {d.pick.event.title}, {events.when(d.pick.event)}" if d.pick else "",
    )
    today = day
    greet = f"☀️ Good morning, {esc(d.name)}"
    sub_head = f"{esc(f'{day:%A}, {day:%b} {day.day}')} · {esc(lines['tone'])}"
    tip = f"💡 {esc(lines['tip'])}" if lines["tip"] else None
    total = len(d.agenda) + len(d.due_soon) + len(d.exams) + len(d.follow_ups) + (1 if d.pick else 0)

    agenda_lines = []
    for n, e in enumerate(d.agenda, 1):
        agenda_lines += views.entry_lines(e, today, lines["context"].get(n))
    due_lines = [views.deadline_bullet(e, today, compact=True)[0] for e in d.due_soon]
    exam_lines = [views.deadline_bullet(e, today, compact=True)[0] for e in d.exams]
    fu_lines = [fmt.bullet(f"{esc(fmt.short_title(e.title))} · <b>{esc(e.time_text)}</b>", "person") for e in d.follow_ups]
    pick_lines = views.event_lines(d.pick, today) if d.pick else []

    if total <= LIGHT_MAX:
        # Light day: greeting, the one or two items, the tip.
        body = agenda_lines + due_lines + exam_lines + fu_lines + pick_lines
        text = fmt.join(f"{greet}\n{sub_head}", "\n".join(body) if body else "Nothing on your calendar today.", tip)
    else:
        text = fmt.join(
            f"{greet}\n{sub_head}",
            fmt.section(fmt.header("📅", "Today"), agenda_lines),
            fmt.section(fmt.header("deadline", "Due soon"), due_lines),
            fmt.section(fmt.header("exam", "Exams this week"), exam_lines),
            fmt.section(fmt.header("person", "Follow ups"), fu_lines),
            fmt.section(fmt.header("event", "Worth your time"), pick_lines),
            tip,
        )
    buttons = list(views.BRIEF_BUTTONS)
    if d.pick:
        buttons = views.event_buttons([d.pick]) + buttons
    v = fmt.view(text, buttons, max_lines=BRIEF_MAX_LINES)
    v.pick = d.pick  # so the sender can mark it suggested
    return v


# ---------- Weekly preview ----------


def week_range(now: datetime) -> tuple[date, date]:
    """The coming Monday to Sunday (on a Sunday, the week starting tomorrow)."""
    start = now.date() + timedelta(days=(7 - now.weekday()) % 7 or 7)
    return start, start + timedelta(days=6)


async def weekly(user_id, now: datetime | None = None) -> View:
    now = now or datetime.now(TZ)
    start, end = week_range(now)
    as_of = datetime.combine(start, time(0, 0), tzinfo=TZ)
    ents = schedule.agenda_entries(user_id, start, end, as_of)
    deadlines = [e for e in ents if e.kind in ("deadline", "exam")]
    lines = await _lines(
        user_id, "weekly preview", "the week", start, [(f"{e.day:%a} {_entry_text(e)}", "") for e in ents if e.kind not in ("deadline", "exam")][:25],
        "; ".join(f"{e.title} ({e.course}) {e.day:%a} {e.time_text}" + (" [LockDown]" if e.note else "") for e in deadlines if e.kind == "deadline"),
        "; ".join(f"{e.title} {e.day:%a} {e.time_text}" for e in deadlines if e.kind == "exam"),
        extra=f"His goals: {selfmodel.profile_summary(user_id, max_words=80)}", priorities=True,
    )
    # Day-by-day highlights, compact: at most 2 per day (exams and deadlines first), only days with
    # something, no blank lines between days so the whole preview stays under 25 lines.
    rank = {"exam": 0, "deadline": 1, "class": 2, "event": 3, "meeting": 4}
    by_day: dict = {}
    for e in ents:
        by_day.setdefault(e.day, []).append(e)
    rows = []
    for day, es in by_day.items():
        top = sorted(sorted(es, key=lambda e: rank[e.kind])[:2], key=lambda e: e.sort)
        rows.append(fmt.day_header(day))
        for e in top:
            title = fmt.short_title(e.title, e.course, limit=36) + (" due" if e.kind in ("deadline", "exam") else "")
            rows.append(fmt.time_line(e.time_text, title, e.kind))
        if len(es) > 2:
            rows.append(f"↳ +{len(es) - 2} more")
    days = ["\n".join(rows)] if rows else []
    prios = [f"{n}. {esc(p)}" for n, p in enumerate(lines["priorities"], 1)]
    text = fmt.join(
        f"🗓 <b>YOUR WEEK</b> · {start:%b} {start.day} to {end:%b} {end.day}",
        *days,
        fmt.section("🎯 <b>TOP 3 PRIORITIES</b>", prios),
        f"💡 {esc(lines['tip'])}" if lines["tip"] else None,
    )
    return fmt.view(text, views.BRIEF_BUTTONS)
