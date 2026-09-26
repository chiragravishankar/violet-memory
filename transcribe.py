"""Speech to text for voice notes, audio files and video notes. No channel code here.

Checked with openai 2.54.0 (Sep 2026): gpt-4o-mini-transcribe took ~1 s on a 6 s Telegram-style
OGG/Opus note and got names right ("Ramp"); whisper-1 took ~2.5 s. OpenAI accepts ogg, mp3, mp4,
m4a, wav and webm directly, so no ffmpeg conversion is needed for Telegram's formats.
"""

import logging
import os
from pathlib import Path

import llm

log = logging.getLogger("transcribe")

MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
FALLBACK_MODEL = "whisper-1"
MAX_SECONDS = 5 * 60
MIME = {".ogg": "audio/ogg", ".oga": "audio/ogg", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
        ".mp4": "video/mp4", ".wav": "audio/wav", ".webm": "audio/webm"}


def vocabulary(user_id) -> str:
    """A short prompt with the words this user's notes are likely to contain, so names and terms
    come out right ("Chewy draft due Friday", not "Chewy Draft 2")."""
    import schedule
    import selfmodel

    data = selfmodel.get_profile(user_id)
    people = [f.get("attrs", {}).get("name") or f["text"].split(":")[0] for f in data["people"]]
    courses = sorted({i.course for i in schedule.existing_items(user_id) if i.course})
    words = [w for w in people + courses if w and len(w) < 40]
    return ("A student's voice note about classes, deadlines (\"due Friday\", \"draft due\"), exams and people. "
            + ("Names and terms: " + ", ".join(dict.fromkeys(words))[:600] + "." if words else ""))


async def transcribe(path: Path, user_id=None) -> str:
    """The transcript, or "" if nothing could be made out. Tries the fast model, then whisper-1."""
    mime = MIME.get(path.suffix.lower(), "application/octet-stream")
    prompt = vocabulary(user_id) if user_id is not None else ""
    for model in (MODEL, FALLBACK_MODEL):
        try:
            with open(path, "rb") as f:
                r = await llm.client().audio.transcriptions.create(
                    model=model, file=(path.name, f, mime), **({"prompt": prompt} if prompt else {}))
            return (r.text or "").strip()
        except Exception as e:
            log.warning("transcription with %s failed (%s)", model, type(e).__name__)
    return ""


def preview(text: str, words: int = 15) -> str:
    """First ~15 words, with "…" if cut."""
    w = text.split()
    return " ".join(w[:words]) + ("…" if len(w) > words else "")
