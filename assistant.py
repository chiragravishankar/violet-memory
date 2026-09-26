"""Understanding messages: classify, answer questions from schedule + memory, and build
the context line for class reminders. No channel code here."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime

import brain
import llm
import schedule
from schedule import TZ

log = logging.getLogger("assistant")

QUESTION, SCHEDULE, MEMORY = "question", "schedule", "memory"

CLASSIFY_PROMPT = """Classify a message a student sent to their personal memory assistant.

Types:
- "question": they want information back. Includes questions without a question mark and
  commands like "show me tuesday", "who is hena", "what work do i have next week", "list my exams".
- "schedule": they are telling you about something with a date or time to track: a deadline,
  exam, class, meeting or event ("Chewy draft due next Friday 11:59pm", "OM exam on Oct 12").
- "memory": a fact, person, idea or note to remember with nothing to schedule
  ("Met Sam at the Leslie mixer, he builds climate tools").

If you are unsure between question and memory, choose question.
Also say whether the message is about their schedule (deadlines, classes, events, days, what is due),
which matters for questions.

Return JSON: {{"type": "question|schedule|memory", "about_schedule": true|false}}

Message: \"\"\"{text}\"\"\""""

ANSWER_PROMPT = """You are Violet Memory, a friendly personal memory assistant for a student.
Now: {now} (America/New_York).

Answer the question using the schedule below (it is authoritative for dates and times) and the
memory notes. Resolve words like "tuesday", "tomorrow" or "next week" against now.
If neither has the answer, say so briefly.

Style: short and friendly, plain text only. No markdown, no asterisks, no em dashes.
Use simple lines like "Tue Sep 29, 2:00 PM: How to Design Goods (Operations Management)" for lists.

Schedule (next 21 days):
{schedule}

Memory notes:
{memory}

Question: {question}"""

CONTEXT_PROMPT = """Write ONE short line (max 25 words) to add to a class reminder for {course}.
Use only these facts. Mention teammates or people if the memory names any for this course, and the
next deadline if there is one. Do not start with "Reminder". Plain text, no markdown, no em dashes, no emoji.
If there are no useful facts, return exactly: none

Next deadline for this course: {next_due}
Memory about this course: {memory}"""


@dataclass
class Classification:
    type: str
    about_schedule: bool


def _fallback(text: str) -> Classification:
    return Classification(QUESTION if text.rstrip().endswith("?") else MEMORY, False)


async def classify(text: str) -> Classification:
    try:
        data = await llm.chat_json([{"role": "user", "content": CLASSIFY_PROMPT.format(text=text)}])
    except Exception as e:
        log.warning("classify failed (%s), using fallback", type(e).__name__)
        return _fallback(text)
    kind = str(data.get("type", "")).lower()
    if kind not in (QUESTION, SCHEDULE, MEMORY):
        return _fallback(text)
    return Classification(kind, bool(data.get("about_schedule")))


def _plain(text: str) -> str:
    text = text.replace("—", ", ").replace("–", "-").replace("**", "").replace("__", "")
    return re.sub(r"^\s*#+\s*", "", text, flags=re.M).strip()


async def answer(user_id, question: str, about_schedule: bool, now: datetime | None = None) -> str:
    """Schedule questions use the schedule table and Cognee; others use Cognee alone."""
    if not about_schedule:
        return await brain.recall(user_id, question)
    now = now or datetime.now(TZ)
    memory = await brain.recall_raw(user_id, question)
    prompt = ANSWER_PROMPT.format(
        now=f"{now:%A %b} {now.day}, {now.year}, {schedule.fmt_time(now.time())}",
        schedule=schedule.week_view(user_id, days=21, now=now),
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
    prompt = CONTEXT_PROMPT.format(course=course, next_due=next_due, memory=memory or "none")
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
