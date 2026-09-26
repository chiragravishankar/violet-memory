"""Understanding messages: classify, answer questions from schedule + memory, corrections and
reminders. Every reply is written by the voice layer (voice.py). No channel code here."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import brain
import llm
import schedule
import selfmodel
import voice
from schedule import TZ

log = logging.getLogger("assistant")

QUESTION, SCHEDULE, MEMORY, CORRECTION = "question", "schedule", "memory", "correction"
FALLBACK = "Hmm, I couldn't pull that up just now. Try me again in a sec?"

CLASSIFY_TASK = """Classify his message, and if it is a "memory", also write your reply to it.

Types:
- "question": he wants information back. Includes questions without a question mark and
  commands like "show me tuesday", "who is hena", "what work do i have next week", "list my exams".
- "schedule": he's telling you about something with a date or time to track: a deadline,
  exam, class, meeting or event ("Chewy draft due next Friday 11:59pm", "OM exam on Oct 12").
- "memory": a fact, person, idea or note to keep with nothing to schedule
  ("Met Sam at the Leslie mixer, he builds climate tools").
- "correction": he's correcting something about himself or something you said:
  "I am X", "my name is X", "actually ...", "no, ...", "that's wrong, ...", "bro, i am chirag".

If unsure between question and memory, choose question.
about_schedule: the message is about his schedule (deadlines, classes, events, days, what is due).
about_user: a question about himself or his own life (his name, who he is, what he studies, his
courses, projects, goals, the people in his life, his preferences).
ack: only for "memory": your reply acknowledging the specific thing, in your voice, 1 or 2 short
sentences, sometimes with one helpful beat. Empty string otherwise.

Return JSON: {{"type": "question|schedule|memory|correction", "about_schedule": true|false, "about_user": true|false, "ack": "..."}}

His message: \"\"\"{text}\"\"\""""

AGENDA_TASK = """He asked: "{question}"

Answer from his agenda for {period} ({start} to {end}), filtered to {kinds}. It is complete and
comes from all his calendars. Only the agenda counts for dates and times.
{shape}

Agenda:
{agenda}
{after}
Other things you know that may help (only add a detail if it's clearly relevant): {context}"""

ADVICE_TASK = """He asked: "{question}"

Give him 3 to 5 concrete priorities, one per line (numbering is fine here). Hard deadlines first, in
date order, each with its day and time (group several into one line if it reads better). Then tie
the rest to his goals and projects by name, and mention a teammate or mentor if relevant.
Every hard deadline below must appear. Only use dates and times from the agenda; don't invent
times for goal work.

His agenda from {start} to {end}:
{agenda}

Hard deadlines in that period:
{deadlines}

Other things you know that may help: {context}"""

ABOUT_USER_TASK = """He asked about himself: "{question}"

What he has told you about himself (this is the truth about him):
{profile}

Other things you know (can mix people up; if they conflict with the above, the above wins, and
any other name in them is someone else):
{context}

Answer him. If you don't know, say so and ask."""

MEMORY_TASK = """He asked: "{question}"

What you know that's relevant (from things he told you, files he sent and his calendars; names
other than his are other people):
{context}

Answer him. If you don't know, say so plainly and ask for the missing piece."""

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
LIST_INLINE_MAX = 4  # agendas with more items than this are attached word for word


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


def _kinds_label(kinds: set[str] | None) -> str:
    if not kinds:
        return "classes, meetings, events and deadlines"
    names = []
    if kinds & schedule.DEADLINE_KINDS:
        names.append("deadlines")
    if "event" in kinds:
        names.append("meetings and events")
    if kinds & schedule.CLASS_KINDS:
        names.append("classes")
    return " and ".join(names)


def is_schedule_question(question: str, about_schedule: bool, today) -> bool:
    return about_schedule or bool(_SCHEDULE_WORDS.search(question)) or schedule.parse_range(question, today) is not None


async def _context(user_id, question: str, limit: int = 2500) -> str:
    return (await brain.context_for(user_id, question))[:limit] or "nothing relevant"


async def answer(user_id, question: str, about_schedule: bool, now: datetime | None = None,
                 about_user: bool = False, recent: list[str] | None = None) -> str:
    """Answer a question in Violet's voice: schedule questions from the same agenda as /week
    (plus graph context), questions about him from his profile first, others from the graph."""
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
    lines = schedule.agenda_lines(user_id, start, end, now, kinds)
    # Only offered when the period is empty, so the answer can't wander past it.
    after = [] if lines else schedule.agenda_lines(user_id, end + timedelta(days=1), end + timedelta(days=7), now, kinds)
    shown = lines or after
    n_items = sum(1 for line in shown if line.startswith("  "))
    log.info("schedule question: %s, %s to %s, kinds %s, %d agenda lines",
             period, start, end, sorted(kinds) if kinds else "all", len(lines))
    attach = "\n".join(shown) if n_items > LIST_INLINE_MAX else None
    if attach:
        shape = ("The full agenda will be shown word for word right below your reply. Write only a short, "
                 "natural lead-in (one sentence, can note what stands out, like an exam). Don't list the items.")
    else:
        shape = "Answer in a natural sentence or two, or a line per item. Include every item, with day and time."
    if not lines:
        shape += (" Nothing matches in that period: say so plainly with the dates, then give what's next up"
                  " (below).")
    task = AGENDA_TASK.format(
        question=question, period=period, start=schedule.fmt_day(start), end=schedule.fmt_day(end),
        kinds=_kinds_label(kinds), shape=shape,
        agenda="\n".join(lines) or "(nothing)",
        after=f"\nNext up after that:\n" + "\n".join(after) + "\n" if after else "",
        context=await _context(user_id, question, 800),
    )
    lead = await voice.compose(user_id, task, recent)
    return f"{lead}\n\n{attach}" if attach else lead


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
        await brain.save(user_id, f"My name is {name}.", kind="correction")
        draft = f"Fixed his name: he is {name}. Own the mistake briefly."
    else:
        instruction = text if not last_reply else f'You had just said: "{last_reply[:300]}". The user replied: "{text}"'
        changes = await selfmodel.edit(user_id, instruction)
        await brain.save(user_id, "; ".join(changes) if changes else text, kind="correction")
        draft = ("Fixed. What changed: " + "; ".join(changes)) if changes else "Took the correction on board."
    return await voice.say(user_id, draft, kind="correction", user_message=text, recent=recent)


# ---------- Reminders ----------


async def reminder_message(user_id, it: schedule.Item, lead: str, now: datetime,
                           recent: list[str] | None = None) -> str:
    """The reminder in Violet's voice. Classes get one line of context: teammates and what's
    due next for that course."""
    facts = [schedule.reminder_text(it, lead)]
    if it.kind in ("class", "class_session"):
        course = it.course or re.split(r"\s+-\s+", it.title)[0]
        upcoming = schedule.upcoming_for_course(user_id, course, now, limit=3)
        if upcoming:
            facts.append(f"Coming up for {course}: " + "; ".join(
                f"{i.title}, {schedule.fmt_due(i)}" + (f" ({i.note})" if i.note else "") for i in upcoming))
        ctx = await _context(user_id, f"Who are my teammates in {course}, and what have I noted about {course}?", 1200)
        facts.append(f"What you know about {course}: {ctx}")
        facts.append(
            "Shape it like: \"OM with Jacoby in 30, 2 MetroTech. Heads up, your Chewy section is due Friday.\" "
            "Short course name, professor if known, where, then ONE heads-up: the most useful thing coming up "
            "(a group deliverable with teammates beats a routine one). Say deadlines like a person "
            "(\"due at 2\", \"due Friday\"), no labels like \"Next due:\". Two short sentences max.")
    return await voice.say(user_id, "\n".join(facts), kind="reminder", recent=recent)
