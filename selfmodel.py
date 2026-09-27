"""Self-model: a structured profile of each user that other features build on.
No channel code here.

Each fact: text, source (onboarding | chat | document | calendar | inferred),
stated_or_inferred, confidence, first_seen, last_confirmed. Stored as JSON in SQLite and
mirrored into the user's Cognee dataset as one "me" document tagged node_set=["core_profile"].
"""

import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, time, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import brain  # the only module that touches Cognee
import llm
import paths
from schedule import TZ

log = logging.getLogger("selfmodel")

SECTIONS = ["identity", "academics", "goals", "people", "preferences", "patterns"]
SECTION_TITLES = {
    "identity": "Who you are", "academics": "Academics", "goals": "Goals",
    "people": "People", "preferences": "Preferences", "patterns": "Patterns",
}
SINGLE_KEYS = {"name", "location", "school", "year", "reply_style", "reminder_timing", "quiet_hours"}
SOURCES = {"onboarding", "chat", "document", "calendar", "inferred"}
CORE_NODE_SET = ["core_profile"]

DB_PATH = paths.VIOLET_DB
_db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15)
_db.row_factory = sqlite3.Row
_db.execute(
    """CREATE TABLE IF NOT EXISTS profiles (
        user_id TEXT PRIMARY KEY,
        data TEXT NOT NULL,        -- JSON: {section: [fact, ...], "_meta": {...}}
        updated_at TEXT NOT NULL
    )"""
)
_db.commit()

# Hard filter behind the prompt rule: the model does not always follow it.
_SENSITIVE = re.compile(
    r"\b(anxiety|anxious|depress\w*|therap\w*|medicat\w*|diagnos\w*|illness|disease|disorder|adhd|autis\w*|"
    r"mental health|health (condition|issue|problem)s?|sick|surgery|pregnan\w*|disab\w*|salary|debt|loans?|income|savings|bank account|credit score|"
    r"broke|rent money|religio\w*|church|mosque|temple|synagogue|pray\w*|faith|muslim|christian|hindu|jewish|"
    r"buddhist|atheist|girlfriend|boyfriend|dating|crush|divorce\w*|breakup|gay|lesbian|bisexual|queer|"
    r"sexual\w*|politic\w*|democrat\w*|republican\w*|immigration status|visa status)\b",
    re.I,
)


def is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE.search(text))


SENSITIVE_RULE = (
    "Never include sensitive information: health, finances, religion, politics, sexuality, "
    "or romantic or family relationships beyond a person's name and role, unless the user "
    "explicitly asks you to remember it."
)


# ---------- Storage ----------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty() -> dict:
    return {**{s: [] for s in SECTIONS}, "_meta": {}}


def get_profile(user_id) -> dict:
    """The user's profile: {section: [fact, ...], "_meta": {...}}."""
    row = _db.execute("SELECT data FROM profiles WHERE user_id = ?", (str(user_id),)).fetchone()
    data = json.loads(row["data"]) if row else _empty()
    for s in SECTIONS:
        data.setdefault(s, [])
    data.setdefault("_meta", {})
    return data


def save_profile(user_id, data: dict, dirty: bool = True) -> None:
    if dirty:
        data["_meta"]["memory_dirty"] = True
    _db.execute(
        "INSERT INTO profiles (user_id, data, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
        (str(user_id), json.dumps(data, ensure_ascii=False), _now()),
    )
    _db.commit()


def all_user_ids() -> list[str]:
    return [r["user_id"] for r in _db.execute("SELECT user_id FROM profiles")]


def facts(data: dict):
    for s in SECTIONS:
        for f in data[s]:
            yield s, f


def meta(user_id) -> dict:
    return get_profile(user_id)["_meta"]


def set_meta(user_id, **values) -> None:
    data = get_profile(user_id)
    data["_meta"].update(values)
    save_profile(user_id, data, dirty=False)


# ---------- Adding, merging, removing ----------


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _person_name(f: dict) -> str:
    """ "Peilin Li: teammate ..." -> "Peilin Li", when attrs has no name."""
    name = f.get("attrs", {}).get("name")
    if name:
        return name
    head = f["text"].split(":", 1)[0]
    return head if ":" in f["text"] and len(head.split()) <= 5 else ""


def dedupe_people(user_id) -> int:
    """Merge duplicate entries for the same person. Returns how many were merged."""
    data = get_profile(user_id)
    kept, merged = [], 0
    for f in data["people"]:
        twin = next((k for k in kept if _norm(_person_name(k)) and _norm(_person_name(k)) == _norm(_person_name(f))), None)
        if twin is None:
            kept.append(f)
            continue
        extra = f["text"].split(":", 1)[-1].strip()
        if not _near(twin["text"], f["text"]) and _norm(extra) not in _norm(twin["text"]):
            twin["text"] = f"{twin['text']}; {extra}"
        merged += 1
    if merged:
        data["people"] = kept
        save_profile(user_id, data)
    return merged


def _same_fact(section: str, a: dict, b: dict) -> bool:
    ka, kb = a.get("attrs", {}).get("key"), b.get("attrs", {}).get("key")
    if ka and ka == kb and ka in SINGLE_KEYS:
        return True  # one name, one reply style, one set of quiet hours
    if section == "people":
        na, nb = _norm(_person_name(a)), _norm(_person_name(b))
        if na and na == nb:
            return True
    ta, tb = _norm(a["text"]), _norm(b["text"])
    return ta == tb or SequenceMatcher(None, ta, tb).ratio() >= 0.85


_STOP = {"the", "a", "an", "at", "in", "of", "to", "for", "and", "is", "on", "with", "my", "i", "their", "their", "her"}


def _content_words(text: str) -> set[str]:
    return {w for w in _norm(text).split() if w not in _STOP}


def _near(a: str, b: str) -> bool:
    """Same statement in different words, e.g. "Studies MS in Management of Technology at NYU
    Tandon" and "Pursuing an MS in Management of Technology"."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.6 or SequenceMatcher(None, _norm(a), _norm(b)).ratio() >= 0.85


def _strip_name(text: str, person: dict) -> str:
    """"Priya: building fintech agents" -> "building fintech agents" when adding to Priya's entry."""
    name = _person_name(person)
    return re.sub(rf"^{re.escape(name)}\s*[:,-]?\s+", "", text, flags=re.I) if name else text


def _flat_attrs(attrs, section: str) -> dict:
    """The model sometimes nests attrs under the section name: {"identity": {"key": ...}}."""
    attrs = dict(attrs or {})
    for k in (section, *SECTIONS):
        if isinstance(attrs.get(k), dict):
            attrs = {**attrs.pop(k), **attrs}
    return {k: v for k, v in attrs.items() if v not in (None, "") and not isinstance(v, dict)}


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip().rstrip(".;")
    text = re.sub(r"^(my|i am|i'm)\s+", lambda m: "" if m.group(1).lower() == "my" else "", text, flags=re.I)
    return text[:1].upper() + text[1:] if text else text


def add_facts(user_id, new: list[dict], source: str, stated: bool, confidence: float = 0.9,
              allow_sensitive: bool = False) -> list[dict]:
    """Add or update facts. Each new fact: {"section", "text", "attrs"?}. Returns what changed.
    Sensitive facts are dropped unless allow_sensitive (the user explicitly asked)."""
    data = get_profile(user_id)
    changed = []
    for nf in new:
        section, text = nf.get("section"), _clean_text(nf.get("text", ""))
        if section not in SECTIONS or not text:
            continue
        if not allow_sensitive and is_sensitive(text + " " + json.dumps(nf.get("attrs") or {})):
            log.info("dropped a sensitive fact")
            continue
        if section == "identity" and (nf.get("attrs") or {}).get("key") == "name" and (nf.get("attrs") or {}).get("value"):
            text = f"Name: {nf['attrs']['value']}"
        attrs = _flat_attrs(nf.get("attrs"), section)
        if attrs.get("key") == "name" and not stated:
            continue  # never guess who the user is: names only come from the user saying so
        candidate = {"text": text, "attrs": attrs}
        existing = next((f for f in data[section] if _same_fact(section, f, candidate)), None)
        if existing is None and section != "people":
            # The same fact filed under another section ("Studies X" in identity and academics).
            existing_any = next((f for s2, f in facts(data) if s2 != "people" and _near(f["text"], text)), None)
            if existing_any is not None:
                continue
        now = _now()
        if existing and section == "people" and not _near(existing["text"], text):
            # A new detail about someone we know: add it to their entry, keep what we had.
            if _norm(text) not in _norm(existing["text"]):
                existing["text"] = f"{existing['text']}; {_strip_name(text, existing)}"
                existing["attrs"] = {**attrs, **existing.get("attrs", {})}
                existing["last_confirmed"] = now
                if stated:
                    existing["stated_or_inferred"] = "stated"
                changed.append({"section": section, **existing})
            continue
        if existing:
            if existing["stated_or_inferred"] == "stated" and not stated and _norm(existing["text"]) != _norm(text):
                continue  # don't let a guess overwrite something the user told us
            updated = _norm(existing["text"]) != _norm(text)
            existing["text"] = text
            existing["attrs"] = {**existing.get("attrs", {}), **attrs}
            existing["last_confirmed"] = now
            existing["confidence"] = max(existing["confidence"], confidence)
            if stated:
                existing["stated_or_inferred"] = "stated"
            if updated:
                changed.append({"section": section, **existing})
        else:
            fact = {
                "id": uuid.uuid4().hex[:8],
                "text": text,
                "attrs": attrs,
                "source": source if source in SOURCES else "inferred",
                "stated_or_inferred": "stated" if stated else "inferred",
                "confidence": round(confidence, 2),
                "first_seen": now,
                "last_confirmed": now,
            }
            data[section].append(fact)
            changed.append({"section": section, **fact})
    if changed:
        save_profile(user_id, data)
    return changed


def find_fact(data: dict, fact_id: str):
    for s, f in facts(data):
        if f["id"] == fact_id:
            return s, f
    return None, None


def delete_fact(user_id, fact_id: str) -> dict | None:
    data = get_profile(user_id)
    s, f = find_fact(data, fact_id)
    if f is None:
        return None
    data[s].remove(f)
    save_profile(user_id, data)
    return f


def confirm_fact(user_id, fact_id: str) -> dict | None:
    data = get_profile(user_id)
    s, f = find_fact(data, fact_id)
    if f is None:
        return None
    f["stated_or_inferred"], f["confidence"], f["last_confirmed"] = "stated", 1.0, _now()
    save_profile(user_id, data)
    return f


def forget_facts(user_id, query: str) -> list[dict]:
    """Remove every fact that mentions the query (case-insensitive)."""
    q = _norm(query)
    data = get_profile(user_id)
    removed = []
    for s in SECTIONS:
        keep = []
        for f in data[s]:
            hay = _norm(f["text"] + " " + " ".join(str(v) for v in f.get("attrs", {}).values()))
            (removed if q and q in hay else keep).append(f)
        data[s] = keep
    if removed:
        save_profile(user_id, data)
    return removed


# ---------- Reading ----------


def pref(user_id, key: str) -> dict | None:
    for f in get_profile(user_id)["preferences"]:
        if f.get("attrs", {}).get("key") == key:
            return f
    return None


def quiet_hours(user_id) -> tuple[time, time] | None:
    f = pref(user_id, "quiet_hours")
    if not f:
        return None
    try:
        a = datetime.strptime(f["attrs"]["start"], "%H:%M").time()
        b = datetime.strptime(f["attrs"]["end"], "%H:%M").time()
        return a, b
    except (KeyError, ValueError):
        return None


def quiet_until(user_id, now: datetime) -> datetime | None:
    """If now is inside the user's quiet hours, the moment they end; else None."""
    qh = quiet_hours(user_id)
    if not qh:
        return None
    start, end = qh
    local = now.astimezone(TZ)
    t = local.time()
    inside = (start <= t < end) if start < end else (t >= start or t < end)
    if not inside:
        return None
    end_dt = datetime.combine(local.date(), end, tzinfo=TZ)
    return end_dt if end_dt > local else end_dt + timedelta(days=1)


def owner_name(user_id) -> str | None:
    """The user's name from the profile (only ever set from what they said)."""
    for f in get_profile(user_id)["identity"]:
        a = f.get("attrs", {})
        if a.get("key") == "name" and f["stated_or_inferred"] == "stated":
            return a.get("value") or re.sub(r"^name:\s*", "", f["text"], flags=re.I)
    return None


brain.set_owner_resolver(owner_name)


def set_identity(user_id, name: str | None = None, **facts_by_key: str) -> list[dict]:
    """Set core identity facts the user stated (name, program, location)."""
    new = []
    if name:
        new.append({"section": "identity", "text": f"Name: {name}", "attrs": {"key": "name", "value": name}})
    for key, text in facts_by_key.items():
        section = "academics" if key == "program" else "identity"
        new.append({"section": section, "text": text, "attrs": {"key": key}})
    return add_facts(user_id, new, "chat", stated=True, confidence=1.0)


def profile_summary(user_id, max_words: int = 150) -> str:
    """Compact text for prompts: who the user is, goals, people, preferences, patterns."""
    data = get_profile(user_id)
    order = ["identity", "academics", "goals", "preferences", "people", "patterns"]
    parts = []
    for s in order:
        fs = sorted(data[s], key=lambda f: (f["stated_or_inferred"] != "stated", -f["confidence"]))
        if fs:
            parts.append(f"{SECTION_TITLES[s]}: " + "; ".join(f["text"] for f in fs) + ".")
    text = " ".join(parts)
    words = text.split()
    return " ".join(words[:max_words]) + (" ..." if len(words) > max_words else "") if words else ""


def format_profile(user_id) -> str:
    """Grouped view for /me with "you told me" and "I noticed" labels."""
    data = get_profile(user_id)
    lines = []
    for s in SECTIONS:
        if not data[s]:
            continue
        lines.append(f"{SECTION_TITLES[s]}")
        for f in data[s]:
            label = "you told me" if f["stated_or_inferred"] == "stated" else "I noticed"
            lines.append(f"  • {f['text']} ({label})")
        lines.append("")
    if not lines:
        return ""
    return "🪞 What I know about you\n\n" + "\n".join(lines).rstrip()


# ---------- LLM extraction ----------

FACT_SCHEMA = """Each fact: {{"section": one of {sections}, "text": a short phrase about the user that starts
with a verb and never with their name, "I" or "my" (e.g. "Studies MS Management of Technology at
NYU Tandon", "Wants a PM role at a climate tech startup", "Prefers short replies"). For a person,
start with their name ("Omar: teammate in Statistics"). "attrs": {{...}}}}.
Put each fact in one section only: program and courses go in academics, not identity.
Keep names of projects, companies and people ("Building Voltly, an app that ...").
attrs keys:
- identity: "key" is one of name, school, year, skill, experience, location; "value" holds the
  plain value (for name: "Alex Rivera")
- academics: "course" when it is about a course
- goals: "kind" is career or project
- people: "name", "relationship" (teammate, mentor, friend, professor, ...), "context" (e.g. OM group)
- preferences: "key" is reply_style ("value": short or detailed), reminder_timing ("value": text),
  or quiet_hours ("start": "HH:MM", "end": "HH:MM", 24h)
- patterns: "kind" is routine, busy_days or topic"""

ONBOARDING_EXTRACT = """A student is setting up their profile. Question asked: "{question}"
Their answer: "{answer}"

Extract the facts their answer states about them. {sensitive}
Already known (do not repeat these, only return new or changed facts):
{known}
{schema}
Return JSON: {{"facts": [...]}}. Empty list if the answer has nothing usable."""

LEARN_PROMPT = """Does this message reveal a LASTING fact about the user (who they are, their program or
courses, goals, projects, the people in their life and their role, preferences, routines)?
Regular routines count as lasting ("every Thursday", "usually", "on weekends"): put them in patterns
(kind routine), e.g. "Meets Omar on Thursday evenings to work on the stats project".
Ignore one-off details (a single deadline, today's plans, a mood, an errand, a question they ask).
Only return facts that are NEW or CHANGE what we already know; never repeat a known fact.
Other people's names in the message are other people, never the user. Only give the user a name
(identity key name) if they say it about themselves ("my name is ...", "I am ...", "I'm ...").
{sensitive}
Say "stated" if they say it about themselves directly, "inferred" if you are reading between lines.

What we already know: {known}

Message: "{text}"

{schema}
Return JSON: {{"facts": [ {{...fact, "stated": true|false}} ]}}. Usually the list is empty."""

RESUME_CHECK = """Is this text a resume, CV or LinkedIn profile export? Answer JSON {{"resume": true|false}}.

{text}"""

RESUME_EXTRACT = """Extract a profile from this resume or LinkedIn export. Return all of these that exist:
- identity: name (attrs key name, value), location, skills as one fact, and one fact per job or
  internship (up to 3, most recent first) like "Worked as Software Engineer at GridWise Energy (2022 to 2025)"
- academics: current program, and earlier degrees as one fact
- goals: career goal from the summary, and each named project ("Building Voltly, ...") with its name
{sensitive}
Already known (do not repeat these, only return new or changed facts):
{known}
{schema}
Return JSON: {{"facts": [...]}}.

{text}"""

EDIT_PROMPT = """The user wants to correct their profile. Current facts (id: section: text):
{facts}

Their instruction: "{instruction}"

Return JSON: {{"ops": [ {{"op": "update", "id": "...", "text": "...", "attrs": {{...}}}},
{{"op": "delete", "id": "..."}}, {{"op": "add", "section": "...", "text": "...", "attrs": {{...}}}} ]}}.
Only do what they asked. Words like "also", "and" or "too" mean add, not replace. {sensitive}
{schema}"""

REFLECT_PROMPT = """Weekly reflection about a student. Today is {today}.

Profile now:
{profile}

Their messages this week:
{messages}

Their calendar this week and next:
{agenda}

1. Patterns: lasting routines, busy days, recurring topics you can see (max 4). Skip anything
   already in the profile. Write each as a short phrase starting with a verb, never with their
   name ("Meets Omar on Thursday evenings", "Has most deadlines on Fridays"). {sensitive}
2. Up to 2 short yes/no questions to confirm the most useful "I noticed" facts (ids from the
   profile marked inferred, or from your new patterns as "new:N" by index).

Return JSON: {{"patterns": [{{"text": "...", "attrs": {{"kind": "routine|busy_days|topic"}}, "confidence": 0.0-1.0}}],
"questions": [{{"fact": "id or new:N", "question": "..."}}]}}"""


def _schema() -> str:
    return FACT_SCHEMA.format(sections=SECTIONS)


def _listing(user_id) -> str:
    return "\n".join(f"- [{s}] {f['text']}" for s, f in facts(get_profile(user_id))) or "(nothing yet)"


def _known(user_id) -> str:
    return profile_summary(user_id, max_words=80) or "nothing yet"


async def extract_onboarding(user_id, question: str, answer: str) -> list[dict]:
    data = await llm.chat_json([{"role": "user", "content": ONBOARDING_EXTRACT.format(
        question=question, answer=answer, sensitive=SENSITIVE_RULE, schema=_schema(),
        known=_listing(user_id))}])
    return add_facts(user_id, data.get("facts") or [], "onboarding", stated=True, confidence=1.0)


SELF_DESCRIPTION = """The user wrote this about themselves (a pasted profile or self-introduction).
Everything in it is stated by them. "I", "me" and "my" are the user; every other name is someone else
(a teammate, professor, mentor, founder, ...), never the user.
Extract their profile: identity (name with attrs key name and value, location, background),
academics (program, courses with professors), goals (career goals, each named project), people
(one fact per person with name and relationship), preferences, patterns. {sensitive}
Already known (do not repeat, only new or changed):
{known}
{schema}
Return JSON: {{"facts": [...]}}.

{text}"""


async def seed_from_self_description(user_id, text: str) -> list[dict]:
    data = await llm.chat_json([{"role": "user", "content": SELF_DESCRIPTION.format(
        text=text[:15000], sensitive=SENSITIVE_RULE, schema=_schema(), known=_listing(user_id))}])
    return add_facts(user_id, data.get("facts") or [], "chat", stated=True, confidence=0.95)


def looks_like_self_description(text: str) -> bool:
    return len(text) > 600 and bool(re.search(r"\b(about me|my profile|i am|i'm|my name|myself)\b", text, re.I))


async def learn(user_id, text: str) -> list[dict]:
    """Background check after each message: store lasting facts, ignore one-offs."""
    if len(text.split()) < 3:
        return []
    if looks_like_self_description(text):
        return await seed_from_self_description(user_id, text)
    data = await llm.chat_json([{"role": "user", "content": LEARN_PROMPT.format(
        text=text, known=_known(user_id), sensitive=SENSITIVE_RULE, schema=_schema())}])
    found = data.get("facts") or []
    explicit = bool(re.search(r"\b(remember|note|don't forget|save) (that|this)\b", text, re.I))
    changed = []
    for stated in (True, False):
        group = [f for f in found if bool(f.get("stated")) == stated]
        if group:
            changed += add_facts(user_id, group, "chat" if stated else "inferred",
                                 stated=stated, confidence=0.9 if stated else 0.6,
                                 allow_sensitive=explicit and stated)
    if changed:
        log.info("learned %d fact(s) for %s", len(changed), user_id)
    return changed


def looks_like_resume(text: str, caption: str | None, file_name: str) -> bool | None:
    """True/False when obvious from caption or file name, None when the LLM should decide."""
    hint = f"{caption or ''} {file_name}".lower()
    if re.search(r"\b(resume|résumé|cv|linkedin)\b", hint):
        return True
    words = text.lower()
    hits = sum(w in words for w in ("experience", "education", "skills", "linkedin", "projects", "summary"))
    return None if hits >= 3 else False


async def is_resume(text: str, caption: str | None, file_name: str) -> bool:
    quick = looks_like_resume(text, caption, file_name)
    if quick is not None:
        return quick
    data = await llm.chat_json([{"role": "user", "content": RESUME_CHECK.format(text=text[:3000])}])
    return bool(data.get("resume"))


async def seed_from_resume(user_id, text: str) -> list[dict]:
    data = await llm.chat_json([{"role": "user", "content": RESUME_EXTRACT.format(
        text=text[:12000], sensitive=SENSITIVE_RULE, schema=_schema(), known=_listing(user_id))}])
    return add_facts(user_id, data.get("facts") or [], "document", stated=True, confidence=0.95)


async def edit(user_id, instruction: str) -> list[str]:
    """Apply a plain-language correction. Returns short descriptions of what changed."""
    data = get_profile(user_id)
    listing = "\n".join(f"{f['id']}: {s}: {f['text']}" for s, f in facts(data)) or "(empty)"
    ops = (await llm.chat_json([{"role": "user", "content": EDIT_PROMPT.format(
        facts=listing, instruction=instruction, sensitive=SENSITIVE_RULE, schema=_schema())}])).get("ops") or []
    done = []
    for op in ops:
        kind = op.get("op")
        if kind == "delete":
            f = delete_fact(user_id, str(op.get("id")))
            if f:
                done.append(f"Removed: {f['text']}")
        elif kind == "update":
            data = get_profile(user_id)
            s, f = find_fact(data, str(op.get("id")))
            new_attrs = _flat_attrs(op.get("attrs"), s)
            if f and s != "identity" and (new_attrs.get("key") == "name" or str(op.get("text", "")).lower().startswith("name:")):
                # Never turn someone else's entry into the user's name: add it to identity instead.
                name = new_attrs.get("value") or re.sub(r"^name:\s*", "", str(op["text"]), flags=re.I).strip()
                for nf in set_identity(user_id, name=name):
                    done.append(f"Updated: {nf['text']}")
                continue
            if f and op.get("text"):
                f["attrs"] = {**f.get("attrs", {}), **new_attrs}
                f["text"] = _clean_text(op["text"])
                if f["attrs"].get("key") == "name" and f["attrs"].get("value"):
                    f["text"] = f"Name: {f['attrs']['value']}"
                f.update(stated_or_inferred="stated", source="chat", confidence=1.0, last_confirmed=_now())
                save_profile(user_id, data)
                done.append(f"Updated: {f['text']}")
        elif kind == "add":
            for f in add_facts(user_id, [op], "chat", stated=True, confidence=1.0, allow_sensitive=True):
                done.append(f"Added: {f['text']}")
    return done


async def reflect(user_id, messages: list[str], agenda: str) -> list[dict]:
    """Update patterns from the week and queue up to 2 confirmation questions.
    Returns the questions queued (each {"fact", "question"})."""
    data = get_profile(user_id)
    listing = "\n".join(
        f"{f['id']} [{s}, {f['stated_or_inferred']}]: {f['text']}" for s, f in facts(data)
    ) or "(empty)"
    today = datetime.now(TZ)
    out = await llm.chat_json([{"role": "user", "content": REFLECT_PROMPT.format(
        today=f"{today:%A %b} {today.day}", profile=listing,
        messages="\n".join(f"- {m}" for m in messages[-80:]) or "(none)",
        agenda=agenda or "(empty)", sensitive=SENSITIVE_RULE)}])

    new_ids = []
    for p in out.get("patterns") or []:
        added = add_facts(user_id, [{"section": "patterns", "text": p.get("text", ""), "attrs": p.get("attrs")}],
                          "inferred", stated=False, confidence=float(p.get("confidence") or 0.6))
        new_ids.append(added[0]["id"] if added else None)

    data = get_profile(user_id)
    questions = []
    # The model picks which inferred facts to check; the wording is always a plain yes/no
    # confirmation of that fact, so "yes" and "no" map cleanly to keep or delete.
    picked = []
    for q in out.get("questions") or []:
        ref = str(q.get("fact", ""))
        fid = new_ids[int(ref[4:])] if ref.startswith("new:") and ref[4:].isdigit() and int(ref[4:]) < len(new_ids) else ref
        if fid:
            picked.append(fid)
    picked += [i for i in new_ids if i]
    questions = []
    for fid in dict.fromkeys(picked):
        s, f = find_fact(data, fid)
        if f and f["stated_or_inferred"] == "inferred":
            t = f["text"]
            questions.append({"fact": fid, "question": f"I noticed this about you: {t[:1].lower() + t[1:]}. Is that right?"})
        if len(questions) == 2:
            break
    data["_meta"]["pending_questions"] = questions
    data["_meta"]["last_reflection"] = today.date().isoformat()
    save_profile(user_id, data)
    return questions


def next_question(user_id) -> dict | None:
    qs = meta(user_id).get("pending_questions") or []
    return qs[0] if qs else None


def answer_question(user_id, reply: str) -> tuple[str, dict | None]:
    """Apply a yes/no answer to the first pending question. Returns (outcome, fact)."""
    data = get_profile(user_id)
    qs = data["_meta"].get("pending_questions") or []
    if not qs:
        return "none", None
    q = qs.pop(0)
    data["_meta"]["pending_questions"] = qs
    save_profile(user_id, data, dirty=False)
    r = reply.strip().lower()
    if re.match(r"^(y|yes|yeah|yep|yup|correct|right|true|sure|kind of|mostly)\b", r):
        return "confirmed", confirm_fact(user_id, q["fact"])
    if re.match(r"^(n|no|nope|nah|wrong|not really|false)\b", r):
        return "deleted", delete_fact(user_id, q["fact"])
    return "other", None


# ---------- Cognee "me" entity ----------


def memory_document(user_id) -> str:
    """First-person profile text so the graph gets one central "me" entity linked to
    courses, projects and people."""
    data = get_profile(user_id)
    name = next((f["attrs"].get("value") or f["text"] for f in data["identity"]
                 if f.get("attrs", {}).get("key") == "name"), None)
    me = re.sub(r"^(my name is|name is|name|is named|named)[:\s]+", "", name, flags=re.I).strip() if name else "the user"
    lines = [f"Core profile of {me} (me, the user of Violet Memory)."]
    for s in SECTIONS:
        for f in data[s]:
            a = f.get("attrs", {})
            if s == "people" and a.get("name"):
                rel = a.get("relationship") or "someone I know"
                ctx = f" ({a['context']})" if a.get("context") else ""
                lines.append(f"{a['name']} is my {rel}{ctx}. {f['text']}")
            else:
                lines.append(f"{SECTION_TITLES[s]}: {me}: {f['text']}")
    return "\n".join(lines)


async def sync_to_memory(user_id, force: bool = False) -> bool:
    """Replace the profile document in the user's Cognee dataset (tagged core_profile).
    Returns True if it was written; call brain.connect(user_id) afterwards."""
    data = get_profile(user_id)
    if not force and not data["_meta"].get("memory_dirty"):
        return False
    if not any(True for _ in facts(data)):
        return False
    doc = memory_document(user_id)
    data_id = await brain.replace_document(user_id, doc, data["_meta"].get("memory_data_id"), CORE_NODE_SET)
    data = get_profile(user_id)
    data["_meta"].update(memory_data_id=data_id, memory_dirty=False)
    save_profile(user_id, data, dirty=False)
    return True
