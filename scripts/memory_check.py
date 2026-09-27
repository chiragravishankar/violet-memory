"""Memory round trip and recall latency, through brain.py (the bot's own code path).

    COGNEE_MODE=local .venv/bin/python scripts/memory_check.py   # bot stopped (local DB lock)
    .venv/bin/python scripts/memory_check.py --roundtrip          # cloud (default mode)

Prints timings only, plus whether each answer came back non-empty.
"""

import argparse
import asyncio
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

import brain  # noqa: E402
import selfmodel  # noqa: E402,F401

OWNER = os.getenv("CALENDAR_USER_ID", "1067732196")
QUESTIONS = ["whats my name?", "who is Keith?", "what am I working on?"]


async def timed(coro):
    t = time.monotonic()
    out = await coro
    return out, time.monotonic() - t


async def main(roundtrip: bool, rounds: int, show: bool) -> None:
    print(f"mode: {brain.MODE}")
    if brain.CLOUD:
        t = time.monotonic()
        await brain.start(wait=120)
        print(f"connect: {'ok' if brain.connected() else 'FAILED'} in {time.monotonic() - t:.1f}s")
        if not brain.connected():
            return

    if roundtrip:
        token = uuid.uuid4().hex[:6]
        fact = f"Violet self-test {token} used the answer word heron."
        await brain.save("selftest", fact)
        _, took = await timed(brain.connect("selftest"))
        print(f"remember (dataset user_selftest): {took:.1f}s, queued left {brain.queued('selftest') if brain.CLOUD else 0}")
        t0 = time.monotonic()
        while True:  # remember() returns before the server finishes the graph
            ans, took = await timed(brain.recall_raw("selftest", f"What answer word did Violet self-test {token} use?", wait=120))
            if "heron" in ans.lower() or time.monotonic() - t0 > 180:
                break
            await asyncio.sleep(5)
        print(f"recall: {took:.1f}s (graph ready after {time.monotonic() - t0:.0f}s), "
              f"round trip {'OK' if 'heron' in ans.lower() else 'MISSED'}")

    times = []
    for r in range(rounds):
        for q in QUESTIONS:
            ctx, took = await timed(brain.context_for(OWNER, q, wait=60))
            times.append(took)
            print(f"  round {r + 1} {q!r}: {took:.2f}s, {'found' if ctx else 'empty'}"
                  + (f"\n    {ctx[:300]!r}" if show else ""))
    if not times:
        return
    print(f"recall latency ({brain.MODE}): median {statistics.median(times):.2f}s, "
          f"min {min(times):.2f}s, max {max(times):.2f}s over {len(times)} calls")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--roundtrip", action="store_true")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.roundtrip, a.rounds, a.show))
