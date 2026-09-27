"""The ClawMax endpoint, over real HTTP, in a temp data folder (fake memory, real LLM, no Telegram).

    .venv/bin/python tests/test_clawmax.py
"""

import asyncio
import logging
import os
import sys
import tempfile
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

OWNER, OTHER = "900000801", "900000802"
os.environ.update(VIOLET_DATA_DIR=tempfile.mkdtemp(prefix="violet-clawmax-"), VIOLET_OWNER_ID=OWNER,
                  COGNEE_MODE="cloud", VIOLET_CLAWMAX_TOKEN="test-token-abc", VIOLET_CLAWMAX_PORT="18766")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

import harness  # noqa: E402
import brain  # noqa: E402
import users  # noqa: E402

URL = "http://127.0.0.1:18766/agent/message"
AUTH = {"authorization": "Bearer test-token-abc"}
results = []


class Capture(logging.Handler):
    lines: list[str] = []

    def emit(self, record):
        Capture.lines.append(record.getMessage())


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))


async def ask(c: httpx.AsyncClient, text: str, **extra):
    r = await c.post(URL, headers=AUTH, json={"text": text, **extra}, timeout=180)
    return r.status_code, (r.json().get("reply") or r.json().get("error") or "")


async def main():
    import bot
    import clawmax
    import schedule
    import selfmodel

    logging.getLogger().addHandler(Capture())
    logging.getLogger().setLevel(logging.INFO)
    harness.install_fake_cognee(brain)
    users.bootstrap()
    users.add(OTHER, status="active", name="Other")
    selfmodel.set_identity(OWNER, name="Chirag Testowner")
    selfmodel.set_identity(OTHER, name="Olivia Other")
    day = (datetime.now(schedule.TZ) + timedelta(days=2)).date()
    schedule.add_items(OWNER, [schedule.Item(kind="deadline", title="Owner Essay Draft", due_date=day, due_time=dtime(17, 0))],
                       source="chat", label="t")
    schedule.add_items(OTHER, [schedule.Item(kind="deadline", title="Other Secret Lab", due_date=day)], source="chat", label="t")
    import actions

    async def no_google(act):  # never touch the real Google Calendar from a test
        return {"event_id": "test-event", "link": None, "confirm": "Added it to your calendar."}
    actions._do_calendar = no_google
    runner = await clawmax.start(bot.build_app)
    AUTH["authorization"] = f"Bearer {os.environ['VIOLET_CLAWMAX_TOKEN']}"  # (.env reloads on import)

    async with httpx.AsyncClient() as c:
        r = await c.post(URL, json={"text": "whats my name?"})
        check("no token -> 401", r.status_code == 401)
        r = await c.post(URL, headers={"authorization": "Bearer wrong"}, json={"text": "hi"})
        check("wrong token -> 401", r.status_code == 401)
        r = await c.get("http://127.0.0.1:18766/")
        check("nothing else is served", r.status_code in (404, 405))

        st, rep = await ask(c, "whats my name?")
        check("answers as the owner", st == 200 and "chirag" in rep.lower(), rep[:150])
        print("   whats my name? ->", rep[:150])
        st, rep = await ask(c, "whats my name?", user_id=OTHER, sender_id=OTHER, from_id=OTHER)
        check("ignores user/sender ids in the request", "olivia" not in rep.lower() and "chirag" in rep.lower(), rep[:150])
        st, rep = await ask(c, "what's due this week?")
        check("owner's schedule, not another user's", "essay" in rep.lower() and "secret lab" not in rep.lower(), rep[:200])
        print("   what's due this week? ->", rep[:200].replace("\n", " | "))
        check("reply is Markdown, no HTML tags", "<b>" not in rep and "</" not in rep, rep[:120])
        st, rep = await ask(c, "/week")
        check("commands work (/week)", st == 200 and "WEEK" in rep.upper(), rep[:120])

        st, rep = await ask(c, f"put 'Study group with Omar' on my calendar {day.isoformat()} at 3pm for an hour")
        check("action card comes back as text", "reply **yes** to confirm" in rep.lower(), rep[-200:])
        check("no 'tap' instructions on the card", "tap " not in rep.lower(), rep[:120])
        print("   calendar card ->", rep[-160:].replace("\n", " | "))
        a = next((x for x in actions.pending_for(OWNER)), None)
        check("a pending card exists", a is not None)

        st, rep = await ask(c, "yes")
        a2 = actions.get(a.id) if a else None
        check("'yes' goes through the approval logic", a2 is not None and a2.status == "done" and "added" in rep.lower(),
              f"{a2 and a2.status}: {rep[:150]}")
        print("   yes ->", rep[:150].replace("\n", " | "))
        check("no Telegram 'tap' wording in card replies", "tap " not in rep.lower())

        leaks = [l for l in Capture.lines if "whats my name" in l.lower() or "essay" in l.lower() or os.environ["VIOLET_CLAWMAX_TOKEN"] in l]
        check("logs have no message text or token", not leaks, str(leaks[:2]))

        codes = []
        for _ in range(25):
            r = await c.post(URL, headers=AUTH, json={"text": ""})
            codes.append(r.status_code)
        check("rate limited after 20 a minute", 429 in codes, str(codes[-5:]))
    await runner.cleanup()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
