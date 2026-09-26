"""Pluggable event sources. No channel code here.

A source is a module-level object with a `name` and an async `fetch(client, now)` that
returns normalized Events. Add Luma, Engage or Meetup as their own module and list them in
SOURCES; nothing else changes. Lead times (how far ahead to suggest) live here, in one place.
"""

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from schedule import TZ

# How many days before an event to suggest it: (earliest, latest). Suggest when the event is
# between `latest` and `earliest` days away.
LEAD_DAYS = {
    "nyu_events": (4, 3),
    "luma": (8, 6),
}
DEFAULT_LEAD_DAYS = (4, 3)

HORIZON_DAYS = 30  # fetch events this far ahead
FETCH_EVERY = timedelta(hours=6)
USER_AGENT = "VioletMemory/1.0 (personal student assistant; httpx)"
TIMEOUT = httpx.Timeout(45.0, connect=15.0)


def lead_days(source: str) -> tuple[int, int]:
    return LEAD_DAYS.get(source, DEFAULT_LEAD_DAYS)


@dataclass
class Event:
    id: str  # "<source>:<source id>", unique across sources
    source: str
    title: str
    start: datetime  # America/New_York
    end: datetime | None = None
    all_day: bool = False
    location: str | None = None
    online: bool = False
    organizer: str | None = None
    tags: list[str] = field(default_factory=list)
    description: str = ""  # plain text, short
    url: str | None = None
    registration_deadline: str | None = None  # YYYY-MM-DD when the source states one
    canceled: bool = False


class EventSource:
    name = "base"

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> list[Event]:
        raise NotImplementedError


def client() -> httpx.AsyncClient:
    """Polite HTTP client: clear User-Agent, timeouts."""
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)


def strip_html(text: str | None, limit: int = 600) -> str:
    text = re.sub(r"<(br|/p|/div|/li)[^>]*>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return text[:limit].rsplit(" ", 1)[0] + "..." if len(text) > limit else text


def local(dt: datetime) -> datetime:
    return dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)


def sources() -> list[EventSource]:
    """Every enabled source. Import here so a broken optional source can't break the rest."""
    import nyu_events

    return [nyu_events.SOURCE]
