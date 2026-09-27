"""End-to-end reply timings through the agent, plus the fresh-memory and correction tests.

    .venv/bin/python scripts/timing.py --label before

Your three questions run as the owner; the test turns are removed from the conversation history
afterwards. The Priya and code-word tests use a throwaway test user whose cloud dataset is
deleted at the end.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

import agent  # noqa: E402
import brain  # noqa: E402
import selfmodel  # noqa: E402,F401

OWNER = os.getenv("CALENDAR_USER_ID", "1067732196")
TEST = "900000099"
OUT = Path("data/timings.json")


class _Paths(logging.Handler):
    last = ""

    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("answer:"):
            _Paths.last = msg.removeprefix("answer: ")


logging.getLogger("agent").addHandler(_Paths())
logging.getLogger("agent").setLevel(logging.INFO)


async def ask(uid, text):
    _Paths.last = ""
    t = time.monotonic()
    r = await agent.respond(uid, text)
    took = time.monotonic() - t
    r.path = _Paths.last
    if r.connect:
        asyncio.get_running_loop().create_task(brain.connect(uid))  # as the bot does
    return r, took


async def settle(uid, timeout=240):
    """Wait until the user's queued memories are sent and the cloud has built them."""
    t = time.monotonic()
    while brain.queued(uid) and time.monotonic() - t < timeout:
        await brain.flush(uid)
        await asyncio.sleep(2)
    await brain.wait_searchable(uid, timeout=max(30, timeout - (time.monotonic() - t)))
    return time.monotonic() - t


async def main(label: str) -> None:
    await brain.start(wait=60)
    results = {}
    first_id = agent._db.execute("SELECT COALESCE(MAX(id), 0) FROM conversation").fetchone()[0]
    try:
        for q in ["whats my name?", "who is Keith?", "what am I working on?"]:
            r, took = await ask(OWNER, q)
            results[q] = {"seconds": round(took, 2), "tools": r.tools, "path": r.path, "reply": r.text[:200]}
            print(f"{q!r}: {took:.1f}s tools={r.tools} {r.path}\n   {r.text[:160]!r}")

        agent.clear_history(TEST)
        r, took = await ask(TEST, "met Priya at the Leslie mixer, she builds fintech tools")
        print(f"tell Priya: {took:.1f}s tools={r.tools}")
        await asyncio.sleep(10)
        agent.clear_history(TEST)  # the answer must come from memory, not the chat so far
        r, took = await ask(TEST, "who's Priya?")
        ok = "fintech" in r.text.lower()
        results["who's Priya? (10s later)"] = {"seconds": round(took, 2), "tools": r.tools, "path": r.path,
                                               "reply": r.text[:200], "ok": ok}
        print(f"who's Priya? 10s later: {took:.1f}s tools={r.tools} {r.path} {'OK' if ok else 'MISSED'}\n   {r.text[:160]!r}")

        # Strict buffer test: a fact that only exists in memory (not the profile), asked right away.
        await brain.save(TEST, "Priya's startup is called LedgerLeaf.")
        asyncio.get_running_loop().create_task(brain.connect(TEST))
        agent.clear_history(TEST)
        r, took = await ask(TEST, "what's Priya's startup called?")
        ok = "ledgerleaf" in r.text.lower()
        results["fresh fact asked immediately"] = {"seconds": round(took, 2), "tools": r.tools, "path": r.path,
                                                   "reply": r.text[:200], "ok": ok}
        print(f"fresh fact asked immediately: {took:.1f}s tools={r.tools} {r.path} {'OK' if ok else 'MISSED'}\n   {r.text[:160]!r}")

        agent.clear_history(TEST)
        await ask(TEST, "Remember this: my test code word is heron.")
        await settle(TEST)
        agent.clear_history(TEST)
        r, took = await ask(TEST, "Correction: my test code word is now kestrel, not heron.")
        print(f"correction: {took:.1f}s tools={r.tools} {r.path}")
        results["correction reply"] = {"seconds": round(took, 2), "tools": r.tools}
        settled = await settle(TEST)
        t = time.monotonic()
        ans = await brain.recall_raw(TEST, "What is my test code word?", wait=60)
        took = time.monotonic() - t
        ctx = await brain.context_for(TEST, "test code word heron kestrel", wait=60)
        stale = "heron" in ctx.lower()
        ok = "kestrel" in ans.lower() and "heron" not in ans.lower() and not stale
        results["code-word correction"] = {"recall_seconds": round(took, 2), "settle_seconds": round(settled, 1),
                                           "answer": ans[:200], "old_word_still_in_memory": stale, "ok": ok}
        print(f"code word after correction: {ans[:120]!r} ({took:.1f}s); old word still in memory: {stale}; "
              f"{'OK' if ok else 'STALE'}")
    finally:
        agent._db.execute("DELETE FROM conversation WHERE id > ? AND user_id = ?", (first_id, OWNER))
        agent._db.commit()
        agent.clear_history(TEST)
        try:
            await brain.forget_dataset(TEST)
        except Exception as e:
            print(f"(could not delete test dataset: {brain._safe(e)})")
        agent._db.execute("DELETE FROM profiles WHERE user_id = ?", (TEST,))  # the test user's people
        agent._db.execute("DELETE FROM memory_outbox WHERE user_id = ?", (TEST,))
        agent._db.commit()
    data = json.loads(OUT.read_text()) if OUT.exists() else {}
    data[label] = results
    OUT.write_text(json.dumps(data, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    asyncio.run(main(ap.parse_args().label))
