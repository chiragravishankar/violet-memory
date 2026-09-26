"""Calendar feed sync: Brightspace (assignments) and Google Calendar (events).
No channel code here.

Feed URLs are secrets: they are read from .env and never logged or put in errors.
"""

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx
import icalendar
import recurring_ical_events

import brain
import schedule
from schedule import TZ, Item

log = logging.getLogger("feeds")

RECURRING_DAYS = 30  # expand recurring Google events this far ahead
SYNC_EVERY = timedelta(hours=3)


class FeedError(Exception):
    """Fetching or parsing a feed failed. The message never contains the URL."""


@dataclass
class Feed:
    source: str  # brightspace, google
    env: str  # name of the .env variable holding the URL


# Brightspace first: its course names let Google class events be recognised.
FEEDS = [Feed("brightspace", "BRIGHTSPACE_ICS_URL"), Feed("google", "GOOGLE_ICS_URL")]


def calendar_user() -> str | None:
    """Telegram user id the feeds belong to."""
    return os.getenv("CALENDAR_USER_ID") or os.getenv("GOOGLE_ICS_USER_ID")


async def fetch(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        raise FeedError(f"network error ({type(e).__name__})") from None
    if resp.status_code != 200:
        raise FeedError(f"HTTP {resp.status_code}")
    return resp.content


def _calendar(data: bytes):
    try:
        return icalendar.Calendar.from_ical(data)
    except Exception as e:
        raise FeedError(f"could not parse calendar ({type(e).__name__})") from None


def _local(value) -> tuple[date, object]:
    """(date, time or None) in America/New_York. All-day entries have no time."""
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
        return dt.date(), dt.time().replace(second=0, microsecond=0)
    return value, None


def _future(items: list[Item], now: datetime) -> list[Item]:
    return [i for i in items if schedule.due_datetime(i) >= now]


# ---------- Brightspace ----------

_DUE = re.compile(r"\s*-\s*Due\s*$", re.I)
_LOCKDOWN = re.compile(r"\s*-?\s*Requires Respondus LockDown Browser\s*", re.I)
_PROCTORED = re.compile(r"^Proctoring Enabled:\s*", re.I)
_CODE_TERM = re.compile(r"^[A-Z]{2,4}-[A-Z]{2}\s*\d{3,4}\s+(Fall|Spring|Summer)\s+\d{4}\s+", re.I)
_SCHEDULE_WORDS = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\d\s*(am|pm)\b", re.I)


def clean_title(summary: str) -> tuple[str, str | None]:
    """"X- Requires Respondus LockDown Browser - Due" -> ("X", "Needs LockDown Browser")."""
    notes = []
    title = _DUE.sub("", summary.strip())
    if _LOCKDOWN.search(title):
        notes.append("Needs LockDown Browser")
        title = _LOCKDOWN.sub(" ", title)
    if _PROCTORED.match(title):
        notes.append("Proctored")
        title = _CODE_TERM.sub("", _PROCTORED.sub("", title))
    title = re.sub(r"\s+", " ", title).strip(" -")
    return title, ", ".join(notes) or None


def clean_course(location: str) -> str:
    """"Global Innovation: Friday 11am" -> "Global Innovation"."""
    location = location.strip()
    if ":" in location:
        head, tail = location.split(":", 1)
        if _SCHEDULE_WORDS.search(tail):
            return head.strip()
    return location


def _kind(title: str) -> str:
    t = title.lower()
    if re.search(r"\b(exam|examination|midterm)\b", t) or "final exam" in t:
        return "exam"
    if re.search(r"\bquiz", t):
        return "quiz"
    return "assignment"


def parse_brightspace(data: bytes, now: datetime | None = None) -> list[Item]:
    now = now or datetime.now(TZ)
    items = []
    for ev in _calendar(data).walk("VEVENT"):
        start = ev.get("DTSTART")
        if start is None:
            continue
        d, t = _local(start.dt)
        end = ev.get("DTEND")
        end_t = _local(end.dt)[1] if end is not None else None
        title, note = clean_title(str(ev.get("SUMMARY", "")))
        if not title:
            continue
        timed_block = t is not None and end_t is not None and end_t != t
        items.append(Item(
            kind="event" if timed_block else _kind(title),
            title=title,
            course=clean_course(str(ev.get("LOCATION", ""))),
            due_date=d,
            due_time=t,
            end_time=end_t if timed_block else None,
            uid=str(ev.get("UID")),
            note=note,
        ))
    return _future(items, now)


# ---------- Google Calendar ----------


def _place(location: str) -> str | None:
    """Short place name: "online" for meeting links (which can carry login tokens),
    else the first part of an address ("2 MetroTech Center")."""
    location = location.strip()
    if not location:
        return None
    if "://" in location or location.lower().startswith(("zoom", "meet.google", "teams")):
        return "online"
    return location.split(",")[0].strip()


def _google_item(ev, recurring: bool) -> Item | None:
    if str(ev.get("STATUS", "")).upper() == "CANCELLED":
        return None
    start = ev.get("DTSTART")
    if start is None:
        return None
    d, t = _local(start.dt)
    end = ev.get("DTEND")
    end_t = _local(end.dt)[1] if end is not None and t is not None else None
    uid = str(ev.get("UID", "")) or f"{ev.get('SUMMARY', '')}|{start.dt}"
    return Item(
        kind="event",
        title=str(ev.get("SUMMARY", "")).strip() or "(no title)",
        due_date=d, due_time=t, end_time=end_t,
        room=_place(str(ev.get("LOCATION", ""))),
        uid=f"{uid}|{d.isoformat()}T{t or ''}" if recurring else uid,
        series_uid=uid if recurring else None,
    )


def parse_google(data: bytes, known_courses: list[str], now: datetime | None = None) -> list[Item]:
    """Future events: every one-off event, plus recurring ones for the next 30 days.
    A recurring timed event named after one of the user's courses is a class."""
    now = now or datetime.now(TZ)
    cal = _calendar(data)
    # Decide what recurs from the raw calendar: the expansion library marks every
    # occurrence it returns (one-off events too) with a RECURRENCE-ID.
    raw = list(cal.walk("VEVENT"))
    recurring_uids = {
        str(ev.get("UID")) for ev in raw if ev.get("RRULE") is not None or ev.get("RECURRENCE-ID") is not None
    }
    items: list[Item] = []
    for ev in recurring_ical_events.of(cal).between(now, now + timedelta(days=RECURRING_DAYS)):
        if str(ev.get("UID")) in recurring_uids:
            it = _google_item(ev, recurring=True)
            if it:
                items.append(it)
    for ev in raw:
        if str(ev.get("UID")) not in recurring_uids:
            it = _google_item(ev, recurring=False)
            if it:
                items.append(it)

    for it in items:
        course = schedule.course_of(it.title, known_courses)
        is_class = re.search(r"\b(class|lecture|recitation|seminar|lab section)\b", it.title, re.I)
        if it.series_uid and it.due_time and (course or is_class):
            it.kind, it.course = "class_session", course or ""
    return _future(items, now)


# ---------- Sync ----------


def memory_summary(source: str, added: list[Item]) -> str:
    """Short text about new items for Cognee. Recurring series become one line."""
    name = {"google": "Google Calendar", "brightspace": "Brightspace"}[source]
    lines, series_done = [], set()
    for it in sorted(added, key=schedule.due_datetime):
        room = f" ({it.room})" if it.room else ""
        note = f" [{it.note}]" if it.note else ""
        course = f" for {it.course}" if it.course and it.course.lower() not in it.title.lower() else ""
        if it.series_uid:
            if it.series_uid in series_done:
                continue
            series_done.add(it.series_uid)
            when = f"every {it.due_date:%A}" + (f" at {schedule.fmt_time(it.due_time)}" if it.due_time else "")
            kind = "Class" if it.kind == "class_session" else "Recurring"
            lines.append(f"- {kind}: {it.title}{course}, {when}{room}")
        else:
            when = schedule.fmt_day(it.due_date) + (f", {schedule.fmt_time(it.due_time)}" if it.due_time else ", all day")
            what = "due" if it.kind in schedule.DEADLINE_KINDS else "event"
            lines.append(f"- {when} ({what}): {it.title}{course}{room}{note}")
    today = datetime.now(TZ).date()
    return f"New items from my {name} (synced {schedule.fmt_day(today)}, {today.year}):\n" + "\n".join(lines)


async def sync_feed(feed: Feed, user_id: str) -> schedule.SyncResult | None:
    """Fetch, parse and store one feed, and save new items to memory.
    Returns None if the feed is not configured. Call brain.connect(user_id) afterwards."""
    url = os.getenv(feed.env)
    if not url:
        return None
    data = await fetch(url)
    if feed.source == "google":
        courses = sorted({i.course for i in schedule.existing_items(user_id) if i.course})
        items = parse_google(data, courses)
    else:
        items = parse_brightspace(data)
    result = schedule.sync_events(user_id, items, feed.source)
    if result.added:
        await brain.save(user_id, memory_summary(feed.source, result.added))
    log.info(
        "%s sync: %d in feed, %d new, %d already there (%d taken over from PDF/chat, %d dates corrected), "
        "%d updated, %d removed",
        feed.source, result.in_feed, len(result.added), result.already, result.adopted,
        result.corrected, result.updated, result.removed,
    )
    return result


async def sync_all() -> dict[str, schedule.SyncResult | str]:
    """Sync every configured feed. Errors are reported by type only."""
    user_id = calendar_user()
    results: dict[str, schedule.SyncResult | str] = {}
    if not user_id:
        return results
    for feed in FEEDS:
        try:
            r = await sync_feed(feed, user_id)
            if r is not None:
                results[feed.source] = r
        except FeedError as e:
            log.warning("%s sync failed: %s", feed.source, e)
            results[feed.source] = str(e)
        except Exception as e:
            log.warning("%s sync failed: %s", feed.source, type(e).__name__)
            results[feed.source] = type(e).__name__
    return results
