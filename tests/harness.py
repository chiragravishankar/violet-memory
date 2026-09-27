"""Test harness: run Violet's real Telegram handlers against a fake Telegram and a fake Cognee Cloud.

Set VIOLET_DATA_DIR (a temp folder) before importing this, so tests never touch real data. Nothing is
sent to Telegram: FakeBot records every outgoing message per chat. FakeCognee keeps memories per
dataset and logs which dataset every call touched, so tests can prove one user's actions only ever
reach their own memory. LLM calls are real.
"""

import asyncio
import itertools
import time
import uuid
from collections import defaultdict

import telegram
from telegram import Update


class FakeBot(telegram.Bot):
    def __init__(self):
        super().__init__("123456:TEST-TOKEN")
        with self._unfrozen():  # PTB objects are frozen after __init__
            self.sent: list[dict] = []
            self._ids = itertools.count(1000)

    async def _do_post(self, endpoint, data, **_):
        if endpoint == "getMe":
            return {"id": 999, "is_bot": True, "first_name": "Violet", "username": "violet_test_bot"}
        if endpoint in ("sendMessage", "editMessageText", "sendVoice", "sendDocument", "sendPhoto"):
            chat_id = int(data.get("chat_id") or 0)
            text = str(data.get("text") or data.get("caption") or "")
            self.sent.append({"endpoint": endpoint, "chat_id": chat_id, "text": text,
                              "markup": str(data.get("reply_markup") or "")})
            return {"message_id": next(self._ids), "date": int(time.time()),
                    "chat": {"id": chat_id, "type": "private"}, "text": text}
        return True

    def to(self, chat_id) -> list[dict]:
        return [m for m in self.sent if m["chat_id"] == int(chat_id)]


class FakeCognee:
    """Stands in for the cognee module inside brain (cloud mode)."""

    def __init__(self):
        self.store: dict[str, list[str]] = defaultdict(list)
        self.calls: list[tuple[str, str]] = []  # (operation, dataset)
        self.ids: dict[str, str] = {}

    async def remember(self, data, dataset_name="main_dataset", **_):
        self.calls.append(("remember", dataset_name))
        self.store[dataset_name] += data if isinstance(data, list) else [data]
        return {"status": "running"}

    async def recall(self, query_text, query_type=None, datasets=None, **_):
        for d in datasets or []:
            self.calls.append(("recall", d))
        words = {w for w in query_text.lower().replace("?", " ").split() if len(w) > 3}
        hits = [t for d in datasets or [] for t in self.store[d] if words & set(t.lower().replace(".", " ").split())]
        return [{"text": "\n".join(hits)}] if hits else []

    async def forget(self, data_id=None, dataset_id=None, **_):
        name = next((n for n, i in self.ids.items() if i == str(dataset_id)), str(dataset_id))
        self.calls.append(("forget", name))
        return {"status": "ok"}

    def dataset_id(self, name: str) -> uuid.UUID:
        self.ids.setdefault(name, str(uuid.uuid4()))
        return uuid.UUID(self.ids[name])

    def touched(self) -> set[str]:
        return {d for _, d in self.calls}


def install_fake_cognee(brain) -> FakeCognee:
    """Point brain at FakeCognee in cloud mode, already 'connected'."""
    fake = FakeCognee()
    brain.cognee = fake
    brain.CLOUD = True
    brain._ready = asyncio.Event()
    brain._ready.set()

    async def dataset_id(name):
        return fake.dataset_id(name) if fake.store.get(name) else None

    async def items(name):
        return None, []

    async def searchable(*_, **__):
        return True

    brain._cloud_dataset_id = dataset_id
    brain._cloud_items = items
    brain.wait_searchable = searchable
    brain._api_key = lambda: "test"
    return fake


class Chat:
    """Drives the real Application: send text, commands and button taps as a given user."""

    def __init__(self, app, bot: FakeBot):
        self.app, self.bot = app, bot
        self._ids = itertools.count(1)

    def _user(self, uid, name):
        return {"id": int(uid), "is_bot": False, "first_name": name}

    async def send(self, uid, text, name="Test"):
        n = next(self._ids)
        msg = {"message_id": n, "date": int(time.time()), "chat": {"id": int(uid), "type": "private"},
               "from": self._user(uid, name), "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        before = len(self.bot.sent)
        await self.app.process_update(Update.de_json({"update_id": n, "message": msg}, self.bot))
        await settle()
        return self.bot.sent[before:]

    async def tap(self, uid, data, name="Test"):
        n = next(self._ids)
        cq = {"id": str(n), "from": self._user(uid, name), "chat_instance": "x", "data": data,
              "message": {"message_id": n, "date": int(time.time()), "chat": {"id": int(uid), "type": "private"},
                          "from": {"id": 999, "is_bot": True, "first_name": "Violet"}, "text": "card"}}
        before = len(self.bot.sent)
        await self.app.process_update(Update.de_json({"update_id": n, "callback_query": cq}, self.bot))
        await settle()
        return self.bot.sent[before:]


async def settle(timeout: float = 90.0) -> None:
    """Wait for the background tasks a message started (split-message buffer, memory, learning)."""
    end = time.monotonic() + timeout
    await asyncio.sleep(0.05)
    while time.monotonic() < end:
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=min(5.0, end - time.monotonic()))


async def make_app():
    """The real Violet application (handlers, gate) on a FakeBot, without the background loops."""
    import bot as violet_bot

    fake_bot = FakeBot()
    violet_bot.BUFFER_SECONDS = 0.05
    app = violet_bot.build_app("", bot=fake_bot)
    await app.initialize()
    return app, fake_bot
