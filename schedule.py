"""Schedule logic: LLM extraction of deadlines and classes, storage, dedupe,
reminders and week view. No channel (Telegram) code here."""

import asyncio
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import llm

log = logging.getLogger("schedule")

TZ = ZoneInfo("America/New_York")
SEMESTER = "Fall 2026"
SEMESTER_START = date(2026, 8, 24)  # generous bounds: anything outside is rejected
SEMESTER_END = date(2027, 1, 15)

DEADLINE_KINDS = {"assignment", "exam", "quiz", "project", "deadline"}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
END_OF_DAY = time(23, 59)

CHUNK_CHARS = 1500  # small chunks: mini models skip items in long, dense lists
CHUNK_OVERLAP = 200
MAX_PARALLEL = 4

DB_PATH = Path(__file__).resolve().parent / "data" / "violet.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        kind TEXT NOT NULL,          -- file, chat, brightspace
        label TEXT,
        created_at TEXT NOT NULL,
        undone INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        batch_id INTEGER REFERENCES batches(id),
        source TEXT NOT NULL,        -- file, chat, brightspace
        kind TEXT NOT NULL,          -- assignment, exam, quiz, project, deadline, class
        course TEXT,
        title TEXT NOT NULL,
        due_date TEXT,               -- YYYY-MM-DD (deadlines)
        due_time TEXT,               -- HH:MM or NULL when no time was given
        weekday INTEGER,             -- 0=Mon (classes)
        start_time TEXT,
        end_time TEXT,
        room TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        remind_at TEXT NOT NULL,     -- UTC ISO
        lead TEXT NOT NULL,          -- week, day, hours
        sent INTEGER NOT NULL DEFAULT 0
    );
    """
)
if "uid" not in {r["name"] for r in _db.execute("PRAGMA table_info(items)")}:
    _db.execute("ALTER TABLE items ADD COLUMN uid TEXT")  # calendar event UID (Google sync)
_db.commit()


# ---------- Extraction ----------

EXTRACT_PROMPT = """You extract schedule items for a student. Semester: {semester}, timezone America/New_York.
Today is {today}.

Context: {context}

Return JSON: {{"items": [ ... ]}}. Each item is one of:

Deadline (assignment, exam, quiz, project milestone, other deadline):
{{"kind": "assignment|exam|quiz|project|deadline", "course": "...", "title": "...",
  "date": "YYYY-MM-DD", "time": "HH:MM" or null, "evidence": "..."}}

Weekly class meeting:
{{"kind": "class", "course": "...", "title": "...", "days": ["monday", ...],
  "start_time": "HH:MM", "end_time": "HH:MM" or null, "room": "..." or null, "evidence": "..."}}

Rules:
- Only include items whose date (month and day, or a numeric date) or weekly day and time
  is written explicitly in the text. Never invent or guess dates. Skip items with only
  vague timing like "week 5", "mid-semester", "TBA" or "later".
- Resolve the year from the semester: Aug to Dec is 2026, Jan is 2027.
- time is 24h HH:MM only if a time is written for that item, else null.
- Midterms and finals are kind "exam". Project proposals, drafts and presentations are "project".
- course: short course name or code. Use the context (file caption or name) when the text
  does not say which course. Empty string if unknown.
- title: short, like "Exam One", "Problem Set 3", "Final Project Proposal".
- evidence: copy, character for character, the single line or sentence from the text that
  contains BOTH the item name and its date (or class days and time). If the name and the
  date are not in the same line or sentence, skip the item.
- List every qualifying item. Calendars and syllabi often list many; do not stop early.
- If nothing qualifies, return {{"items": []}}.

Text:
\"\"\"
{text}
\"\"\""""


@dataclass
class Item:
    kind: str
    title: str
    course: str = ""
    due_date: date | None = None
    due_time: time | None = None
    weekday: int | None = None
    start_time: time | None = None
    end_time: time | None = None
    room: str | None = None
    id: int | None = None
    uid: str | None = None  # calendar event UID, per occurrence for recurring events
    series_uid: str | None = None  # recurring series UID (not stored)


@dataclass
class AddResult:
    batch_id: int | None
    added: list[Item] = field(default_factory=list)
    duplicates: int = 0
    past: int = 0


def chunk_text(text: str) -> list[str]:
    text = text.strip()
    if len(text) <= CHUNK_CHARS:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + CHUNK_CHARS, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + CHUNK_CHARS // 2, end)
            end = cut if cut > 0 else end
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


async def extract(text: str, context: str = "") -> list[Item]:
    """Run LLM extraction over every chunk of text and return validated items."""
    chunks = chunk_text(text)
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def run(chunk: str) -> list[Item]:
        async with sem:
            return await _extract_chunk(chunk, context)

    results = await asyncio.gather(*(run(c) for c in chunks), return_exceptions=True)
    items: list[Item] = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("chunk extraction failed: %s", r)
            continue
        items.extend(r)
    return _dedupe_within(items)


async def _extract_chunk(chunk: str, context: str) -> list[Item]:
    today = datetime.now(TZ).date()
    prompt = EXTRACT_PROMPT.format(
        semester=SEMESTER,
        today=f"{today:%A}, {today.isoformat()}",
        context=context or "none",
        text=chunk,
    )
    data = await llm.chat_json([{"role": "user", "content": prompt}], model_name=llm.extract_model())
    out = []
    for raw in data.get("items") or []:
        out.extend(_validate(raw, chunk))
    return out


_PUNCT = str.maketrans({"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"'})


def _norm_space(s: str) -> str:
    s = str(s or "").translate(_PUNCT)
    s = re.sub(r"\s*-\s*", "-", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _parse_time(s) -> time | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%H:%M").time()
    except ValueError:
        return None


def _title_in(title: str, course: str, evidence: str) -> bool:
    """Most of the title's distinctive words must appear in the quoted evidence."""
    words = [w for w in _norm_title(title, course).split() if len(w) > 1 or w.isdigit()]
    if not words:
        return True
    ev = set(re.findall(r"[a-z0-9]+", evidence))
    return sum(w in ev for w in words) / len(words) >= 0.6


def _date_in(d: date, evidence: str) -> bool:
    """The quoted evidence must state this calendar day."""
    months = {d.strftime("%B").lower(), d.strftime("%b").lower(), d.strftime("%b").lower() + "."}
    if d.month == 9:
        months.add("sept")
    tokens = re.findall(r"[a-z]+\.?|\d+", evidence)
    for i, t in enumerate(tokens):
        if t in months and str(d.day) in (tokens[i + 1 : i + 2] + tokens[max(0, i - 1) : i]):
            return True
    numeric = {f"{d.month}/{d.day}", f"{d.month:02d}/{d.day:02d}", d.isoformat()}
    return any(n in evidence for n in numeric)


def _validate(raw: dict, chunk: str) -> list[Item]:
    """Drop anything the text does not actually support."""
    if not isinstance(raw, dict):
        return []
    kind = str(raw.get("kind", "")).lower().strip()
    title = str(raw.get("title", "")).strip()
    course = str(raw.get("course", "") or "").strip()
    evidence = _norm_space(raw.get("evidence", ""))
    if not title or not evidence or evidence not in _norm_space(chunk):
        return []  # the model must quote the source; otherwise it may have invented it
    if not _title_in(title, course, evidence):
        return []  # the quote must name this item, not a neighbouring one

    if kind == "class":
        start = _parse_time(raw.get("start_time"))
        days = [d for d in (raw.get("days") or []) if str(d).lower() in WEEKDAYS]
        if not start or not days:
            return []
        return [
            Item(
                kind="class",
                title=title,
                course=course,
                weekday=WEEKDAYS.index(str(d).lower()),
                start_time=start,
                end_time=_parse_time(raw.get("end_time")),
                room=(str(raw.get("room")).strip() or None) if raw.get("room") else None,
            )
            for d in dict.fromkeys(days)
        ]

    if kind not in DEADLINE_KINDS:
        return []
    try:
        d = date.fromisoformat(str(raw.get("date")))
    except ValueError:
        return []
    if not (SEMESTER_START <= d <= SEMESTER_END) or not _date_in(d, evidence):
        return []
    return [Item(kind=kind, title=title, course=course, due_date=d, due_time=_parse_time(raw.get("time")))]


# ---------- Dedupe ----------

_STOP = {"the", "a", "an", "of", "for", "and", "due", "submission", "submit"}


def _norm_title(title: str, course: str = "") -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    course_words = set(re.findall(r"[a-z0-9]+", course.lower()))
    return " ".join(w for w in words if w not in _STOP and w not in course_words)


def _norm_course(course: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (course or "").lower())


_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "i": "1", "ii": "2", "iii": "3",
}
_ANCHOR_WORDS = {"exam", "examination", "midterm", "final", "quiz", "proposal", "presentation"}


def _title_tokens(title: str, course: str) -> list[str]:
    return [_NUMBER_WORDS.get(w, w) for w in _norm_title(title, course).split()]


def similar_titles(a: str, b: str, course: str = "") -> bool:
    """Same assignment under slightly different names, e.g. "Exam 1" and
    "Proctoring Enabled: MG-GY 6033 Exam One". Different numbers ("Ch 2" vs "Ch 3")
    or an extra meaningful word ("Ch 2 Assignment" vs "Ch 2 Video Assignment") differ."""
    ta, tb = _title_tokens(a, course), _title_tokens(b, course)
    if not ta or not tb:
        return False
    nums_a = {t for t in ta if t.isdigit() and len(t) < 4}  # ignore years and course codes
    nums_b = {t for t in tb if t.isdigit() and len(t) < 4}
    if nums_a != nums_b:
        return False
    sa, sb = set(ta), set(tb)
    if sa == sb:
        return True
    small, big = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if small <= big and small & _ANCHOR_WORDS:
        return True
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= 0.9


def _courses_match(a: str, b: str) -> bool:
    na, nb = _norm_course(a), _norm_course(b)
    return na == nb or not na or not nb


def same_item(a: Item, b: Item) -> bool:
    kinds = {a.kind, b.kind}
    if kinds == {"class", "event"}:
        # A calendar event at a syllabus class's weekday and start time is that class.
        c, e = (a, b) if a.kind == "class" else (b, a)
        return e.due_time is not None and e.due_date.weekday() == c.weekday and e.due_time == c.start_time
    if (a.kind == "class") != (b.kind == "class"):
        return False
    if not _courses_match(a.course, b.course):
        return False
    if a.kind == "class":
        return a.weekday == b.weekday and a.start_time == b.start_time
    course = a.course or b.course
    return a.due_date == b.due_date and similar_titles(a.title, b.title, course)


def _dedupe_within(items: list[Item]) -> list[Item]:
    out: list[Item] = []
    for it in items:
        match = next((o for o in out if same_item(o, it)), None)
        if match is None:
            out.append(it)
        else:  # keep the richer version
            match.due_time = match.due_time or it.due_time
            match.room = match.room or it.room
            match.end_time = match.end_time or it.end_time
            match.course = match.course or it.course
    return out


# ---------- Storage ----------


def _row_to_item(r: sqlite3.Row) -> Item:
    return Item(
        id=r["id"],
        kind=r["kind"],
        title=r["title"],
        course=r["course"] or "",
        due_date=date.fromisoformat(r["due_date"]) if r["due_date"] else None,
        due_time=_parse_time(r["due_time"]),
        weekday=r["weekday"],
        start_time=_parse_time(r["start_time"]),
        end_time=_parse_time(r["end_time"]),
        room=r["room"],
        uid=r["uid"],
    )


def existing_items(user_id) -> list[Item]:
    rows = _db.execute("SELECT * FROM items WHERE user_id = ?", (str(user_id),)).fetchall()
    return [_row_to_item(r) for r in rows]


def add_items(user_id, items: list[Item], source: str, label: str) -> AddResult:
    """Store new items with reminders. Skips duplicates (any source) and past deadlines."""
    uid = str(user_id)
    now = datetime.now(TZ)
    existing = existing_items(uid)
    result = AddResult(batch_id=None)
    to_add = []
    for it in items:
        if any(same_item(it, e) for e in existing):
            result.duplicates += 1
        elif it.kind != "class" and due_datetime(it) < now:
            result.past += 1
        else:
            to_add.append(it)
    if not to_add:
        return result

    cur = _db.execute(
        "INSERT INTO batches (user_id, kind, label, created_at) VALUES (?, ?, ?, ?)",
        (uid, source, label, datetime.now(timezone.utc).isoformat()),
    )
    result.batch_id = cur.lastrowid
    for it in to_add:
        _insert_item(uid, result.batch_id, source, it, now)
        result.added.append(it)
    _db.commit()
    return result


def _hhmm(t: time | None) -> str | None:
    return t.strftime("%H:%M") if t else None


def _insert_item(uid: str, batch_id, source: str, it: Item, now: datetime) -> None:
    cur = _db.execute(
        """INSERT INTO items (user_id, batch_id, source, kind, course, title, due_date, due_time,
                              weekday, start_time, end_time, room, uid, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uid, batch_id, source, it.kind, it.course, it.title,
            it.due_date.isoformat() if it.due_date else None,
            _hhmm(it.due_time), it.weekday, _hhmm(it.start_time), _hhmm(it.end_time),
            it.room, it.uid, datetime.now(timezone.utc).isoformat(),
        ),
    )
    it.id = cur.lastrowid
    _schedule_reminders(uid, it, now)


# ---------- Calendar feed sync ----------


@dataclass
class SyncResult:
    added: list[Item] = field(default_factory=list)
    updated: int = 0
    removed: int = 0
    duplicates: int = 0


def sync_events(user_id, events: list[Item], source: str) -> SyncResult:
    """Mirror a calendar feed's future events. Matches by UID first; new events that
    match any existing item (same title and day, e.g. a Brightspace deadline) are skipped.
    Future events that disappeared from the feed are removed."""
    uid = str(user_id)
    now = datetime.now(TZ)
    result = SyncResult()
    rows = _db.execute(
        "SELECT * FROM items WHERE user_id = ? AND source = ? AND uid IS NOT NULL", (uid, source)
    ).fetchall()
    by_uid = {r["uid"]: r for r in rows}
    seen: set[str] = set()
    others = existing_items(uid)
    batch_id = None

    for ev in events:
        if ev.uid in seen:
            continue
        seen.add(ev.uid)
        row = by_uid.get(ev.uid)
        if row is not None:
            if (row["title"], row["due_date"], row["due_time"], row["end_time"], row["room"]) != (
                ev.title, ev.due_date.isoformat(), _hhmm(ev.due_time), _hhmm(ev.end_time), ev.room,
            ):
                _db.execute(
                    "UPDATE items SET title=?, due_date=?, due_time=?, end_time=?, room=? WHERE id=?",
                    (ev.title, ev.due_date.isoformat(), _hhmm(ev.due_time), _hhmm(ev.end_time), ev.room, row["id"]),
                )
                _db.execute("DELETE FROM reminders WHERE item_id = ? AND sent = 0", (row["id"],))
                ev.id = row["id"]
                _schedule_reminders(uid, ev, now)
                result.updated += 1
            continue
        if any(same_item(ev, o) for o in others):
            result.duplicates += 1
            continue
        if batch_id is None:
            batch_id = _db.execute(
                "INSERT INTO batches (user_id, kind, label, created_at) VALUES (?, ?, ?, ?)",
                (uid, source, f"{source} sync", datetime.now(timezone.utc).isoformat()),
            ).lastrowid
        _insert_item(uid, batch_id, source, ev, now)
        others.append(ev)
        result.added.append(ev)

    for u, row in by_uid.items():
        if u not in seen and due_datetime(_row_to_item(row)) >= now:
            _db.execute("DELETE FROM reminders WHERE item_id = ?", (row["id"],))
            _db.execute("DELETE FROM items WHERE id = ?", (row["id"],))
            result.removed += 1
    _db.commit()
    return result


def undo_last_file(user_id) -> tuple[str, int] | None:
    """Remove every item added from the user's most recent file. Returns (label, count)."""
    uid = str(user_id)
    batch = _db.execute(
        "SELECT id, label FROM batches WHERE user_id = ? AND kind = 'file' AND undone = 0 "
        "ORDER BY id DESC LIMIT 1",
        (uid,),
    ).fetchone()
    if batch is None:
        return None
    ids = [r["id"] for r in _db.execute("SELECT id FROM items WHERE batch_id = ?", (batch["id"],))]
    _db.executemany("DELETE FROM reminders WHERE item_id = ?", [(i,) for i in ids])
    _db.execute("DELETE FROM items WHERE batch_id = ?", (batch["id"],))
    _db.execute("UPDATE batches SET undone = 1 WHERE id = ?", (batch["id"],))
    _db.commit()
    return batch["label"], len(ids)


# ---------- Reminders ----------


def due_datetime(it: Item) -> datetime:
    return datetime.combine(it.due_date, it.due_time or END_OF_DAY, tzinfo=TZ)


def _schedule_reminders(uid: str, it: Item, now: datetime) -> None:
    if it.kind == "class":
        return
    due = due_datetime(it)
    if it.kind == "event":
        leads = [("hours", timedelta(hours=2))] if it.due_time else []
    else:
        leads = [("day", timedelta(days=1)), ("hours", timedelta(hours=2))]
    if it.kind == "exam":
        leads.insert(0, ("week", timedelta(weeks=1)))
    for lead, delta in leads:
        at = due - delta
        if at > now:
            _db.execute(
                "INSERT INTO reminders (item_id, user_id, remind_at, lead) VALUES (?, ?, ?, ?)",
                (it.id, uid, at.astimezone(timezone.utc).isoformat(), lead),
            )


def pop_due_reminders() -> list[tuple[str, str]]:
    """Return (user_id, message) for every reminder that is due, marking them sent."""
    now = datetime.now(timezone.utc).isoformat()
    rows = _db.execute(
        """SELECT r.id AS rid, r.user_id, r.lead, i.* FROM reminders r
           JOIN items i ON i.id = r.item_id
           WHERE r.sent = 0 AND r.remind_at <= ? ORDER BY r.remind_at""",
        (now,),
    ).fetchall()
    out = []
    for r in rows:
        _db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (r["rid"],))
        it = _row_to_item(r)
        when = {"week": "in 1 week", "day": "tomorrow", "hours": "in 2 hours"}[r["lead"]]
        verb = {"exam": "is", "event": "starts"}.get(it.kind, "is due")
        out.append((r["user_id"], f"⏰ {label(it)} {verb} {when}: {fmt_due(it)}"))
    _db.commit()
    return out


# ---------- Formatting ----------


def fmt_time(t: time) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def fmt_day(d: date) -> str:
    return f"{d:%a} {d:%b} {d.day}"


def fmt_due(it: Item) -> str:
    s = fmt_day(it.due_date)
    return f"{s}, {fmt_time(it.due_time)}" if it.due_time else s


def label(it: Item) -> str:
    return f"{it.title} ({it.course})" if it.course and it.course.lower() not in it.title.lower() else it.title


def fmt_class_group(items: list[Item]) -> list[str]:
    """One line per course/time, e.g. 'OM, Mon and Wed 9:30 AM, KMC 3-120'."""
    groups: dict[tuple, list[Item]] = {}
    for it in items:
        groups.setdefault((it.course or it.title, it.start_time, it.end_time, it.room), []).append(it)
    lines = []
    for (name, start, end, room), its in groups.items():
        days = " and ".join(WEEKDAYS[i.weekday][:3].title() for i in sorted(its, key=lambda i: i.weekday))
        t = fmt_time(start) + (f" to {fmt_time(end)}" if end else "")
        lines.append(f"{name}, {days} {t}" + (f", {room}" if room else ""))
    return lines


def week_view(user_id, days: int = 7) -> str:
    today = datetime.now(TZ).date()
    now = datetime.now(TZ)
    items = existing_items(user_id)
    lines = []
    for n in range(days):
        d = today + timedelta(days=n)
        entries = []  # (sort key, line)
        for i in items:
            if i.kind == "class" and i.weekday == d.weekday():
                room = f", {i.room}" if i.room else ""
                entries.append((i.start_time, f"  {fmt_time(i.start_time)} {i.course or i.title} class{room}"))
            elif i.kind == "event" and i.due_date == d:
                if i.due_time and n == 0 and due_datetime(i) < now:
                    continue
                when = fmt_time(i.due_time) if i.due_time else "All day"
                room = f", {i.room}" if i.room else ""
                entries.append((i.due_time or time(0, 0), f"  {when} {i.title}{room}"))
            elif i.kind not in ("class", "event") and i.due_date == d:
                when = fmt_time(i.due_time) if i.due_time else "any time"
                entries.append((i.due_time or END_OF_DAY, f"  📌 {label(i)}, due {when}"))
        if not entries:
            continue
        lines.append(("Today, " if n == 0 else "") + fmt_day(d))
        lines += [line for _, line in sorted(entries, key=lambda e: e[0])]
    if not lines:
        return "Nothing on your calendar for the next 7 days 💜"
    return "📅 Your next 7 days\n\n" + "\n".join(lines)
