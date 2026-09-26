"""Luma (lu.ma) iCal feeds, e.g. the "What's Happening in New York" discover feed. No channel code.

Feed URLs come from LUMA_ICS_URLS in .env (comma-separated) and are never logged.

Quirks handled (checked against the real feed, Sep 2026):
- The event page / registration link is the URL after "Get up-to-date information at:" in
  DESCRIPTION.
- LOCATION is often a luma.com/event URL. The address is the "Address:" block in DESCRIPTION;
  "Check event page for more details" means the address is shown after registering, so we give
  the rough area from GEO instead.
- Organizer: ORGANIZER's CN and the "Hosted by ..." line. Descriptions end truncated with "…".
- Times are UTC; all-day events use VALUE=DATE. Every event's STATUS is TENTATIVE (ignored).
"""

import logging
import os
import re
from datetime import date, datetime, time, timedelta

import httpx
import icalendar

from eventsources import HORIZON_DAYS, Event, EventSource, local
from schedule import TZ

log = logging.getLogger("luma")

_LINK = re.compile(r"Get up-to-date information at:\s*(https?://\S+)", re.I)
_ADDRESS = re.compile(r"Address:\s*\n(.*?)(?:\n\s*\n|$)", re.I | re.S)
_HOSTED = re.compile(r"^\s*Hosted by (.+?)\s*$", re.I | re.M)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF️‍]")

# Flags. Narrower than the bare words: "applications" and "applying" show up in normal copy
# ("700 completed applications", "by actively applying"), so match how Luma hosts phrase it.
_APPROVAL = re.compile(
    r"\b(apply to (attend|join)|apply (here|now|below|by)|application (required|only|form|deadline)|"
    r"by application|applications? (close|due|are open)|requires? (host )?approval|approval required|"
    r"subject to approval|vetted|invite[- ]only|by invitation)\b", re.I)
_WAITLIST = re.compile(r"\b(at capacity|waitlist|wait list|sold out)\b", re.I)
_PRICE = re.compile(r"\$\s?(\d+(?:\.\d{2})?)|tickets are ([^.\n]{0,40})", re.I)
_HACKATHON = re.compile(r"\bhack(athon|day|night)s?\b", re.I)


def urls() -> list[str]:
    return [u.strip() for u in (os.getenv("LUMA_ICS_URLS") or "").split(",") if u.strip()]


def area_from_geo(lat: float, lon: float) -> str:
    """Rough NYC borough for events whose address is shown only after registering."""
    if 40.70 <= lat <= 40.88 and -74.03 <= lon <= -73.91 and (lon <= -73.97 or lat >= 40.74):
        return "Manhattan"
    if 40.57 <= lat <= 40.74 and -74.05 <= lon <= -73.83:
        return "Brooklyn"
    if 40.54 <= lat <= 40.80 and -73.96 <= lon <= -73.70:
        return "Queens"
    if 40.78 <= lat <= 40.92 and -73.93 <= lon <= -73.76:
        return "the Bronx"
    return "New York area"


def _clean(text: str) -> str:
    text = _EMOJI.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def parse(data: bytes, now: datetime) -> list[Event]:
    cal = icalendar.Calendar.from_ical(data)
    out = []
    for ev in cal.walk("VEVENT"):
        start_v = ev.get("DTSTART")
        if start_v is None or not ev.get("SUMMARY"):
            continue
        raw_start = start_v.dt
        all_day = not isinstance(raw_start, datetime)
        if all_day:
            start = datetime.combine(raw_start, time(0, 0), tzinfo=TZ)
            end_raw = ev.get("DTEND").dt if ev.get("DTEND") else raw_start + timedelta(days=1)
            end = datetime.combine(end_raw - timedelta(days=1), time(23, 59), tzinfo=TZ)  # DTEND is exclusive
        else:
            start = local(raw_start)
            end = local(ev.get("DTEND").dt) if ev.get("DTEND") else None
        if (end or start) < now:
            continue

        desc = str(ev.get("DESCRIPTION") or "")
        link = _LINK.search(desc)
        addr = _ADDRESS.search(desc)
        address = " ".join(l.strip() for l in addr.group(1).splitlines() if l.strip()) if addr else ""
        loc_field = str(ev.get("LOCATION") or "")
        if loc_field and not loc_field.startswith("http"):
            location = loc_field  # a real address, fuller than the Address block
        elif address and not address.lower().startswith("check event page"):
            location = address.replace(" United States", "")
        else:
            geo = ev.get("GEO")
            area = area_from_geo(geo.latitude, geo.longitude) if geo is not None else None
            location = f"address after registering ({area})" if area else "address after registering"
        online = bool(re.search(r"\b(online|virtual|zoom|livestream)\b", loc_field + " " + address, re.I)) and not addr

        hosted = _HOSTED.search(desc)
        cn = ev.get("ORGANIZER").params.get("CN") if ev.get("ORGANIZER") is not None else None
        organizer = hosted.group(1) if hosted else cn

        # Body: drop the link line, the address block and the "Hosted by" line.
        body = _LINK.sub("", desc)
        body = _ADDRESS.sub("", body)
        body = _HOSTED.sub("", body)
        body = _clean(re.sub(r"\n{2,}", "\n", body))[:700]

        blob = f"{ev.get('SUMMARY')} {desc}"
        flags = []
        if _APPROVAL.search(blob):
            flags.append("needs_approval")
        if _WAITLIST.search(blob):
            flags.append("waitlist")
        price = None
        m = _PRICE.search(blob)
        if m:
            flags.append("paid")
            price = f"${m.group(1)}" if m.group(1) else m.group(2).strip()
        if _HACKATHON.search(blob):
            flags.append("hackathon")

        out.append(Event(
            id=f"luma:{str(ev.get('UID')).split('@')[0]}",
            source="luma",
            title=_clean(str(ev.get("SUMMARY"))),
            start=start,
            end=end,
            all_day=all_day,
            location=location,
            online=online,
            organizer=_clean(organizer) if organizer else None,
            tags=[f.replace("_", " ") for f in flags],
            description=body,
            url=link.group(1).rstrip(").,") if link else None,
            flags=flags,
            price=price,
        ))
    horizon = now + timedelta(days=HORIZON_DAYS)
    return [e for e in out if e.start < horizon]


class LumaSource(EventSource):
    name = "luma"

    def __init__(self):
        self.last_counts: dict[str, int] = {}

    async def fetch(self, client: httpx.AsyncClient, now: datetime) -> list[Event]:
        """One request per configured feed; deduped by UID across feeds."""
        found: dict[str, Event] = {}
        self.last_counts = {}
        for n, url in enumerate(urls(), 1):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                evs = parse(resp.content, now)
            except Exception as e:
                log.warning("luma feed %d failed (%s)", n, type(e).__name__)
                continue
            self.last_counts[f"luma feed {n}"] = len(evs)
            for e in evs:
                found.setdefault(e.id, e)
        return list(found.values())


SOURCE = LumaSource()
