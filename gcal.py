"""Google Calendar (iCal feed) sync. No channel code here.

The feed URL is a secret: it is read from .env and never logged or included in errors.
"""

import logging
import os
from datetime import date, datetime, timedelta

import httpx
import icalendar
import recurring_ical_events

import brain
import schedule
from schedule import TZ, Item

log = logging.getLogger("gcal")

SOURCE = "google"
RECURRING_DAYS = 30  # expand recurring events this far ahead
SYNC_EVERY = timedelta(hours=3)


class FeedError(Exception):
    """Fetching or parsing the feed failed. The message never contains the URL."""


def configured() -> tuple[str, str] | None:
    url, user_id = os.getenv("GOOGLE_ICS_URL"), os.getenv("GOOGLE_ICS_USER_ID")
    return (url, user_id) if url and user_id else None


async def fetch(url: str) -> bytes:
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        raise FeedError(f"network error ({type(e).__name__})") from None
    if resp.status_code != 200:
        raise FeedError(f"HTTP {resp.status_code}")
    return resp.content


def _local(value) -> tuple[date, object]:
    """(date, time or None) in America/New_York. All-day events have no time."""
    if isinstance(value, datetime):
        dt = value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)
        return dt.date(), dt.time().replace(second=0, microsecond=0)
    return value, None


def _place(location: str) -> str | None:
    """Short place name: "online" for meeting links (which can carry login tokens),
    else the first part of an address ("2 MetroTech Center")."""
    location = location.strip()
    if not location:
        return None
    if "://" in location or location.lower().startswith(("zoom", "meet.google", "teams")):
        return "online"
    return location.split(",")[0].strip()


def _to_item(ev, recurring: bool) -> Item | None:
    if str(ev.get("STATUS", "")).upper() == "CANCELLED":
        return None
    start = ev.get("DTSTART")
    if start is None:
        return None
    d, t = _local(start.dt)
    end = ev.get("DTEND")
    end_t = _local(end.dt)[1] if end is not None and t is not None else None
    uid = str(ev.get("UID", "")) or f"{ev.get('SUMMARY', '')}|{start.dt}"
    title = str(ev.get("SUMMARY", "")).strip() or "(no title)"
    location = _place(str(ev.get("LOCATION", "")))
    occurrence_uid = f"{uid}|{d.isoformat()}T{t or ''}" if recurring else uid
    return Item(
        kind="event", title=title, due_date=d, due_time=t, end_time=end_t,
        room=location, uid=occurrence_uid, series_uid=uid if recurring else None,
    )


def parse(data: bytes, now: datetime | None = None) -> list[Item]:
    """Future events: every one-off event, plus recurring ones for the next 30 days."""
    now = now or datetime.now(TZ)
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as e:
        raise FeedError(f"could not parse calendar ({type(e).__name__})") from None

    # Decide what recurs from the raw calendar: the expansion library marks every
    # occurrence it returns (one-off events too) with a RECURRENCE-ID.
    raw = list(cal.walk("VEVENT"))
    recurring_uids = {
        str(ev.get("UID")) for ev in raw if ev.get("RRULE") is not None or ev.get("RECURRENCE-ID") is not None
    }

    items: list[Item] = []
    horizon = now + timedelta(days=RECURRING_DAYS)
    # Recurring series (and their moved or edited occurrences), expanded in the window.
    for ev in recurring_ical_events.of(cal).between(now, horizon):
        if str(ev.get("UID")) in recurring_uids:
            it = _to_item(ev, recurring=True)
            if it:
                items.append(it)
    # One-off events: all future ones, however far ahead.
    for ev in raw:
        if str(ev.get("UID")) in recurring_uids:
            continue
        it = _to_item(ev, recurring=False)
        if it:
            items.append(it)
    return [i for i in items if schedule.due_datetime(i) >= now]


def memory_summary(added: list[Item]) -> str:
    """Short text about new events for Cognee. Recurring series become one line."""
    lines, series_done = [], set()
    for it in sorted(added, key=schedule.due_datetime):
        room = f" ({it.room})" if it.room else ""
        if it.series_uid:
            if it.series_uid in series_done:
                continue
            series_done.add(it.series_uid)
            when = f"every {it.due_date:%A}" + (f" at {schedule.fmt_time(it.due_time)}" if it.due_time else "")
            lines.append(f"- Recurring: {it.title}, {when}{room}")
        else:
            when = schedule.fmt_day(it.due_date) + (f", {schedule.fmt_time(it.due_time)}" if it.due_time else ", all day")
            lines.append(f"- {when}: {it.title}{room}")
    today = datetime.now(TZ).date()
    return f"New events from my Google Calendar (synced {schedule.fmt_day(today)}, {today.year}):\n" + "\n".join(lines)


async def sync(user_id, url: str) -> schedule.SyncResult:
    """Fetch, parse and store the feed, then save new events to memory.

    Returns the result; call brain.connect(user_id) afterwards to build the graph.
    """
    events = parse(await fetch(url))
    result = schedule.sync_events(user_id, events, SOURCE)
    if result.added:
        await brain.save(user_id, memory_summary(result.added))
    log.info(
        "google sync: %d in feed, %d new, %d updated, %d removed, %d duplicates",
        len(events), len(result.added), result.updated, result.removed, result.duplicates,
    )
    return result
