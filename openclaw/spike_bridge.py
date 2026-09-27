"""Spike-only bridge stub for the five unknowns (not the real bridge). Records inbound events from the
violet-bridge plugin and answers through the Gateway's message tool so we can see what survives.

    .venv/bin/python openclaw/spike_bridge.py
"""

import hashlib
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
BRIDGE_TOKEN = ENV["VIOLET_BRIDGE_TOKEN"]
GATEWAY = "http://127.0.0.1:18789/tools/invoke"
GW_HEADERS = {"authorization": f"Bearer {ENV['OPENCLAW_GATEWAY_TOKEN']}", "content-type": "application/json"}
LOG = ROOT / "openclaw" / "state" / "spike-events.jsonl"


def invoke(args: dict) -> dict:
    r = httpx.post(GATEWAY, headers=GW_HEADERS, json={"tool": "message", "args": args}, timeout=60)
    try:
        return {"status": r.status_code, **r.json()}
    except Exception:
        return {"status": r.status_code, "text": r.text[:300]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.headers.get("authorization") != f"Bearer {BRIDGE_TOKEN}":
            self.send_response(401); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        kind = self.path.rsplit("/", 1)[-1]
        text = body.get("text") or body.get("data") or ""
        rec = {"at": time.time(), "kind": kind, "senderId": body.get("senderId"), "chatId": body.get("chatId"),
               "messageId": body.get("messageId"), "text_sha": hashlib.sha256(text.encode()).hexdigest()[:12],
               "starts_with_start": text.startswith("/start"), "start_arg_len": len(text.split(" ", 1)[1]) if text.startswith("/start ") else None,
               "media": [{k: (v if k != "path" else bool(v and os.path.exists(v))) for k, v in m.items()} for m in body.get("media") or []]}
        with LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        if kind == "message":
            time.sleep(6)  # long enough to see whether "typing…" shows while Violet works
        self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
        # Echo the facts back so the tester sees them (never the raw text of anything but the test bot's own chat)
        chat = body.get("chatId") or body.get("senderId")
        if kind == "message":
            note = (f"got: {text[:80]!r} | media: {[m.get('kind') for m in body.get('media') or []]} "
                    f"| file readable: {[bool(m.get('path') and os.path.exists(m['path'])) for m in body.get('media') or []]}")
        else:
            note = f"button tap: {text!r} on message {body.get('messageId')}"
        invoke({"action": "send", "channel": "telegram", "to": str(chat), "message": note})


if __name__ == "__main__":
    print("spike bridge on 127.0.0.1:8765", flush=True)
    HTTPServer(("127.0.0.1", 8765), H).serve_forever()
