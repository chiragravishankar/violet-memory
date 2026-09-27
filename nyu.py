"""NYU Helper: answers about NYU (with sources), the right office, professors' contacts, study rooms,
library hours. No channel code here.

Where answers come from, in order:
1. The shared knowledge base (nyu_kb: crawled nyu.edu pages, plus the shared Cognee dataset).
2. If that isn't confident: a live web search restricted to nyu.edu domains (OpenAI Responses API
   web_search tool with filters.allowed_domains; same LLM_API_KEY, no extra key).
Every answer carries its source links and how recent they are.

Professor emails are only ever taken from somewhere they literally appear (the user's own documents,
their mail for the owner, or an NYU faculty page we fetched and checked). Never guessed or built from a
name or NetID pattern.
"""

import asyncio
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

import httpx

import brain
import libcal
import llm
import nyu_kb
import paths
import selfmodel
import users
from schedule import TZ

log = logging.getLogger("nyu")

OFFICES_FILE = paths.ROOT / "config" / "nyu_offices.json"
NYU_DOMAINS = ["nyu.edu"]  # includes engineering.nyu.edu, stern.nyu.edu, library.nyu.edu, ...
LIVE_MODEL = "gpt-5.4"
ANSWER_MODEL = "gpt-4.1-mini"
SENSITIVE = re.compile(r"\b(visa|f-?1|j-?1|i-?20|cpt|opt|stem opt|immigration|sevis|work authori[sz]ation|health|"
                       r"medical|insurance|sick|therapy|counsel|mental|legal|lawyer|lawsuit|financial aid|fafsa|loan|"
                       r"scholarship|aid package)\b", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def sensitive(question: str) -> bool:
    return bool(SENSITIVE.search(question))


# ---------- offices ----------

def offices() -> list[dict]:
    try:
        return json.loads(OFFICES_FILE.read_text())["offices"]
    except Exception:
        return []


def office(topic: str, n: int = 2) -> list[dict]:
    """The office(s) that handle a topic, with contact details and where each came from."""
    words = {w for w in re.findall(r"[a-z0-9]+", topic.lower()) if len(w) > 2} - nyu_kb._STOP
    scored = []
    for o in offices():
        hay = " ".join([o.get("name", "")] + o.get("topics", [])).lower()
        score = sum(2 if w in o.get("name", "").lower() else 1 for w in words if w in hay)
        if score:
            scored.append((score, o))
    scored.sort(key=lambda x: -x[0])
    keep = ("name", "url", "email", "phone", "location", "hours", "note", "sources", "checked")
    return [{k: o.get(k) for k in keep if o.get(k)} for _, o in scored[:n]]


# ---------- answers ----------

ANSWER_TASK = """Answer the student's NYU question using ONLY the NYU page excerpts below.
Campus facts: Tandon is in Brooklyn (MetroTech) and its library is Dibner; Bobst is at Washington Square.
Answer for the campus the student means.
Question: {question}

Excerpts:
{pages}

Return JSON: {{"answer": "2 to 5 plain sentences with the concrete facts (dates, places, steps, contacts)",
"confident": true only if the excerpts clearly and directly answer it (not just related),
"sources": ["the URLs you actually used"], "as_of": "the fetched date of those pages"}}"""


# Campus words that point to a specific library or building, so "print at Tandon" finds the Dibner pages.
CAMPUS_HINTS = {"tandon": "dibner brooklyn", "brooklyn": "dibner", "metrotech": "dibner", "washington square": "bobst",
                "stern": "bobst"}


async def search(question: str) -> dict:
    """What NYU's own pages say, with sources. Knowledge base first, live nyu.edu search if needed."""
    low = question.lower()
    hints = " ".join(v for k, v in CAMPUS_HINTS.items() if k in low)
    pages = nyu_kb.search(f"{question} {hints}", k=5)
    kb_graph = asyncio.ensure_future(brain.shared_context(nyu_kb.DATASET, question, wait=5))
    out = None
    if pages:
        text = "\n\n".join(f"[{p['url']}] (fetched {p['fetched']}) {p['title']}\n{p['excerpt']}" for p in pages)
        graph = await kb_graph
        if graph:
            text += f"\n\n[knowledge graph, same pages]\n{graph[:2000]}"
        try:
            data = await llm.chat_json([{"role": "user", "content": ANSWER_TASK.format(question=question, pages=text[:14000])}],
                                       model_name=ANSWER_MODEL)
            if data.get("confident") and data.get("answer"):
                out = {"answer": data["answer"], "sources": data.get("sources") or [p["url"] for p in pages[:2]],
                       "as_of": data.get("as_of") or pages[0]["fetched"], "from": "NYU knowledge base (crawled nyu.edu pages)"}
        except Exception as e:
            log.info("kb answer failed (%s)", type(e).__name__)
    else:
        kb_graph.cancel()
    if out is None:
        out = await live(question)
    if sensitive(question):
        out["rule"] = ("Sensitive topic (immigration, health, legal or financial aid): give the official information "
                       "and the right office only, no personal advice.")
        out["office"] = office(question, 1)
    return out


async def live(question: str) -> dict:
    """A live web search limited to nyu.edu, answered with citations."""
    try:
        r = await asyncio.wait_for(llm.client().responses.create(
            model=LIVE_MODEL,
            input=(f"A student asks: {question}\nAnswer from official NYU pages only, briefly (2 to 5 sentences), "
                   "with the concrete facts. If the pages don't say, say so."),
            tools=[{"type": "web_search", "filters": {"allowed_domains": NYU_DOMAINS}}]), 25)
    except Exception as e:
        log.warning("live nyu search failed (%s)", type(e).__name__)
        return {"answer": "", "sources": [], "from": "live search failed", "unsure": True}
    llm.track(r)
    sources = []
    for item in r.output:
        for c in getattr(item, "content", None) or []:
            for a in getattr(c, "annotations", None) or []:
                url = getattr(a, "url", None)
                if url and url not in sources:
                    sources.append(url.split("?utm_source")[0])
    text = re.sub(r"\s*\(\[[^\]]+\]\([^)]+\)\)", "", r.output_text).strip()  # links go in sources instead
    return {"answer": text, "sources": sources[:4], "as_of": date.today().isoformat() + " (live search)",
            "from": "live search of nyu.edu"}


# ---------- professors ----------

def _surname(name: str) -> str:
    parts = [p for p in re.sub(r"\b(prof(essor)?|dr)\.?\s+", "", name, flags=re.I).split() if p]
    return parts[-1].lower() if parts else ""


async def _doc_texts(user_id) -> list[tuple[str, str]]:
    """(file name, text) of the user's own documents (text formats only; no OCR here)."""
    import docs

    out = []
    for p in sorted((paths.FILES_DIR / str(user_id)).glob("*")):
        if p.suffix.lower() not in (".pdf", ".docx", ".txt", ".md"):
            continue
        try:
            text = (await docs.extract(p)).text
        except Exception:
            continue
        out.append((re.sub(r"^\d{8}_\d{6}_", "", p.name), text))
    return out


def _near(text: str, surname: str, width: int = 700) -> list[str]:
    low = text.lower()
    out, i = [], low.find(surname)
    while i >= 0 and len(out) < 6:
        out.append(text[max(0, i - width // 2): i + width])
        i = low.find(surname, i + 1)
    return out


def _from_documents(texts, surname: str) -> dict:
    found = {"emails": [], "office_hours": [], "office": []}
    for fname, text in texts:
        for w in _near(text, surname):
            for e in _EMAIL.findall(w):
                local = e.split("@")[0].lower()
                if surname[:4] in local or re.search(rf"{re.escape(surname)}[^\n]{{0,120}}{re.escape(e)}", w, re.I | re.S):
                    found["emails"].append({"email": e, "source": f"your document “{fname}”"})
            m = re.search(r"office hours?\s*[:\-–]?\s*([^\n]{5,140})", w, re.I)
            if m:
                found["office_hours"].append({"text": m.group(1).strip(), "source": f"your document “{fname}”"})
            m = re.search(r"\boffice(?!\s*hours?)\s*(?:location|room)?\s*[:\-–]\s*([^\n]{3,90})", w, re.I)
            if m:
                found["office"].append({"text": m.group(1).strip(), "source": f"your document “{fname}”"})
    return found


def _from_mail(user_id, surname: str) -> list[dict]:
    if not users.is_owner(user_id):
        return []
    try:
        import inbox

        rows = inbox._db.execute("SELECT from_addr, from_name, subject, date FROM emails WHERE user_id = ? AND mine = 0 "
                                 "AND lower(from_name) LIKE ? ORDER BY date DESC LIMIT 5",
                                 (str(user_id), f"%{surname}%")).fetchall()
    except Exception:
        return []
    return [{"email": r["from_addr"], "source": f"their email to you (“{(r['subject'] or '')[:60]}”, {r['date'][:10]})"}
            for r in rows if r["from_addr"]]


PROFILE_TASK = """Find the official NYU faculty profile page for {name}{course}. Prefer engineering.nyu.edu
(Tandon) or stern.nyu.edu faculty directories, else another nyu.edu page. Return ONLY JSON:
{{"profile_url": "...", "email": "only if it is written on that page, else null",
"office": "only if on the page, else null", "office_hours": "only if on the page, else null", "title": "..."}}"""


async def _from_web(name: str, course: str) -> dict:
    """A faculty page found by a live nyu.edu search, then fetched and checked: an email only counts if
    it's literally on the page we fetched."""
    try:
        r = await asyncio.wait_for(llm.client().responses.create(
            model=LIVE_MODEL, input=PROFILE_TASK.format(name=name, course=f" (teaches {course})" if course else ""),
            tools=[{"type": "web_search", "filters": {"allowed_domains": NYU_DOMAINS}}]), 25)
        llm.track(r)
        m = re.search(r"\{.*\}", r.output_text, re.S)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        log.info("faculty search failed (%s)", type(e).__name__)
        return {}
    url = str(data.get("profile_url") or "")
    if not re.match(r"^https://([a-z0-9-]+\.)*nyu\.edu/", url):
        return {}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": nyu_kb.UA}) as c:
            page = (await c.get(url)).text
    except httpx.HTTPError:
        return {"profile_url": url}
    page_text = re.sub(r"<[^>]+>", " ", page)
    out = {"profile_url": url, "title": data.get("title")}
    email = str(data.get("email") or "")
    if email and email.lower() in page.lower():
        out["email"] = email
    elif email:
        log.info("dropped a faculty email that wasn't on the page")
    for key in ("office", "office_hours"):
        v = str(data.get(key) or "")
        if v and all(w.lower() in page_text.lower() for w in re.findall(r"[A-Za-z0-9]{3,}", v)[:3]):
            out[key] = v
    return out


def _save_professor(user_id, name: str, email: str, source: str, extra: dict) -> None:
    """Keep what was found on the professor's entry in the people store (matched by surname)."""
    surname = _surname(name)
    data = selfmodel.get_profile(user_id)
    for f in data["people"]:
        n = (f.get("attrs", {}).get("name") or f["text"].split(":")[0]).lower()
        if surname and surname in n.split():
            f.setdefault("attrs", {}).update(email=email, email_source=source, **extra)
            selfmodel.save_profile(user_id, data)
            return
    selfmodel.add_facts(user_id, [{"section": "people", "text": f"{name}: professor",
                                   "attrs": {"name": name, "relationship": "professor", "email": email,
                                             "email_source": source, **extra}}], "chat", stated=True, confidence=0.9)


async def find_professor(user_id, name: str, course: str = "") -> dict:
    """Email, office hours and office for a professor, each with where it came from."""
    surname = _surname(name)
    if not surname:
        return {"error": "need a name"}
    result = {"name": name, "emails": [], "office_hours": [], "office": [], "never_guessed": True}
    for f in selfmodel.get_profile(user_id)["people"]:
        a = f.get("attrs") or {}
        n = (a.get("name") or f["text"].split(":")[0]).lower()
        if surname in n.split() and a.get("email"):
            result["emails"].append({"email": a["email"], "source": a.get("email_source") or "your people list"})
            if a.get("office_hours"):
                result["office_hours"].append({"text": a["office_hours"], "source": a.get("email_source") or "your people list"})
    texts = await _doc_texts(user_id)
    docs_found = _from_documents(texts, surname)
    for k in result:
        if isinstance(result[k], list):
            result[k] += docs_found.get(k, [])
    result["emails"] += _from_mail(user_id, surname)
    if not result["emails"] or not result["office_hours"]:
        web = await _from_web(name, course)
        if web.get("profile_url"):
            result["profile_page"] = web["profile_url"]
            src = f"NYU faculty page {web['profile_url']}"
            if web.get("email"):
                result["emails"].append({"email": web["email"], "source": src})
            if web.get("office_hours"):
                result["office_hours"].append({"text": web["office_hours"], "source": src})
            if web.get("office"):
                result["office"].append({"text": web["office"], "source": src})
    seen = set()
    result["emails"] = [e for e in result["emails"] if not (e["email"].lower() in seen or seen.add(e["email"].lower()))]
    if result["emails"]:
        best = result["emails"][0]
        extra = {k: v[0]["text"] for k, v in (("office_hours", result["office_hours"]), ("office", result["office"])) if v}
        _save_professor(user_id, name, best["email"], best["source"], extra)
        result["saved_to_people"] = True
    else:
        result["not_found"] = ("No email found in your documents" + (", your mail" if users.is_owner(user_id) else "")
                               + " or an NYU faculty page. Look in the course syllabus on Brightspace (instructor "
                               "info), NYU Home people search (sign in with your NetID): https://globalhome.nyu.edu/people/search/, "
                               "or the Tandon faculty directory: https://engineering.nyu.edu/about/our-engineers/faculty")
    return result


# ---------- study rooms and hours ----------

def _time(s: str):
    s = (s or "").strip().lower().replace(".", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        raise ValueError("time")
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    from datetime import time as dtime

    return dtime(h, mi)


async def study_room(library: str, day: str, start: str, end: str, people: int = 1) -> dict:
    lib = "bobst" if "bobst" in (library or "").lower() else "dibner"  # Dibner is closest to Tandon
    try:
        d = date.fromisoformat(day[:10])
        t0, t1 = _time(start), _time(end)
    except ValueError:
        return {"error": "need date as YYYY-MM-DD and times like 15:00"}
    out = await libcal.find(lib, d, t0, t1, max(1, int(people or 1)))
    out["offer_next"] = ("Offer: add it to their calendar, tell teammates (you can draft the message), and a reminder "
                         "15 minutes before (add_study_room_reminder) because of the check-in grace period.")
    return out


async def library_hours(library: str) -> dict:
    lib = "dibner" if "dibner" in (library or "").lower() else "bobst"
    return await libcal.is_open(lib)
