"""Understanding messages: classify, answer questions from schedule + memory, corrections and
reminders. Every reply is written by the voice layer (voice.py). No channel code here."""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import brain
import llm
import schedule
import selfmodel
import views
import voice
from fmt import View
from schedule import TZ

log = logging.getLogger("assistant")

QUESTION, SCHEDULE, MEMORY, CORRECTION = "question", "schedule", "memory", "correction"
FALLBACK = "Hmm, I couldn't pull that up just now. Try me again in a sec?"

CLASSIFY_TASK = """Classify their message, and if it is a "memory", also write your reply to it.

Types:
- "question": the user wants information back. Includes questions without a question mark and
  commands like "show me tuesday", "who is hena", "what work do i have next week", "list my exams".
- "schedule": the user is telling you about something with a date or time to track: a deadline,
  exam, class, meeting or event ("marketing draft due next Friday 11:59pm", "stats exam on Oct 12").
- "memory": a fact, person, idea or note to keep with nothing to schedule
  ("Met Dana at the design mixer, she builds climate tools").
- "correction": the user is correcting something about themselves or something you said:
  "I am X", "my name is X", "actually ...", "no, ...", "that's wrong, ...", "bro, i am chirag".

If unsure between question and memory, choose question.
about_schedule: the message is about their schedule (deadlines, classes, events, days, what is due).
about_user: a question about themselves or their own life (their name, who the user is, what the user studies, their
courses, projects, goals, the people in their life, their preferences).
ack: only for "memory": your reply acknowledging the specific thing, in your voice, 1 or 2 short
sentences, sometimes with one helpful beat. Empty string otherwise.

Return JSON: {{"type": "question|schedule|memory|correction", "about_schedule": true|false, "about_user": true|false, "ack": "..."}}

Their message: \"\"\"{text}\"\"\""""

ADVICE_TASK = """The user asked: "{question}"

Give them 3 to 5 concrete priorities, one per line (numbering is fine here). Hard deadlines first, in
date order, each with its day and time (group several into one line if it reads better). Then tie
the rest to their goals and projects by name, and mention a teammate or mentor if relevant.
Every hard deadline below must appear. Only use dates and times from the agenda; don't invent
times for goal work.

Their agenda from {start} to {end}:
{agenda}

Hard deadlines in that period:
{deadlines}

Other things you know that may help: {context}"""

ABOUT_USER_TASK = """The user asked about themselves: "{question}"

What the user has told you about themselves (this is the truth about them):
{profile}

Other things you know (can mix people up; if they conflict with the above, the above wins, and
any other name in them is someone else):
{context}

Answer them. If you don't know, say so and ask."""

MEMORY_TASK = """The user asked: "{question}"

What you know that's relevant (from things the user told you, files the user sent and their calendars; names
other than their are other people):
{context}

Answer them. If you don't know, say so plainly and ask for the missing piece."""

_ADVICE = re.compile(r"\b(focus|prioriti[sz]e|priorities|plan my|should i|what should|where should|how should|advice)\b", re.I)
_SCHEDULE_WORDS = re.compile(
    r"\b(schedule|calendar|agenda|due|deadline|class|classes|lecture|meeting|meetings|exam|exams|"
    r"quiz|assignment|assignments|homework|busy|free|plans?|week|today|tomorrow|tonight|weekend)\b",
    re.I,
)
_CORRECTION = re.compile(
    r"^\s*(bro,?\s*|dude,?\s*)?(my name is|i am\b(?! (going|gonna|meeting|at|in|on|free|busy|done|not sure|working on|taking|looking|trying|so|very|really|tired|feeling))|"
    r"i'm\s+[A-Z]\w+\s+[A-Z]\w+|actually\b|no,|nope,|that's wrong|thats wrong|wrong,)",
    re.I,
)
_NAME = re.compile(
    r"^\s*(?:bro,?\s*|dude,?\s*|no,?\s*|actually,?\s*)*(?:my name is|i am|i'm|im|call me)\s+"
    r"([A-Za-z][A-Za-z'-]+(?:\s+[A-Za-z][A-Za-z'-]+){0,3})\s*[.!]*\s*$",
    re.I,
)
_NOT_NAME_WORDS = {
    "a", "an", "the", "to", "at", "in", "on", "of", "for", "from", "with", "and", "not", "so", "very", "really",
    "here", "there", "back", "home", "sure", "fine", "ok", "okay", "good", "great", "happy", "sad", "sorry",
    "ready", "late", "early", "tired", "busy", "free", "done", "sick", "gym", "student", "class", "work",
    "school", "bored", "hungry", "confused", "lost", "stuck", "new", "just", "still", "also", "your", "you",
}


@dataclass
class Classification:
    type: str
    about_schedule: bool
    about_user: bool = False
    ack: str = ""  # Violet's reply for a "memory" message, written in the same call


def _fallback(text: str) -> Classification:
    return Classification(QUESTION if text.rstrip().endswith("?") else MEMORY, False)


async def classify(text: str, user_id=None, recent: list[str] | None = None) -> Classification:
    """Classify a message. With user_id, a "memory" also gets its acknowledgment in the
    same call (Violet's voice), so simple saves need only one model call."""
    messages = [{"role": "user", "content": CLASSIFY_TASK.format(text=text)}]
    if user_id is not None:
        messages.insert(0, {"role": "system", "content": voice.system_prompt(user_id, recent)})
    try:
        data = await llm.chat_json(messages, model_name=voice.voice_model(), temperature=0.4)
    except Exception as e:
        log.warning("classify failed (%s), using fallback", type(e).__name__)
        return _fallback(text)
    kind = str(data.get("type", "")).lower()
    if kind not in (QUESTION, SCHEDULE, MEMORY, CORRECTION):
        return _fallback(text)
    if kind != CORRECTION and _CORRECTION.match(text):
        kind = CORRECTION  # "my name is X" / "actually, ..." even if the model missed it
    ack = str(data.get("ack") or "")
    if ack and voice.mentions_internals(ack):
        ack = ""  # let the caller voice it again
    budget, heart_ok = voice.emoji_budget(recent)
    if not heart_ok:
        ack = ack.replace("💜", "")
    return Classification(kind, bool(data.get("about_schedule")), bool(data.get("about_user")),
                          voice.vary_opener(voice.trim_offer(voice.polish(ack, budget), recent), recent) if ack else "")


def is_schedule_question(question: str, about_schedule: bool, today) -> bool:
    return about_schedule or bool(_SCHEDULE_WORDS.search(question)) or schedule.parse_range(question, today) is not None


async def _context(user_id, question: str, limit: int = 2500) -> str:
    return (await brain.context_for(user_id, question, wait=8))[:limit] or brain.fallback_note() or "nothing relevant"


async def answer(user_id, question: str, about_schedule: bool, now: datetime | None = None,
                 about_user: bool = False, recent: list[str] | None = None) -> "str | View":
    """Answer a question in Violet's voice: schedule questions from the same agenda as /week
    (plus graph context), questions about them from their profile first, others from the graph."""
    now = now or datetime.now(TZ)
    try:
        return await _answer(user_id, question, about_schedule, now, about_user, recent)
    except Exception as e:
        log.warning("answer failed (%s)", type(e).__name__)
        return FALLBACK


async def _answer(user_id, question, about_schedule, now, about_user, recent) -> str:
    advice = bool(_ADVICE.search(question))
    if about_user and not advice and not about_schedule:
        listing = "\n".join(f"- {f['text']}" for _, f in selfmodel.facts(selfmodel.get_profile(user_id))) or "(nothing yet)"
        task = ABOUT_USER_TASK.format(question=question, profile=listing, context=await _context(user_id, question, 1500))
        return await voice.compose(user_id, task, recent)
    if not advice and not is_schedule_question(question, about_schedule, now.date()):
        task = MEMORY_TASK.format(question=question, context=await _context(user_id, question))
        return await voice.compose(user_id, task, recent)

    start, end, period = schedule.parse_range(question, now.date()) or (
        now.date(), now.date() + timedelta(days=6), "the next 7 days"
    )
    if advice:
        # Planning needs room to look ahead, even when "this week" is only the weekend.
        end = max(end, now.date() + timedelta(days=6))
        lines = schedule.agenda_lines(user_id, start, end, now)
        deadlines = schedule.agenda_lines(user_id, start, end, now, schedule.DEADLINE_KINDS)
        task = ADVICE_TASK.format(
            question=question, start=schedule.fmt_day(start), end=schedule.fmt_day(end),
            agenda="\n".join(lines) or "(nothing scheduled)", deadlines="\n".join(deadlines) or "(none)",
            context=await _context(user_id, question, 1200),
        )
        log.info("advice question: %s to %s, %d agenda lines", start, end, len(lines))
        return await voice.compose(user_id, task, recent, max_emoji=3)

    kinds = schedule.parse_kinds(question)
    log.info("schedule question: %s, %s to %s, kinds %s", period, start, end, sorted(kinds) if kinds else "all")
    # Rendered by code from the shared agenda: same layout as /week, every time.
    return views.period_view(user_id, start, end, period, kinds, now)


# ---------- Corrections ----------


def _looks_like_name(candidate: str) -> bool:
    words = candidate.lower().split()
    return (
        1 <= len(words) <= 4
        and not any(w in _NOT_NAME_WORDS for w in words)
        and not words[0].endswith(("ing", "ed"))
    )


async def correct(user_id, text: str, last_reply: str | None = None, recent: list[str] | None = None) -> str:
    """Apply a correction: update the profile, save the corrected fact, confirm in Violet's voice."""
    m = _NAME.match(text)
    if m and _looks_like_name(m.group(1)):
        # "i am chirag ravishankar": set the name directly, no model involved.
        name = " ".join(w[:1].upper() + w[1:] for w in m.group(1).split())
        selfmodel.set_identity(user_id, name=name)
        await brain.correct(user_id, f"My name is {name}.", "what my name is")
        draft = f"Fixed their name: the user is {name}. Own the mistake briefly."
    else:
        instruction = text if not last_reply else f'You had just said: "{last_reply[:300]}". The user replied: "{text}"'
        changes = await selfmodel.edit(user_id, instruction)
        await brain.correct(user_id, "; ".join(changes) if changes else text, text)
        draft = ("Fixed. What changed: " + "; ".join(changes)) if changes else "Took the correction on board."
    return await voice.say(user_id, draft, kind="correction", user_message=text, recent=recent)


# ---------- Reminders ----------


CONTEXT_TASK = """Write ONE short line (max 12 words) for a reminder about {what}. Only facts below.
Most useful first: a teammate or group deliverable for this course, then the next deadline.
Say deadlines like a person ("your marketing section is due Friday"). If nothing useful, return: none

Coming up for this course: {upcoming}
What you know about it: {memory}"""


async def reminder_message(user_id, it: schedule.Item, lead: str, now: datetime,
                           recent: list[str] | None = None) -> View:
    """Compact reminder view. Classes get one context line (teammates, what's due next)."""
    context = None
    if it.kind in ("class", "class_session"):
        course = it.course or re.split(r"\s+-\s+", it.title)[0]
        upcoming = schedule.upcoming_for_course(user_id, course, now, limit=3)
        try:
            mem = await asyncio.wait_for(_context(user_id, f"Who are my teammates in {course}, and what have I noted about {course}?", 1200), 6)
        except asyncio.TimeoutError:
            mem = "nothing"
        task = CONTEXT_TASK.format(
            what=course,
            upcoming="; ".join(f"{i.title}, {schedule.fmt_due(i)}" for i in upcoming) or "nothing",
            memory=mem,
        )
        try:
            line = (await voice.compose(user_id, task, recent, max_emoji=0)).splitlines()[0].strip()
            line = re.split(r"(?<=[.!?])\s+", line)[0]  # one short line
            context = None if not line or line.lower().strip(". ") == "none" else line
        except Exception as e:
            log.warning("reminder context failed (%s)", type(e).__name__)
    return views.reminder_view(it, lead, context)


# ---------- Events ----------

_EVENT_WORDS = re.compile(
    r"\b(events?|happening|what'?s on|whats on|going on|workshops?|mixers?|meetups?|talks?|panels?|fairs?|"
    r"info sessions?|networking|hackathons?|pitch nights?|speakers?|career events?)\b",
    re.I,
)

def is_event_question(text: str) -> bool:
    return bool(_EVENT_WORDS.search(text))


LEAD_TASK = """Write ONE short lead-in line (max 8 words) for their morning event picks. It's {today}.
There {count} ({days}). Like "A couple worth your time this week." or "One worth grabbing a spot for."
Match the count and the days. No emoji, no event names."""


async def picks_message(user_id, picks, at: datetime, recent: list[str] | None = None) -> View:
    """The 9 AM suggestions, rendered by code; the model only writes the lead-in line."""
    try:
        count = "is 1 event" if len(picks) == 1 else f"are {len(picks)} events"
        days = ", ".join(f"{p.event.start:%A}" + (" next week" if (p.event.start.date() - at.date()).days >= 6 else "") for p in picks)
        lead = (await voice.compose(user_id, LEAD_TASK.format(today=f"{at:%A}", count=count, days=days),
                                    recent, max_emoji=0)).splitlines()[0]
    except Exception:
        lead = "A few worth your time."
    return views.events_view(picks, at, lead=lead)


EVENTS_PICK_TASK = """The user asked: "{question}"

Candidate events (NYU Events and Luma) from {start} to {end}, best matches first:
{facts}

Pick the ones that answer their question (for example, career events, AI hackathons, or what's at a
place), at most 5, best first. Return JSON: {{"ids": ["..."], "lead": "one short line (max 10 words)"}}.
If none fit, return an empty list and a lead that says so plainly."""


async def answer_events(user_id, question: str, now: datetime | None = None, recent: list[str] | None = None) -> "str | View":
    """Questions about events: the model picks which events fit, code renders them (with buttons)."""
    import events

    now = now or datetime.now(TZ)
    # No period named ("any AI hackathons coming up?"): look two weeks out, about what Luma covers.
    start_d, end_d, _ = schedule.parse_range(question, now.date()) or (now.date(), now.date() + timedelta(days=13), "")
    start = max(now, datetime.combine(start_d, datetime.min.time(), tzinfo=TZ))
    end = datetime.combine(end_d, datetime.max.time(), tzinfo=TZ)
    found = events.matches(user_id, start, end, min_score=30)[:25]
    if not found:
        return await voice.say(user_id, f"No relevant events found from {schedule.fmt_day(start_d)} to "
                                        f"{schedule.fmt_day(end_d)}.", kind="answer", user_message=question, recent=recent)
    facts = "\n".join(f"id={s.event.id} | {events.fact_line(s)}" for s in found)
    try:
        data = await llm.chat_json([{"role": "user", "content": EVENTS_PICK_TASK.format(
            question=question, start=schedule.fmt_day(start_d), end=schedule.fmt_day(end_d), facts=facts)}],
            model_name=voice.voice_model())
    except Exception as e:
        log.warning("events answer failed (%s)", type(e).__name__)
        return FALLBACK
    by_id = {s.event.id: s for s in found}
    chosen = [by_id[i] for i in (data.get("ids") or []) if i in by_id][:5]
    lead = voice.polish(str(data.get("lead") or ""), 0)
    if not chosen:
        return await voice.say(user_id, lead or "None of the upcoming events fit that.", kind="answer",
                               user_message=question, recent=recent)
    events.remember_list(user_id, [s.event.id for s in chosen])
    return views.events_view(chosen, now, title="Events", lead=lead)
