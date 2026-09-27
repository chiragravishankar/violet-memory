"""The delivery log: everything Violet sends on its own is claimed here before it goes out, so nothing is
ever sent twice, even across restarts or when the underlying item is re-created (e.g. by a feed sync).
No channel code here.

Kinds: reminder, brief, weekly_preview, event_picks, checkin, followup, reflection, and *_demo for
/demo_time test fires (kept separate so a test never blocks a real reminder).
"""

import sqlite3
from datetime import datetime, timezone

import paths

_db = sqlite3.connect(paths.VIOLET_DB, check_same_thread=False, timeout=15)
_db.execute("PRAGMA journal_mode=WAL")
_db.execute("""CREATE TABLE IF NOT EXISTS deliveries (
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,          -- stable: survives restarts and re-created items
    at TEXT NOT NULL,           -- when it was claimed (just before sending)
    PRIMARY KEY (user_id, kind, key)
)""")
_db.commit()


def claim(user_id, kind: str, key: str) -> bool:
    """True if this delivery is new (and now recorded); False if it already went out. Call before sending."""
    cur = _db.execute("INSERT OR IGNORE INTO deliveries (user_id, kind, key, at) VALUES (?, ?, ?, ?)",
                      (str(user_id), kind, key, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    _db.commit()
    return cur.rowcount == 1


def was_sent(user_id, kind: str, key: str) -> bool:
    return bool(_db.execute("SELECT 1 FROM deliveries WHERE user_id = ? AND kind = ? AND key = ?",
                            (str(user_id), kind, key)).fetchone())


def history(user_id=None, limit: int = 20) -> list[tuple]:
    q = "SELECT user_id, kind, key, at FROM deliveries" + (" WHERE user_id = ?" if user_id else "") + " ORDER BY at DESC LIMIT ?"
    return _db.execute(q, ((str(user_id), limit) if user_id else (limit,))).fetchall()


def reminder_key(uid: str | None, title: str, remind_at: str, lead: str) -> str:
    """A reminder is identified by what it's about and when it fires, not by row ids (which change)."""
    return f"{uid or title}|{remind_at[:16]}|{lead}"
