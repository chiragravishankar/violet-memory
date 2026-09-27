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
import paths

log = logging.getLogger("schedule")

TZ = ZoneInfo("America/New_York")
SEMESTER = "Fall 2026"
SEMESTER_START = date(2026, 8, 24)  # generous bounds: anything outside is rejected
SEMESTER_END = date(2027, 1, 15)

DEADLINE_KINDS = {"assignment", "exam", "quiz", "project", "deadline"}
# Timed, non-deadline kinds: "class" is a weekly syllabus meeting (weekday + time),
# "class_session" a dated class occurrence (from a calendar), "event" anything else.
TIMED_KINDS = {"class", "class_session", "event"}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
END_OF_DAY = time(23, 59)

CHUNK_CHARS = 1500  # small chunks: mini models skip items in long, dense lists
CHUNK_OVERLAP = 200
MAX_PARALLEL = 4

DB_PATH = paths.VIOLET_DB
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
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
_cols = {r["name"] for r in _db.execute("PRAGMA table_info(items)")}
if "uid" not in _cols:
    _db.execute("ALTER TABLE items ADD COLUMN uid TEXT")  # calendar feed UID
if "note" not in _cols:
    _db.execute("ALTER TABLE items ADD COLUMN note TEXT")
if "display_title" not in _cols:
    _db.execute("ALTER TABLE items ADD COLUMN display_title TEXT")  # cleaned once, cached
if "done" not in _cols:
    _db.execute("ALTER TABLE items ADD COLUMN done INTEGER NOT NULL DEFAULT 0")
# One row per item, lead and time: re-planning after a restart can never duplicate a reminder.
_db.execute("CREATE UNIQUE INDEX IF NOT EXISTS reminders_once ON reminders (item_id, lead, remind_at)")
_db.commit()


# ---------- Extraction ----------

EXTRACT_PROMPT = """You extract schedule items for a student. Semester: {semester}, timezone America/New_York.
Today is {today}.

Context: {context}
{relative}
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
- time is 24h HH:MM only if a time is written for that item, else null. "Midnight" on a day means
  the end of that day (23:59), "noon" is 12:00.
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
    source: str | None = None  # file, chat, brightspace, google
    note: str | None = None  # e.g. "Needs LockDown Browser"
    display_title: str | None = None  # cleaned-up title for views (the original stays in title)
    done: bool = False

    @property
    def shown(self) -> str:
        return self.display_title or self.title


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


RELATIVE_RULES = """Relative dates are allowed in this message. Resolve them with this calendar
(first line is today): {days}
"this <weekday>" or "next <weekday>" or a bare weekday = the first such day after today.
"tomorrow" = the day after today. "next week <weekday>" = that weekday in the following week.
"""


def _relative_ok(d: date, evidence: str, today: date) -> bool:
    """A relative date in a chat message ("next Friday", "tomorrow") matches the resolved day."""
    ev = evidence.lower()
    delta = (d - today).days
    if "tomorrow" in ev:
        return delta == 1
    if "today" in ev or "tonight" in ev:
        return delta == 0
    day_names = [w for w in WEEKDAYS if w in ev or w[:3] in re.findall(r"[a-z]+", ev)]
    return bool(day_names) and d.weekday() == WEEKDAYS.index(day_names[0]) and 0 < delta <= 14


async def extract(text: str, context: str = "", allow_relative: bool = False) -> list[Item]:
    """Run LLM extraction over every chunk of text and return validated items.

    allow_relative: accept "next Friday" style dates (chat messages), checked against
    the weekday named in the quoted evidence."""
    chunks = chunk_text(text)
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def run(chunk: str) -> list[Item]:
        async with sem:
            return await _extract_chunk(chunk, context, allow_relative)

    results = await asyncio.gather(*(run(c) for c in chunks), return_exceptions=True)
    items: list[Item] = []
    for r in results:
        if isinstance(r, Exception):
            log.warning("chunk extraction failed: %s", r)
            continue
        items.extend(r)
    return _dedupe_within(items)


async def _extract_chunk(chunk: str, context: str, allow_relative: bool = False) -> list[Item]:
    today = datetime.now(TZ).date()
    relative = ""
    if allow_relative:
        days = ", ".join(f"{today + timedelta(days=n):%a %Y-%m-%d}" for n in range(15))
        relative = RELATIVE_RULES.format(days=days)
    prompt = EXTRACT_PROMPT.format(
        semester=SEMESTER,
        today=f"{today:%A}, {today.isoformat()}",
        context=context or "none",
        relative=relative,
        text=chunk,
    )
    data = await llm.chat_json([{"role": "user", "content": prompt}], model_name=llm.extract_model())
    out = []
    for raw in data.get("items") or []:
        out.extend(_validate(raw, chunk, today if allow_relative else None))
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


def _validate(raw: dict, chunk: str, relative_today: date | None = None) -> list[Item]:
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
    if not (SEMESTER_START <= d <= SEMESTER_END):
        return []
    if not _date_in(d, evidence) and not (relative_today and _relative_ok(d, evidence, relative_today)):
        return []
    return [Item(kind=kind, title=title, course=course, due_date=d, due_time=_parse_time(raw.get("time")))]


# ---------- Dedupe ----------

_STOP = {"the", "a", "an", "of", "for", "and", "due", "submission", "submit", "deadline", "date"}


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
    # "Weekly Economist Assignment #4 (Publication Date: September 26th)" is "... #4".
    pa = set(_title_tokens(re.sub(r"\([^)]*\)", " ", a), course))
    pb = set(_title_tokens(re.sub(r"\([^)]*\)", " ", b), course))
    if pa and pa == pb:
        return True
    small, big = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    if small <= big and small & _ANCHOR_WORDS:
        return True
    return SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= 0.9


_COURSE_STOP = {"for", "the", "and", "of", "to", "in", "a", "an", "fall", "spring", "summer", "2026", "2027"}


def _course_tokens(course: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (course or "").lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words if w not in _COURSE_STOP}


def _courses_match(a: str, b: str) -> bool:
    """Same course under different names: "OM" matches nothing but itself, while
    "Financial Analysis for Tech Manager - Foy" matches "Financial Analysis for Tech Managers"."""
    na, nb = _norm_course(a), _norm_course(b)
    if na == nb or not na or not nb or na in nb or nb in na:
        return True
    ta, tb = _course_tokens(a), _course_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.75


def course_of(title: str, known_courses: list[str]) -> str | None:
    """The known course a calendar event title refers to, e.g. "Operation Management - Jacoby"."""
    base = re.split(r"\s+-\s+", title)[0]
    for c in known_courses:
        if c and _course_tokens(base) and _courses_match(base, c) and len(_course_tokens(base)) >= 2:
            return c
    return None


def same_item(a: Item, b: Item) -> bool:
    if (a.kind == "class") != (b.kind == "class"):
        c, e = (a, b) if a.kind == "class" else (b, a)
        if e.kind in ("event", "class_session"):
            # A calendar event at a syllabus class's weekday and start time is that class.
            return e.due_time is not None and e.due_date.weekday() == c.weekday and e.due_time == c.start_time
        return False
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
        source=r["source"],
        note=r["note"],
        display_title=r["display_title"],
        done=bool(r["done"]),
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
                              weekday, start_time, end_time, room, uid, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            uid, batch_id, source, it.kind, it.course, it.title,
            it.due_date.isoformat() if it.due_date else None,
            _hhmm(it.due_time), it.weekday, _hhmm(it.start_time), _hhmm(it.end_time),
            it.room, it.uid, it.note, datetime.now(timezone.utc).isoformat(),
        ),
    )
    it.id = cur.lastrowid
    it.source = source
    _schedule_reminders(uid, it, now)


# ---------- Calendar feed sync ----------


@dataclass
class SyncResult:
    in_feed: int = 0
    added: list[Item] = field(default_factory=list)
    already: int = 0  # matched an item that was already there (PDF, chat or another feed)
    adopted: int = 0  # of those, PDF or chat items now kept up to date by this feed
    corrected: int = 0  # PDF items whose date the feed corrected
    updated: int = 0
    removed: int = 0


_FEED_FIELDS = ("kind", "course", "title", "due_date", "due_time", "end_time", "room", "note")


def _feed_values(it: Item) -> tuple:
    return (
        it.kind, it.course, it.title, it.due_date.isoformat() if it.due_date else None,
        _hhmm(it.due_time), _hhmm(it.end_time), it.room, it.note,
    )


def _take_over(row_id: int, it: Item, source: str, batch_id, uid: str, now: datetime) -> None:
    """Point an existing row at the feed item (same id, so /week and reminders follow)."""
    _db.execute(
        f"UPDATE items SET {', '.join(f + '=?' for f in _FEED_FIELDS)}, source=?, uid=?, batch_id=COALESCE(?, batch_id), "
        "display_title=CASE WHEN title=? THEN display_title ELSE NULL END WHERE id=?",
        (*_feed_values(it), source, it.uid, batch_id, it.title, row_id),
    )
    _db.execute("DELETE FROM reminders WHERE item_id = ? AND sent = 0", (row_id,))
    it.id, it.source = row_id, source
    _schedule_reminders(uid, it, now)


def sync_events(user_id, events: list[Item], source: str) -> SyncResult:
    """Mirror a calendar feed's future items.

    - Known UID: update the row if anything changed.
    - Matches a PDF or chat item (course, similar title, same day): the feed takes it over,
      so there is one row and it stays current. Same course and exact title on another day:
      the PDF date was wrong, the feed corrects it.
    - Matches an item from another feed: keep that one, skip this.
    - Future items that disappeared from the feed are removed.
    """
    uid = str(user_id)
    now = datetime.now(TZ)
    result = SyncResult(in_feed=len(events))
    by_uid = {
        r["uid"]: r for r in _db.execute(
            "SELECT * FROM items WHERE user_id = ? AND source = ? AND uid IS NOT NULL", (uid, source)
        ).fetchall()
    }
    seen: set[str] = set()
    batch_id = None

    def batch() -> int:
        nonlocal batch_id
        if batch_id is None:
            batch_id = _db.execute(
                "INSERT INTO batches (user_id, kind, label, created_at) VALUES (?, ?, ?, ?)",
                (uid, source, f"{source} sync", datetime.now(timezone.utc).isoformat()),
            ).lastrowid
        return batch_id

    for ev in events:
        if ev.uid in seen:
            continue
        seen.add(ev.uid)
        row = by_uid.get(ev.uid)
        if row is not None:
            if tuple(row[f] for f in _FEED_FIELDS) != _feed_values(ev):
                _take_over(row["id"], ev, source, None, uid, now)
                result.updated += 1
            continue

        others = [o for o in existing_items(uid) if o.source != source]
        match = next((o for o in others if same_item(ev, o)), None)
        if match is None and ev.kind in DEADLINE_KINDS:
            match = next(
                (o for o in others if o.source in ("file", "chat") and o.kind in DEADLINE_KINDS
                 and _courses_match(o.course, ev.course)
                 and _title_tokens(o.title, o.course) == _title_tokens(ev.title, ev.course)),
                None,
            )
            if match is not None:
                result.corrected += 1
        if match is not None:
            result.already += 1
            if match.source in ("file", "chat"):
                _take_over(match.id, ev, source, batch(), uid, now)
                result.adopted += 1
            continue
        _insert_item(uid, batch(), source, ev, now)
        result.added.append(ev)

    # PDF or chat copies of items this feed now owns (left over from earlier syncs).
    mine = [i for i in existing_items(uid) if i.source == source]
    for o in existing_items(uid):
        if o.source in ("file", "chat") and any(same_item(o, m) for m in mine):
            _db.execute("DELETE FROM reminders WHERE item_id = ?", (o.id,))
            _db.execute("DELETE FROM items WHERE id = ?", (o.id,))
            result.already += 1

    for u, row in by_uid.items():
        if u not in seen and due_datetime(_row_to_item(row)) >= now:
            _db.execute("DELETE FROM reminders WHERE item_id = ?", (row["id"],))
            _db.execute("DELETE FROM items WHERE id = ?", (row["id"],))
            result.removed += 1
    _db.commit()
    return result


def add_event(user_id, it: Item, source: str) -> bool:
    """Put an event he's going to on his calendar (with reminders). False if already there."""
    uid = str(user_id)
    if _db.execute("SELECT 1 FROM items WHERE user_id = ? AND uid = ?", (uid, it.uid)).fetchone():
        return False
    batch = _db.execute(
        "INSERT INTO batches (user_id, kind, label, created_at) VALUES (?, ?, ?, ?)",
        (uid, source, it.title[:60], datetime.now(timezone.utc).isoformat()),
    ).lastrowid
    _insert_item(uid, batch, source, it, datetime.now(TZ))
    _db.commit()
    return True


FOLLOW_UP = re.compile(r"\b(follow[- ]?up|reach out|email|text|call|ping|reply to|get back to|send .* to)\b", re.I)


def get_item(item_id: int, user_id=None) -> Item | None:
    """One item by id; with user_id, only if it's that user's."""
    if user_id is not None:
        r = _db.execute("SELECT * FROM items WHERE id = ? AND user_id = ?", (item_id, str(user_id))).fetchone()
    else:
        r = _db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return _row_to_item(r) if r else None


def update_due(item_id: int, d: date, t: time | None) -> None:
    """Move an item to a new date/time (e.g. a deadline change announced by email) and re-plan reminders."""
    _db.execute("UPDATE items SET due_date = ?, due_time = ? WHERE id = ?",
                (d.isoformat(), t.strftime("%H:%M") if t else None, item_id))
    _db.execute("DELETE FROM reminders WHERE item_id = ? AND sent = 0", (item_id,))
    it = get_item(item_id)
    if it:
        _schedule_reminders(_db.execute("SELECT user_id FROM items WHERE id = ?", (item_id,)).fetchone()[0], it, datetime.now(TZ))
    _db.commit()


def mark_done_id(item_id: int) -> None:
    _db.execute("UPDATE items SET done = 1 WHERE id = ?", (item_id,))
    _db.execute("DELETE FROM reminders WHERE item_id = ? AND sent = 0", (item_id,))
    _db.commit()


def mark_done(user_id, query: str) -> Item | None:
    """Mark the upcoming deadline or item best matching the query as done (reminders stop)."""
    now = datetime.now(TZ)
    cands = [i for i in existing_items(user_id) if not i.done and i.kind != "class"
             and (i.due_date is None or due_datetime(i) >= now - timedelta(days=1))]
    q = set(re.findall(r"[a-z0-9]+", query.lower())) - {"the", "a", "my", "done", "finished", "with", "i", "submitted"}
    def overlap(i):
        return len(q & set(re.findall(r"[a-z0-9]+", f"{i.title} {i.shown} {i.course}".lower())))
    best = max(cands, key=lambda i: (overlap(i), -(due_datetime(i) - now).total_seconds() if i.due_date else 0), default=None)
    if best is None or overlap(best) == 0:
        return None
    _db.execute("UPDATE items SET done = 1 WHERE id = ?", (best.id,))
    _db.execute("DELETE FROM reminders WHERE item_id = ? AND sent = 0", (best.id,))
    _db.commit()
    best.done = True
    return best


_TITLE_JUNK = re.compile(r"^\s*(\[[^\]]{1,12}\]\s*)+|\s*-\s*Due\s*$|^Proctoring Enabled:\s*", re.I)
_TITLE_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D]")

TITLES_TASK = """Clean up calendar titles for a student's phone. Keep meaning and names; make them short
(at most 6 words, 40 characters) and natural. Drop tags like [OOF], course codes, "Due", filler like
"How to" when the rest is clear, and the student's own name ({owner}). Examples:
"30 Min Meeting between Jordan Lee and {owner}" -> "Meeting with Jordan Lee"
"How to Design Goods and Services That Fit the Operations Strategy" -> "Designing Goods and Services"
"Weekly Economist Assignment #4 (Publication Date: September 26th)" -> "Economist Assignment #4"
Keep numbers that tell items apart ("Ch 2", "#4"). If a title is already short and clear, return it as is.
Return JSON: {{"titles": {{"<id>": "<clean title>"}}}}

Titles:
{titles}"""


async def normalize_titles(user_id=None, owner: str | None = None) -> int:
    """Clean titles that have no display title yet: deterministic cleanup, then one fast-model
    call for the long or awkward ones. Cached in display_title; the original title is kept."""
    import llm

    q = "SELECT id, title, user_id FROM items WHERE display_title IS NULL" + (" AND user_id = ?" if user_id else "")
    rows = _db.execute(q, (str(user_id),) if user_id else ()).fetchall()
    if not rows:
        return 0
    cleaned, ask = {}, {}
    for r in rows:
        t = re.sub(r"\s+", " ", _TITLE_EMOJI.sub("", _TITLE_JUNK.sub("", r["title"]))).strip(" -,")
        cleaned[r["id"]] = t or r["title"]
        if len(t) > 40 or re.search(r"\bbetween\b|\bmin meeting\b|^how to\b|\(publication|\bfall 20\d\d\b", t, re.I):
            ask[r["id"]] = t
    for i in range(0, len(ask), 40):
        chunk = dict(list(ask.items())[i:i + 40])
        try:
            data = await llm.chat_json([{"role": "user", "content": TITLES_TASK.format(
                owner=owner or "the student", titles="\n".join(f"{k}: {v}" for k, v in chunk.items()))}])
            for k, v in (data.get("titles") or {}).items():
                v = re.sub(r"\s+", " ", str(v)).strip().replace("\u2014", ", ")
                if k.isdigit() and int(k) in chunk and 0 < len(v) <= 60:
                    cleaned[int(k)] = v
        except Exception as e:
            logging.getLogger("schedule").warning("title cleanup failed (%s)", type(e).__name__)
    _db.executemany("UPDATE items SET display_title = ? WHERE id = ?", [(v, k) for k, v in cleaned.items()])
    _db.commit()
    return len(cleaned)


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


DEADLINE_LEADS = [("3d", timedelta(days=3)), ("1d", timedelta(days=1)), ("3h", timedelta(hours=3))]
EXAM_LEADS = [("1w", timedelta(weeks=1))] + DEADLINE_LEADS
CLASS_LEAD = ("30m", timedelta(minutes=30))
EVENT_LEAD = ("1h", timedelta(hours=1))
# Events he said "going" to from discovery (source "nyu_events", later "luma", ...): 1 day and 2 hours.
RSVP_LEADS = [("1d", timedelta(days=1)), ("2h", timedelta(hours=2))]
APPROVAL_LEAD = ("approval", timedelta(days=2))  # check the host approved his registration
CALENDAR_SOURCES = {"file", "chat", "brightspace", "google", "email"}
STALE_AFTER = timedelta(hours=1)  # a reminder missed by more than this (bot was off) is dropped
LEAD_WORDS = {
    "1w": "in 1 week", "3d": "in 3 days", "1d": "tomorrow", "3h": "in 3 hours",
    "1h": "in 1 hour", "30m": "in 30 minutes", "2h": "in 2 hours", "approval": "in 2 days",
}


def reminder_plan(it: Item, start: datetime, end: datetime) -> list[tuple[str, datetime]]:
    """(lead, time) for every reminder of this item that falls in [start, end).

    Assignments: 3 days, 1 day, 3 hours before (exams also 1 week). Classes: 30 minutes
    before each meeting. Other timed events: 1 hour before. All-day events: none.
    """
    if it.kind == "class":  # weekly: one reminder per meeting
        out, d = [], (start + CLASS_LEAD[1]).date()
        while d <= min((end + CLASS_LEAD[1]).date(), SEMESTER_END):
            if d.weekday() == it.weekday and it.start_time:
                at = datetime.combine(d, it.start_time, tzinfo=TZ) - CLASS_LEAD[1]
                if start <= at < end:
                    out.append((CLASS_LEAD[0], at))
            d += timedelta(days=1)
        return out
    if it.kind in ("class_session", "event"):
        if it.due_time is None:
            return []
        if it.kind == "class_session":
            leads = [CLASS_LEAD]
        elif it.source and it.source not in CALENDAR_SOURCES:
            leads = RSVP_LEADS  # an event he chose to go to
            if it.note and "[needs approval]" in it.note:
                leads = [APPROVAL_LEAD] + leads
        else:
            leads = [EVENT_LEAD]
    else:
        leads = EXAM_LEADS if it.kind == "exam" else DEADLINE_LEADS
    due = due_datetime(it)
    return [(lead, due - delta) for lead, delta in leads if start <= due - delta < end]


def _schedule_reminders(uid: str, it: Item, now: datetime) -> None:
    horizon = now + (timedelta(days=2) if it.kind == "class" else timedelta(days=400))
    for lead, at in reminder_plan(it, now, horizon):
        _db.execute(
            "INSERT OR IGNORE INTO reminders (item_id, user_id, remind_at, lead) VALUES (?, ?, ?, ?)",
            (it.id, uid, at.astimezone(timezone.utc).isoformat(), lead),
        )


def plan_all_reminders(now: datetime | None = None) -> None:
    """Re-plan unsent reminders for every item with the current rules. Safe to run on
    every start and loop: sent reminders are kept, so nothing is ever sent twice.
    Weekly classes get their next two days of meetings planned."""
    now = now or datetime.now(TZ)
    _db.execute("DELETE FROM reminders WHERE sent = 0")
    for r in _db.execute("SELECT * FROM items").fetchall():
        _schedule_reminders(r["user_id"], _row_to_item(r), now)
    _db.commit()


def ensure_class_reminders(now: datetime | None = None) -> None:
    """Plan the next two days of weekly class meetings (their reminders roll forward)."""
    now = now or datetime.now(TZ)
    for r in _db.execute("SELECT * FROM items WHERE kind = 'class'").fetchall():
        _schedule_reminders(r["user_id"], _row_to_item(r), now)
    _db.commit()


NON_URGENT_LEADS = {"1w", "3d", "1d"}  # these wait out quiet hours; 3h, 1h and 30m do not


def pop_due_reminders(now: datetime | None = None, quiet_until=None) -> list[tuple[str, Item, str]]:
    """(user_id, item, lead) for every reminder that is due. Each is marked sent before
    it is returned, so a crash or restart can never send it twice. Reminders missed
    while the bot was off for more than an hour are dropped rather than sent late.

    quiet_until(user_id, now) -> datetime | None: when given, non-urgent reminders that fall
    in the user's quiet hours are moved to the end of them instead of being sent."""
    now = now or datetime.now(TZ)
    rows = _db.execute(
        """SELECT r.id AS rid, r.user_id AS ruser, r.lead, r.remind_at, i.* FROM reminders r
           JOIN items i ON i.id = r.item_id
           WHERE r.sent = 0 AND r.remind_at <= ? ORDER BY r.remind_at""",
        (now.astimezone(timezone.utc).isoformat(),),
    ).fetchall()
    out = []
    for r in rows:
        if quiet_until and r["lead"] in NON_URGENT_LEADS and now - datetime.fromisoformat(r["remind_at"]) <= STALE_AFTER:
            until = quiet_until(r["ruser"], now)
            if until:
                try:
                    _db.execute("UPDATE reminders SET remind_at = ? WHERE id = ?",
                                (until.astimezone(timezone.utc).isoformat(), r["rid"]))
                    _db.commit()
                    continue
                except sqlite3.IntegrityError:
                    pass  # an identical reminder already waits there: send this one now
        _db.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (r["rid"],))
        _db.commit()
        if now - datetime.fromisoformat(r["remind_at"]) > STALE_AFTER:
            continue
        out.append((r["ruser"], _row_to_item(r), r["lead"]))
    return out


def preview_reminders(user_id, at: datetime, window: timedelta = timedelta(minutes=5)) -> list[tuple[Item, str]]:
    """Reminders that would fire in (at - window, at], computed from the rules. Nothing is stored."""
    out = []
    for it in existing_items(user_id):
        for lead, when in reminder_plan(it, at - window + timedelta(seconds=1), at + timedelta(seconds=1)):
            out.append((it, lead))
    return out


def reminder_text(it: Item, lead: str) -> str:
    when = LEAD_WORDS[lead]
    lockdown = " 🔒 Needs LockDown Browser." if it.note and "LockDown" in it.note else ""
    if lead == "approval":
        return f"⏰ {it.title} is {when} ({fmt_due(it)}) and needs host approval. Check whether you got approved."
    if it.kind in ("class", "class_session"):
        room = f" at {it.room}" if it.room else ""
        return f"🎓 {it.title} starts {when}{room}."
    if it.kind == "event":
        room = f", {it.room}" if it.room else ""
        return f"⏰ {it.title} starts {when}: {fmt_time(it.due_time)}{room}"
    verb = "is" if it.kind == "exam" else "is due"
    return f"⏰ {label(it)} {verb} {when}: {fmt_due(it)}.{lockdown}"


def upcoming_for_course(user_id, course: str, after: datetime, limit: int = 3) -> list[Item]:
    """The next few deadlines for this course after the given time, soonest first."""
    items = [
        i for i in existing_items(user_id)
        if i.kind in DEADLINE_KINDS and i.course and course and _courses_match(i.course, course)
        and due_datetime(i) > after
    ]
    return sorted(items, key=due_datetime)[:limit]


def next_due_for_course(user_id, course: str, after: datetime) -> Item | None:
    """The next deadline for this course after the given time."""
    items = [
        i for i in existing_items(user_id)
        if i.kind in DEADLINE_KINDS and i.course and course and _courses_match(i.course, course)
        and due_datetime(i) > after
    ]
    return min(items, key=due_datetime, default=None)


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


@dataclass
class Entry:
    """One line of the agenda, ready for any view."""
    day: date
    sort: time
    kind: str  # class, deadline, exam, meeting, event
    time_text: str  # "2:00 PM", "All day", "any time"
    title: str
    course: str = ""
    place: str | None = None
    who: str | None = None  # e.g. "Prof. Jacoby"
    note: str | None = None  # e.g. "Needs LockDown Browser"
    item: "Item | None" = None


_PROF_SUFFIX = re.compile(r"^(.*?)\s+-\s+([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+)?)$")


def split_class_title(title: str) -> tuple[str, str | None]:
    """"Operation Management - Jacoby" -> ("Operation Management", "Prof. Jacoby")."""
    m = _PROF_SUFFIX.match(title.strip())
    return (m.group(1), f"Prof. {m.group(2)}") if m else (title, None)


def agenda_entries(user_id, start: date, end: date, now: datetime | None = None,
                   kinds: set[str] | None = None) -> list[Entry]:
    """Everything from start to end (both days included, the last up to 11:59 PM), in day and
    time order: weekly classes, class sessions, meetings, events he's going to, deadlines and exams,
    from every source. /today, /week, schedule questions and the brief all use this."""
    now = now or datetime.now(TZ)
    items = [i for i in existing_items(user_id) if not i.done and (kinds is None or i.kind in kinds)]
    out: list[Entry] = []
    d = start
    while d <= end:
        day = []
        for i in items:
            if i.kind == "class" and i.weekday == d.weekday() and d <= SEMESTER_END:
                day.append(Entry(d, i.start_time, "class", fmt_time(i.start_time), i.course or i.title,
                                 course=i.course, place=i.room, item=i))
            elif i.kind in ("event", "class_session") and i.due_date == d:
                if i.due_time and d == now.date() and due_datetime(i) < now:
                    continue  # already over today
                when = fmt_time(i.due_time) if i.due_time else "All day"
                if i.kind == "class_session":
                    title, who = split_class_title(i.title)
                    title = i.display_title or title
                    day.append(Entry(d, i.due_time or time(0, 0), "class", when, title, course=i.course,
                                     place=i.room, who=who, item=i))
                else:
                    kind = "event" if i.source and i.source not in CALENDAR_SOURCES else "meeting"
                    day.append(Entry(d, i.due_time or time(0, 0), kind, when, i.shown, place=i.room, item=i))
            elif i.kind in DEADLINE_KINDS and i.due_date == d:
                when = fmt_time(i.due_time) if i.due_time else "any time"
                note = "Needs LockDown Browser" if i.note and "LockDown" in i.note else None
                day.append(Entry(d, i.due_time or END_OF_DAY, "exam" if i.kind == "exam" else "deadline", when,
                                 i.shown, course=i.course, note=note, item=i))
        out += sorted(day, key=lambda e: e.sort)
        d += timedelta(days=1)
    return out


_TEXT_ICON = {"class": "🎓 ", "deadline": "📌 ", "exam": "📌 ", "meeting": "", "event": ""}


def agenda_lines(user_id, start: date, end: date, now: datetime | None = None,
                 kinds: set[str] | None = None) -> list[str]:
    """Plain-text agenda (for the model to read), built from agenda_entries."""
    now = now or datetime.now(TZ)
    lines, last_day = [], None
    for e in agenda_entries(user_id, start, end, now, kinds):
        if e.day != last_day:
            lines.append(("Today, " if e.day == now.date() else "") + fmt_day(e.day))
            last_day = e.day
        if e.kind in ("deadline", "exam"):
            lock = " 🔒" if e.note else ""
            name = f"{e.title} ({e.course})" if e.course and e.course.lower() not in e.title.lower() else e.title
            lines.append(f"  📌 {name}, due {e.time_text}{lock}")
        else:
            where = f", {e.place}" if e.place else ""
            what = f"{e.title} class" if e.kind == "class" and e.item and e.item.kind == "class" else e.title
            lines.append(f"  {e.time_text} {_TEXT_ICON[e.kind]}{what}{where}")
    return lines


def week_view(user_id, days: int = 7, now: datetime | None = None) -> str:
    now = now or datetime.now(TZ)
    lines = agenda_lines(user_id, now.date(), now.date() + timedelta(days=days - 1), now)
    if not lines:
        return f"Nothing on the calendar for the next {days} days."
    return f"📅 Your next {days} days\n\n" + "\n".join(lines)


# ---------- Date ranges and kinds in questions ----------

CLASS_KINDS = {"class", "class_session"}
_KIND_WORDS = [
    (re.compile(r"\b(due|deadlines?|assignments?|homework|hw|exams?|quiz(zes)?|submit|submissions?)\b", re.I), DEADLINE_KINDS),
    (re.compile(r"\b(meetings?|events?|calls?)\b", re.I), {"event"}),
    (re.compile(r"\b(class|classes|lectures?|sessions?)\b", re.I), CLASS_KINDS),
]


def parse_kinds(text: str) -> set[str] | None:
    """Which kinds of item a question is about ("what's due" -> deadlines), or None for all."""
    kinds: set[str] = set()
    for pattern, ks in _KIND_WORDS:
        if pattern.search(text):
            kinds |= ks
    return kinds or None

_DAY_WORDS = {
    **{w: i for i, w in enumerate(WEEKDAYS)},
    **{w[:3]: i for i, w in enumerate(WEEKDAYS[:5])},  # not "sat"/"sun": ordinary words
    "tues": 1, "weds": 2, "thur": 3, "thurs": 3,
}


def parse_range(text: str, today: date) -> tuple[date, date, str] | None:
    """(start, end, label) for the period a question asks about, in America/New_York.

    today, tomorrow, a weekday (the next one, or today if it is that day), this week
    (today through Sunday), next week (next Monday through Sunday), this weekend,
    next N days (today plus the following N-1 days, like /week). None if no period is named."""
    t = text.lower()
    words = re.findall(r"[a-z]+|\d+", t)
    m = re.search(r"next\s+(\d+)\s+days?", t)
    if m:
        n = max(1, min(int(m.group(1)), 60))
        return today, today + timedelta(days=n - 1), f"the next {n} days"
    if "next week" in t:
        start = today + timedelta(days=7 - today.weekday())
        return start, start + timedelta(days=6), "next week"
    if "weekend" in t:
        sat = today + timedelta(days=(5 - today.weekday()) % 7)
        if today.weekday() == 6:
            sat = today - timedelta(days=1)
        return max(sat, today), sat + timedelta(days=1), "this weekend"
    if "this week" in t or "rest of the week" in t:
        return today, today + timedelta(days=6 - today.weekday()), "this week"
    if "tomorrow" in words:
        d = today + timedelta(days=1)
        return d, d, "tomorrow"
    for w in words:
        if w in _DAY_WORDS:
            d = today + timedelta(days=(_DAY_WORDS[w] - today.weekday()) % 7)
            return d, d, f"{d:%A}"
    if "today" in words or "tonight" in words:
        return today, today, "today"
    return None


def next_reminder_at(kinds: tuple[str, ...] = ("class", "class_session")) -> datetime | None:
    """When the next unsent reminder for these item kinds fires (used to wake memory ahead of it)."""
    q = ",".join("?" * len(kinds))
    row = _db.execute(f"SELECT MIN(r.remind_at) FROM reminders r JOIN items i ON i.id = r.item_id "
                      f"WHERE r.sent = 0 AND i.kind IN ({q})", kinds).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def remove_item(user_id, item_id: int) -> bool:
    """Delete one of the user's items and its reminders (undoing something added from an email)."""
    cur = _db.execute("DELETE FROM items WHERE id = ? AND user_id = ?", (item_id, str(user_id)))
    if cur.rowcount:
        _db.execute("DELETE FROM reminders WHERE item_id = ?", (item_id,))
    _db.commit()
    return cur.rowcount > 0
