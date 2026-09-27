"""Multi-user isolation tests. Must pass before any commit that touches users or data.

    .venv/bin/python tests/test_isolation.py

Runs Violet's real handlers in a temp data folder with a fake Telegram and a fake Cognee Cloud (LLM
calls are real). Two users with clearly marked private data, plus a stranger:
  A = the owner, "Chirag Testowner" (Keith Mauppa at Leslie eLab, OM Final, Project Nightjar)
  B = a normal user, "Maya Test" (Stats Quiz 2, a cat named Pixel)
Every command, question, button, agent tool and background job run for one user must never show,
change or reach the other's data or memory. Exits 1 on any failure.
"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="violet-iso-")
A, B, S = "900000901", "900000902", "900000903"
os.environ.update(VIOLET_DATA_DIR=TMP, VIOLET_OWNER_ID=A, COGNEE_MODE="cloud")
from cryptography.fernet import Fernet  # noqa: E402

os.environ["VIOLET_SECRET_KEY"] = Fernet.generate_key().decode()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402
import brain  # noqa: E402
import users  # noqa: E402

A_MARK = ["Mauppa", "Leslie", "OM Final", "Operations Management", "Nightjar", "Testowner", "program manager",
          "A-Secret", "credits proposal"]
B_MARK = ["Stats Quiz", "Pixel", "Maya Test", "B-Secret", "Patel"]
URL_A = "https://brightspace.nyu.edu/d2l/le/calendar/feed/user/feed.ics?token=AAAAsecretA"
URL_B = "https://brightspace.nyu.edu/d2l/le/calendar/feed/user/feed.ics?token=BBBBsecretB"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail and not ok else ""))


def leaks(texts, marks) -> list[str]:
    blob = "\n".join(t if isinstance(t, str) else json.dumps(t, default=str) for t in texts)
    return [m for m in marks if m.lower() in blob.lower()]


def ics(prefix: str, n: int) -> bytes:
    now = datetime.now(timezone.utc)
    evs = []
    for i in range(n):
        d = (now + timedelta(days=i + 1)).strftime("%Y%m%dT170000Z")
        evs.append(f"BEGIN:VEVENT\r\nUID:{prefix}-{i}\r\nDTSTART:{d}\r\nDTEND:{d}\r\n"
                   f"SUMMARY:{prefix} Assignment {i} - Due\r\nLOCATION:{prefix} Course\r\nEND:VEVENT\r\n")
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(evs) + "END:VCALENDAR\r\n").encode()


async def seed(fake):
    import events
    import schedule
    import selfmodel
    from eventsources import Event

    users.bootstrap()
    users.add(B, status="active", role="user", name="Maya")
    now = datetime.now(schedule.TZ)
    tomorrow = (now + timedelta(days=1)).date()

    selfmodel.set_identity(A, name="Chirag Testowner")
    selfmodel.add_facts(A, [{"section": "people", "text": "Keith Mauppa: senior program manager at Leslie eLab",
                             "attrs": {"name": "Keith Mauppa", "relationship": "mentor", "context": "Leslie eLab"}},
                            {"section": "goals", "text": "Working on Project Nightjar and a credits proposal"}],
                        "chat", stated=True, confidence=1.0)
    selfmodel.set_identity(B, name="Maya Test")
    selfmodel.add_facts(B, [{"section": "people", "text": "Riya Patel: roommate, studies biology",
                             "attrs": {"name": "Riya Patel", "relationship": "roommate"}},
                            {"section": "goals", "text": "Wants to ace Stats Quiz 2"}],
                        "chat", stated=True, confidence=1.0)

    schedule.add_items(A, [schedule.Item(kind="exam", title="OM Final Exam", course="Operations Management",
                                         due_date=tomorrow, due_time=dtime(10, 0)),
                           schedule.Item(kind="deadline", title="Follow up with Keith Mauppa about the credits proposal",
                                         due_date=tomorrow)], source="chat", label="seed")
    schedule.add_items(B, [schedule.Item(kind="quiz", title="Stats Quiz 2", course="Statistics",
                                         due_date=tomorrow, due_time=dtime(14, 0))], source="chat", label="seed")

    await brain.save(A, "My secret project is Project Nightjar. Keith Mauppa at Leslie eLab is reviewing it.")
    await brain.save(B, "My cat is named Pixel and my roommate Riya Patel studies biology.")
    await brain.flush(A)
    await brain.flush(B)

    e1 = Event(id="test:e1", source="test", title="Founders Dinner", start=datetime.combine(tomorrow, dtime(18, 0), schedule.TZ),
               location="Tandon", description="Dinner for student founders")
    e2 = Event(id="test:e2", source="test", title="Biology Mixer", start=datetime.combine(tomorrow, dtime(19, 0), schedule.TZ),
               location="Silver", description="Meet biology students")
    events.upsert([e1, e2], now)
    for uid, eid, score, reason in [(A, "test:e1", 95, "fits your Project Nightjar"), (A, "test:e2", 20, "generic"),
                                    (B, "test:e1", 40, "generic founders event"), (B, "test:e2", 90, "your roommate Riya's field")]:
        events._db.execute("INSERT OR REPLACE INTO event_scores (user_id, event_id, score, reason, reg_deadline, scored_at) "
                           "VALUES (?,?,?,?,NULL,?)", (uid, eid, score, reason, datetime.now(timezone.utc).isoformat()))
    events._db.commit()
    await events.act(A, "going", events.get_event("test:e1"))

    users.set_feed(A, "brightspace", URL_A, items=3)
    users.set_feed(B, "brightspace", URL_B, items=2)


async def main():
    import agent
    import actions
    import bot
    import brief
    import events
    import feeds
    import inbox
    import schedule
    import selfmodel
    import views

    fake = harness.install_fake_cognee(brain)
    await seed(fake)
    app, tg = await harness.make_app()
    chat = harness.Chat(app, tg)
    calls = {"agent": 0}
    real_respond = agent.respond

    async def counting(*a, **k):
        calls["agent"] += 1
        return await real_respond(*a, **k)

    agent.respond = counting
    bot.agent.respond = counting

    # ---------- strangers ----------
    out = await chat.send(S, "hi, my student id is N12345678, can you help?", name="Stranger")
    check("stranger gets exactly the waitlist reply", len(out) == 1 and "invite-only" in out[0]["text"], str(out))
    check("stranger stored as waitlist", (users.get(S) or {}).get("status") == "waitlist")
    logged = bot._db.execute("SELECT COUNT(*) FROM messages WHERE text LIKE '%N12345678%'").fetchone()[0]
    check("stranger's message content not logged", logged == 0)
    check("stranger never reaches the agent", calls["agent"] == 0)
    out = await chat.send(S, "hello??", name="Stranger")
    out += await chat.send(S, "/start not-a-code", name="Stranger")
    check("waitlist reply is rate-limited", len(out) == 0, str(out))
    out = await chat.tap(S, f"tr:anything", name="Stranger")
    check("stranger button taps do nothing", len(out) == 0)
    check("stranger has no memory dataset", not any(d.endswith(S) for d in fake.touched()))

    # ---------- commands, as B then A ----------
    fake.calls.clear()
    b_out = []
    for cmd in ["/week", "/today", "/brief", "/me", "/events", "/events all", "/connect status", "/help",
                "/inbox", "/actions", "/email alerts on", "/settings calendar auto", "/users", "/usage", "/invite",
                "/block 900000901", "/undo"]:
        b_out += await chat.send(B, cmd, name="Maya")
    check("B's commands show none of A's data", not leaks([m["text"] for m in b_out], A_MARK), str(leaks([m["text"] for m in b_out], A_MARK)))
    check("B's commands send nothing to A's chat", not tg.to(A))
    check("B can't use owner commands", users.is_active(A) and not any("violet-" in m["text"] for m in b_out))
    check("B gets 'coming soon' for Google features", sum("coming soon" in m["text"].lower() for m in b_out) >= 4)
    check("B's feed status never shows the link", not any("BBBBsecret" in m["text"] or "token=" in m["text"] for m in b_out))
    check("B's commands touch only B's memory", fake.touched() <= {brain.dataset_for(B)}, str(fake.touched()))

    a_out = []
    for cmd in ["/week", "/today", "/brief", "/me", "/events", "/connect status"]:
        a_out += await chat.send(A, cmd, name="Chirag")
    check("A's commands show none of B's data", not leaks([m["text"] for m in a_out], B_MARK), str(leaks([m["text"] for m in a_out], B_MARK)))
    check("A's /week shows A's own OM Final", any("OM Final" in m["text"] for m in a_out))

    # ---------- questions (real LLM), as B ----------
    fake.calls.clear()
    qs = ["what's Chirag's schedule?", "who is Keith?", "what's my schedule tomorrow?",
          "what's Chirag's secret project?", "tell me about Project Nightjar", "what do you know about Leslie eLab?",
          "did Keith reply to my email?", "add a meeting with Keith to my Google Calendar tomorrow at 3"]
    replies = {}
    for q in qs:
        out = await chat.send(B, q, name="Maya")
        replies[q] = " ".join(m["text"] for m in out)
    for q, r in replies.items():
        marks = [m for m in A_MARK if m.lower() not in q.lower()]  # words they asked about don't count as leaks
        check(f"B asks {q!r}: no A data", not leaks([r], marks), f"{leaks([r], marks)} in {r[:200]!r}")
    check("B's own schedule answer has B's quiz", "quiz" in replies["what's my schedule tomorrow?"].lower(),
          replies["what's my schedule tomorrow?"][:200])
    check("B's questions touch only B's memory", fake.touched() <= {brain.dataset_for(B)}, str(fake.touched()))
    check("no action card was created for B", not actions._db.execute("SELECT 1 FROM actions WHERE user_id = ?", (B,)).fetchone())

    fake.calls.clear()
    out = await chat.send(A, "who is Keith?", name="Chirag")
    ra = " ".join(m["text"] for m in out)
    check("A asks 'who is Keith?': gets A's Keith", "mauppa" in ra.lower() or "leslie" in ra.lower(), ra[:200])
    check("A's answer has no B data", not leaks([ra], B_MARK))
    check("A's question touches only A's memory", fake.touched() <= {brain.dataset_for(A)}, str(fake.touched()))

    # ---------- agent tools called directly as B ----------
    fake.calls.clear()
    turn = agent.Turn(B, datetime.now(schedule.TZ))
    a_items_before = {i.id: i.done for i in schedule.existing_items(A)}
    tool_calls = [("get_agenda", {"start": (datetime.now(schedule.TZ) + timedelta(days=1)).date().isoformat(),
                                  "end": (datetime.now(schedule.TZ) + timedelta(days=1)).date().isoformat()}),
                  ("get_next_items", {"n": 10}), ("search_memory", {"query": "Nightjar Keith Leslie"}),
                  ("get_profile", {}), ("find_events", {"query": ""}), ("find_people", {"query": "Keith Mauppa"}),
                  ("get_followups", {}), ("mark_done", {"item": "OM Final Exam"}),
                  ("respond_to_event", {"event_id": "test:e1", "action": "not_for_me"}),
                  ("search_email", {"query": "from:keith"}), ("save_contact_email", {"name": "Keith", "email": "k@x.com"}),
                  ("propose_action", {"type": "calendar_add", "payload": {"title": "x", "start": "2030-01-01T10:00"}, "summary": "x"}),
                  ("render_view", {"view": "week"}), ("correct_memory", {"new_fact": "Keith Mauppa left Leslie eLab", "replaces": "Keith at Leslie"}),
                  ("save_memory", {"text": "B-Secret: I like pottery"})]
    tool_out = []
    for name, args in tool_calls:
        try:
            tool_out.append(await agent.IMPL[name](turn, **args) if name not in agent.OWNER_TOOLS
                            else {"not_available": agent.COMING_SOON} if not users.is_owner(B) else await agent.IMPL[name](turn, **args))
        except Exception as e:
            tool_out.append({"error": type(e).__name__})
    await harness.settle()
    tool_out += [v.plain for v in turn.views]
    check("B's agent tools return none of A's data", not leaks(tool_out, A_MARK), str(leaks(tool_out, A_MARK)))
    check("B's mark_done didn't touch A's items", {i.id: i.done for i in schedule.existing_items(A)} == a_items_before)
    st = events._db.execute("SELECT status FROM event_state WHERE user_id = ? AND event_id = 'test:e1'", (A,)).fetchone()
    check("B's event response didn't change A's 'going'", st and st[0] == "going")
    check("B's tools touch only B's memory", fake.touched() <= {brain.dataset_for(B)}, str(fake.touched()))
    check("B's correction removed none of A's memories",
          brain._outbox_db().execute("SELECT COUNT(*) FROM memory_outbox WHERE user_id = ?", (A,)).fetchone()[0] >= 1)
    check("owner-only tools refuse B even if called", "coming soon" in json.dumps(await agent.t_search_email(turn, "x")).lower())

    # ---------- buttons with A's tokens, tapped by B ----------
    tok = "atoken123456"
    bot._db.execute("INSERT INTO voice_transcripts (token, user_id, text, created_at) VALUES (?,?,?,?)",
                    (tok, int(A), "A-Secret transcript about Nightjar", datetime.now(timezone.utc).isoformat()))
    bot._db.commit()
    it = next(i for i in schedule.existing_items(A) if "OM Final" in i.title)
    card = actions.propose(A, "calendar_add", actions.calendar_from_item(it), it.title, "agent")
    bot._more_pages["morepage1"] = (int(A), ["A-Secret more page"])
    out = []
    for data in [f"tr:{tok}", f"tx:{tok}", "more:morepage1", f"act:do:{card.id}", f"act:edit:{card.id}",
                 f"act:undo:{card.id}", "em:undo:someemail", f"draft:{it.id}", "inbox", "week:0", "ev:going:test:e2"]:
        out += await chat.tap(B, data, name="Maya")
    check("B's button taps reveal none of A's data", not leaks([m["text"] for m in out], A_MARK), str(leaks([m["text"] for m in out], A_MARK)))
    check("B can't approve A's card", actions.get(card.id).status == "pending")
    check("A's show-more page is still there for A", "morepage1" in bot._more_pages)

    # ---------- background jobs ----------
    now = datetime.now(schedule.TZ)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    for uid in (A, B):
        for i in schedule.existing_items(uid):
            schedule._db.execute("INSERT INTO reminders (item_id, user_id, remind_at, lead, sent) VALUES (?,?,?,?,0)",
                                 (i.id, uid, past, "hours"))
    schedule._db.commit()
    due = schedule.pop_due_reminders(now)
    by_user = {}
    for uid, item, lead in due:
        v = await bot.assistant.reminder_message(uid, item, lead, now, [])
        by_user.setdefault(str(uid), []).append(v.plain)
    check("reminders go to the item's owner", all(str(i.id) in {str(x.id) for x in schedule.existing_items(u)}
                                                    for u, i, _ in due))
    check("B's reminders show none of A's data", not leaks(by_user.get(B, []), A_MARK), str(by_user.get(B)))
    check("A's reminders show none of B's data", not leaks(by_user.get(A, []), B_MARK))

    fake.calls.clear()
    vb = await brief.daily(B, now.date() + timedelta(days=1), now)
    check("B's brief has none of A's data", not leaks([vb.plain], A_MARK), str(leaks([vb.plain], A_MARK)))
    check("B's brief touches only B's memory", fake.touched() <= {brain.dataset_for(B)}, str(fake.touched()))
    va = await brief.daily(A, now.date() + timedelta(days=1), now)
    check("A's brief has none of B's data", not leaks([va.plain], B_MARK))

    pb = events.picks_for_day(B, (now + timedelta(days=1)).date())
    mb = events.matches(B, now, now + timedelta(days=3))
    check("B's event picks use B's own scores", not leaks([p.reason for p in pb] + [m.reason for m in mb], A_MARK))
    check("A 'going' isn't B 'going'", not events._db.execute(
        "SELECT 1 FROM event_state WHERE user_id = ? AND event_id = 'test:e1' AND status = 'going'", (B,)).fetchone())
    fb = [e.title for e in schedule.agenda_entries(B, now.date(), now.date() + timedelta(days=7), now)
          if brief._FOLLOW_UP.search(e.title)]
    check("B's follow-ups are B's own", not leaks(fb, A_MARK), str(fb))

    async def fake_fetch(url):
        return ics("A-Secret", 3) if url == URL_A else ics("B-Secret", 2)

    feeds.fetch = fake_fetch
    res = await feeds.sync_all()
    a_titles = [i.title for i in schedule.existing_items(A)]
    b_titles = [i.title for i in schedule.existing_items(B)]
    check("feed sync: each user's feed lands only in their schedule",
          any("A-Secret" in t for t in a_titles) and not any("A-Secret" in t for t in b_titles)
          and any("B-Secret" in t for t in b_titles) and not any("B-Secret" in t for t in a_titles), str(res.keys()))
    check("feed links are encrypted at rest", not users._db.execute("SELECT 1 FROM feeds WHERE secret LIKE '%secret%'").fetchone())
    check("feed links never logged", not bot._db.execute("SELECT 1 FROM messages WHERE text LIKE '%token=%'").fetchone())

    # ---------- memory layer ----------
    check("buffer: B never sees A's fresh memories", not leaks(brain.fresh(B, "Nightjar Keith Leslie"), A_MARK))
    ca = await brain.context_for(A, "secret project Nightjar")
    cb = await brain.context_for(B, "secret project Nightjar")
    check("recall cache is per user", "Nightjar" in ca and not leaks([cb], A_MARK), f"A={ca[:80]!r} B={cb[:80]!r}")
    try:
        brain.dataset_for("x/../user_900000901")
        bad = False
    except ValueError:
        bad = True
    check("a dataset name can't be forged from a user id", bad)

    # ---------- limits and blocking ----------
    users.set_limits(B, messages=users.used(B, "messages") + 1)
    await chat.send(B, "hello", name="Maya")
    out = await chat.send(B, "hello again", name="Maya")
    check("daily message limit gives a friendly message", any("limit" in m["text"].lower() for m in out), str(out))
    users.set_limits(B, messages=1000)
    await chat.send(A, f"/block {B}", name="Chirag")
    out = await chat.send(B, "are you there?", name="Maya")
    check("blocked user gets nothing", len(out) == 0, str(out))
    await chat.send(A, f"/unblock {B}", name="Chirag")
    out = await chat.send(B, "/week", name="Maya")
    check("unblocked user works again", len(out) >= 1)
    check("usage is tracked per user", users.used(B, "messages") > 0 and users.used(B, "tokens") > 0,
          f"B messages {users.used(B, 'messages')}, tokens {users.used(B, 'tokens')}")

    await app.shutdown()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f", {len(failed)} FAILED" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
