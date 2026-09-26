"""NYU Events Calendar (events.nyu.edu, LiveWhale). No channel code here.

Verified feed behaviour (Sep 2026):
- JSON at /live/json/events/<filters>, response {"meta", "links", "data": [...]}.
- Filters: /start_date/YYYY-MM-DD, /end_date/YYYY-MM-DD, /tag/<tag>, /group/<group fullname>
  (/gid/<id> is silently ignored), /paginate/<n> sets the page size (capped at 1000),
  pages via ?page=N, /response_fields/a,b,c adds optional fields.
- Groups (from /live/json/groups/paginate/2000): Tandon = "Engineering", "Stern",
  "Wasserman-Center" and "wasserman-center-feed-only", "Entrepreneurial-Institute" (runs the
  Leslie eLab; there is no separate Leslie group or tag), "Berkley-Center-for-Entrepreneurship".
- A repeating event keeps one id for every occurrence; we keep its next occurrence.
- There is no structured registration deadline field; deadlines appear in text only.
"""

import logging
from datetime import datetime, timedelta

import httpx

from eventsources import HORIZON_DAYS, Event, EventSource, local, strip_html

log = logging.getLogger("nyu_events")

BASE = "https://events.nyu.edu/live/json/events"
FIELDS = "location,tags,summary,description,group_title,event_types"
FEEDS = {
    "all": "",
    "tag ihub": "/tag/ihub",
    "Tandon": "/group/Engineering",
    "Stern": "/group/Stern",
    "Wasserman": "/group/Wasserman-Center",
    "Wasserman (feed only)": "/group/wasserman-center-feed-only",
    "Entrepreneurial Institute (Leslie eLab)": "/group/Entrepreneurial-Institute",
    "Berkley Center": "/group/Berkley-Center-for-Entrepreneurship",
}
PAGE_SIZE = 1000  # the server's cap


def feed_url(path: str, now: datetime) -> str:
    start = now.date().isoformat()
    end = (now.date() + timedelta(days=HORIZON_DAYS)).isoformat()
    return f"{BASE}{path}/start_date/{start}/end_date/{end}/paginate/{PAGE_SIZE}/response_fields/{FIELDS}"


def _dt(iso: str | None) -> datetime | None:
    return local(datetime.fromisoformat(iso)) if iso else None


def normalize(raw: dict) -> Event | None:
    start = _dt(raw.get("date_iso"))
    if not start or not raw.get("title"):
        return None
    all_day = bool(raw.get("is_all_day"))
    end = _dt(raw.get("date2_iso"))
    if all_day and end is None:
        end = start.replace(hour=23, minute=59)
    tags = [t for t in (raw.get("tags") or []) if t] + [t.strip() for t in (raw.get("event_types") or []) if t]
    online = bool(raw.get("is_online")) and (raw.get("online_type") or "").lower() != "hybrid"
    return Event(
        id=f"nyu_events:{raw['id']}",
        source="nyu_events",
        title=strip_html(raw["title"], 200),
        start=start,
        end=end,
        all_day=all_day,
        location=", ".join(p.strip() for p in strip_html(raw.get("location") or raw.get("location_title") or "", 160).splitlines() if p.strip()) or None,
        online=online,
        organizer=strip_html(raw.get("group_title") or "", 120) or None,
        tags=list(dict.fromkeys(tags)),
        description=strip_html(raw.get("summary") or raw.get("description"), 600),
        url=raw.get("url") or f"https://events.nyu.edu/event/{raw['id']}",
        canceled=bool(raw.get("is_canceled")) or str(raw.get("title", "")).upper().startswith("CANCELED"),
    )


class NYUEventsSource(EventSource):
    name = "nyu_events"

    def __init__(self):
        self.last_counts: dict[str, int] = {}

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> list[Event]:
        """One request per feed (the main feed needs a second page when there are more than
        1000 events in the window). Keeps each event id's next upcoming occurrence."""
        found: dict[str, Event] = {}
        self.last_counts = {}
        for name, path in FEEDS.items():
            url = feed_url(path, now)
            rows = []
            try:
                while url:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    body = resp.json()
                    rows += body.get("data") or []
                    url = (body.get("links") or {}).get("next")
            except (httpx.HTTPError, ValueError) as e:
                log.warning("nyu_events feed %s failed (%s)", name, type(e).__name__)
                continue
            self.last_counts[name] = len(rows)
            for raw in rows:
                ev = normalize(raw)
                if ev is None or (ev.end or ev.start) < now:
                    continue
                prev = found.get(ev.id)
                if prev is None or ev.start < prev.start:
                    found[ev.id] = ev
        return list(found.values())


SOURCE = NYUEventsSource()
