#!/usr/bin/env python3
"""Send one message to Violet and print its reply. Reads the message from stdin.

Needs two secrets in the environment: VIOLET_CLAWMAX_TOKEN (the service token) and VIOLET_CLAWMAX_URL
(the tunnel address, e.g. https://example.trycloudflare.com). Only the message text is sent.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    token = os.environ.get("VIOLET_CLAWMAX_TOKEN", "").strip()
    base = os.environ.get("VIOLET_CLAWMAX_URL", "").strip().rstrip("/")
    if not token or not base:
        print("Violet error: VIOLET_CLAWMAX_TOKEN or VIOLET_CLAWMAX_URL is not set for this skill.")
        return 1
    text = sys.stdin.read().strip()
    if not text:
        print("Violet error: no message given.")
        return 1
    url = base if base.endswith("/agent/message") else base + "/agent/message"
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=170) as r:
            print(json.loads(r.read().decode()).get("reply", "(no reply)"))
            return 0
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("error", "")
        except Exception:
            detail = ""
        print(f"Violet error: HTTP {e.code} {detail}".strip())
    except Exception as e:
        print(f"Violet error: couldn't reach Violet ({type(e).__name__}).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
