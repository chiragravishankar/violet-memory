"""Spike-only: the outbound unknowns through the Gateway's message tool (HTML, message ids + edits, buttons,
voice note from a local file, typing). Sends to the owner's chat with the TEST bot only.

    .venv/bin/python openclaw/spike_outbound.py
"""

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
GW = "http://127.0.0.1:18789/tools/invoke"
H = {"authorization": f"Bearer {ENV['OPENCLAW_GATEWAY_TOKEN']}", "content-type": "application/json"}
TEST_API = f"https://api.telegram.org/bot{ENV['TELEGRAM_TEST_BOT_TOKEN']}"
CHAT = ENV.get("VIOLET_OWNER_ID", "1067732196")


def invoke(args: dict) -> tuple[float, dict]:
    t = time.monotonic()
    r = httpx.post(GW, headers=H, json={"tool": "message", "args": args}, timeout=60)
    try:
        out = r.json()
    except Exception:
        out = {"raw": r.text[:300]}
    return time.monotonic() - t, {"status": r.status_code, **out}


def rendered(message_id) -> dict:
    """What Telegram actually shows: re-set the same (empty) keyboard on the test bot's own message, which
    returns the full Message with its text and formatting entities. Test bot only; changes nothing visible."""
    kb = {"inline_keyboard": [[{"text": "·", "callback_data": "vb:noop"}]]}
    r = httpx.post(f"{TEST_API}/editMessageReplyMarkup", json={"chat_id": CHAT, "message_id": int(message_id), "reply_markup": kb}, timeout=20).json()
    httpx.post(f"{TEST_API}/editMessageReplyMarkup", json={"chat_id": CHAT, "message_id": int(message_id)}, timeout=20)
    m = r.get("result") or {}
    return {"ok": r.get("ok"), "text": (m.get("text") or m.get("caption") or "")[:120],
            "entities": [e["type"] for e in m.get("entities") or m.get("caption_entities") or []],
            "error": None if r.get("ok") else r.get("description", "")[:120]}


def mid(res: dict):
    blob = json.dumps(res)
    import re
    m = re.search(r'"message[_]?[iI]d"\s*:\s*"?(\d+)', blob)
    return m.group(1) if m else None


def main():
    out = {}
    html = 'HTML test: <b>bold</b> <i>italic</i> <a href="https://nyu.edu">link</a> &amp; 5 &lt; 6 · <code>code</code>'
    dt, r = invoke({"action": "send", "channel": "telegram", "to": CHAT, "message": html})
    out["html_send"] = {"seconds": round(dt, 2), "result": r}
    m1 = mid(r)
    out["html_message_id"] = m1
    if m1:
        out["html_rendered"] = rendered(m1)
    md = "Markdown test: **bold** _italic_ [link](https://nyu.edu) & 5 < 6 · `code`"
    dt, r = invoke({"action": "send", "channel": "telegram", "to": CHAT, "message": md})
    m2 = mid(r)
    out["markdown_send"] = {"seconds": round(dt, 2), "message_id": m2, "rendered": rendered(m2) if m2 else None}
    dt, r = invoke({"action": "send", "channel": "telegram", "to": CHAT, "message": "Buttons test (tap one):",
                    "presentation": {"blocks": [{"type": "buttons", "buttons": [
                        {"label": "✅ Going", "value": "vb:ev:going:test1"}, {"label": "⏰ Later", "value": "vb:ev:later:test1"}]}]}})
    m3 = mid(r)
    out["buttons_send"] = {"seconds": round(dt, 2), "message_id": m3, "result": r if not m3 else "ok"}
    if m3:
        dt, r = invoke({"action": "edit", "channel": "telegram", "chatId": CHAT, "messageId": m3,
                        "message": "Buttons test (edited in place): <b>edit works</b>"})
        out["edit"] = {"seconds": round(dt, 2), "result": r, "rendered": rendered(m3)}
    voice = ROOT / "openclaw" / "state" / "media" / "outbound" / "spike-voice.ogg"
    voice.parent.mkdir(parents=True, exist_ok=True)
    src = ROOT / "openclaw" / "state" / "spike-voice.ogg"
    if src.exists():
        src.replace(voice)
    if voice.exists():
        dt, r = invoke({"action": "send", "channel": "telegram", "to": CHAT, "media": str(voice), "asVoice": True})
        out["voice"] = {"seconds": round(dt, 2), "result": r}
    for act in ("typing", "chat-action", "sendChatAction"):
        dt, r = invoke({"action": act, "channel": "telegram", "to": CHAT, "chatId": CHAT, "kind": "typing"})
        out[f"typing:{act}"] = {"status": r.get("status"), "error": str(r.get("error") or r.get("raw") or "")[:200]}
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
