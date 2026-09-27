"""Who can use Violet: users, invite codes, the waitlist, daily limits, usage, and each user's calendar
feed links (encrypted at rest). No channel code here.

Statuses: invited, active, waitlist, blocked. Roles: owner, user. Only active users get replies
(beyond one rate-limited waitlist message); only the owner gets Google features and owner commands.
Feed links are secrets: stored with Fernet (VIOLET_SECRET_KEY in .env), never logged, only ever
shown masked.
"""

import contextvars
import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

import paths

log = logging.getLogger("users")

OWNER_ID = os.getenv("VIOLET_OWNER_ID") or os.getenv("CALENDAR_USER_ID") or "1067732196"
FRIENDS = ["1482163307"]  # kept active when the users table is first created

# Per-day limits for normal users (the owner has none unless set). Override per user with set_limits().
DEFAULT_LIMITS = {"messages": 150, "voice_minutes": 20, "documents": 15, "memory_saves": 150}
LIMIT_NAMES = {"messages": "messages", "voice_minutes": "minutes of voice notes", "documents": "documents",
               "memory_saves": "new memories"}

WAITLIST_REPLY_EVERY = timedelta(hours=24)  # per stranger
WAITLIST_REPLIES_PER_HOUR = 30  # across all strangers

# The user whose message is being handled (set by the bot's gate; copied into tasks it starts),
# so usage like LLM tokens is counted against the right person.
acting: contextvars.ContextVar[str | None] = contextvars.ContextVar("acting_user", default=None)

_db = sqlite3.connect(paths.VIOLET_DB, check_same_thread=False, timeout=15)
_db.row_factory = sqlite3.Row
_db.execute("PRAGMA journal_mode=WAL")
_db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    telegram_id TEXT PRIMARY KEY,
    name TEXT,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- invited, active, waitlist, blocked
    role TEXT NOT NULL DEFAULT 'user',  -- owner, user
    limits TEXT,                   -- JSON overrides of DEFAULT_LIMITS, or NULL
    invite_code TEXT,
    last_active TEXT,
    last_waitlist_reply TEXT
);
CREATE TABLE IF NOT EXISTS invites (
    code TEXT PRIMARY KEY,
    created_by TEXT NOT NULL,
    max_uses INTEGER NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    user_id TEXT NOT NULL,
    day TEXT NOT NULL,             -- YYYY-MM-DD (UTC)
    metric TEXT NOT NULL,          -- messages, voice_minutes, documents, memory_saves, tokens, cognee_saves, cognee_recalls
    amount REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day, metric)
);
CREATE TABLE IF NOT EXISTS feeds (
    user_id TEXT NOT NULL,
    source TEXT NOT NULL,          -- brightspace, google
    secret TEXT NOT NULL,          -- the feed URL, Fernet-encrypted
    added_at TEXT NOT NULL,
    last_sync TEXT,
    last_status TEXT,              -- ok, or an error type (never the URL)
    items INTEGER,
    PRIMARY KEY (user_id, source)
);
""")
_db.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).isoformat(timespec="seconds")


# ---------- users ----------

def bootstrap() -> None:
    """Create the owner (and keep the existing friend active) on first run."""
    if not get(OWNER_ID):
        _db.execute("INSERT INTO users (telegram_id, created_at, status, role) VALUES (?,?,?,?)",
                    (OWNER_ID, _iso(), "active", "owner"))
    for uid in FRIENDS:
        if not get(uid):
            _db.execute("INSERT INTO users (telegram_id, created_at, status, role) VALUES (?,?,?,?)",
                        (uid, _iso(), "active", "user"))
    _db.commit()


def fill_names(name_of) -> int:
    """Give users without a name the one from their profile (name_of: user_id -> name or None)."""
    n = 0
    for r in _db.execute("SELECT telegram_id FROM users WHERE name IS NULL OR name = ''").fetchall():
        name = name_of(r[0])
        if name:
            _db.execute("UPDATE users SET name = ? WHERE telegram_id = ?", (name, r[0]))
            n += 1
    _db.commit()
    return n


def get(user_id) -> dict | None:
    r = _db.execute("SELECT * FROM users WHERE telegram_id = ?", (str(user_id),)).fetchone()
    return dict(r) if r else None


def is_active(user_id) -> bool:
    u = get(user_id)
    return bool(u and u["status"] == "active")


def is_owner(user_id) -> bool:
    u = get(user_id)
    return bool(u and u["role"] == "owner" and u["status"] == "active")


def active_ids() -> list[str]:
    """Everyone background jobs work for."""
    return [r[0] for r in _db.execute("SELECT telegram_id FROM users WHERE status = 'active' ORDER BY created_at")]


def touch(user_id, name: str | None = None) -> None:
    _db.execute("UPDATE users SET last_active = ?, name = COALESCE(?, name) WHERE telegram_id = ?",
                (_iso(), name, str(user_id)))
    _db.commit()


def add(user_id, status: str = "active", role: str = "user", name: str | None = None) -> None:
    _db.execute("INSERT OR IGNORE INTO users (telegram_id, name, created_at, status, role) VALUES (?,?,?,?,?)",
                (str(user_id), name, _iso(), status, role))
    _db.commit()


def set_status(user_id, status: str) -> bool:
    """block / unblock. The owner can't be blocked."""
    if str(user_id) == OWNER_ID:
        return False
    cur = _db.execute("UPDATE users SET status = ? WHERE telegram_id = ?", (status, str(user_id)))
    _db.commit()
    return cur.rowcount > 0


def listing() -> list[dict]:
    return [dict(r) for r in _db.execute(
        "SELECT telegram_id, name, status, role, created_at, last_active FROM users "
        "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'invited' THEN 1 WHEN 'waitlist' THEN 2 ELSE 3 END, "
        "COALESCE(last_active, created_at) DESC")]


# ---------- invites and the waitlist ----------

def create_invite(created_by, max_uses: int = 1, days: int = 7) -> dict:
    code = "violet-" + secrets.token_urlsafe(6).replace("_", "x").replace("-", "y").lower()
    expires = _now() + timedelta(days=days)
    _db.execute("INSERT INTO invites (code, created_by, max_uses, expires_at, created_at) VALUES (?,?,?,?,?)",
                (code, str(created_by), max(1, max_uses), _iso(expires), _iso()))
    _db.commit()
    return {"code": code, "max_uses": max(1, max_uses), "expires_at": expires}


def redeem(code: str, user_id, name: str | None = None) -> tuple[bool, str]:
    """(joined, reason). reason: joined, already, blocked, invalid, expired, used_up."""
    u = get(user_id)
    if u and u["status"] == "blocked":
        return False, "blocked"
    if u and u["status"] == "active":
        return True, "already"
    inv = _db.execute("SELECT * FROM invites WHERE code = ?", ((code or "").strip().lower(),)).fetchone()
    if inv is None:
        return False, "invalid"
    if datetime.fromisoformat(inv["expires_at"]) < _now():
        return False, "expired"
    if inv["uses"] >= inv["max_uses"]:
        return False, "used_up"
    _db.execute("UPDATE invites SET uses = uses + 1 WHERE code = ?", (inv["code"],))
    if u:
        _db.execute("UPDATE users SET status = 'active', invite_code = ?, name = COALESCE(?, name) WHERE telegram_id = ?",
                    (inv["code"], name, str(user_id)))
    else:
        _db.execute("INSERT INTO users (telegram_id, name, created_at, status, role, invite_code) VALUES (?,?,?,?,?,?)",
                    (str(user_id), name, _iso(), "active", "user", inv["code"]))
    _db.commit()
    return True, "joined"


def waitlist(user_id, name: str | None = None) -> bool:
    """Record a stranger on the waitlist (nothing about their message). True if they should get the
    one friendly reply now (at most once a day each, and a global cap per hour)."""
    uid = str(user_id)
    u = get(uid)
    if u is None:
        _db.execute("INSERT INTO users (telegram_id, name, created_at, status, role) VALUES (?,?,?,?,?)",
                    (uid, name, _iso(), "waitlist", "user"))
        u = get(uid)
    if u["status"] == "blocked":
        return False
    last = u["last_waitlist_reply"]
    if last and _now() - datetime.fromisoformat(last) < WAITLIST_REPLY_EVERY:
        return False
    hour_ago = _iso(_now() - timedelta(hours=1))
    recent = _db.execute("SELECT COUNT(*) FROM users WHERE last_waitlist_reply >= ?", (hour_ago,)).fetchone()[0]
    if recent >= WAITLIST_REPLIES_PER_HOUR:
        return False
    _db.execute("UPDATE users SET last_waitlist_reply = ? WHERE telegram_id = ?", (_iso(), uid))
    _db.commit()
    return True


# ---------- limits and usage ----------

class LimitReached(Exception):
    def __init__(self, metric: str, limit: float):
        self.metric, self.limit = metric, limit
        super().__init__(f"You've hit today's limit of {limit:g} {LIMIT_NAMES.get(metric, metric)}. "
                         "It resets tomorrow (UTC).")


def _day() -> str:
    return _now().date().isoformat()


def limits(user_id) -> dict:
    u = get(user_id) or {}
    base = {} if u.get("role") == "owner" else dict(DEFAULT_LIMITS)
    base.update(json.loads(u.get("limits") or "{}"))
    return base


def set_limits(user_id, **values) -> None:
    u = get(user_id)
    cur = json.loads((u or {}).get("limits") or "{}")
    cur.update(values)
    _db.execute("UPDATE users SET limits = ? WHERE telegram_id = ?", (json.dumps(cur), str(user_id)))
    _db.commit()


def used(user_id, metric: str, day: str | None = None) -> float:
    r = _db.execute("SELECT amount FROM usage WHERE user_id = ? AND day = ? AND metric = ?",
                    (str(user_id), day or _day(), metric)).fetchone()
    return r[0] if r else 0.0


def record(user_id, metric: str, amount: float = 1) -> None:
    if not user_id or not amount:
        return
    _db.execute("INSERT INTO usage (user_id, day, metric, amount) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id, day, metric) DO UPDATE SET amount = amount + excluded.amount",
                (str(user_id), _day(), metric, amount))
    _db.commit()


def check(user_id, metric: str, amount: float = 1) -> None:
    """Raise LimitReached if this would go over the user's daily limit."""
    lim = limits(user_id).get(metric)
    if lim is not None and used(user_id, metric) + amount > lim:
        raise LimitReached(metric, lim)


def spend(user_id, metric: str, amount: float = 1) -> None:
    """check() then record()."""
    check(user_id, metric, amount)
    record(user_id, metric, amount)


def usage_report(day: str | None = None) -> list[dict]:
    rows = _db.execute("SELECT user_id, metric, amount FROM usage WHERE day = ? ORDER BY user_id", (day or _day(),))
    out: dict[str, dict] = {}
    for uid, metric, amount in rows:
        out.setdefault(uid, {"user_id": uid, "name": (get(uid) or {}).get("name")})[metric] = amount
    return list(out.values())


# ---------- calendar feed links (secrets) ----------

def _fernet():
    from cryptography.fernet import Fernet

    key = os.getenv("VIOLET_SECRET_KEY")
    if not key:
        raise RuntimeError("VIOLET_SECRET_KEY is not set in .env")
    return Fernet(key.encode())


def set_feed(user_id, source: str, url: str, items: int | None = None) -> None:
    token = _fernet().encrypt(url.strip().encode()).decode()
    _db.execute("INSERT INTO feeds (user_id, source, secret, added_at, last_status, items) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(user_id, source) DO UPDATE SET secret = excluded.secret, added_at = excluded.added_at, "
                "last_status = excluded.last_status, items = excluded.items",
                (str(user_id), source, token, _iso(), "ok" if items is not None else None, items))
    _db.commit()


def get_feed(user_id, source: str) -> str | None:
    r = _db.execute("SELECT secret FROM feeds WHERE user_id = ? AND source = ?", (str(user_id), source)).fetchone()
    if not r:
        return None
    try:
        return _fernet().decrypt(r[0].encode()).decode()
    except Exception as e:
        log.warning("could not decrypt the %s feed for a user (%s)", source, type(e).__name__)
        return None


def feeds(user_id) -> list[dict]:
    """The user's feeds without the links: source, last sync, status, item count."""
    return [dict(r) for r in _db.execute(
        "SELECT source, added_at, last_sync, last_status, items FROM feeds WHERE user_id = ?", (str(user_id),))]


def mark_feed(user_id, source: str, status: str, items: int | None = None) -> None:
    _db.execute("UPDATE feeds SET last_sync = ?, last_status = ?, items = COALESCE(?, items) WHERE user_id = ? AND source = ?",
                (_iso(), status, items, str(user_id), source))
    _db.commit()


def remove_feed(user_id, source: str) -> bool:
    cur = _db.execute("DELETE FROM feeds WHERE user_id = ? AND source = ?", (str(user_id), source))
    _db.commit()
    return cur.rowcount > 0


def mask(url: str) -> str:
    """Enough to recognise a link, never enough to use it: host plus the last 4 characters."""
    from urllib.parse import urlparse

    host = urlparse(url).netloc or "link"
    return f"{host}/…{url.strip()[-4:]}"
