"""Event discovery: storage, relevance scoring, calendar conflicts, suggestion timing and the
user's responses. Sources live in eventsources.py / nyu_events.py. No channel code here.

Events are stored apart from the user's schedule; an event only joins the schedule when the user
says "going".
"""

import asyncio
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import brain
import eventsources
import llm
import schedule
import selfmodel
import voice
from eventsources import Event, lead_days
from schedule import TZ

log = logging.getLogger("events")

SUGGEST_MIN = 70  # daily suggestions
LATE_MIN = 90  # found inside or after its window: suggest once, only if this good
LIST_MIN = 50  # /events and questions
PICKS_PER_DAY = 3
DELIVERY_TIME = time(9, 0)
DELIVERY_UNTIL = time(11, 0)  # if the bot was off all morning, skip the day rather than ping at 4 PM
SCORE_BATCH = 30
SCORE_PARALLEL = 5

DB_PATH = Path(__file__).resolve().parent / "data" / "violet.db"
_db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
_db.row_factory = sqlite3.Row
_db.execute("PRAGMA journal_mode=WAL")  # several modules share this file
_db.executescript(
    """
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,           -- "<source>:<source id>"
        source TEXT NOT NULL,
        title TEXT NOT NULL,
        start TEXT NOT NULL,           -- ISO, America/New_York
        end TEXT,
        all_day INTEGER NOT NULL DEFAULT 0,
        location TEXT,
        online INTEGER NOT NULL DEFAULT 0,
        organizer TEXT,
        tags TEXT,                     -- JSON list
        description TEXT,
        url TEXT,
        reg_deadline TEXT,             -- YYYY-MM-DD if the source states one
        canceled INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS event_scores (
        user_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        score INTEGER NOT NULL,
        reason TEXT,
        reg_deadline TEXT,             -- found in the text by the scorer, checked against it
        scored_at TEXT NOT NULL,
        PRIMARY KEY (user_id, event_id)
    );
    CREATE TABLE IF NOT EXISTS event_state (
        user_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        status TEXT NOT NULL,          -- suggested, going, hidden, later
        later_on TEXT,                 -- day to re-suggest ("remind me later")
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, event_id)
    );
    CREATE TABLE IF NOT EXISTS event_prefs (
        user_id TEXT NOT NULL,
        kind TEXT NOT NULL,            -- like, dislike
        text TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS event_picks (
        user_id TEXT NOT NULL,
        day TEXT NOT NULL,
        demo INTEGER NOT NULL DEFAULT 0,
        event_ids TEXT NOT NULL,       -- JSON list, best first
        message TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, day, demo)
    );
    """
)
_ev_cols = {r["name"] for r in _db.execute("PRAGMA table_info(events)")}
if "flags" not in _ev_cols:
    _db.execute("ALTER TABLE events ADD COLUMN flags TEXT")  # JSON: needs_approval, waitlist, paid, hackathon
if "price" not in _ev_cols:
    _db.execute("ALTER TABLE events ADD COLUMN price TEXT")
_db.commit()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- Storage ----------


def _row_event(r) -> Event:
    return Event(
        id=r["id"], source=r["source"], title=r["title"],
        start=datetime.fromisoformat(r["start"]), end=datetime.fromisoformat(r["end"]) if r["end"] else None,
        all_day=bool(r["all_day"]), location=r["location"], online=bool(r["online"]), organizer=r["organizer"],
        tags=json.loads(r["tags"] or "[]"), description=r["description"] or "", url=r["url"],
        registration_deadline=r["reg_deadline"], canceled=bool(r["canceled"]),
        flags=json.loads(r["flags"] or "[]"), price=r["price"],
    )


def get_event(event_id: str) -> Event | None:
    r = _db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_event(r) if r else None


def first_seen(event_id: str) -> datetime:
    r = _db.execute("SELECT first_seen FROM events WHERE id = ?", (event_id,)).fetchone()
    return datetime.fromisoformat(r[0]).astimezone(TZ)


def upsert(evs: list[Event], now: datetime) -> list[str]:
    """Store fetched events (deduped by id). Returns the ids that are new."""
    new = []
    seen_at = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    for e in evs:
        vals = (e.source, e.title, e.start.isoformat(), e.end.isoformat() if e.end else None, int(e.all_day),
                e.location, int(e.online), e.organizer, json.dumps(e.tags), e.description, e.url,
                e.registration_deadline, int(e.canceled), json.dumps(e.flags), e.price)
        cur = _db.execute(
            """UPDATE events SET source=?, title=?, start=?, end=?, all_day=?, location=?, online=?, organizer=?,
               tags=?, description=?, url=?, reg_deadline=COALESCE(?, reg_deadline), canceled=?, flags=?, price=?,
               last_seen=? WHERE id=?""",
            (*vals, seen_at, e.id),
        )
        if cur.rowcount == 0:
            _db.execute(
                """INSERT INTO events (source, title, start, end, all_day, location, online, organizer, tags,
                   description, url, reg_deadline, canceled, flags, price, first_seen, last_seen, id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*vals, seen_at, seen_at, e.id),
            )
            new.append(e.id)
    _db.commit()
    return new


async def refresh(now: datetime | None = None) -> dict:
    """Fetch every source once and store what's new. Errors are reported by type only."""
    now = now or datetime.now(TZ)
    report = {}
    async with eventsources.client() as c:
        for src in eventsources.sources():
            try:
                evs = await src.fetch(c, now)
            except Exception as e:
                log.warning("%s fetch failed (%s)", src.name, type(e).__name__)
                report[src.name] = {"error": type(e).__name__}
                continue
            new = upsert(evs, now)
            report[src.name] = {"fetched": len(evs), "new": len(new), "feeds": getattr(src, "last_counts", {})}
            log.info("%s: %d events, %d new", src.name, len(evs), len(new))
    return report


def upcoming(start: datetime, end: datetime) -> list[Event]:
    rows = _db.execute(
        "SELECT * FROM events WHERE canceled = 0 AND start < ? AND COALESCE(end, start) >= ? ORDER BY start",
        (end.isoformat(), start.isoformat()),
    ).fetchall()
    # ISO strings with the same offset compare correctly; re-check in Python across DST changes.
    return [e for e in map(_row_event, rows) if e.start < end and (e.end or e.start) >= start]


# ---------- Scoring ----------

SCORE_PROMPT = """Score NYU events for one student, 0 to 100, by how worth his time each is.

What he has told you about himself (the only facts about him you may use):
{profile}
He liked events like: {likes}
He said "not for me" to: {dislikes}

Calibration:
- 90 to 100: a direct hit on something specific he's doing: one of his named projects (say which),
  his courses, founders/startups/VC events, Leslie eLab or Entrepreneurial Institute programs,
  employer recruiting in his target fields (VC, fintech, AI/ML, Web3, banks, hedge funds).
- 70 to 89: clearly useful for a specific goal (say which), e.g. a PM or growth talk, a startup
  workshop, a hackathon in his areas, a Tandon or Stern event on his topics.
- 40 to 60: generic career or skills workshops (resume, LinkedIn, interviews, soft skills),
  general networking, fairs outside his fields.
- 0 to 30: no specific link to him (athletics, arts, library trainings, religious or cultural
  club meetings, other programs' events), faculty or staff only, undergrad only, alumni only,
  other campuses (Abu Dhabi, Shanghai), canceled.
- "Internship search" is a reason only for recruiting events with employers or career fairs in his
  fields, never for generic workshops or club meetings.
- A title starting with "DEADLINE:" is a sign-up deadline for a program; judge the program.
- Boost in-person events in Brooklyn or Manhattan, Tandon, Stern, Wasserman, I-Hub, Leslie eLab.

reason: one short line to him, always "you"/"your" (never his name or "his"). It must say what THIS
event actually is (a dinner, a panel, a workshop, a hackathon...) and which specific thing of his it
fits: a named project (Violet Memory, ScraperKast), a goal (VC, fintech or AI internships, founders),
a course, or the Leslie eLab credits proposal. Write each reason fresh for its event; never reuse
wording from another event. Don't start with "Right up". No specific reason: score
30 or less and say it's generic. Never invent facts about him.
deadline: the registration, RSVP or application deadline as YYYY-MM-DD only if the text states a
date for it, else null. Today is {today}.

Score every event listed. Return JSON: {{"scores": [{{"id": "...", "score": 0-100, "reason": "...", "deadline": null}}]}}

Events:
{events}"""

_BOOST = re.compile(r"entrepreneurial institute|leslie|wasserman|tandon|engineering|stern|berkley|\bi-?hub\b", re.I)
_OTHER_CAMPUS = re.compile(r"shanghai|abu dhabi", re.I)
_BUILDER = re.compile(r"\b(agent builders?|ai builders?|builders night|hackathon|founders?|vcs?|venture|investors?|demo night|demo day|pitch night)\b", re.I)
_UNDERGRAD_ONLY = re.compile(r"undergrad(uate)?s? only|for undergraduate students only|first-year students only", re.I)


_OFF_TOPIC = re.compile(
    r"\b(fitness|workout|run club|running club|yoga|pilates|meditation|breathwork|sound bath|dating|singles|"
    r"speed dating|crafts?|knitting|pottery|ceramics|candle|painting class|paint and sip|fashion|runway|"
    r"art opening|gallery opening|exhibition opening|wine tasting|whisky|whiskey|cocktail|brunch|happy hour|"
    r"dance party|rooftop party|day party|club night|dj set|comedy show|festival|fest)\b", re.I)
_ON_TOPIC = re.compile(
    r"\b(ai|a\.i\.|artificial intelligence|llms?|agents?|agentic|machine learning|startups?|founders?|"
    r"vc|venture|investors?|fintech|web3|crypto|blockchain|healthtech|health tech|biotech|zk|product|"
    r"growth|marketing|nyu|hackathon|demo night|demo day|pitch|builders?|developers?|engineers?)\b", re.I)


def prefilter(e: Event) -> str | None:
    """Why a broad-feed event is an obvious mismatch (skip the model), or None to score it."""
    blob = f"{e.title} {e.organizer or ''} {e.description}"
    m = _OFF_TOPIC.search(blob)
    if m and not _ON_TOPIC.search(blob):
        return m.group(0).lower()
    return None


def _event_line(e: Event) -> str:
    when = f"{e.start:%a %b %d}" + ("" if e.all_day else f" {schedule.fmt_time(e.start.time())}")
    where = "online" if e.online else (e.location or "?")
    return (f"id={e.id} | {e.title} | {when} | {where} | by {e.organizer or '?'} | "
            f"tags: {', '.join(e.tags[:10])}" + (f" | {e.price}" if e.price else "") + f" | {e.description[:300]}")


BOOST_CEILING = 89  # nudges can't lift an event into the 90+ tier; only the model can call a must-see
MAX_LATE_PER_DAY = 1


def _adjust(e: Event, score: int) -> int:
    """Deterministic nudges on top of the model's score (only for events it found some fit in).
    Penalties apply fully; boosts never push past BOOST_CEILING."""
    model_score = score
    blob = " ".join([e.title, e.organizer or "", e.location or "", " ".join(e.tags)])
    types = {t.lower() for t in e.tags}
    if _OTHER_CAMPUS.search(blob):
        score -= 30
    if ("faculty" in types or "alumni" in types) and not ({"students", "open to the public"} & types):
        score -= 25
    if _UNDERGRAD_ONLY.search(e.description + " " + e.title):
        score -= 25
    if score >= 40 and (_BOOST.search(blob) or "ihub" in types):
        score += 8
    if score >= 40 and (_BUILDER.search(f"{e.title} {e.description}") or "hackathon" in e.flags):
        score += 6
    if score >= 40 and e.organizer and re.search(r"\bNYU\b|New York University", e.organizer):
        score += 5
    if score >= 40 and not e.online and e.location and re.search(r"brooklyn|manhattan|new york, ny", e.location, re.I):
        score += 3
    if score > model_score:
        score = max(model_score, min(score, BOOST_CEILING))
    return max(0, min(100, score))


def _prefs(user_id, kind: str) -> list[str]:
    return [r[0] for r in _db.execute(
        "SELECT text FROM event_prefs WHERE user_id = ? AND kind = ? ORDER BY created_at DESC LIMIT 12", (str(user_id), kind))]


_KINDS_IN_REASON = {
    "hackathon": r"hack", "dinner": r"dinner|supper|meal", "panel": r"panel", "workshop": r"workshop|class|training",
    "fair": r"fair|expo", "conference": r"conference|summit|forum|\.local",
}


def _reason_fits(reason: str, e: Event) -> bool:
    """Catch reasons written for a different event: calling a dinner a hackathon, say."""
    text = f"{e.title} {e.description}".lower()
    r = reason.lower()
    if "home turf" in r and "hack" not in text:
        return False
    for word, pattern in _KINDS_IN_REASON.items():
        if re.search(rf"\b{word}s?\b", r) and not re.search(pattern, text):
            return False
    return True


async def _score_batch(user_id, batch: list[Event], profile: str, likes: str, dislikes: str) -> int:
    prompt = SCORE_PROMPT.format(
        profile=profile, likes=likes, dislikes=dislikes, today=datetime.now(TZ).date().isoformat(),
        events="\n".join(_event_line(e) for e in batch),
    )
    data = await llm.chat_json([{"role": "user", "content": prompt}], model_name=voice.voice_model())
    by_id = {e.id: e for e in batch}
    n = 0
    for s in data.get("scores") or []:
        e = by_id.get(str(s.get("id")))
        if not e:
            continue
        try:
            score = _adjust(e, int(s.get("score", 0)))
        except (TypeError, ValueError):
            continue
        deadline = s.get("deadline")
        if deadline:
            try:
                d = date.fromisoformat(str(deadline))
                # Only keep a deadline the text actually states, and only before the event.
                deadline = d.isoformat() if schedule._date_in(d, (e.description + " " + e.title).lower()) and d <= e.start.date() else None
            except ValueError:
                deadline = None
        reason = voice.polish(str(s.get("reason") or ""), 0)
        if not _reason_fits(reason, e):
            reason, score = "Loosely related to your interests.", min(score, 60)
        _db.execute(
            "INSERT OR REPLACE INTO event_scores (user_id, event_id, score, reason, reg_deadline, scored_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(user_id), e.id, score, reason, deadline or None, _utcnow()),
        )
        n += 1
    _db.commit()
    return n


async def score_new(user_id, now: datetime | None = None) -> int:
    """Score every upcoming event this user has no score for yet (scores are cached)."""
    now = now or datetime.now(TZ)
    evs = [e for e in upcoming(now, now + timedelta(days=eventsources.HORIZON_DAYS))
           if not _db.execute("SELECT 1 FROM event_scores WHERE user_id = ? AND event_id = ?", (str(user_id), e.id)).fetchone()]
    kept = []
    dropped: dict[str, int] = {}
    for e in evs:
        why = prefilter(e) if e.source in eventsources.PREFILTER_SOURCES else None
        if why:
            # Cache the verdict so it's never sent to the model.
            _db.execute(
                "INSERT OR REPLACE INTO event_scores (user_id, event_id, score, reason, reg_deadline, scored_at) VALUES (?, ?, 0, ?, NULL, ?)",
                (str(user_id), e.id, f"filtered out ({why}, no tech or startup angle)", _utcnow()),
            )
            dropped[e.source] = dropped.get(e.source, 0) + 1
        else:
            kept.append(e)
    _db.commit()
    for src in eventsources.PREFILTER_SOURCES:
        n_src = sum(1 for e in evs if e.source == src)
        if n_src:
            log.info("%s prefilter: %d kept, %d dropped", src, n_src - dropped.get(src, 0), dropped.get(src, 0))
    evs = kept
    if not evs:
        return 0
    data = selfmodel.get_profile(user_id)
    profile = "\n".join(f"- {f['text']}" for sec in ("identity", "academics", "goals", "people", "patterns")
                         for f in data[sec])[:3500] or "an NYU graduate student"
    likes = "; ".join(_prefs(user_id, "like")) or "nothing yet"
    dislikes = "; ".join(_prefs(user_id, "dislike")) or "nothing yet"
    sem = asyncio.Semaphore(SCORE_PARALLEL)

    async def run(batch):
        async with sem:
            try:
                return await _score_batch(user_id, batch, profile, likes, dislikes)
            except Exception as e:
                log.warning("scoring batch failed (%s)", type(e).__name__)
                return 0

    total = 0
    for attempt in (1, 2):  # the model sometimes skips a few events in a batch: one more pass
        done = await asyncio.gather(*(run(evs[i:i + SCORE_BATCH]) for i in range(0, len(evs), SCORE_BATCH)))
        total += sum(done)
        evs = [e for e in evs if not _db.execute("SELECT 1 FROM event_scores WHERE user_id = ? AND event_id = ?",
                                                 (str(user_id), e.id)).fetchone()]
        if not evs:
            break
    log.info("scored %d new events for %s (%d left unscored)", total, user_id, len(evs))
    return total


@dataclass
class Scored:
    event: Event
    score: int
    reason: str
    deadline: date | None
    conflicts: list[str]  # human descriptions, e.g. "your 1:00 PM meeting with Keith Mauppa"
    class_conflict: bool  # clashes with a class or an exam: never suggested
    on_calendar: bool = False  # the same event is already on his calendar
    status: str | None = None
    label: str | None = None  # "last-minute", "you asked me to remind you"


def scored(user_id, e: Event) -> Scored | None:
    r = _db.execute("SELECT score, reason, reg_deadline FROM event_scores WHERE user_id = ? AND event_id = ?",
                    (str(user_id), e.id)).fetchone()
    if not r:
        return None
    st = _db.execute("SELECT status FROM event_state WHERE user_id = ? AND event_id = ?", (str(user_id), e.id)).fetchone()
    deadline = r["reg_deadline"] or e.registration_deadline
    confl, is_class, same = conflicts(user_id, e)
    return Scored(e, r["score"], r["reason"] or "", date.fromisoformat(deadline) if deadline else None,
                  confl, is_class, status=st[0] if st else None, on_calendar=same)


# ---------- Calendar conflicts ----------


def _blocks(user_id, day: date) -> list[tuple[datetime, datetime, str, bool, str]]:
    """(start, end, description, is_class, title) for everything on his calendar that day."""
    out = []
    for it in schedule.existing_items(user_id):
        if it.kind == "class" and it.weekday == day.weekday() and it.start_time and day <= schedule.SEMESTER_END:
            s = datetime.combine(day, it.start_time, tzinfo=TZ)
            e = datetime.combine(day, it.end_time, tzinfo=TZ) if it.end_time else s + timedelta(minutes=75)
            out.append((s, e, f"your {it.course or it.title} class at {schedule.fmt_time(it.start_time)}", True, it.title))
        elif it.due_date == day and it.due_time and it.kind in ("class_session", "event"):
            if it.uid and it.uid.startswith(tuple(f"{k}:" for k in eventsources.LEAD_DAYS) + ("nyu_events:",)):
                continue  # an event he's already going to
            s = datetime.combine(day, it.due_time, tzinfo=TZ)
            e = datetime.combine(day, it.end_time, tzinfo=TZ) if it.end_time and it.end_time > it.due_time else s + timedelta(minutes=60)
            is_class = it.kind == "class_session"
            what = f"your {it.title} class" if is_class else f"your {schedule.fmt_time(it.due_time)} {it.title}"
            out.append((s, e, what, is_class, it.title))
        elif it.due_date == day and it.due_time and it.kind == "exam":
            due = datetime.combine(day, it.due_time, tzinfo=TZ)
            out.append((due - timedelta(minutes=90), due, f"{it.title} (exam)", True, it.title))  # blocks, like a class
    return out


def conflicts(user_id, e: Event) -> tuple[list[str], bool, bool]:
    """(clash descriptions, clashes with a class, the same event is already on his calendar)."""
    if e.all_day:
        return [], False, False
    end = e.end if e.end and e.end > e.start else e.start + timedelta(minutes=90)
    hits, same = [], False
    for s, t, what, is_class, title in _blocks(user_id, e.start.date()):
        if not (s < end and e.start < t):
            continue
        if s == e.start and schedule.similar_titles(title, e.title):
            same = True  # e.g. he already has it in Google Calendar
            continue
        hits.append((what, is_class))
    return [w for w, _ in hits], any(c for _, c in hits), same


# ---------- Suggestion timing ----------


def window(e: Event, deadline: date | None) -> tuple[date, date]:
    """Days on which this event may be suggested: its source's lead time, pulled earlier so it
    lands at least 1 day before a registration deadline."""
    far, near = lead_days(e.source, e.flags)
    start, end = e.start.date() - timedelta(days=far), e.start.date() - timedelta(days=near)
    if deadline:
        by = deadline - timedelta(days=1)
        if by < end:
            end = by
            start = min(start, by - timedelta(days=far - near))
    return start, end


def _already(user_id, event_id: str, demo: bool) -> bool:
    st = _db.execute("SELECT status FROM event_state WHERE user_id = ? AND event_id = ?", (str(user_id), event_id)).fetchone()
    if st and st[0] in ("suggested", "going", "hidden"):
        return True
    q = "SELECT event_ids FROM event_picks WHERE user_id = ?" + ("" if demo else " AND demo = 0")
    return any(event_id in json.loads(r[0]) for r in _db.execute(q, (str(user_id),)))


def _dedupe(items: list[Scored]) -> list[Scored]:
    """One entry per real event: the same event can be posted twice on NYU Events, or on both NYU
    Events and Luma. Same start day and a similar title count as one (the first, best-scored, wins)."""
    out: list[Scored] = []
    for s in items:
        if not any(o.event.start.date() == s.event.start.date() and schedule.similar_titles(o.event.title, s.event.title)
                   for o in out):
            out.append(s)
    return out


def _price(e: Event) -> float:
    m = re.search(r"\d+(?:\.\d+)?", e.price or "")
    return float(m.group(0)) if m else 0.0


def picks_for_day(user_id, day: date, demo: bool = False) -> list[Scored]:
    """The (at most 3) events to suggest at 9 AM on this day, best first. Nothing already
    suggested, going, hidden or picked before; nothing that clashes with a class."""
    at = datetime.combine(day, DELIVERY_TIME, tzinfo=TZ)
    out = []
    for e in upcoming(at, at + timedelta(days=eventsources.HORIZON_DAYS)):
        if e.start.date() <= day:
            continue  # suggestions are for the coming days
        s = scored(user_id, e)
        if s is None or s.class_conflict or s.on_calendar:
            continue
        later = _db.execute("SELECT later_on FROM event_state WHERE user_id = ? AND event_id = ? AND status = 'later'",
                            (str(user_id), e.id)).fetchone()
        if later:
            if later[0] == day.isoformat() and s.score >= SUGGEST_MIN:
                s.label = "you asked me to remind you"
                out.append(s)
            continue
        if s.score < SUGGEST_MIN or _already(user_id, e.id, demo):
            continue
        if _price(e) > eventsources.MAX_PRICE and s.score < LATE_MIN:
            continue
        w_start, w_end = window(e, s.deadline)
        seen = first_seen(e.id)
        found_before_window = seen <= datetime.combine(w_start, DELIVERY_TIME, tzinfo=TZ)
        if found_before_window and w_start <= day <= w_end:
            out.append(s)
        elif not found_before_window and seen.date() <= day and s.score >= LATE_MIN:
            s.label = "last-minute"
            out.append(s)
    # Asked-for reminders first, then on-time picks, then at most one late find to fill a slot.
    out.sort(key=lambda s: (s.label != "you asked me to remind you", s.label == "last-minute", -s.score, s.event.start))
    picked, late = [], 0
    for s in _dedupe(out):
        if s.label == "last-minute":
            if late >= MAX_LATE_PER_DAY:
                continue
            late += 1
        picked.append(s)
    return picked[:PICKS_PER_DAY]


def record_picks(user_id, day: date, picks: list[Scored], message: str | None, demo: bool = False) -> None:
    """Store the day's picks (for the daily brief) and mark them suggested (real runs only)."""
    _db.execute(
        "INSERT OR REPLACE INTO event_picks (user_id, day, demo, event_ids, message, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(user_id), day.isoformat(), int(demo), json.dumps([p.event.id for p in picks]), message, _utcnow()),
    )
    if not demo:
        for p in picks:
            _set_state(user_id, p.event.id, "suggested")
    _db.commit()


def picked_today(user_id, day: date) -> bool:
    return bool(_db.execute("SELECT 1 FROM event_picks WHERE user_id = ? AND day = ? AND demo = 0",
                            (str(user_id), day.isoformat())).fetchone())


def clear_demo(user_id) -> None:
    _db.execute("DELETE FROM event_picks WHERE user_id = ? AND demo = 1", (str(user_id),))
    _db.commit()


# ---------- Lists for /events and questions ----------


def matches(user_id, start: datetime, end: datetime, min_score: int = LIST_MIN) -> list[Scored]:
    """Relevant events in a period, best first (lead times don't apply). Hidden ones and
    class clashes are left out."""
    out = []
    for e in upcoming(start, end):
        if e.start < start and not e.all_day:
            continue  # already started
        s = scored(user_id, e)
        if s and s.score >= min_score and s.status != "hidden" and not s.class_conflict and not s.on_calendar:
            out.append(s)
    return _dedupe(sorted(out, key=lambda s: (-s.score, s.event.start)))


def when(e: Event) -> str:
    day = schedule.fmt_day(e.start.date())
    if e.all_day:
        return day + (f" to {schedule.fmt_day(e.end.date())}" if e.end and e.end.date() > e.start.date() else ", all day")
    return f"{day}, {schedule.fmt_time(e.start.time())}"


def where(e: Event) -> str:
    return "online" if e.online else (e.location or "place TBA")


def fact_line(s: Scored) -> str:
    """Everything the voice needs about one event, as plain facts."""
    e = s.event
    parts = [f"{e.title} ({e.organizer})" if e.organizer else e.title, when(e), where(e), f"why: {s.reason}"]
    if s.deadline:
        parts.append(f"register by {schedule.fmt_day(s.deadline)}")
    parts += flag_notes(e)
    parts.append(f"clashes with {', '.join(s.conflicts)}" if s.conflicts else "he's free then")
    if s.label:
        parts.append(s.label)
    parts.append(f"link: {e.url}")
    return " | ".join(parts)


def list_line(s: Scored) -> str:
    """One event for a list shown word for word."""
    e = s.event
    extra = f" (clashes with {', '.join(s.conflicts)})" if s.conflicts else ""
    dl = f" Register by {schedule.fmt_day(s.deadline)}." if s.deadline else ""
    notes = "; ".join(flag_notes(e))
    notes = f" ({notes[:1].upper() + notes[1:]}.)" if notes else ""
    return f"{when(e)}: {e.title}, {where(e)}{extra}\n{s.reason}{dl}{notes}\n{e.url}"


def flag_notes(e: Event) -> list[str]:
    out = []
    if "needs_approval" in e.flags:
        out.append("needs approval, apply early")
    if "waitlist" in e.flags:
        out.append("waitlist only")
    if e.price:
        out.append(f"{e.price} ticket" if e.price.startswith("$") else f"paid: {e.price}")
    return out


def remember_list(user_id, ids: list[str]) -> None:
    """The events he was just shown, so "going" or "not for me" knows what he means."""
    selfmodel.set_meta(user_id, last_events={"ids": ids, "at": _utcnow()})


def last_list(user_id, max_age_days: int = 3) -> list[str]:
    m = selfmodel.meta(user_id).get("last_events") or {}
    if not m.get("ids"):
        return []
    at = datetime.fromisoformat(m["at"])
    return m["ids"] if datetime.now(timezone.utc) - at < timedelta(days=max_age_days) else []


# ---------- Replies to suggestions ----------

ACTION = re.compile(
    r"^\s*(?:i'?m\s+|i\s+am\s+|ok,?\s*|yes,?\s*|yeah,?\s*)?"
    r"(going|register|registered|sign me up|count me in|i'll go|not for me|nah,? not for me|pass|skip it|"
    r"more like this|remind me later|later)\b[\s,.:!-]*(.*)$",
    re.I,
)
ACTIONS = {
    "going": "going", "register": "going", "registered": "going", "sign me up": "going", "count me in": "going",
    "i'll go": "going", "not for me": "hide", "pass": "hide", "skip it": "hide", "more like this": "like",
    "remind me later": "later", "later": "later",
}


def parse_action(text: str) -> tuple[str, str] | None:
    m = ACTION.match(text)
    if not m:
        return None
    word = re.sub(r"^nah,?\s*", "", m.group(1).lower())
    return ACTIONS.get(word), m.group(2).strip()


def resolve(user_id, ref: str) -> tuple[list[Event], list[Event]]:
    """(chosen, candidates) for a reply like "going", "going to the pitch night", "going to both",
    "going 2". Chosen is empty when it's ambiguous."""
    cands = [e for e in (get_event(i) for i in last_list(user_id)) if e]
    if not cands:
        return [], []
    r = ref.lower()
    if not r or r in ("to it", "it", "this", "that"):
        return (cands if len(cands) == 1 else []), cands
    if re.search(r"\b(both|all|either|every)\b", r):
        return cands, cands
    nums = {"1": 0, "one": 0, "first": 0, "2": 1, "two": 1, "second": 1, "3": 2, "three": 2, "third": 2,
            "4": 3, "fourth": 3, "5": 4, "fifth": 4}
    for w, i in nums.items():
        if re.search(rf"\b{w}\b", r) and i < len(cands):
            return [cands[i]], cands
    words = {w for w in re.findall(r"[a-z0-9]+", r) if len(w) > 2 and w not in {"the", "to", "for", "and", "one", "event"}}
    best = max(cands, key=lambda e: len(words & set(re.findall(r"[a-z0-9]+", (e.title + " " + (e.organizer or "")).lower()))))
    hit = len(words & set(re.findall(r"[a-z0-9]+", (best.title + " " + (best.organizer or "")).lower())))
    return ([best] if hit else ([cands[0]] if len(cands) == 1 else [])), cands


def _set_state(user_id, event_id: str, status: str, later_on: str | None = None) -> None:
    _db.execute(
        "INSERT OR REPLACE INTO event_state (user_id, event_id, status, later_on, updated_at) VALUES (?, ?, ?, ?, ?)",
        (str(user_id), event_id, status, later_on, _utcnow()),
    )
    _db.commit()


def _pref_text(e: Event) -> str:
    return f"{e.title} (by {e.organizer or '?'}; {', '.join(e.tags[:6])})"


async def act(user_id, action: str, e: Event) -> str:
    """Apply a reply to one event. Returns a draft for the voice layer."""
    if action == "going":
        _set_state(user_id, e.id, "going")
        added = schedule.add_event(user_id, schedule.Item(
            kind="event", title=e.title, due_date=e.start.date(),
            due_time=None if e.all_day else e.start.time().replace(second=0, microsecond=0),
            end_time=e.end.time().replace(second=0, microsecond=0) if e.end and not e.all_day and e.end.date() == e.start.date() else None,
            room=where(e), uid=e.id,
            note=(e.url or "") + (" [needs approval]" if "needs_approval" in e.flags else ""),
        ), source=e.source)
        await brain.save(user_id, f"I'm going to {e.title} ({e.organizer or 'NYU'}) on {when(e)} at {where(e)}. {e.url}")
        tail = "Reminders 1 day and 2 hours before." if not e.all_day else ""
        if "needs_approval" in e.flags:
            tail += " It needs host approval, so apply now; you'll remind him to check his approval 2 days before."
        return (f"Added to his calendar: {e.title}, {when(e)}, {where(e)}. {tail} Link to register if needed: {e.url}"
                if added else f"{e.title} is already on his calendar.")
    if action == "hide":
        _set_state(user_id, e.id, "hidden")
        _db.execute("INSERT INTO event_prefs (user_id, kind, text, created_at) VALUES (?, 'dislike', ?, ?)",
                    (str(user_id), _pref_text(e), _utcnow()))
        _db.commit()
        return f"Hid {e.title}, and you'll steer away from things like it."
    if action == "like":
        _db.execute("INSERT INTO event_prefs (user_id, kind, text, created_at) VALUES (?, 'like', ?, ?)",
                    (str(user_id), _pref_text(e), _utcnow()))
        _db.commit()
        return f"You'll look out for more like {e.title} (same kind of topic and hosts)."
    if action == "later":
        day = e.start.date() - timedelta(days=1)
        _set_state(user_id, e.id, "later", day.isoformat())
        return f"You'll bring up {e.title} again on {schedule.fmt_day(day)}, the day before."
    return "Not sure what he wanted there."
