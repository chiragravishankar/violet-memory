"""Nothing is sent twice across a restart.

    .venv/bin/python tests/test_no_resend.py

Two separate processes (a real restart) share one temp data folder and a throwaway user:
1. /demo_time at a reminder's moment fires it; after the restart the same /demo_time doesn't resend it.
2. The real reminder path: a due reminder goes out once; the item is then deleted and re-created (as a feed
   sync does), and after the restart it still isn't sent again.
3. A brief claimed for a day can't be claimed again after the restart.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
U = "900000555"


def child(phase: str, data_dir: str) -> dict:
    env = {**os.environ, "VIOLET_DATA_DIR": data_dir, "VIOLET_OWNER_ID": "900000501", "COGNEE_MODE": "cloud"}
    out = subprocess.run([sys.executable, __file__, phase], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    line = [l for l in out.stdout.splitlines() if l.startswith("RESULT ")]
    if not line:
        print(out.stdout[-2000:], out.stderr[-2000:])
        raise SystemExit(f"phase {phase} failed")
    return json.loads(line[-1][7:])


async def phase(name: str) -> dict:
    sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tests"))
    import harness
    import brain
    import users
    import schedule
    import delivered
    from datetime import datetime, time as dtime, timedelta, timezone

    harness.install_fake_cognee(brain)
    users.bootstrap()
    users.add(U, status="active", name="Throwaway")
    app, tg = await harness.make_app()
    chat = harness.Chat(app, tg)
    day = (datetime.now(schedule.TZ) + timedelta(days=2)).date()
    fire_at = datetime.combine(day, dtime(11, 0), schedule.TZ)  # 3h before a 2 PM deadline
    res = {}
    if name == "first":
        schedule.add_items(U, [schedule.Item(kind="deadline", title="Throwaway essay", due_date=day, due_time=dtime(14, 0))],
                           source="chat", label="test")
    out = await chat.send(U, f"/demo_time {fire_at:%Y-%m-%d %H:%M}", name="Throwaway")
    res["demo_reminders_sent"] = sum("Throwaway essay" in m["text"] for m in out)
    res["demo_said_already"] = any("already went out" in m["text"] for m in out)

    # the real path: pretend it's 11:00 that day
    if name == "first":
        due = schedule.pop_due_reminders(fire_at + timedelta(seconds=30))
        res["real_sent_first"] = [(u, i.title, l) for u, i, l in due]
        # a feed sync deletes and re-creates the item (new row id, reminders re-planned)
        it = next(i for i in schedule.existing_items(U) if i.title == "Throwaway essay")
        schedule.remove_item(U, it.id)
        schedule.add_items(U, [schedule.Item(kind="deadline", title="Throwaway essay", due_date=day, due_time=dtime(14, 0))],
                           source="chat", label="test again")
        res["brief_claim_first"] = delivered.claim(U, "brief", day.isoformat())
    else:
        due = schedule.pop_due_reminders(fire_at + timedelta(seconds=45))
        res["real_sent_after_restart"] = [(u, i.title, l) for u, i, l in due]
        res["brief_claim_after_restart"] = delivered.claim(U, "brief", day.isoformat())
    res["log"] = [list(r) for r in delivered.history(U, 10)]
    await app.shutdown()
    return res


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="violet-resend-")
    a = child("first", data_dir)
    b = child("second", data_dir)
    checks = [
        ("/demo_time fires the reminder the first time", a["demo_reminders_sent"] == 1),
        ("after a restart, /demo_time doesn't resend it", b["demo_reminders_sent"] == 0 and b["demo_said_already"]),
        ("the real reminder goes out once", len(a["real_sent_first"]) == 1),
        ("after the item is re-created and the bot restarts, it isn't sent again", b["real_sent_after_restart"] == []),
        ("a brief is claimed once", a["brief_claim_first"] is True),
        ("after a restart the same brief can't be claimed again", b["brief_claim_after_restart"] is False),
    ]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    print("delivery log:", *[f"  {r}" for r in b["log"]], sep="\n")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("first", "second"):
        print("RESULT " + json.dumps(asyncio.run(phase(sys.argv[1])), default=str))
    else:
        sys.exit(main())
