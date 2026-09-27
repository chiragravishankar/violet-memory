"""Crawl the NYU knowledge base now (the bot also does this weekly).

    .venv/bin/python scripts/nyu_crawl.py              # crawl + upload changed pages to Cognee Cloud
    .venv/bin/python scripts/nyu_crawl.py --no-upload  # crawl into the local index only
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
for noisy in ("httpx", "cognee"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

import brain  # noqa: E402
import nyu_kb  # noqa: E402


async def main(upload: bool) -> None:
    if upload:
        await brain.start(wait=60)
    report = await nyu_kb.crawl(upload=upload)
    print(json.dumps(report, indent=1))
    print(nyu_kb.stats())


if __name__ == "__main__":
    asyncio.run(main("--no-upload" not in sys.argv))
