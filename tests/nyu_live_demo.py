"""NYU Helper test with real Telegram messages to the owner's chat.

    .venv/bin/python tests/nyu_live_demo.py

Each question is simulated as coming from the owner and goes through Violet's real handlers; a short
"🧪 Test" line with the question is sent first, then Violet's replies arrive as normal messages in the
owner's chat. Everything sent is also printed here with timings.
"""

import asyncio
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import telegram  # noqa: E402

import harness  # noqa: E402
import brain  # noqa: E402
import users  # noqa: E402

QUESTIONS = [
    "What's Professor Jacoby's email?",
    "When are Foy's office hours?",
    "Book a study room at Dibner tomorrow 3-5 for 2 people",
    "How do I get my NYU ID replaced?",
    "When's the last day to drop a class this semester?",
    "Where do I print at Tandon?",
    "Who do I talk to about CPT?",
    "Is Bobst open right now?",
    "I need to email the registrar about a hold on my account",
]


class RecordingBot(telegram.Bot):
    """The real bot (messages really go out), recording what it sends."""

    def __init__(self, token):
        super().__init__(token)
        with self._unfrozen():
            self.sent = []

    async def _do_post(self, endpoint, data, **kw):
        if endpoint in ("sendMessage", "editMessageText"):
            self.sent.append({"chat_id": int(data.get("chat_id") or 0), "text": str(data.get("text") or "")})
        return await super()._do_post(endpoint, data, **kw)


async def main():
    import bot as violet_bot

    owner = users.OWNER_ID
    await brain.start(wait=60)
    tg = RecordingBot(os.environ["TELEGRAM_BOT_TOKEN"])
    violet_bot.BUFFER_SECONDS = 0.05
    app = violet_bot.build_app("", bot=tg)
    await app.initialize()
    chat = harness.Chat(app, tg)
    for q in (sys.argv[1:] or QUESTIONS):
        await tg.send_message(chat_id=int(owner), text=f"🧪 Test: {q}")
        t = time.monotonic()
        out = await chat.send(owner, q, name="Chirag")
        took = time.monotonic() - t
        print(f"\n=== {q}  ({took:.1f}s)")
        for m in out:
            print("  " + re.sub(r"<[^>]+>", "", m["text"]).replace("\n", "\n  "))
        await asyncio.sleep(1)
    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
