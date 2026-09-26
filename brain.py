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
# Cognee's session memory feeds earlier questions and answers back in as context, so one
# wrong answer ("your name is Keith") keeps coming back. The profile and graph are the truth.
os.environ.setdefault("CACHING", "false")

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


# Resolves a user id to the owner's name (set by selfmodel, which knows the profile).
_owner_name = lambda user_id: None  # noqa: E731


def set_owner_resolver(fn) -> None:
    global _owner_name
    _owner_name = fn


def anchor(user_id: int | str, text: str, kind: str = "message") -> str:
    """Prefix text so the graph knows who "I", "me" and "my" are, and that other names
    are other people. kind: message, document, calendar, correction."""
    name = _owner_name(user_id)
    who = f"{name} (the user)" if name else "The user"
    head = {
        "message": f"{who} wrote:",
        "document": f"{who} shared this document:",
        "calendar": f"From the calendar of {who}:",
        "correction": f"Correction from {who}, this replaces anything that said otherwise:",
    }[kind]
    return f"{head}\n{text}"


async def save(user_id: int | str, text: str, kind: str = "message") -> None:
    """Store text in the user's dataset, anchored to the user. Fast; call connect() afterwards."""
    ds = dataset_for(user_id)
    await cognee.add(anchor(user_id, text, kind), dataset_name=ds)
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


def document_memory(file_name: str, caption: str | None, text: str) -> str:
    """Text stored in memory for a file, with a header so questions can name the file."""
    header = [f"Document: {file_name}"]
    if caption:
        header.append(f"Caption: {caption}")
    return "\n".join(header) + "\n\n" + text


def _search_kwargs(system_prompt: str | None) -> dict:
    return {"system_prompt": system_prompt} if system_prompt else {}


async def recall_raw(user_id: int | str, question: str, system_prompt: str | None = None) -> str:
    """Cognee's answer from this user's memories, or "" if there is none or search fails."""
    try:
        results = await cognee.search(
            query_text=question,
            query_type=SearchType.GRAPH_COMPLETION,
            datasets=[dataset_for(user_id)],
            **_search_kwargs(system_prompt),
        )
    except Exception as e:
        log.info("search failed for %s: %s", dataset_for(user_id), type(e).__name__)
        return ""
    return _first_text(results)


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


# Items in memory that /forget must never delete wholesale: files and calendar summaries.
_BULK_MARKERS = ("shared this document:", "From the calendar of", "Document:", "New items from my", "Core profile of")


async def forget_text(user_id: int | str, text: str) -> int:
    """Delete saved chat memories that mention text. Documents, calendar summaries and the
    profile document are kept (the profile is rewritten by the caller). Returns the count."""
    needle = text.strip().lower()
    if not needle:
        return 0
    try:
        ds = next(d for d in await cognee.datasets.list_datasets() if d.name == dataset_for(user_id))
        items = await cognee.datasets.list_data(ds.id)
    except (StopIteration, Exception) as e:
        log.info("forget: could not list memories (%s)", type(e).__name__)
        return 0
    removed = 0
    for d in items:
        try:
            content = Path(str(d.raw_data_location).replace("file://", "")).read_text(errors="replace")
        except Exception:
            continue
        if any(m in content[:300] for m in _BULK_MARKERS) or needle not in content.lower():
            continue
        try:
            await cognee.datasets.delete_data(ds.id, d.id)
            removed += 1
        except Exception as e:
            log.info("forget: delete failed (%s)", type(e).__name__)
    return removed


async def context_for(user_id: int | str, question: str) -> str:
    """What the graph knows that is relevant to the question, as text (no answer written).
    The voice layer writes the answer from this."""
    try:
        results = await cognee.search(
            query_text=question,
            query_type=SearchType.GRAPH_COMPLETION,
            datasets=[dataset_for(user_id)],
            only_context=True,
        )
    except Exception as e:
        log.info("context search failed for %s: %s", dataset_for(user_id), type(e).__name__)
        return ""
    return _first_text(results)
