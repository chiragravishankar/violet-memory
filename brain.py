"""Violet Memory brain: all memory logic. No channel (Telegram) code here.

This is the only module that touches Cognee. Two modes (COGNEE_MODE in .env):

- cloud (default): memory lives in Cognee Cloud. start() calls cognee.serve() with
  COGNEE_SERVICE_URL and COGNEE_API_KEY, after which Cognee 1.6.1 routes remember, recall,
  forget, improve, add, cognify, search and datasets.list_data to the hosted instance.
  Checked in the installed SDK (Sep 2026): datasets.list_datasets and delete_data stay local,
  and remote add/remember drop node_set, so this module doesn't rely on any of those in cloud mode.
  The hosted workspace sleeps when idle and can take up to a minute to wake, so writes go
  through a SQLite outbox (retried with backoff, never lost) and reads are time-boxed: an
  interactive read gives up after a few seconds and callers fall back to profile and schedule
  data (see fallback_note()).
- local: the original setup, Cognee's databases under data/. Kept for rollback.

The API key is never logged or printed.
"""

import asyncio
import hashlib
import logging
import os
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

import paths
import users

load_dotenv()

# Keep Cognee's databases next to this project instead of inside site-packages.
_HERE = Path(__file__).resolve().parent
os.environ.setdefault("DATA_ROOT_DIRECTORY", str(paths.DATA_DIR / "cognee_data"))
os.environ.setdefault("SYSTEM_ROOT_DIRECTORY", str(paths.DATA_DIR / "cognee_system"))
# Cognee's session memory feeds earlier questions and answers back in as context, so one
# wrong answer ("your name is Keith") keeps coming back. The profile and graph are the truth.
os.environ.setdefault("CACHING", "false")

# Read before importing cognee: its import reloads .env with override=True, which would undo
# an explicit COGNEE_MODE=local on the command line.
MODE = "local" if os.getenv("COGNEE_MODE", "cloud").strip().lower() == "local" else "cloud"
CLOUD = MODE == "cloud"
SERVICE_URL = os.getenv("COGNEE_SERVICE_URL", "").strip().rstrip("/")

import cognee  # noqa: E402  (must import after env setup)
from cognee import SearchType  # noqa: E402

log = logging.getLogger("brain")


INTERACTIVE_WAIT = 10.0  # seconds a reply waits for a memory read before falling back
BACKGROUND_WAIT = 90.0  # seconds a background job waits (covers a cold workspace)
CONNECT_WAIT = 90.0  # seconds per serve() attempt; the first call may wake the workspace
WRITE_WAIT = 300.0  # seconds for one remember() batch (builds the graph server-side)
RETRY_STEPS = [30, 120, 600, 1800, 3600]  # outbox backoff, seconds
CONNECT_STEPS = [5, 15, 30, 60, 120, 300]  # serve() backoff, seconds
WARM_AFTER = 240.0  # ping the workspace on new activity if it's been quiet this long
BATCH = 20  # memories per remember() call

# Per-user count of cognify runs still in progress.
_pending: dict[str, int] = defaultdict(int)
# Users who have saved at least one memory in this process or earlier.
_known: set[str] = set()


def _api_key() -> str:
    return os.getenv("COGNEE_API_KEY", "").strip()


def _safe(e: BaseException) -> str:
    """Short error description for logs: type and HTTP status, never bodies or the key."""
    import re

    s = str(e)
    m = re.search(r"\((\d{3})\)", s)
    out = type(e).__name__ + (f" {m.group(1)}" if m else "")
    key = _api_key()
    return out.replace(key, "***") if key else out


def dataset_for(user_id: int | str) -> str:
    """Each user gets their own private dataset, named from their user id. Nothing else can name a
    dataset, so one user's memory calls can never reach another's."""
    uid = str(user_id)
    if not uid or not all(c.isalnum() or c in "_-" for c in uid):
        raise ValueError("bad user id")
    return f"user_{uid}"


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
        "email": f"From an email {who} received (a summary, not the email itself):",
    }[kind]
    return f"{head}\n{text}"


# ---------- cloud connection ----------

_ready: asyncio.Event | None = None
_connector: asyncio.Task | None = None
_flusher: asyncio.Task | None = None
_warming: asyncio.Task | None = None
_last_ok = 0.0  # monotonic time of the last successful cloud call
_slow_since = 0.0  # set when a cloud call failed or timed out, cleared on success
_locks: dict[str, asyncio.Lock] = {}


def _ok() -> None:
    global _last_ok, _slow_since
    _last_ok, _slow_since = time.monotonic(), 0.0


def _slow() -> None:
    global _slow_since
    _slow_since = _slow_since or time.monotonic()


def connected() -> bool:
    return not CLOUD or bool(_ready and _ready.is_set())


def available() -> bool:
    """False while cloud memory is unreachable, asleep or not answering in time."""
    return not CLOUD or (connected() and not _slow_since)


FALLBACK_NOTE = ("Long-term memory is hosted and waking up right now, so it didn't answer in time. "
                 "Answer from the profile, schedule and other tools, and if the answer might be "
                 "incomplete, say in a few words that your memory is still catching up.")


def fallback_note() -> str:
    """Text for an LLM prompt when memory couldn't be read, or "" when it's fine."""
    return "" if available() else FALLBACK_NOTE


async def start(wait: float = 45.0) -> None:
    """Connect to Cognee Cloud (cloud mode) on the running event loop. Waits up to `wait`
    seconds for the first connection, then keeps retrying in the background. Call once at
    startup, before any memory operation. Local mode: nothing to do."""
    global _ready, _connector, _flusher, _locks
    if not CLOUD:
        log.info("memory: local Cognee (COGNEE_MODE=local)")
        return
    _ready, _locks = asyncio.Event(), {}
    for t in (_connector, _flusher):
        if t and not t.done():
            t.cancel()
    _outbox_db()
    if not SERVICE_URL or not _api_key():
        log.error("memory: COGNEE_MODE=cloud but COGNEE_SERVICE_URL or COGNEE_API_KEY is empty. "
                  "New memories are queued and nothing is read until both are set (or set COGNEE_MODE=local).")
        return
    _connector = asyncio.create_task(_connect_loop())
    _flusher = asyncio.create_task(_flush_loop())
    try:
        await asyncio.wait_for(asyncio.shield(_ready.wait()), wait)
    except asyncio.TimeoutError:
        log.warning("memory: Cognee Cloud not connected after %.0fs, still trying in the background", wait)


async def _connect_loop() -> None:
    from cognee.exceptions import CogneeConfigurationError

    attempt = 0
    while True:
        attempt += 1
        t = time.monotonic()
        try:
            await asyncio.wait_for(cognee.serve(url=SERVICE_URL, api_key=_api_key()), CONNECT_WAIT)
            _ok()
            _ready.set()
            log.info("memory: connected to Cognee Cloud in %.1fs (attempt %d)", time.monotonic() - t, attempt)
            return
        except CogneeConfigurationError:
            wait = 1800
            log.error("memory: Cognee Cloud rejected the API key. Memory is paused; fix COGNEE_API_KEY. "
                      "Checking again in %d min.", wait // 60)
        except (asyncio.TimeoutError, Exception) as e:
            wait = CONNECT_STEPS[min(attempt - 1, len(CONNECT_STEPS) - 1)]
            log.warning("memory: Cognee Cloud unreachable (%s, attempt %d), retrying in %ds", _safe(e), attempt, wait)
        await asyncio.sleep(wait)


def touch(reason: str = "") -> None:
    """Called on user activity: if the hosted workspace may have gone to sleep, start waking it
    now so the reply that follows doesn't wait on it. Never blocks."""
    global _warming
    if not CLOUD or not connected() or time.monotonic() - _last_ok < WARM_AFTER:
        return
    if _warming and not _warming.done():
        return
    if reason:
        log.info("memory: waking the workspace (%s)", reason)
    _warming = asyncio.create_task(_warm())


async def _warm() -> None:
    from cognee.api.v1.serve.state import get_remote_client

    client = get_remote_client()
    if client is None:
        return
    t = time.monotonic()
    try:
        status = await asyncio.wait_for(client._auth_check(), BACKGROUND_WAIT)
        if status and status < 400:
            _ok()
            log.info("memory: workspace awake (%.1fs)", time.monotonic() - t)
        else:
            _slow()
            log.warning("memory: warm-up got status %s", status)
    except (asyncio.TimeoutError, Exception) as e:
        _slow()
        log.warning("memory: warm-up failed (%s)", _safe(e))


# ---------- outbox (cloud writes) ----------

_DB_PATH = paths.VIOLET_DB
_odb: sqlite3.Connection | None = None


def _outbox_db() -> sqlite3.Connection:
    global _odb
    if _odb is None:
        _odb = sqlite3.connect(_DB_PATH, check_same_thread=False, timeout=15)
        _odb.execute("PRAGMA journal_mode=WAL")
        _odb.execute("""CREATE TABLE IF NOT EXISTS memory_outbox (
            id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
            name TEXT NOT NULL, created TEXT NOT NULL, next_try TEXT NOT NULL, attempts INTEGER DEFAULT 0,
            sent TEXT, source TEXT DEFAULT 'live')""")
        _odb.execute("CREATE INDEX IF NOT EXISTS memory_outbox_due ON memory_outbox(sent, next_try)")
        cols = {r[1] for r in _odb.execute("PRAGMA table_info(memory_outbox)")}
        if "ready" not in cols:  # when the cloud confirmed the item is searchable
            _odb.execute("ALTER TABLE memory_outbox ADD COLUMN ready TEXT")
            _odb.execute("UPDATE memory_outbox SET ready=sent WHERE sent IS NOT NULL")
        _odb.commit()
    return _odb


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upload_name(text: str) -> str:
    """The name Cognee gives a text item (text_<md5 of the text>), without extension."""
    return "text_" + hashlib.md5(text.encode("utf-8")).hexdigest()


def enqueue(user_id, text: str, kind: str = "message", source: str = "live", sent: str | None = None) -> None:
    """Queue already-anchored text for the user's cloud dataset (or record it as already sent,
    which also counts as searchable)."""
    db = _outbox_db()
    now = _now()
    db.execute("INSERT INTO memory_outbox (user_id, kind, text, name, created, next_try, sent, source, ready) "
               "VALUES (?,?,?,?,?,?,?,?,?)", (str(user_id), kind, text, upload_name(text), now, now, sent, source, sent))
    db.commit()
    clear_cache(user_id)


def queued(user_id=None) -> int:
    q, args = "SELECT COUNT(*) FROM memory_outbox WHERE sent IS NULL", ()
    if user_id is not None:
        q, args = q + " AND user_id=?", (str(user_id),)
    return _outbox_db().execute(q, args).fetchone()[0]


async def flush(user_id, wait: float = WRITE_WAIT) -> int:
    """Send this user's due outbox items with remember(). Returns how many went out.
    Failures reschedule with backoff; nothing is dropped."""
    if not connected() or not _api_key():
        return 0
    uid = str(user_id)
    lock = _locks.setdefault(uid, asyncio.Lock())
    sent = 0
    async with lock:
        db = _outbox_db()
        while True:
            rows = db.execute("SELECT id, text, attempts FROM memory_outbox WHERE user_id=? AND sent IS NULL "
                              "AND next_try<=? ORDER BY id LIMIT ?", (uid, _now(), BATCH)).fetchall()
            if not rows:
                return sent
            t = time.monotonic()
            try:
                await asyncio.wait_for(
                    cognee.remember([r[1] for r in rows], dataset_name=dataset_for(uid), self_improvement=False), wait)
            except (asyncio.TimeoutError, Exception) as e:
                _slow()
                attempts = max(r[2] for r in rows) + 1
                delay = RETRY_STEPS[min(attempts - 1, len(RETRY_STEPS) - 1)]
                nxt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
                db.executemany("UPDATE memory_outbox SET attempts=attempts+1, next_try=? WHERE id=?",
                               [(nxt, r[0]) for r in rows])
                db.commit()
                log.warning("memory: %d item(s) for %s not saved yet (%s, attempt %d), retrying in %ds",
                            len(rows), dataset_for(uid), _safe(e), attempts, delay)
                return sent
            _ok()
            users.record(uid, "cognee_saves", len(rows))
            db.executemany("UPDATE memory_outbox SET sent=? WHERE id=?", [(_now(), r[0]) for r in rows])
            db.commit()
            clear_cache(uid)
            _confirm_later(uid, [r[0] for r in rows])
            sent += len(rows)
            log.info("memory: saved %d item(s) to %s in %.1fs", len(rows), dataset_for(uid), time.monotonic() - t)


async def _flush_loop() -> None:
    """Background retry for queued memories (e.g. saved while the workspace was asleep)."""
    while True:
        await asyncio.sleep(60)
        if not connected():
            continue
        try:
            users = [r[0] for r in _outbox_db().execute(
                "SELECT DISTINCT user_id FROM memory_outbox WHERE sent IS NULL AND next_try<=?", (_now(),))]
            for uid in users:
                ds = dataset_for(uid)
                _pending[ds] += 1
                try:
                    await flush(uid)
                finally:
                    _pending[ds] -= 1
        except Exception:
            log.exception("memory: outbox loop error")


# ---------- writes ----------

async def save(user_id: int | str, text: str, kind: str = "message") -> None:
    """Store text in the user's dataset, anchored to the user. Fast; call connect() afterwards.
    Cloud mode only queues it (never waits on the network). Things the user asked to keep count
    toward their daily limit (raises users.LimitReached); calendar and email summaries don't."""
    ds = dataset_for(user_id)
    if kind in ("message", "document", "correction"):
        users.spend(user_id, "memory_saves")
    if CLOUD:
        enqueue(user_id, anchor(user_id, text, kind), kind)
    else:
        await cognee.add(anchor(user_id, text, kind), dataset_name=ds)
    _known.add(ds)


async def connect(user_id: int | str) -> None:
    """Build the knowledge graph for the user's new memories (slow, LLM calls). Background only."""
    ds = dataset_for(user_id)
    _pending[ds] += 1
    try:
        if CLOUD:
            await flush(user_id)
        else:
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


# ---------- reads ----------

def _search_kwargs(system_prompt: str | None) -> dict:
    return {"system_prompt": system_prompt} if system_prompt else {}


async def _cloud_recall(user_id, question: str, wait: float, **kw) -> str:
    """One time-boxed recall against the cloud. "" on miss, timeout or error (see available())."""
    if not connected():
        if wait < BACKGROUND_WAIT or not _ready:
            return ""
        try:
            await asyncio.wait_for(asyncio.shield(_ready.wait()), wait)
        except asyncio.TimeoutError:
            return ""
    ds = dataset_for(user_id)
    users.record(user_id, "cognee_recalls")
    t = time.monotonic()
    task = asyncio.ensure_future(cognee.recall(
        question, query_type=SearchType.GRAPH_COMPLETION, datasets=[ds], **kw))
    try:
        # shield: if we stop waiting, the request still finishes and wakes the workspace
        results = await asyncio.wait_for(asyncio.shield(task), wait)
    except asyncio.TimeoutError:
        _slow()
        task.add_done_callback(_after_slow_recall)
        log.warning("memory: recall for %s took over %.0fs, answering without it", ds, wait)
        return ""
    except Exception as e:
        if "(404)" in str(e):  # no dataset yet for this user: nothing to find
            _ok()
            return ""
        if "(409)" in str(e):  # the server is still building the graph from a recent remember()
            _ok()
            log.info("memory: %s is still being processed, answering without it for now", ds)
            return ""
        _slow()
        log.warning("memory: recall for %s failed (%s)", ds, _safe(e))
        return ""
    _ok()
    log.info("memory: recall for %s in %.2fs", ds, time.monotonic() - t)
    return _first_text(results)


def _after_slow_recall(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is None:
        _ok()  # the workspace answered after all: it's awake now


async def recall_raw(user_id: int | str, question: str, system_prompt: str | None = None,
                     wait: float = INTERACTIVE_WAIT) -> str:
    """Cognee's answer from this user's memories, or "" if there is none or search fails."""
    if CLOUD:
        return await _cloud_recall(user_id, question, wait, **_search_kwargs(system_prompt))
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


def _tidy(text: str) -> str:
    """Cloud context comes as a node dump; drop the markers and empty file-name nodes."""
    import re

    text = text.replace("__node_content_start__\n", "").replace("\n__node_content_end__", "")
    text = re.sub(r"Node: text_[0-9a-f]{32}\nNone\n*", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _first_text(results) -> str:
    """Pull a plain text answer out of Cognee search or recall results."""
    for r in results or []:
        # cognee 1.6.1 returns dicts here despite its SearchResult type hint; cloud recall
        # returns dicts too, with the text under one of several keys depending on the source.
        if isinstance(r, dict):
            value = next((r[k] for k in ("search_result", "answer", "text", "content", "context", "result")
                          if r.get(k)), "")
        else:
            value = getattr(r, "search_result", r)
        if isinstance(value, list):
            value = next((v for v in value if v), "")
        if isinstance(value, dict):
            value = value.get("answer") or value.get("text") or value.get("content") or ""
        text = _tidy(str(value)) if value else ""
        if text:
            return text.replace("—", ", ").replace("**", "")
    return ""


async def context_for(user_id: int | str, question: str, wait: float = INTERACTIVE_WAIT) -> str:
    """What the graph knows that is relevant to the question, as text (no answer written).
    The voice layer writes the answer from this. Pass wait=BACKGROUND_WAIT from background jobs.
    Answers are cached per user for CACHE_TTL; saving anything new for the user clears them."""
    if CLOUD:
        hit = _cached(user_id, question)
        if hit is not None:
            return hit
        gen = _generation[str(user_id)]
        text = await _cloud_recall(user_id, question, wait, only_context=True)
        if text and gen == _generation[str(user_id)]:  # nothing new was saved meanwhile
            _cache.setdefault(str(user_id), {})[_qkey(question)] = (time.monotonic(), text)
        return text
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


# ---------- datasets, forgetting, the profile document ----------

async def _cloud_dataset_id(ds_name: str):
    """The cloud dataset's id (list_datasets doesn't route to the cloud, so ask the API)."""
    from cognee.api.v1.serve.state import get_remote_client

    client = get_remote_client()
    if client is None:
        raise RuntimeError("not connected")
    session = await client._get_session()
    async with session.get(f"{client.service_url}/api/v1/datasets") as resp:
        if resp.status >= 400:
            raise RuntimeError(f"list datasets failed ({resp.status})")
        rows = await resp.json()
    for r in rows or []:
        if r.get("name") == ds_name:
            return uuid.UUID(str(r["id"]))
    return None


async def cloud_datasets() -> list[dict]:
    """[{name, id, items}] in the cloud workspace (for migration reports and checks)."""
    from cognee.api.v1.serve.state import get_remote_client

    client = get_remote_client()
    session = await client._get_session()
    async with session.get(f"{client.service_url}/api/v1/datasets") as resp:
        rows = await resp.json() if resp.status < 400 else []
    out = []
    for r in rows or []:
        try:
            n = len(await client.list_data(uuid.UUID(str(r["id"]))))
        except Exception:
            n = None
        out.append({"name": r.get("name"), "id": str(r.get("id")), "items": n})
    return out


async def _cloud_items(ds_name: str) -> tuple:
    ds_id = await _cloud_dataset_id(ds_name)
    if ds_id is None:
        return None, []
    return ds_id, await cognee.datasets.list_data(ds_id)


# Items in memory that /forget must never delete wholesale: files and calendar summaries.
_BULK_MARKERS = ("shared this document:", "From the calendar of", "Document:", "New items from my", "Core profile of")


async def forget_text(user_id: int | str, text: str) -> int:
    """Delete saved chat memories that mention text. Documents, calendar summaries and the
    profile document are kept (the profile is rewritten by the caller). Returns the count."""
    needle = text.strip().lower()
    if not needle:
        return 0
    if CLOUD:
        return await _cloud_forget_text(user_id, needle)
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


async def _cloud_forget_text(user_id, needle: str) -> int:
    """The cloud can't hand back item text cheaply, so match on the outbox's copy of what was
    saved, then delete the cloud items with the same content name."""
    clear_cache(user_id)
    db = _outbox_db()
    rows = [r for r in db.execute("SELECT id, text, name, sent FROM memory_outbox WHERE user_id=?", (str(user_id),))
            if needle in r[1].lower() and not any(m in r[1][:300] for m in _BULK_MARKERS)]
    if not rows:
        return 0
    removed = 0
    unsent = [r for r in rows if not r[3]]
    for r in unsent:
        db.execute("DELETE FROM memory_outbox WHERE id=?", (r[0],))
        removed += 1
    db.commit()
    names = {r[2] for r in rows if r[3]}
    if names:
        try:
            ds_id, items = await asyncio.wait_for(_cloud_items(dataset_for(user_id)), BACKGROUND_WAIT)
        except (asyncio.TimeoutError, Exception) as e:
            log.info("forget: could not list cloud memories (%s)", _safe(e))
            return removed
        for d in items:
            if Path(str(d.name)).stem not in names:
                continue
            try:
                await cognee.forget(data_id=d.id, dataset_id=ds_id)
                removed += 1
            except Exception as e:
                log.info("forget: cloud delete failed (%s)", _safe(e))
        db.executemany("DELETE FROM memory_outbox WHERE id=?", [(r[0],) for r in rows if r[3]])
        db.commit()
    return removed


async def replace_document(user_id, doc: str, old_id: str | None, node_set: list[str] | None = None) -> str | None:
    """Swap one standalone document (the profile) in the user's dataset: delete old_id, store doc,
    return the new item's id (None if it couldn't be found). Raises if the store failed, so the
    caller keeps it marked dirty and tries again later."""
    ds_name = dataset_for(user_id)
    want = upload_name(doc)
    if CLOUD:
        if not connected():
            raise RuntimeError("cloud memory not connected")
        if old_id:
            try:
                ds_id = await _cloud_dataset_id(ds_name)
                if ds_id:
                    await asyncio.wait_for(cognee.forget(data_id=uuid.UUID(old_id), dataset_id=ds_id), BACKGROUND_WAIT)
            except (asyncio.TimeoutError, Exception) as e:
                log.info("could not delete old profile document (%s)", _safe(e))
        # node_set doesn't cross to the cloud; the profile is found by its "Core profile of" header
        await asyncio.wait_for(cognee.remember([doc], dataset_name=ds_name, self_improvement=False), WRITE_WAIT)
        _ok()
        clear_cache(user_id)
        enqueue(user_id, doc, "profile", sent=_now())
        _outbox_db().execute("DELETE FROM memory_outbox WHERE user_id=? AND kind='profile' AND name!=?",
                             (str(user_id), want))
        _outbox_db().commit()
        _known.add(ds_name)
        try:
            _, items = await _cloud_items(ds_name)
            return next((str(d.id) for d in items if Path(str(d.name)).stem == want), None)
        except Exception as e:
            log.info("could not find profile document id (%s)", _safe(e))
            return None
    if old_id:
        try:
            ds = next(d for d in await cognee.datasets.list_datasets() if d.name == ds_name)
            await cognee.datasets.delete_data(ds.id, uuid.UUID(old_id))
        except Exception as e:
            log.info("could not delete old profile document (%s)", type(e).__name__)
    await cognee.add(doc, dataset_name=ds_name, node_set=node_set)
    _known.add(ds_name)
    try:
        ds = next(d for d in await cognee.datasets.list_datasets() if d.name == ds_name)
        return next((str(d.id) for d in await cognee.datasets.list_data(ds.id) if d.name == want), None)
    except Exception as e:
        log.info("could not find profile document id (%s)", type(e).__name__)
        return None


async def dataset_status(user_id) -> str | None:
    """The cloud's processing status for the user's dataset (e.g. DATASET_PROCESSING_COMPLETED)."""
    from cognee.api.v1.serve.state import get_remote_client

    client = get_remote_client()
    ds_id = await _cloud_dataset_id(dataset_for(user_id))
    if client is None or ds_id is None:
        return None
    session = await client._get_session()
    async with session.get(f"{client.service_url}/api/v1/datasets/status", params={"dataset": str(ds_id)}) as resp:
        if resp.status >= 400:
            return None
        return (await resp.json()).get(str(ds_id))


async def wait_searchable(user_id, timeout: float = 240.0, min_wait: float = 8.0) -> bool:
    """Wait until the cloud has finished building the user's latest memories. remember() returns
    before processing starts, so wait at least min_wait and need two finished readings in a row."""
    if not CLOUD:
        return True
    t = time.monotonic()
    await asyncio.sleep(min_wait)
    done_once = False
    while time.monotonic() - t < timeout:
        try:
            st = await dataset_status(user_id)
        except Exception:
            st = None
        if st == "DATASET_PROCESSING_COMPLETED":
            if done_once:
                return True
            done_once = True
        else:
            done_once = False
        await asyncio.sleep(4)
    return False


async def forget_dataset(user_id) -> bool:
    """Delete a user's whole cloud dataset (tests and cleanup only)."""
    if not CLOUD:
        return False
    ds_id = await _cloud_dataset_id(dataset_for(user_id))
    if ds_id is None:
        return False
    await cognee.forget(dataset_id=ds_id)
    _outbox_db().execute("DELETE FROM memory_outbox WHERE user_id=?", (str(user_id),))
    _outbox_db().commit()
    return True


# ---------- recall cache and prefetch ----------

CACHE_TTL = 600.0  # seconds a recall answer is reused (cleared when the user saves something new)
_cache: dict[str, dict[str, tuple[float, str]]] = {}
_generation: dict[str, int] = defaultdict(int)  # bumped on every save, so in-flight answers aren't cached stale


def _qkey(question: str) -> str:
    import re

    return " ".join(re.findall(r"[a-z0-9]+", question.lower()))


def _cached(user_id, question: str) -> str | None:
    hit = _cache.get(str(user_id), {}).get(_qkey(question))
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def clear_cache(user_id) -> None:
    _cache.pop(str(user_id), None)
    _generation[str(user_id)] += 1


def start_recall(user_id, question: str, wait: float = INTERACTIVE_WAIT) -> asyncio.Task:
    """Start a memory lookup now (in parallel with everything else); await the task when needed."""
    return asyncio.ensure_future(context_for(user_id, question, wait=wait))


# ---------- short-term buffer: what the user said in the last few minutes ----------

FRESH_MINUTES = 15


def _confirm_later(user_id, ids: list[int]) -> None:
    """Mark items searchable once the cloud has finished building them."""
    async def run():
        ok = await wait_searchable(user_id, timeout=600)
        if ok:
            db = _outbox_db()
            db.executemany("UPDATE memory_outbox SET ready=? WHERE id=?", [(_now(), i) for i in ids])
            db.commit()
            clear_cache(user_id)  # recall can see them now
            log.info("memory: %d item(s) for %s are searchable", len(ids), dataset_for(user_id))
        else:
            log.warning("memory: %s still processing after 10 min; items stay in the short-term buffer", dataset_for(user_id))

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


def _strip_anchor(text: str) -> str:
    head, _, rest = text.partition("\n")
    return rest.strip() if rest and head.rstrip().endswith(":") else text.strip()


def fresh(user_id, query: str = "", limit: int = 8) -> list[str]:
    """What the user told Violet in the last FRESH_MINUTES that the cloud can't search yet (queued or
    still being built), newest first. With a query, only items that share a word with it."""
    import re

    if not CLOUD:
        return []
    since = (datetime.now(timezone.utc) - timedelta(minutes=FRESH_MINUTES)).isoformat(timespec="seconds")
    day = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    rows = _outbox_db().execute(
        "SELECT text FROM memory_outbox WHERE user_id=? AND kind!='profile' AND ready IS NULL "
        "AND (created>=? OR created>=?) ORDER BY id DESC LIMIT 50", (str(user_id), since, day)).fetchall()
    items = [_strip_anchor(r[0]) for r in rows]
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2} - _STOP
    if words:
        items = [t for t in items if words & set(re.findall(r"[a-z0-9]+", t.lower()))]
    return [t[:600] for t in items[:limit]]


_STOP = {"the", "and", "who", "what", "whats", "what's", "when", "where", "does", "did", "about", "with", "that",
         "this", "have", "has", "are", "was", "his", "her", "she", "him", "you", "your", "for", "from", "know",
         "tell", "remember", "just", "now", "not", "any", "all", "how", "why", "which", "there", "they", "them"}


# ---------- corrections that stick ----------

PINPOINT_TASK = """The user corrected a fact. Decide which saved memory items to delete so the old fact is gone.

New fact: {new}
What it replaces: {old}

Saved items (id: text):
{items}

Return JSON: {{"remove": [ids of items that ONLY state the replaced fact, so deleting them loses nothing else],
"mixed": [ids of items that state the replaced fact but also contain other information]}}
Only list items that really state the OLD version of this fact. When unsure, leave an item out."""


async def correct(user_id, new_fact: str, replaces: str = "", wait: float = 12.0, background: bool = True) -> dict:
    """Replace a fact: find the memory items that state the old version, delete them (cloud forget, or
    drop them from the queue), then save the corrected fact. Items that mix the old fact with other
    things are kept and the fact is saved as a correction note that overrides them.

    The corrected fact is queued right away (so the short-term buffer has it), but held back from the
    cloud until the old items are gone. background=True returns without waiting for that cleanup."""
    uid = str(user_id)
    if not CLOUD:
        await save(uid, new_fact, kind="correction")
        return {"removed": 0, "note": True}
    text = anchor(uid, new_fact, "correction")
    db = _outbox_db()
    now = _now()
    cur = db.execute("INSERT INTO memory_outbox (user_id, kind, text, name, created, next_try, source) "
                     "VALUES (?,?,?,?,?,?,?)", (uid, "correction", text, upload_name(text), now, "9999", "live"))
    db.commit()
    clear_cache(uid)
    _known.add(dataset_for(uid))
    job = _finish_correction(uid, cur.lastrowid, new_fact, replaces, wait)
    if background:
        asyncio.get_running_loop().create_task(job)
        return {"removed": None, "note": None}
    return await job


async def _finish_correction(uid: str, row_id: int, new_fact: str, replaces: str, wait: float) -> dict:
    import re

    import llm

    removed, mixed = [], []
    db = _outbox_db()
    try:
        words = {w for w in re.findall(r"[a-z0-9]+", f"{replaces} {new_fact}".lower()) if len(w) > 2} - _STOP
        rows = db.execute("SELECT id, text, name, sent FROM memory_outbox WHERE user_id=? AND kind!='profile' AND id!=? "
                          "ORDER BY id DESC LIMIT 400", (uid, row_id)).fetchall()
        scored = sorted(((len(words & set(re.findall(r"[a-z0-9]+", r[1].lower()))), r) for r in rows),
                        key=lambda x: -x[0])
        cands = [r for n, r in scored if n >= 2][:25]
        if cands:
            try:
                data = await asyncio.wait_for(llm.chat_json([{"role": "user", "content": PINPOINT_TASK.format(
                    new=new_fact, old=replaces or "(the earlier version of this fact)",
                    items="\n".join(f"{r[0]}: {_strip_anchor(r[1])[:500]}" for r in cands))}]), wait)
                ids = {int(i) for i in data.get("remove") or [] if str(i).isdigit()}
                mixed = [int(i) for i in data.get("mixed") or [] if str(i).isdigit()]
                removed = [r for r in cands if r[0] in ids]
            except (asyncio.TimeoutError, Exception) as e:
                log.info("correct: could not pinpoint old items (%s)", _safe(e))
        if removed:
            await _delete_items(uid, removed)
    finally:
        note = not removed or bool(mixed)
        if not note:  # the old version is gone: store the new fact plainly
            text = anchor(uid, new_fact, "message")
            db.execute("UPDATE memory_outbox SET kind='message', text=?, name=? WHERE id=?", (text, upload_name(text), row_id))
        db.execute("UPDATE memory_outbox SET next_try=? WHERE id=?", (_now(), row_id))  # release it
        db.commit()
        clear_cache(uid)
    log.info("memory: correction for %s removed %d old item(s)%s", dataset_for(uid), len(removed),
             ", kept a correction note" if note else "")
    await flush(uid)
    return {"removed": len(removed), "note": note}


async def _delete_items(uid: str, rows: list) -> None:
    """Delete outbox rows (id, text, name, sent): unsent ones just leave the queue, sent ones are
    forgotten in the cloud by matching content name."""
    db = _outbox_db()
    sent_names = {r[2] for r in rows if r[3]}
    if sent_names and connected():
        try:
            ds_id, items = await asyncio.wait_for(_cloud_items(dataset_for(uid)), 20)
            targets = [d for d in items if Path(str(d.name)).stem in sent_names]
            await asyncio.gather(*(cognee.forget(data_id=d.id, dataset_id=ds_id) for d in targets))
        except (asyncio.TimeoutError, Exception) as e:
            log.warning("correct: cloud forget failed (%s)", _safe(e))
    db.executemany("DELETE FROM memory_outbox WHERE id=?", [(r[0],) for r in rows])
    db.commit()
    clear_cache(uid)
