"""Walk through multi-user Violet and print the conversation. Nothing is sent to Telegram.

    .venv/bin/python tests/demo_multiuser.py new    # a brand-new user + a stranger (temp data, fake memory)
    .venv/bin/python tests/demo_multiuser.py real   # the friend and the owner on real data (read-only
                                                    # questions; test turns are removed afterwards)
"""

import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODE = sys.argv[1] if len(sys.argv) > 1 else "new"
OWNER, FRIEND, NEW, STRANGER = "1067732196", "1482163307", "900000911", "900000912"
if MODE == "new":
    os.environ.update(VIOLET_DATA_DIR=tempfile.mkdtemp(prefix="violet-demo-"), VIOLET_OWNER_ID=OWNER, COGNEE_MODE="cloud")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import brain  # noqa: E402
import users  # noqa: E402

BS_LINK = "https://brightspace.nyu.edu/d2l/le/calendar/feed/user/feed.ics?token=demo-sam-token"
G_LINK = "https://calendar.google.com/calendar/ical/sam.demo%40gmail.com/private-demo123/basic.ics"


def show(who: str, said: str, out: list[dict]) -> None:
    print(f"\n{who}: {said}")
    for m in out:
        text = re.sub(r"<[^>]+>", "", m["text"]).strip()
        print("  Violet: " + text.replace("\n", "\n          "))
    if not out:
        print("  (no reply)")


def ics(n: int, kind: str) -> bytes:
    now = datetime.now(timezone.utc)
    evs = []
    for i in range(n):
        d = now + timedelta(days=1 + i % 10, hours=i)
        stamp = d.strftime("%Y%m%dT%H0000Z")
        title = (f"Algorithms Problem Set {i + 1} - Due" if kind == "bs" else f"Study group {i + 1}")
        where = "Algorithms: Monday 2pm" if kind == "bs" else "Bobst Library"
        evs.append(f"BEGIN:VEVENT\r\nUID:{kind}-{i}\r\nDTSTART:{stamp}\r\nDTEND:{stamp}\r\nSUMMARY:{title}\r\n"
                   f"LOCATION:{where}\r\nEND:VEVENT\r\n")
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(evs) + "END:VCALENDAR\r\n").encode()


async def new_user():
    import feeds

    harness.install_fake_cognee(brain)
    users.bootstrap()

    async def fetch(url):
        return ics(12, "bs") if "brightspace" in url else ics(5, "g")

    feeds.fetch = fetch
    app, tg = await harness.make_app()
    chat = harness.Chat(app, tg)

    show("Owner", "/invite 1 7", out := await chat.send(OWNER, "/invite 1 7", name="Chirag"))
    code = re.search(r"violet-[a-z0-9]+", out[0]["text"]).group(0)
    script = [f"/start {code}", "call me Sam", "NYU Tandon, MS in Computer Science, first year",
              "taking Algorithms and building a study-group app", "land a summer SWE internship",
              "short and quick", "no pings 11pm to 8am", BS_LINK, G_LINK, "/brief"]
    for said in script:
        out = await chat.send(NEW, said, name="Sam")
        show("Sam", "[calendar link]" if "http" in said else said, out)
    leaked = [m for m in tg.sent if "demo-sam-token" in m["text"] or "private-demo123" in m["text"]]
    print(f"\n[check] feed links ever shown back: {len(leaked)}; stored encrypted: "
          f"{[f['source'] for f in users.feeds(NEW)]}; Sam's items: "
          f"{len(__import__('schedule').existing_items(NEW))}")

    for said in ["hey, what's this bot?", "hello??"]:
        show("Stranger", said, await chat.send(STRANGER, said, name="Alex"))
    print(f"[check] stranger status: {users.get(STRANGER)['status']}")
    await app.shutdown()


async def real_users():
    import agent
    import bot

    users.bootstrap()
    await brain.start(wait=60)
    first_msg = bot._db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    first_turn = agent._db.execute("SELECT COALESCE(MAX(id), 0) FROM conversation").fetchone()[0]
    app, tg = await harness.make_app()
    chat = harness.Chat(app, tg)
    try:
        for who, uid, said in [("Friend", FRIEND, "what's my schedule tomorrow?"), ("Friend", FRIEND, "who is Keith?"),
                               ("Friend", FRIEND, "/inbox"), ("Owner", OWNER, "whats my name?"),
                               ("Owner", OWNER, "who is Keith?"), ("Owner", OWNER, "what's my schedule tomorrow?")]:
            show(who, said, await chat.send(uid, said, name=who))
        friend_text = " ".join(m["text"] for m in tg.to(FRIEND))
        print(f"\n[check] owner's details in the friend's replies: "
              f"{[w for w in ['Mauppa', 'Leslie', 'Chirag', 'Chewy', 'Jacoby'] if w.lower() in friend_text.lower()] or 'none'}")
    finally:
        bot._db.execute("DELETE FROM messages WHERE id > ? AND user_id IN (?, ?)", (first_msg, int(FRIEND), int(OWNER)))
        bot._db.commit()
        agent._db.execute("DELETE FROM conversation WHERE id > ? AND user_id IN (?, ?)", (first_turn, FRIEND, OWNER))
        agent._db.commit()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(new_user() if MODE == "new" else real_users())
