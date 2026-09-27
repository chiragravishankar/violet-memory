"""ClawMax endpoint: POST /agent/message {text} -> {reply}, for the owner's personal ClawMax agent.

- Always serves the owner (VIOLET_OWNER_ID). Any user or sender id in the request is ignored: ClawMax
  skills can't prove who is talking, so this path is personal-only by design.
- Auth: Authorization: Bearer <VIOLET_CLAWMAX_TOKEN>. Rate-limited. Logs never contain message text or
  secrets.
- Runs the exact same Telegram handlers: each request becomes a normal Telegram update from the owner in
  a separate internal chat (CHAT_ID), processed by a second, non-polling copy of the application whose bot
  records what would be sent instead of sending it. Replies come back as plain Markdown. Action cards come
  back as text ("Reply yes to confirm or no to cancel") and a later "yes"/"no" goes through the same
  approval logic as typing it in Telegram.
- Listens on 127.0.0.1 only; the Cloudflare Tunnel is the only way in, and this server serves nothing but
  /agent/message.
"""

import asyncio
import hmac
import html
import itertools
import json
import logging
import os
import re
import time
from collections import deque

import telegram
from aiohttp import web
from telegram import Update

log = logging.getLogger("clawmax")

HOST, PORT = "127.0.0.1", int(os.getenv("VIOLET_CLAWMAX_PORT", "8766"))
CHAT_ID = 999_000_000_001  # internal chat for the ClawMax path (kept apart from the owner's Telegram chat)
MAX_TEXT = 4000
PER_MINUTE, PER_DAY = 20, 300
TIMEOUT = 150.0


class RecordingBot(telegram.Bot):
    """Records what the handlers would send to the ClawMax chat, instead of calling Telegram."""

    def __init__(self, token: str):
        super().__init__(token)
        with self._unfrozen():
            self.out: list[dict] = []
            self._ids = itertools.count(1_000_000_000)

    async def _do_post(self, endpoint, data, **_):
        if endpoint == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "Violet", "username": "violet_clawmax"}
        if endpoint in ("sendMessage", "editMessageText", "sendVoice", "sendDocument", "sendPhoto"):
            chat = int(data.get("chat_id") or CHAT_ID)
            mk = data.get("reply_markup")
            mk = json.loads(mk) if isinstance(mk, str) else (mk.to_dict() if hasattr(mk, "to_dict") else (mk or {}))
            self.out.append({"endpoint": endpoint, "text": str(data.get("text") or data.get("caption") or ""), "markup": mk})
            return {"message_id": next(self._ids), "date": int(time.time()),
                    "chat": {"id": chat, "type": "private"}, "text": str(data.get("text") or "")}
        return True


def to_markdown(text: str) -> str:
    """Telegram HTML -> plain Markdown."""
    t = re.sub(r"<a\s+href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"[\2](\1)", text, flags=re.S)
    t = re.sub(r"</?(b|strong)>", "**", t)
    t = re.sub(r"</?(i|em)>", "_", t)
    t = re.sub(r"</?code>", "`", t)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def _card_hint(markup: dict) -> str | None:
    """Action cards have Telegram buttons; here they become a text instruction."""
    data = [b.get("callback_data", "") for row in markup.get("inline_keyboard", []) for b in row]
    if any(d.startswith("act:send:") or d.startswith("act:draft:") for d in data):
        return "Reply **send** to send it, **draft** to save it as a draft, or **no** to cancel."
    if any(d.startswith("act:do:") for d in data):
        return "Reply **yes** to confirm or **no** to cancel."
    return None


class _Limiter:
    def __init__(self):
        self.minute, self.day = deque(), deque()

    def allow(self) -> bool:
        now = time.monotonic()
        for q, span in ((self.minute, 60), (self.day, 86400)):
            while q and now - q[0] > span:
                q.popleft()
        if len(self.minute) >= PER_MINUTE or len(self.day) >= PER_DAY:
            return False
        self.minute.append(now)
        self.day.append(now)
        return True


_app = None
_bot: RecordingBot | None = None
_lock = asyncio.Lock()
_limiter = _Limiter()
_ids = itertools.count(1)


def _token() -> str:
    return os.getenv("VIOLET_CLAWMAX_TOKEN", "")


async def handle(request: web.Request) -> web.Response:
    t0 = time.monotonic()
    token = _token()
    got = request.headers.get("authorization", "")
    if not token or not hmac.compare_digest(got, f"Bearer {token}"):
        log.warning("clawmax: rejected a request (bad or missing token)")
        return web.json_response({"error": "unauthorized"}, status=401)
    if not _limiter.allow():
        log.warning("clawmax: rate limited")
        return web.json_response({"error": "rate limited, try again in a minute"}, status=429)
    try:
        body = await request.json()
        text = str(body.get("text") or "").strip()  # every other field (user ids, sender ids...) is ignored
    except Exception:
        return web.json_response({"error": "send JSON: {\"text\": \"...\"}"}, status=400)
    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    if len(text) > MAX_TEXT:
        return web.json_response({"error": f"text longer than {MAX_TEXT} characters"}, status=413)
    try:
        reply = await asyncio.wait_for(_run(text), TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("clawmax: timed out after %.0fs", TIMEOUT)
        return web.json_response({"error": "Violet took too long, try again"}, status=504)
    except Exception as e:
        log.exception("clawmax: request failed (%s)", type(e).__name__)
        return web.json_response({"error": "Violet hit an error"}, status=500)
    log.info("clawmax: answered in %.1fs (%d chars)", time.monotonic() - t0, len(reply))
    return web.json_response({"reply": reply})


async def _run(text: str) -> str:
    import users

    owner = int(users.OWNER_ID)  # always the owner, whatever the request says
    async with _lock:  # one at a time: the recorder is shared
        _bot.out.clear()
        n = next(_ids)
        msg = {"message_id": n, "date": int(time.time()), "chat": {"id": CHAT_ID, "type": "private"},
               "from": {"id": owner, "is_bot": False, "first_name": "Owner"}, "text": text}
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await _app.process_update(Update.de_json({"update_id": 10_000_000 + n, "message": msg}, _bot))
        parts, hints = [], []
        for m in _bot.out:
            if m["text"]:
                parts.append(to_markdown(m["text"]))
            h = _card_hint(m["markup"] or {})
            if h and h not in hints:
                hints.append(h)
        if hints:  # there are no buttons here: drop Telegram's "tap Do it / tap Send" wording
            parts = [re.sub(r"(?i)[^.!?\n]*\btap\b[^.!?\n]*[.!?]?\s*", "", p).strip() for p in parts]
        return "\n\n".join(p for p in parts + hints if p) or "(no reply)"


def serves(chat_id: int) -> bool:
    """True for updates coming through the ClawMax path."""
    return chat_id == CHAT_ID


async def start(build_app) -> web.AppRunner | None:
    """Start the endpoint (if VIOLET_CLAWMAX_TOKEN is set). build_app(token, bot=...) builds the handler app."""
    global _app, _bot
    if not _token():
        log.info("clawmax: VIOLET_CLAWMAX_TOKEN not set; endpoint off")
        return None
    _bot = RecordingBot("123456:CLAWMAX-RECORDER")
    _app = build_app("", bot=_bot)
    await _app.initialize()
    server = web.Application(client_max_size=64 * 1024)
    server.router.add_post("/agent/message", handle)  # the only route: everything else is 404
    runner = web.AppRunner(server, access_log=None)  # no access log: paths and timings only via our own lines
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    log.info("clawmax: endpoint listening on http://%s:%d/agent/message", HOST, PORT)
    return runner
