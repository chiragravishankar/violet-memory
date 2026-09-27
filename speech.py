"""Voice replies: when to speak, rewriting text for the ear, and text-to-speech. No channel code here.

Checked with openai 2.54.0 (Sep 2026): gpt-4o-mini-tts returns Telegram-ready OGG/Opus directly
(response_format="opus"), in about 1.5 to 3 seconds for a short reply, and takes tone instructions.
No ffmpeg needed.
"""

import asyncio
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

import llm
import selfmodel
import voice

log = logging.getLogger("speech")

TTS_MODEL = os.getenv("VIOLET_TTS_MODEL", "gpt-4o-mini-tts")
FALLBACK_TTS = "tts-1"
VOICE = os.getenv("VIOLET_VOICE", "coral")  # warm and natural; alloy, ash, ballad, echo, fable, nova, onyx, sage, shimmer, verse also work
TONE = ("Warm, friendly and relaxed, like a smart friend sending a quick voice note. Natural pace, "
        "light smile in the voice, never robotic.")
TIMEOUT = 15.0  # seconds for rewrite + speech; slower than this and the reply goes out as text
MAX_WORDS = 100  # about 40 seconds of speech

MODES = ("mirror", "on", "off")


def mode(user_id) -> str:
    return (selfmodel.meta(user_id).get("settings") or {}).get("voice", "mirror")


def set_mode(user_id, m: str) -> None:
    s = selfmodel.meta(user_id).get("settings") or {}
    s["voice"] = m
    selfmodel.set_meta(user_id, settings=s)


def wants_voice(user_id, was_voice: bool) -> bool:
    m = mode(user_id)
    return m == "on" or (m == "mirror" and was_voice)


REWRITE_TASK = """Rewrite this for Violet to SAY out loud as a voice note. {shape}

Rules for speech:
- Sound like a friend talking: warm, natural, contractions, no list reading.
- No emojis, symbols, arrows, bullets, markdown or URLs. Never read a link out; say "link's in the chat".
- Times the way people say them: "two PM", "at one", "Friday night", "tomorrow at five". Dates like
  "Monday the twenty eighth" only if the day isn't enough.
- At most {max_words} words. If the content is longer, give the gist and say the rest is in the chat.
Return JSON: {{"speech": "...", "cut": true or false (true if you left things out)}}

Text:
\"\"\"{text}\"\"\""""


async def rewrite(user_id, text: str, structured: bool) -> tuple[str, bool]:
    """(what to say, whether it was shortened). Structured views get a one or two sentence summary."""
    shape = ("It's a formatted list he can see below: say a one or two sentence summary of what matters most, "
             "then that the full list is below." if structured else "Keep the meaning and personality.")
    data = await llm.chat_json(
        [{"role": "system", "content": voice.system_prompt(user_id)},
         {"role": "user", "content": REWRITE_TASK.format(shape=shape, max_words=MAX_WORDS if not structured else 45, text=text[:4000])}],
        model_name=voice.voice_model(), temperature=0.5)
    speech = voice.polish(str(data.get("speech") or ""), 0).replace("↳", "").replace("·", ",")
    return speech, bool(data.get("cut")) or structured


async def synthesize(text: str) -> Path:
    """OGG/Opus voice note in a temp file (the caller deletes it)."""
    last = None
    for model in (TTS_MODEL, FALLBACK_TTS):
        try:
            kw = {"instructions": TONE} if model.startswith("gpt-4o") else {}
            r = await llm.client().audio.speech.create(model=model, voice=VOICE, input=text, response_format="opus", **kw)
            path = Path(tempfile.gettempdir()) / f"violet-{uuid.uuid4().hex}.ogg"
            path.write_bytes(r.content)
            return path
        except Exception as e:
            last = e
            log.warning("tts with %s failed (%s)", model, type(e).__name__)
    raise last or RuntimeError("tts failed")


async def make(user_id, text: str, structured: bool = False) -> tuple[Path, str, bool, float]:
    """Rewrite and synthesize within TIMEOUT. Returns (audio file, spoken text, shortened, seconds).
    Raises on failure or timeout so the caller can fall back to text."""
    t = time.monotonic()

    async def run():
        speech, cut = await rewrite(user_id, text, structured)
        if not speech:
            raise RuntimeError("empty speech")
        return await synthesize(speech), speech, cut

    path, speech, cut = await asyncio.wait_for(run(), TIMEOUT)
    took = time.monotonic() - t
    log.info("voice reply for %s in %.1fs (%d words%s)", user_id, took, len(speech.split()), ", shortened" if cut else "")
    return path, speech, cut, took
