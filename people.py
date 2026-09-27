"""The people loop: who the user met (and where), follow-ups they promised, and the morning-after check-in
for events. No channel code here.

- People live in the profile's people store (selfmodel). Extra attrs: met_at (where), met_on (date),
  field (what they do, e.g. "fintech"), last_contact.
- Follow-ups are real records (followups table): person, what was promised, due date, status
  open/done/snoozed. Done updates the person's last_contact.
- Check-ins: the morning after an event the user said "going" to, or a networking-type calendar event,
  the brief asks "Who'd you meet at X?" once per event (checkins table).
"""

import json
import logging
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

import delivered
import llm
import paths
import schedule
import selfmodel
from fmt import View, esc
from schedule import TZ

log = logging.getLogger("people")

PAGE = 15
FOLLOWUP_HOUR = time(10, 0)  # follow-up reminders go out from 10 AM on the due day (not in quiet hours)
NETWORKING = re.compile(r"\b(mixer|networking|meet ?up|happy hour|reception|coffee chat|career fair|open house|social|"
                        r"kick-?off|conference|summit|panel|demo day|hackathon|founders?|dinner|gathering|info session)\b", re.I)

_db = sqlite3.connect(paths.VIOLET_DB, check_same_thread=False, timeout=15)
_db.row_factory = sqlite3.Row
_db.executescript("""
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    person TEXT NOT NULL,
    promise TEXT NOT NULL,           -- what the user said they'd do
    due_date TEXT,                   -- YYYY-MM-DD or NULL
    status TEXT NOT NULL DEFAULT 'open',   -- open, done, snoozed
    snooze_until TEXT,
    created_at TEXT NOT NULL,
    done_at TEXT,
    source TEXT,                     -- chat, migrated, checkin
    item_id INTEGER                  -- the schedule item it was migrated from
);
CREATE UNIQUE INDEX IF NOT EXISTS followups_item ON followups(user_id, item_id) WHERE item_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS checkins (
    user_id TEXT NOT NULL,
    event_key TEXT NOT NULL,
    title TEXT NOT NULL,
    event_date TEXT NOT NULL,
    status TEXT NOT NULL,            -- asked, telling, told, didnt_go, nobody_new
    asked_at TEXT,
    answered_at TEXT,
    PRIMARY KEY (user_id, event_key)
);
""")
_db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(TZ).date()


# ---------- people ----------

def _name(f: dict) -> str:
    return (f.get("attrs") or {}).get("name") or f["text"].split(":")[0].strip()


def add_person(user_id, name: str, relationship: str = "", context: str = "", met_at: str = "", field: str = "",
               met_on: str | None = None) -> str:
    """Save or update someone the user met. Returns the entry text."""
    text = f"{name}: " + ", ".join(x for x in [relationship, field, f"met at {met_at}" if met_at else "", context] if x)
    attrs = {"name": name, "relationship": relationship, "context": context}
    if met_at:
        attrs.update(met_at=met_at, met_on=met_on or _today().isoformat())
    if field:
        attrs["field"] = field
    selfmodel.add_facts(user_id, [{"section": "people", "text": text, "attrs": {k: v for k, v in attrs.items() if v}}],
                        "chat", stated=True, confidence=0.95)
    return text


def all_people(user_id) -> list[dict]:
    out = []
    for f in selfmodel.get_profile(user_id)["people"]:
        a = f.get("attrs") or {}
        out.append({"name": _name(f), "text": f["text"], "relationship": a.get("relationship", ""),
                    "met_at": a.get("met_at") or a.get("context") or "", "met_on": a.get("met_on") or (f.get("added") or "")[:10],
                    "field": a.get("field", ""), "email": a.get("email", ""), "last_contact": a.get("last_contact", "")})
    return out


def search(user_id, met_at: str = "", field: str = "", name: str = "") -> list[dict]:
    """People matching where they were met, what they do, or their name (any given, all words)."""
    def has(hay: str, q: str) -> bool:
        words = [w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 2 and w not in {"the", "and"}]
        return all(w in hay.lower() for w in words) if words else True
    out = []
    for p in all_people(user_id):
        blob = f"{p['text']} {p['met_at']} {p['field']} {p['relationship']}"
        if has(f"{p['met_at']} {p['text']}", met_at) and has(blob, field) and has(p["name"], name):
            out.append(p)
    return sorted(out, key=lambda p: p["met_on"] or "", reverse=True)


def people_view(user_id) -> View:
    """/people: recent people grouped by where they were met, newest first."""
    ppl = sorted(all_people(user_id), key=lambda p: (p["met_on"] or "", p["name"]), reverse=True)
    if not ppl:
        return View("👥 <b>PEOPLE</b>\n\nNo one yet. Tell me when you meet someone: \"met Priya at the Leslie mixer, she builds fintech tools\".")
    groups: dict[str, list[dict]] = {}
    for p in ppl:
        groups.setdefault(p["met_at"] or "Other", []).append(p)
    lines = []
    for where, ps in groups.items():
        lines.append(f"<b>{esc(where)}</b>")
        for p in ps:
            extra = " · ".join(x for x in [p["relationship"] if p["relationship"] not in where else "", p["field"],
                                          f"last contact {p['last_contact']}" if p["last_contact"] else ""] if x)
            lines.append(f"• {esc(p['name'])}" + (f" · {esc(extra)}" if extra else ""))
    pages, cur, count = [], [], 0
    for l in lines:
        cur.append(l)
        count += l.startswith("•")
        if count >= PAGE and l.startswith("•"):
            pages.append("\n".join(cur)); cur, count = [], 0
    if cur:
        pages.append("\n".join(cur))
    return View(f"👥 <b>PEOPLE</b> · {len(ppl)}\n\n" + pages[0], [], pages[1:])


# ---------- follow-ups ----------

def add_followup(user_id, person: str, promise: str, due: str | None = None, source: str = "chat",
                 item_id: int | None = None) -> int | None:
    try:
        cur = _db.execute("INSERT INTO followups (user_id, person, promise, due_date, created_at, source, item_id) "
                          "VALUES (?,?,?,?,?,?,?)", (str(user_id), person.strip(), promise.strip(), due, _now(), source, item_id))
    except sqlite3.IntegrityError:
        return None  # already migrated
    _db.commit()
    return cur.lastrowid


def get(user_id, fid: int) -> dict | None:
    r = _db.execute("SELECT * FROM followups WHERE id = ? AND user_id = ?", (int(fid), str(user_id))).fetchone()
    return dict(r) if r else None


def open_followups(user_id, days: int | None = None, person: str = "") -> list[dict]:
    """Open (and snoozed-but-due-again) follow-ups, soonest first; overdue included. days limits to due
    within that many days (undated ones too)."""
    today = _today()
    rows = [dict(r) for r in _db.execute("SELECT * FROM followups WHERE user_id = ? AND status IN ('open', 'snoozed') "
                                         "ORDER BY due_date IS NULL, due_date, id", (str(user_id),))]
    out = []
    for r in rows:
        if r["status"] == "snoozed" and r["snooze_until"] and r["snooze_until"] > today.isoformat():
            continue
        if days is not None and r["due_date"] and r["due_date"] > (today + timedelta(days=days)).isoformat():
            continue
        if person and not all(w in r["person"].lower() for w in person.lower().split()):
            continue
        out.append(r)
    return out


def all_for(user_id, person: str) -> list[dict]:
    """Every follow-up with this person (any status), newest first: "what did I promise Sam?" """
    words = [w for w in person.lower().split() if w]
    rows = _db.execute("SELECT * FROM followups WHERE user_id = ? ORDER BY created_at DESC", (str(user_id),)).fetchall()
    return [dict(r) for r in rows if all(w in r["person"].lower() for w in words)]


def done(user_id, fid: int) -> dict | None:
    f = get(user_id, fid)
    if not f:
        return None
    _db.execute("UPDATE followups SET status = 'done', done_at = ? WHERE id = ? AND user_id = ?", (_now(), f["id"], str(user_id)))
    _db.commit()
    _touch(user_id, f["person"])
    return f


def snooze(user_id, fid: int, days: int = 2) -> dict | None:
    f = get(user_id, fid)
    if not f:
        return None
    until = (_today() + timedelta(days=days)).isoformat()
    _db.execute("UPDATE followups SET status = 'snoozed', snooze_until = ?, due_date = ? WHERE id = ? AND user_id = ?",
                (until, until, f["id"], str(user_id)))
    _db.commit()
    return get(user_id, fid)


def _touch(user_id, person: str) -> None:
    """Done with a follow-up: that's contact, so the person's last_contact becomes today."""
    data = selfmodel.get_profile(user_id)
    words = person.lower().split()
    for f in data["people"]:
        if words and all(w in _name(f).lower() for w in words[:2]):
            f.setdefault("attrs", {})["last_contact"] = _today().isoformat()
    selfmodel.save_profile(user_id, data)


def _due_text(f: dict) -> str:
    if not f["due_date"]:
        return "no date"
    d = date.fromisoformat(f["due_date"])
    delta = (d - _today()).days
    return "overdue" if delta < 0 else "today" if delta == 0 else "tomorrow" if delta == 1 else f"{d:%a %b} {d.day}"


def buttons(fid: int) -> list[tuple[str, str]]:
    return [("✍️ Draft message", f"fu:draft:{fid}"), ("✅ Done", f"fu:done:{fid}"), ("⏰ Snooze 2 days", f"fu:snooze:{fid}")]


def followups_view(user_id) -> View:
    fus = open_followups(user_id)
    if not fus:
        return View("🤝 <b>FOLLOW-UPS</b>\n\nNothing open. Tell me when you promise someone something and I'll keep track.")
    lines = [f"• <b>{esc(f['person'])}</b> · {esc(f['promise'])} · <i>{_due_text(f)}</i>" for f in fus[:8]]
    rows = [[(f"✅ {f['person'].split()[0]}", f"fu:done:{f['id']}"), ("⏰ +2 days", f"fu:snooze:{f['id']}"),
             ("✍️ Draft", f"fu:draft:{f['id']}")] for f in fus[:5]]
    return View("🤝 <b>FOLLOW-UPS</b> · " + str(len(fus)) + "\n\n" + "\n".join(lines), rows)


def reminder_view(f: dict) -> View:
    return View(f"🤝 Follow-up {_due_text(f)}: <b>{esc(f['person'])}</b>\n↳ {esc(f['promise'])}", [buttons(f["id"])])


def due_reminders(user_id, now: datetime | None = None) -> list[dict]:
    """Follow-ups to remind about now (due today or overdue, from 10 AM), each once per due date."""
    now = now or datetime.now(TZ)
    if now.time() < FOLLOWUP_HOUR:
        return []
    out = []
    for f in open_followups(user_id, days=0):
        if f["due_date"] and delivered.claim(user_id, "followup", f"{f['id']}|{f['due_date']}"):
            out.append(f)
    return out


MIGRATE_TASK = """These are to-do titles from a student's schedule. For each, if it's a follow-up with a person (email,
text, call, reply to, send something to, reach out to, follow up with someone), return who and what was promised.
Return JSON: {{"items": [{{"id": 1, "person": "full name as written", "promise": "what to do, short, without the name"}}]}}.
Leave out anything that isn't about a specific person.

{items}"""


async def migrate(user_id) -> list[dict]:
    """Turn follow-up-like schedule items (titles like "Email Keith about ...") into follow-up records and
    retire the schedule items, so nothing is tracked twice."""
    cands = [i for i in schedule.existing_items(user_id) if not i.done and schedule.FOLLOW_UP.search(i.title)
             and i.kind not in ("class", "class_session", "event")]
    if not cands:
        return []
    data = await llm.chat_json([{"role": "user", "content": MIGRATE_TASK.format(
        items="\n".join(f"{i.id}: {i.title}" for i in cands))}])
    by_id = {i.id: i for i in cands}
    moved = []
    for r in data.get("items") or []:
        it = by_id.get(int(r.get("id") or 0))
        if not it or not r.get("person"):
            continue
        fid = add_followup(user_id, str(r["person"]), str(r.get("promise") or it.title),
                           it.due_date.isoformat() if it.due_date else None, "migrated", it.id)
        if fid:
            schedule.mark_done_id(it.id)  # the follow-up record owns it now (its reminders stop)
            moved.append({"id": fid, "person": r["person"], "promise": r.get("promise"), "from": it.title})
    return moved


# ---------- post-event check-in ----------

def checkin_candidates(user_id, day: date | None = None) -> list[dict]:
    """Events the user went to yesterday (said "going", or a networking-type calendar event) not asked about yet."""
    import events

    day = day or _today()
    yday = day - timedelta(days=1)
    out = []
    for r in events._db.execute("SELECT e.id, e.title, e.start FROM event_state s JOIN events e ON e.id = s.event_id "
                                "WHERE s.user_id = ? AND s.status = 'going'", (str(user_id),)).fetchall():
        if str(r["start"])[:10] == yday.isoformat():
            out.append({"key": f"event:{r['id']}", "title": r["title"], "date": yday.isoformat()})
    for it in schedule.existing_items(user_id):
        if it.kind in ("event", "meeting") and it.due_date == yday and NETWORKING.search(it.title):
            out.append({"key": f"item:{it.uid or it.id}", "title": it.shown, "date": yday.isoformat()})
    seen = {r["event_key"] for r in _db.execute("SELECT event_key FROM checkins WHERE user_id = ?", (str(user_id),))}
    return [c for c in out if c["key"] not in seen]


def ask_checkin(user_id, c: dict) -> bool:
    """Record that we asked (once per event). False if it was already asked."""
    try:
        _db.execute("INSERT INTO checkins (user_id, event_key, title, event_date, status, asked_at) VALUES (?,?,?,?,?,?)",
                    (str(user_id), c["key"], c["title"], c["date"], "asked", _now()))
        _db.commit()
        return delivered.claim(user_id, "checkin", c["key"])
    except sqlite3.IntegrityError:
        return False


def checkin_buttons(key: str) -> list[tuple[str, str]]:
    return [("🎙 Tell you now", f"ci:tell:{key}"[:64]), ("Didn't go", f"ci:no:{key}"[:64]), ("Nobody new", f"ci:none:{key}"[:64])]


def answer_checkin(user_id, key: str, status: str) -> dict | None:
    r = _db.execute("SELECT * FROM checkins WHERE user_id = ? AND event_key LIKE ?", (str(user_id), key + "%")).fetchone()
    if not r:
        return None
    _db.execute("UPDATE checkins SET status = ?, answered_at = ? WHERE user_id = ? AND event_key = ?",
                (status, _now(), str(user_id), r["event_key"]))
    _db.commit()
    return dict(r)


def telling(user_id) -> dict | None:
    """The check-in the user chose to answer now (their next message is who they met)."""
    r = _db.execute("SELECT * FROM checkins WHERE user_id = ? AND status = 'telling' ORDER BY answered_at DESC LIMIT 1",
                    (str(user_id),)).fetchone()
    return dict(r) if r else None
