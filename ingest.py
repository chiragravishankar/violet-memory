"""Pipelines that turn a file or chat message into memory + schedule items.
No channel code here: bot.py downloads the file and sends the replies."""

import logging
from pathlib import Path

import brain
import docs
import schedule

log = logging.getLogger("ingest")


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {plural or word + 's'}"


def _context(file_name: str, caption: str | None) -> str:
    parts = [f'file name "{file_name}"']
    if caption:
        parts.insert(0, f'the user described this file as "{caption}"')
    return ", ".join(parts)


async def process_file(user_id, path: Path, file_name: str, caption: str | None) -> str:
    """Extract text, save it to memory and add schedule items. Returns the reply text.

    Call brain.connect(user_id) afterwards (in the background) to build the graph.
    """
    extracted = await docs.extract(path)
    # A short caption ("OM syllabus") names the file; a sentence doesn't.
    name = caption if caption and len(caption.split()) <= 4 else file_name
    if not extracted.text.strip():
        return f"I couldn't find any text in {name} 💜"

    await brain.save(user_id, brain.document_memory(file_name, caption, extracted.text))

    items = await schedule.extract(extracted.text, _context(file_name, caption))
    result = schedule.add_items(user_id, items, source="file", label=name)

    pages = f" ({_plural(extracted.pages, 'page')})" if extracted.pages else ""
    lines = [f"📄 Read {name}{pages}. Saved to memory."]
    lines += summarize(result)
    if result.added:
        lines.append("Reply /undo to remove these.")
    return "\n".join(lines)


async def process_chat(user_id, text: str) -> str | None:
    """Add deadlines or classes from a chat message. Returns the reply, or None if none were found."""
    items = await schedule.extract(text, "a chat message from the user", allow_relative=True)
    if not items:
        return None
    result = schedule.add_items(user_id, items, source="chat", label=text[:60])
    if not result.added:
        return "That's already in your calendar 💜" if result.duplicates else None
    return "\n".join(summarize(result, skipped_lines=False))


def summarize(result: schedule.AddResult, skipped_lines: bool = True) -> list[str]:
    deadlines = [i for i in result.added if i.kind != "class"]
    classes = [i for i in result.added if i.kind == "class"]
    one_course = len({(i.course or "").lower() for i in deadlines}) <= 1

    lines = []
    if deadlines:
        deadlines.sort(key=schedule.due_datetime)
        lines.append(f"📅 Added {_plural(len(deadlines), 'deadline')}:")
        for n, it in enumerate(deadlines, 1):
            title = it.title if one_course else schedule.label(it)
            lines.append(f"{n}. {title}, {schedule.fmt_due(it)}")
    if classes:
        groups = schedule.fmt_class_group(classes)
        lines.append(f"🗓 Added {_plural(len(groups), 'weekly class', 'weekly classes')}:")
        lines += groups
    if not result.added and skipped_lines and not result.duplicates:
        lines.append("No dated deadlines or class times found.")
    if skipped_lines and result.duplicates:
        lines.append(f"Skipped {result.duplicates} already in your calendar.")
    if skipped_lines and result.past:
        lines.append(f"Skipped {result.past} with dates already past.")
    return lines
