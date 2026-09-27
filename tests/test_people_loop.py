"""The people loop, end to end, on a throwaway user (temp data, fake Telegram and memory, real LLM).

    .venv/bin/python tests/test_people_loop.py
"""

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

os.environ.update(VIOLET_DATA_DIR=tempfile.mkdtemp(prefix="violet-people-"), VIOLET_OWNER_ID="900000701", COGNEE_MODE="cloud")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import brain  # noqa: E402
import users  # noqa: E402

U = "900000702"
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))


def plain(out):
    return " ".join(re.sub(r"<[^>]+>", "", m["text"]) for m in out)


async def main():
    import events
    import people
    import schedule
    from eventsources import Event

    harness.install_fake_cognee(brain)
    users.bootstrap()
    users.add(U, status="active", name="Sam Tester")
    app, tg = await harness.make_app()
    chat = harness.Chat(app, tg)
    now = datetime.now(schedule.TZ)

    out = await chat.send(U, "met Priya Shah at the Leslie mixer, she builds fintech tools. I promised to send her my "
                             "pitch deck by Wednesday", name="Sam")
    print("capture:", plain(out)[:160])
    p = people.search(U, name="Priya")
    check("Priya saved with where we met", p and "leslie" in (p[0]["met_at"] + p[0]["text"]).lower(), str(p))
    check("Priya's field saved", p and "fintech" in (p[0]["field"] + p[0]["text"]).lower(), str(p))
    fus = people.open_followups(U)
    check("the promise became a follow-up record", any("priya" in f["person"].lower() and "deck" in f["promise"].lower() for f in fus), str(fus))

    await chat.send(U, "also met Omar Haddad at the Stern alumni panel, he works in climate VC", name="Sam")
    for q, want in [("who did I meet at the Leslie mixer?", "priya"), ("who do I know in fintech?", "priya"),
                    ("what did I promise Priya?", "deck"), ("who should I follow up with this week?", "priya")]:
        r = plain(await chat.send(U, q, name="Sam"))
        check(f"answers {q!r}", want in r.lower(), r[:200])
        print(f"   {q} -> {r[:160]}")
    r = plain(await chat.send(U, "who did I meet at the Leslie mixer?", name="Sam"))
    check("Omar isn't listed for the Leslie mixer", "omar" not in r.lower(), r[:200])

    out = await chat.send(U, "/people", name="Sam")
    check("/people groups by where", "Leslie" in plain(out) and "Stern" in plain(out), plain(out)[:300])
    print("/people:", plain(out)[:300])
    out = await chat.send(U, "/followups", name="Sam")
    check("/followups lists the open one with buttons", "Priya" in plain(out) and "fu:done" in out[0]["markup"], plain(out)[:200])

    # a follow-up reminder when it's due: buttons, snooze, done -> last_contact
    f = next(f for f in people.open_followups(U) if "priya" in f["person"].lower())
    people._db.execute("UPDATE followups SET due_date = ? WHERE id = ?", (now.date().isoformat(), f["id"])); people._db.commit()
    due = people.due_reminders(U, datetime.combine(now.date(), dtime(10, 30), schedule.TZ))
    check("the reminder fires on the due day, once", len(due) == 1 and not people.due_reminders(U, datetime.combine(now.date(), dtime(11, 0), schedule.TZ)))
    v = people.reminder_view(due[0])
    check("the reminder has Draft / Done / Snooze", [b[0] for b in v.buttons[0]] == ["✍️ Draft message", "✅ Done", "⏰ Snooze 2 days"])
    out = await chat.tap(U, f"fu:snooze:{f['id']}", name="Sam")
    check("Snooze moves it 2 days", people.get(U, f["id"])["due_date"] == (now.date() + timedelta(days=2)).isoformat(), plain(out))
    out = await chat.tap(U, f"fu:done:{f['id']}", name="Sam")
    lc = next(x for x in people.all_people(U) if "priya" in x["name"].lower())["last_contact"]
    check("Done closes it and updates last_contact", people.get(U, f["id"])["status"] == "done" and lc == now.date().isoformat(), f"{lc}")

    # post-event check-in: an event they said "going" to yesterday
    y = now - timedelta(days=1)
    ev = Event(id="test:mixer", source="test", title="Fintech Founders Mixer", start=y.replace(hour=18, minute=0, second=0, microsecond=0))
    events.upsert([ev], now)
    events._db.execute("INSERT OR REPLACE INTO event_state (user_id, event_id, status, updated_at) VALUES (?,?,?,?)",
                       (U, "test:mixer", "going", now.isoformat())); events._db.commit()
    out = await chat.send(U, "/brief", name="Sam")
    text, markup = plain(out), " ".join(m["markup"] for m in out)
    check("the brief asks who they met", "Fintech Founders Mixer" in text and "ci:tell" in markup, text[-300:])
    out2 = await chat.send(U, "/brief", name="Sam")
    check("asked once per event", "ci:tell" not in " ".join(m["markup"] for m in out2))
    key = re.search(r"ci:tell:([^'\"]+)", markup).group(1)
    await chat.tap(U, f"ci:tell:{key}", name="Sam")
    await chat.send(U, "Lena Ortiz, she runs payments at a startup, and I owe her an intro to Priya by Friday", name="Sam")
    lena = people.search(U, name="Lena")
    check("check-in answer saves the person with the event", lena and "mixer" in (lena[0]["met_at"] + lena[0]["text"]).lower(), str(lena))
    check("and the promise as a follow-up", any("lena" in f["person"].lower() for f in people.open_followups(U)), str(people.open_followups(U)))
    await app.shutdown()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
