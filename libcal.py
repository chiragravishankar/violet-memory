"""NYU Libraries study rooms (nyu.libcal.com), read-only. No channel code here.

How LibCal loads its data (checked Sep 2026):
- /spaces?lid=<library> is a page that lists every room as `resources.push({id: "eid_…", title, gid,
  grouping, capacity})`. Fetched once a day per library.
- The availability grid is POST /spaces/availability/grid (lid, gid, eid=-1, start, end, pageIndex,
  pageSize) returning {"slots": [{start, end, itemId, className?}]}: hourly slots, and a slot with a
  className (e.g. s-lc-eq-checkout) is taken. Cached 5 minutes.
- /api_hours_today.php?iid=1287&lid=0&format=json gives today's hours for every location.
- /space/<eid>?date=YYYY-MM-DD opens that room on that day: the user picks the time, signs in with
  their NetID and confirms. We never log in and never submit a booking.
robots.txt allows these paths with Crawl-delay: 10, so requests are spaced at least 10 s apart.
"""

import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime

import httpx

from schedule import TZ

log = logging.getLogger("libcal")

BASE = "https://nyu.libcal.com"
UA = "VioletNYUHelper/1.0 (NYU student assistant; read-only availability checks)"
IID = 1287  # NYU's LibCal institution id (for the hours feed)
CRAWL_DELAY = 10.0
GRID_TTL = 300  # seconds
ROOMS_TTL = 24 * 3600

LIBRARIES = {
    "dibner": {"lid": 13440, "name": "Dibner Library", "hours_name": "Bern Dibner Library",
               "where": "5 MetroTech Center, Brooklyn (next to Tandon)"},
    "bobst": {"lid": 5703, "name": "Bobst Library", "hours_name": "Elmer Holmes Bobst Library",
              "where": "70 Washington Square South, Manhattan"},
}

# Booking rules (NYU Libraries).
RULES = {
    "dibner": {"ahead_days": 60, "group_max_hours": 3, "individual_max_hours": 5, "min_group": 2,
               "text": "Dibner group rooms are on floors 3 and 4: small rooms for 2 to 5 people, large for 4 to 8. "
                       "Up to 3 hours per booking, bookable up to 2 months ahead. Individual rooms up to 5 hours a day."},
    "bobst": {"ahead_days": 14, "group_max_hours": 3, "individual_max_hours": 3, "min_group": 2,
              "text": "Bobst rooms: up to 14 days ahead, 3 hours max, one reservation per day. "
                      "Group rooms need 2 or more people."},
}
GRACE = "There's a 10 to 15 minute grace period: if nobody checks in by then, the room can be released."


@dataclass
class Room:
    eid: int
    title: str
    gid: int
    group: str
    capacity: int

    @property
    def kind(self) -> str:
        g = self.group.lower()
        if "sensory" in g or "pod" in g:
            return "pod"
        if "individual" in g:
            return "individual"
        if "large" in g:
            return "large"
        return "group"

    @property
    def name(self) -> str:
        return re.sub(r"\s*\(Capacity \d+\)\s*$", "", self.title)


_last_request = 0.0
_lock = asyncio.Lock()
_rooms: dict[int, tuple[float, list[Room]]] = {}
_grids: dict[tuple, tuple[float, list[dict]]] = {}
_hours: tuple[float, list[dict]] | None = None


async def _request(method: str, path: str, **kw) -> httpx.Response:
    """One polite request: at most one every CRAWL_DELAY seconds, clear User-Agent."""
    global _last_request
    async with _lock:
        wait = CRAWL_DELAY - (time.monotonic() - _last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
                r = await c.request(method, BASE + path, **kw)
        finally:
            _last_request = time.monotonic()
    r.raise_for_status()
    return r


_RESOURCE = re.compile(r'resources\.push\(\{\s*id: "eid_(\d+)",\s*title: "([^"]*)".*?gid: (\d+),.*?grouping: "([^"]*)"'
                       r'.*?capacity: (\d+)', re.S)


def _unescape(s: str) -> str:
    return html.unescape(s.encode().decode("unicode_escape"))


async def rooms(library: str) -> list[Room]:
    lid = LIBRARIES[library]["lid"]
    hit = _rooms.get(lid)
    if hit and time.monotonic() - hit[0] < ROOMS_TTL:
        return hit[1]
    page = (await _request("GET", f"/spaces?lid={lid}")).text
    out = [Room(int(e), _unescape(t), int(g), _unescape(gr), int(c)) for e, t, g, gr, c in _RESOURCE.findall(page)]
    _rooms[lid] = (time.monotonic(), out)
    return out


async def grid(library: str, gid: int, day: date) -> list[dict]:
    """Every hourly slot on that day for one room category."""
    lid = LIBRARIES[library]["lid"]
    key = (lid, gid, day)
    hit = _grids.get(key)
    if hit and time.monotonic() - hit[0] < GRID_TTL:
        return hit[1]
    data = {"lid": lid, "gid": gid, "eid": -1, "seat": 0, "seatId": 0, "zone": 0,
            "start": day.isoformat(), "end": (day + timedelta(days=1)).isoformat(), "pageIndex": 0, "pageSize": 100}
    r = await _request("POST", "/spaces/availability/grid", data=data,
                       headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}/spaces?lid={lid}&gid={gid}"})
    slots = r.json().get("slots") or []
    _grids[key] = (time.monotonic(), slots)
    return slots


def room_link(eid: int, day: date) -> str:
    return f"{BASE}/space/{eid}?date={day.isoformat()}"


def booking_page(library: str) -> str:
    return f"{BASE}/spaces?lid={LIBRARIES[library]['lid']}"


def _kinds_for(people: int) -> list[str]:
    if people <= 1:
        return ["individual", "pod"]
    if people <= 5:
        return ["group", "large"]
    return ["large", "group"]


def _free_rooms(slots: list[dict], candidates: list[Room], day: date, start: dtime, end: dtime) -> list[Room]:
    need = []
    t = datetime.combine(day, start)
    while t < datetime.combine(day, end):
        need.append(t.strftime("%Y-%m-%d %H:%M:%S"))
        t += timedelta(hours=1)
    free = {}
    for s in slots:
        if not s.get("className"):
            free.setdefault(int(s["itemId"]), set()).add(s["start"])
    # A room is free when every hour that overlaps the request is free (slots are hourly, on the hour).
    starts = {datetime.combine(day, start).replace(minute=0).strftime("%Y-%m-%d %H:%M:%S")} | set(need)
    return [r for r in candidates if starts <= free.get(r.eid, set())]


async def find(library: str, day: date, start: dtime, end: dtime, people: int = 1) -> dict:
    """Free rooms for the whole window, best first, with deep links and the rules that apply.
    Never raises: on any LibCal problem returns the booking page and short instructions."""
    library = library if library in LIBRARIES else "dibner"
    lib, rules = LIBRARIES[library], RULES[library]
    today = datetime.now(TZ).date()
    hours = (datetime.combine(day, end) - datetime.combine(day, start)).seconds / 3600
    problems = []
    if day < today:
        problems.append("That date has passed.")
    if (day - today).days > rules["ahead_days"]:
        problems.append(f"{lib['name']} books at most {rules['ahead_days']} days ahead.")
    kinds = _kinds_for(people)
    max_h = rules["individual_max_hours"] if kinds[0] == "individual" else rules["group_max_hours"]
    if hours > max_h:
        problems.append(f"That's {hours:g} hours; the max per booking is {max_h}.")
    if people == 1 and library == "bobst":
        kinds = ["individual", "pod"]
    if people > 8:
        problems.append("The largest rooms hold 8 (Dibner) or about 11 (Bobst); for bigger groups, split up or ask the library.")
    out = {"library": lib["name"], "where": lib["where"], "date": day.isoformat(),
           "window": f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}", "people": people, "rules": rules["text"],
           "grace": GRACE, "problems": problems, "booking_page": booking_page(library)}
    try:
        all_rooms = await rooms(library)
        cands = [r for r in all_rooms if r.kind in kinds and r.capacity >= people]
        cands.sort(key=lambda r: (kinds.index(r.kind), r.capacity))
        free = []
        for gid in dict.fromkeys(r.gid for r in cands):
            slots = await grid(library, gid, day)
            free += _free_rooms(slots, [r for r in cands if r.gid == gid], day, start, end)
            if len(free) >= 3:
                break
        free.sort(key=lambda r: (kinds.index(r.kind), r.capacity))
        out["rooms"] = [{"room": r.name, "capacity": r.capacity, "type": r.group, "link": room_link(r.eid, day)}
                        for r in free[:3]]
        out["how_to_book"] = ("Open the link, pick the time on the grid, sign in with your NetID and confirm. "
                              "Violet can't book for you.")
        if not free:
            out["note"] = "No room of that size is free for the whole window. Try another time or the other library."
    except Exception as e:
        log.warning("libcal read failed (%s)", type(e).__name__)
        out["rooms"] = []
        out["how_to_book"] = (f"I couldn't read live availability. Book at {booking_page(library)}: pick the room "
                              "type and date, choose a free slot, then sign in with your NetID and confirm.")
    return out


async def hours_today() -> list[dict]:
    global _hours
    if _hours and time.monotonic() - _hours[0] < GRID_TTL:
        return _hours[1]
    r = await _request("GET", f"/api_hours_today.php?iid={IID}&lid=0&format=json&systemTime=0")
    locs = r.json().get("locations") or []
    _hours = (time.monotonic(), locs)
    return locs


async def is_open(library: str) -> dict:
    """Today's hours for a library and its parts, and whether each is open right now."""
    lib = LIBRARIES.get(library) or LIBRARIES["bobst"]
    try:
        locs = await hours_today()
    except Exception as e:
        log.warning("libcal hours failed (%s)", type(e).__name__)
        return {"library": lib["name"], "error": "couldn't read hours", "hours_page": "https://library.nyu.edu/locations/"}
    main = next((l for l in locs if l.get("name") == lib["hours_name"]), None)
    parts = []
    if main:
        # parts of a library follow it in the feed until the next top-level library
        started = False
        for l in locs:
            if l is main:
                started = True
                continue
            if started:
                if l.get("name", "").endswith("Library") and l is not main:
                    break
                parts.append(l)
    rows = ([main] if main else []) + parts[:6]
    return {"library": lib["name"], "checked": datetime.now(TZ).strftime("%a %b %d, %I:%M %p"),
            "today": [{"name": l.get("name"), "hours": l.get("rendered"),
                       "open_now": (l.get("times") or {}).get("currently_open")} for l in rows],
            "source": "https://library.nyu.edu/locations/ (NYU Libraries hours via LibCal)"}
