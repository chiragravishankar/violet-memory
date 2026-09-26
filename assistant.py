"""Understanding messages: classify, answer questions from schedule + memory, and build
the context line for class reminders. No channel code here."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import brain
import llm
import schedule
import selfmodel
from schedule import TZ

log = logging.getLogger("assistant")

QUESTION, SCHEDULE, MEMORY, CORRECTION = "question", "schedule", "memory", "correction"

CLASSIFY_PROMPT = """Classify a message a student sent to their personal memory assistant.

Types:
- "question": they want information back. Includes questions without a question mark and
  commands like "show me tuesday", "who is hena", "what work do i have next week", "list my exams".
- "schedule": they are telling you about something with a date or time to track: a deadline,
  exam, class, meeting or event ("Chewy draft due next Friday 11:59pm", "OM exam on Oct 12").
- "memory": a fact, person, idea or note to remember with nothing to schedule
  ("Met Sam at the Leslie mixer, he builds climate tools").
- "correction": they are correcting something about themselves or something you said:
  "I am X", "my name is X", "actually ...", "no, ...", "that's wrong, ...", "bro, i am chirag".

If you are unsure between question and memory, choose question.
Also say whether the message is about their schedule (deadlines, classes, events, days, what is due),
and whether a question is about the user themself or their own life (their name, who they are,
what they study, their courses, projects, goals, the people in their life, their preferences).

Return JSON: {{"type": "question|schedule|memory|correction", "about_schedule": true|false, "about_user": true|false}}

Message: \"\"\"{text}\"\"\""""

ANSWER_PROMPT = """You are Violet Memory, a friendly personal memory assistant for a student.
Now: {now} (America/New_York).

About them: {profile}

The agenda below is COMPLETE for {period} ({start} to {end}) and already filtered to what the
question asks about ({kinds}), from all their calendars. Answer from it:
- List every agenda item. Never drop, merge, move or invent items.
- Group by day, in date order, times in order within each day, like the agenda.
- Mark deadlines with 📌 and classes with 🎓, as the agenda does.
- Only the agenda counts as the schedule: never list dates or deadlines from the memory notes,
  they can be about other periods or out of date.
- If the agenda is empty, say so in one friendly line naming the period and its dates, then
  list every "Coming up after" item (if any) under a short "Next up:" line.
- Use the memory notes only to add a short helpful detail if relevant.

Style: friendly, plain text only, following their reply style preference if they have one
(short by default). No markdown, no asterisks, no em dashes.

Agenda:
{agenda}

Coming up after (only for when nothing in the agenda matches):
{after}

Memory notes:
{memory}

Question: {question}"""

ADVICE_PROMPT = """You are Violet Memory, a friendly personal assistant for a student who knows them well.
Now: {now} (America/New_York).

About them (goals, projects, people, preferences): {profile}

Their agenda for {period} ({start} to {end}), complete, from all calendars:
{agenda}

Hard deadlines in this period (every one of these must appear in your priorities, with its day and time):
{deadlines}

Memory notes: {memory}

Question: {question}

Give 3 to 5 concrete priorities. Hard deadlines first, in date order (you may group several
into one line). Then tie the rest to their goals and projects by name. Only use dates and times
that are in the agenda; don't invent times for goal work. If a teammate or mentor is relevant, mention them.
Plain text, numbered lines, no markdown, no asterisks, no em dashes. Follow their reply style
preference (short by default)."""

_ADVICE = re.compile(r"\b(focus|prioriti[sz]e|priorities|plan my|should i|what should|where should|how should|advice)\b", re.I)

_SCHEDULE_WORDS = re.compile(
    r"\b(schedule|calendar|agenda|due|deadline|class|classes|lecture|meeting|meetings|exam|exams|"
    r"quiz|assignment|assignments|homework|busy|free|plans?|week|today|tomorrow|tonight|weekend)\b",
    re.I,
)


CONTEXT_PROMPT = """Write ONE short line (max 25 words) to add to a class reminder for {course}.
About the student: {profile}
Use only these facts. Mention teammates or people if the memory names any for this course, and the
next deadline if there is one. Do not start with "Reminder". Plain text, no markdown, no em dashes, no emoji.
If there are no useful facts, return exactly: none

Next deadline for this course: {next_due}
Memory about this course: {memory}"""


@dataclass
class Classification:
    type: str
    about_schedule: bool
    about_user: bool = False


def _fallback(text: str) -> Classification:
    return Classification(QUESTION if text.rstrip().endswith("?") else MEMORY, False)


async def classify(text: str) -> Classification:
    try:
        data = await llm.chat_json([{"role": "user", "content": CLASSIFY_PROMPT.format(text=text)}])
    except Exception as e:
        log.warning("classify failed (%s), using fallback", type(e).__name__)
        return _fallback(text)
    kind = str(data.get("type", "")).lower()
    if kind not in (QUESTION, SCHEDULE, MEMORY, CORRECTION):
        return _fallback(text)
    if kind != CORRECTION and _CORRECTION.match(text):
        kind = CORRECTION  # "my name is X" / "actually, ..." even if the model missed it
    return Classification(kind, bool(data.get("about_schedule")), bool(data.get("about_user")))


_CORRECTION = re.compile(
    r"^\s*(bro,?\s*|dude,?\s*)?(my name is|i am\b(?! (going|gonna|meeting|at|in|on|free|busy|done|not sure|working on|taking|looking|trying|so|very|really|tired|feeling))|"
    r"i'm\s+[A-Z]\w+\s+[A-Z]\w+|actually\b|no,|nope,|that's wrong|thats wrong|wrong,)",
    re.I,
)


def _plain(text: str) -> str:
    text = text.replace("—", ", ").replace("–", "-").replace("**", "").replace("__", "")
    text = re.sub(r"^\s*#+\s*", "", text, flags=re.M)
    return re.sub(r"[ \t]+$", "", text, flags=re.M).strip()


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


def memory_system_prompt(profile: str) -> str:
    """Cognee answer instructions with the user's profile, so memory answers fit them."""
    return (
        "Answer the question using the provided context. The user is the person described here: "
        f"{profile}. Speak to them as \"you\". Be friendly and brief (match their reply style "
        "preference if one is given). Plain text, no markdown, no em dashes."
    )


def is_schedule_question(question: str, about_schedule: bool, today) -> bool:
    return about_schedule or bool(_SCHEDULE_WORDS.search(question)) or schedule.parse_range(question, today) is not None


ABOUT_USER_PROMPT = """You are Violet Memory, a friendly personal memory assistant. The person asking is the user.

Their profile (authoritative: what they told you about themselves):
{profile}

Memory search result (can mix people up; if it conflicts with the profile, the profile wins,
and any other name in it is someone else, not the user):
{memory}

Question: {question}

Answer from the profile first, then the memory result. Talk to them as "you". If neither has the
answer, say so and suggest /me setup. Plain text, no markdown, no em dashes, short unless their
reply style preference says detailed."""


async def answer_about_user(user_id, question: str) -> str:
    listing = "\n".join(f"- [{s}] {f['text']}" for s, f in selfmodel.facts(selfmodel.get_profile(user_id))) or "(empty)"
    memory = await brain.recall_raw(user_id, question, system_prompt=memory_system_prompt(
        selfmodel.profile_summary(user_id) or "unknown"))
    prompt = ABOUT_USER_PROMPT.format(profile=listing, memory=memory or "none", question=question)
    return _plain(await llm.chat([{"role": "user", "content": prompt}]))


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


def _looks_like_name(candidate: str) -> bool:
    words = candidate.lower().split()
    return (
        1 <= len(words) <= 4
        and not any(w in _NOT_NAME_WORDS for w in words)
        and not words[0].endswith(("ing", "ed"))
    )


async def correct(user_id, text: str, last_reply: str | None = None) -> str:
    """Apply a correction: update the profile, save the corrected fact to memory, confirm."""
    m = _NAME.match(text)
    if m and _looks_like_name(m.group(1)):
        # "i am chirag ravishankar": set the name directly, no model involved.
        name = " ".join(w[:1].upper() + w[1:] for w in m.group(1).split())
        selfmodel.set_identity(user_id, name=name)
        await brain.save(user_id, f"My name is {name}.", kind="correction")
        return f"Fixed 💜 Your name is {name}. I'll remember that."
    instruction = text if not last_reply else f'You had just said: "{last_reply[:300]}". The user replied: "{text}"'
    changes = await selfmodel.edit(user_id, instruction)
    fact = "; ".join(changes) if changes else text
    await brain.save(user_id, fact if changes else text, kind="correction")
    if changes:
        return "Fixed 💜\n" + "\n".join(changes)
    return "Thanks for the correction, noted 💜"


async def answer(user_id, question: str, about_schedule: bool, now: datetime | None = None,
                 about_user: bool = False) -> str:
    """Schedule questions: the same agenda as /week for the period the question names
    (default the next 7 days), plus Cognee context. Other questions: Cognee alone."""
    now = now or datetime.now(TZ)
    profile = selfmodel.profile_summary(user_id) or "nothing yet"
    advice = bool(_ADVICE.search(question))
    if about_user and not advice and not about_schedule:
        return await answer_about_user(user_id, question)
    if not advice and not is_schedule_question(question, about_schedule, now.date()):
        return await brain.recall(user_id, question, system_prompt=memory_system_prompt(profile))
    start, end, period = schedule.parse_range(question, now.date()) or (
        now.date(), now.date() + timedelta(days=6), "the next 7 days"
    )
    if advice:
        # Planning needs room to look ahead, even when "this week" is only the weekend.
        end = max(end, now.date() + timedelta(days=6))
        lines = schedule.agenda_lines(user_id, start, end, now)
        deadlines = schedule.agenda_lines(user_id, start, end, now, schedule.DEADLINE_KINDS)
        memory = await brain.recall_raw(user_id, question, system_prompt=memory_system_prompt(profile))
        prompt = ADVICE_PROMPT.format(
            now=f"{now:%A %b} {now.day}, {now.year}, {schedule.fmt_time(now.time())}",
            profile=profile, period=period if end == start else "the coming days",
            start=schedule.fmt_day(start), end=schedule.fmt_day(end),
            agenda="\n".join(lines) or "(nothing scheduled)", memory=memory or "none", question=question,
            deadlines="\n".join(deadlines) or "(none)",
        )
        log.info("advice question: %s to %s, %d agenda lines", start, end, len(lines))
        return _plain(await llm.chat([{"role": "user", "content": prompt}]))
    kinds = schedule.parse_kinds(question)
    lines = schedule.agenda_lines(user_id, start, end, now, kinds)
    # Only offered when the period is empty, so the answer can't wander past it.
    after = [] if lines else schedule.agenda_lines(user_id, end + timedelta(days=1), end + timedelta(days=7), now, kinds)
    log.info("schedule question: %s, %s to %s, kinds %s, %d agenda lines",
             period, start, end, sorted(kinds) if kinds else "all", len(lines))
    memory = await brain.recall_raw(user_id, question, system_prompt=memory_system_prompt(profile))
    prompt = ANSWER_PROMPT.format(
        now=f"{now:%A %b} {now.day}, {now.year}, {schedule.fmt_time(now.time())}",
        profile=profile,
        period=period,
        start=schedule.fmt_day(start),
        end=schedule.fmt_day(end),
        kinds=_kinds_label(kinds),
        agenda="\n".join(lines) or "(nothing scheduled)",
        after="\n".join(after) or "(nothing)",
        memory=memory or "none",
        question=question,
    )
    return _plain(await llm.chat([{"role": "user", "content": prompt}]))


async def class_context(user_id, it: schedule.Item, now: datetime) -> str | None:
    """One line of context for a class reminder: teammates and what's due next."""
    course = it.course or re.split(r"\s+-\s+", it.title)[0]
    nxt = schedule.next_due_for_course(user_id, course, now)
    next_due = f"{nxt.title}, {schedule.fmt_due(nxt)}" + (f" ({nxt.note})" if nxt.note else "") if nxt else "none"
    memory = await brain.recall_raw(
        user_id, f"Who are my teammates or group members in {course}, and what have I noted about {course}?"
    )
    prompt = CONTEXT_PROMPT.format(course=course, next_due=next_due, memory=memory or "none",
                                   profile=selfmodel.profile_summary(user_id, max_words=60) or "nothing yet")
    try:
        line = _plain(await llm.chat([{"role": "user", "content": prompt}])).splitlines()[0].strip()
    except Exception as e:
        log.warning("class context failed (%s)", type(e).__name__)
        line = ""
    if not line or line.lower().strip(".") == "none":
        return f"Next due: {next_due}" if nxt else None
    return line


async def reminder_message(user_id, it: schedule.Item, lead: str, now: datetime) -> str:
    text = schedule.reminder_text(it, lead)
    if it.kind in ("class", "class_session"):
        ctx = await class_context(user_id, it, now)
        if ctx:
            text += f"\n💡 {ctx}"
    return text
