"""Violet Memory brain: all memory logic. No channel (Telegram) code here."""

import logging
import os
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Keep Cognee's databases next to this project instead of inside site-packages.
_HERE = Path(__file__).resolve().parent
os.environ.setdefault("DATA_ROOT_DIRECTORY", str(_HERE / "data" / "cognee_data"))
os.environ.setdefault("SYSTEM_ROOT_DIRECTORY", str(_HERE / "data" / "cognee_system"))

import cognee  # noqa: E402  (must import after env setup)
from cognee import SearchType  # noqa: E402

log = logging.getLogger("brain")

# Per-user count of cognify runs still in progress.
_pending: dict[str, int] = defaultdict(int)
# Users who have saved at least one memory in this process or earlier.
_known: set[str] = set()


def dataset_for(user_id: int | str) -> str:
    """Each user gets their own private dataset, named from their user id."""
    return f"user_{user_id}"


def is_connecting(user_id: int | str) -> bool:
    """True while new memories for this user are still being connected."""
    return _pending[dataset_for(user_id)] > 0


async def save(user_id: int | str, text: str) -> None:
    """Store raw text in the user's dataset. Fast; call connect() afterwards."""
    ds = dataset_for(user_id)
    await cognee.add(text, dataset_name=ds)
    _known.add(ds)


async def connect(user_id: int | str) -> None:
    """Build the knowledge graph for the user's new memories (slow, LLM calls)."""
    ds = dataset_for(user_id)
    _pending[ds] += 1
    try:
        await cognee.cognify(datasets=[ds])
    except Exception:
        log.exception("cognify failed for %s", ds)
    finally:
        _pending[ds] -= 1


async def remember(user_id: int | str, text: str) -> None:
    """Save and connect in one go (for callers that don't need to split them)."""
    await save(user_id, text)
    await connect(user_id)


async def recall(user_id: int | str, question: str) -> str:
    """Answer a question using only this user's memories."""
    ds = dataset_for(user_id)
    connecting = is_connecting(user_id)
    note = "\n\n(New memories may take a few seconds to connect.)" if connecting else ""

    try:
        results = await cognee.search(
            query_text=question,
            query_type=SearchType.GRAPH_COMPLETION,
            datasets=[ds],
        )
    except Exception as e:
        log.info("search failed for %s: %s", ds, e)
        if connecting:
            return "I'm still connecting your new memories. Ask me again in a few seconds 💜"
        if ds not in _known:
            return "I don't have any memories from you yet. Text me something to remember 💜"
        return "Sorry, I couldn't search your memories right now. Try again in a moment 💜"

    answer = _first_text(results)
    if not answer:
        return "I couldn't find anything about that in your memories 💜" + note
    return answer + note


def _first_text(results) -> str:
    """Pull a plain text answer out of Cognee search results."""
    for r in results or []:
        # cognee 1.6.1 returns dicts here despite its SearchResult type hint.
        if isinstance(r, dict) and "search_result" in r:
            value = r["search_result"]
        else:
            value = getattr(r, "search_result", r)
        if isinstance(value, list):
            value = next((v for v in value if v), "")
        if isinstance(value, dict):
            value = value.get("answer") or value.get("text") or ""
        text = str(value).strip() if value else ""
        if text:
            return text.replace("—", ", ").replace("**", "")
    return ""
