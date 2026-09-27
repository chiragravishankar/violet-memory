"""Move each user's local Cognee memory to Cognee Cloud.

    .venv/bin/python scripts/migrate_to_cloud.py --dry-run   # counts only, no network
    .venv/bin/python scripts/migrate_to_cloud.py             # push, falling back to re-ingest
    .venv/bin/python scripts/migrate_to_cloud.py --reingest  # skip push

Stop the bot first (local Cognee holds a database lock while it runs).

1. cognee.push() uploads each local dataset's knowledge graph as-is (no LLM calls).
2. If push isn't supported by the workspace, the texts Violet saved (chat notes, document
   summaries, calendar summaries, corrections, email summaries; never raw emails) are
   re-ingested per user through brain's outbox, and the graph is rebuilt in the cloud.
Either way every migrated text is recorded in the outbox so /forget can still find it, and the
profile (about me + people) is rewritten fresh from SQLite.
"""

import argparse
import asyncio
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

import brain  # noqa: E402
import selfmodel  # noqa: E402,F401  (sets the owner-name resolver used by anchors)

import cognee  # noqa: E402  (after brain: local storage paths)

KINDS = [("wrote:", "message"), ("shared this document:", "document"), ("Document:", "document"),
         ("From the calendar of", "calendar"), ("New items from my", "calendar"), ("New events from my", "calendar"), ("Correction from", "correction"),
         ("From an email", "email"), ("Core profile of", "profile")]


def kind_of(text: str) -> str:
    head = text[:300]
    return next((k for marker, k in KINDS if marker in head), "message")  # early, unanchored chat notes


async def local_items() -> dict[str, list[tuple[str, str]]]:
    """{dataset name: [(kind, text)]} for every local user dataset."""
    out = {}
    for ds in await cognee.datasets.list_datasets():
        uid = ds.name.removeprefix("user_")
        if not ds.name.startswith("user_") or not uid.isdigit() or uid.startswith("9000000"):
            continue  # test datasets from simulations stay behind
        rows = []
        for d in await cognee.datasets.list_data(ds.id):
            try:
                text = Path(str(d.raw_data_location).replace("file://", "")).read_text(errors="replace")
            except Exception:
                continue
            if text.strip():
                rows.append((kind_of(text), text))
        out[ds.name] = rows
    return out


def report(items: dict) -> None:
    for name, rows in items.items():
        c = Counter(k for k, _ in rows)
        print(f"  {name}: {len(rows)} items  " + ", ".join(f"{k} {n}" for k, n in sorted(c.items())))


async def main(dry: bool, reingest: bool) -> None:
    items = await local_items()
    print("Local memory:")
    report(items)
    if dry:
        return
    if not brain.SERVICE_URL or not brain._api_key():
        raise SystemExit("COGNEE_SERVICE_URL and COGNEE_API_KEY must be set in .env")

    pushed = set()
    if not reingest:
        for name in items:
            t = time.monotonic()
            try:
                r = await cognee.push(name, url=brain.SERVICE_URL, api_key=brain._api_key(), mode="preserve")
                print(f"  push {name}: {r.status}, {r.num_nodes} nodes, {r.num_edges} edges in {time.monotonic() - t:.0f}s")
                pushed.add(name)
            except Exception as e:
                print(f"  push {name} failed ({brain._safe(e)}); will re-ingest instead")

    await brain.start(wait=120)
    if not brain.connected():
        raise SystemExit("Could not connect to Cognee Cloud")

    for name, rows in items.items():
        uid = name.removeprefix("user_")
        rows = [(k, t) for k, t in rows if k != "profile"]  # the profile is rewritten fresh below
        sent = brain._now() if name in pushed else None
        for k, text in rows:
            brain.enqueue(uid, text, k, source="migrated", sent=sent)
        if name not in pushed:
            t = time.monotonic()
            n = 0
            while brain.queued(uid):
                got = await brain.flush(uid)
                if not got:
                    print(f"  {name}: {brain.queued(uid)} items still queued (will retry in the bot)")
                    break
                n += got
            print(f"  re-ingested {name}: {n} items in {time.monotonic() - t:.0f}s")
        try:
            ok = await selfmodel.sync_to_memory(uid, force=True)
            print(f"  profile for {name}: {'rewritten' if ok else 'empty, skipped'}")
        except Exception as e:
            print(f"  profile for {name} not written yet ({brain._safe(e)}); the bot retries it")

    print("Cloud workspace now:")
    for d in await brain.cloud_datasets():
        print(f"  {d['name']}: {d['items']} items")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reingest", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.dry_run, a.reingest))
