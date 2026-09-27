"""Pipelines that turn a file or chat message into memory + schedule items.
No channel code here: bot.py downloads the file and sends the replies."""

import logging
from dataclasses import dataclass
from pathlib import Path

import brain
import docs
import schedule
import selfmodel

log = logging.getLogger("ingest")


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {plural or word + 's'}"


def _context(file_name: str, caption: str | None) -> str:
    parts = [f'file name "{file_name}"']
    if caption:
        parts.insert(0, f'the user described this file as "{caption}"')
    return ", ".join(parts)


@dataclass
class Draft:
    """What to tell the user: text for the voice layer, plus an optional list shown word for word."""
    text: str
    attach: str | None = None
    kind: str = "info"


async def process_file(user_id, path: Path, file_name: str, caption: str | None) -> Draft:
    """Extract text, save it to memory and add schedule items (or seed the profile from a
    resume). Call brain.connect(user_id) afterwards (in the background) to build the graph."""
    extracted = await docs.extract(path)
    # A short caption ("OM syllabus") names the file; a sentence doesn't.
    name = caption if caption and len(caption.split()) <= 4 else file_name
    if not extracted.text.strip():
        return Draft(f"Couldn't find any text in {name}. Ask for a clearer copy or the text itself.", kind="error")

    await brain.save(user_id, brain.document_memory(file_name, caption, extracted.text), kind="document")
    pages = f", {_plural(extracted.pages, 'page')}" if extracted.pages else ""

    if await selfmodel.is_resume(extracted.text, caption, file_name):
        added = await selfmodel.seed_from_resume(user_id, extracted.text)
        if not added:
            return Draft(f"Read their resume ({name}{pages}). Nothing new about them in it.", kind="save")
        shown = [f"• {f['text']}" for f in added[:10]] + ([f"...and {len(added) - 10} more"] if len(added) > 10 else [])
        return Draft(f"Read their resume ({name}{pages}) and picked up {len(added)} things about them (listed below). "
                     "The user can see everything with /me and fix anything with /me edit.",
                     attach="\n".join(shown), kind="save")

    items = await schedule.extract(extracted.text, _context(file_name, caption))
    result = schedule.add_items(user_id, items, source="file", label=name)
    await schedule.normalize_titles(user_id, selfmodel.owner_name(user_id))
    facts = [f"Read {name}{pages}, and will remember what's in it."]
    if result.added:
        facts.append(f"Added {_count_words(result)} to their calendar (listed below). /undo removes them.")
    elif not result.duplicates:
        facts.append("No dated deadlines or class times in it.")
    if result.duplicates:
        facts.append(f"Skipped {result.duplicates} already in their calendar.")
    if result.past:
        facts.append(f"Skipped {result.past} with dates already past.")
    return Draft(" ".join(facts), attach="\n".join(item_lines(result)) or None, kind="save")


async def process_chat(user_id, text: str) -> Draft | None:
    """Add deadlines or classes from a chat message. None if the message had none."""
    courses = sorted({i.course for i in schedule.existing_items(user_id) if i.course})
    projects = [f["text"] for f in selfmodel.get_profile(user_id)["academics"]][:8]
    context = ("a chat message from the user. Their courses: " + "; ".join(courses)
               + ". What they told you about their coursework (use it to set the course, e.g. a group "
                 "project named here belongs to that course): " + "; ".join(projects))
    items = await schedule.extract(text, context, allow_relative=True)
    if not items:
        return None
    result = schedule.add_items(user_id, items, source="chat", label=text[:60])
    if not result.added:
        if not result.duplicates:
            return None
        return Draft("That's already in their calendar, nothing new added.", kind="schedule")
    facts = ["Added to their calendar: " + "; ".join(item_lines(result, numbered=False)) + "."]
    for it in result.added:
        if it.kind == "class":
            facts.append("Class reminders go out 30 minutes before each meeting.")
        elif it.kind == "exam":
            facts.append("Exam reminders go out 1 week, 3 days, 1 day and 3 hours before.")
        else:
            facts.append("Reminders go out 3 days, 1 day and 3 hours before.")
        break
    facts.append("If it's for a course with teammates the user has told you about, you can mention them.")
    return Draft(" ".join(facts), kind="schedule")


def _count_words(result: schedule.AddResult) -> str:
    deadlines = [i for i in result.added if i.kind != "class"]
    groups = schedule.fmt_class_group([i for i in result.added if i.kind == "class"])
    parts = []
    if deadlines:
        parts.append(_plural(len(deadlines), "deadline"))
    if groups:
        parts.append(_plural(len(groups), "weekly class", "weekly classes"))
    return " and ".join(parts)


def item_lines(result: schedule.AddResult, numbered: bool = True) -> list[str]:
    """One line per added deadline or weekly class, in date order."""
    deadlines = sorted((i for i in result.added if i.kind != "class"), key=schedule.due_datetime)
    one_course = len({(i.course or "").lower() for i in deadlines}) <= 1
    lines = []
    for n, it in enumerate(deadlines, 1):
        title = it.title if one_course and numbered else schedule.label(it)
        lines.append(f"{n}. {title}, {schedule.fmt_due(it)}" if numbered else f"{title}, due {schedule.fmt_due(it)}")
    lines += schedule.fmt_class_group([i for i in result.added if i.kind == "class"])
    return lines
