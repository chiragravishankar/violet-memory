"""NYU knowledge base: a polite crawl of public NYU pages, shared by every user. No channel code here.

The crawl list is config/nyu_sources.json (edit freely). For each source: start URLs, the URL prefixes the
crawler may follow, a page cap. It respects robots.txt (and its Crawl-delay), identifies itself, waits
between requests, goes at most max_depth links deep, and only reads HTML.

Each page's cleaned text is kept twice:
- nyu_pages + nyu_fts (SQLite full-text index) for fast lookups with exact source links, and
- the shared Cognee Cloud dataset "nyu_knowledge" (never a user's dataset), with source URL and fetch date
  in every document, re-uploaded (old version removed) when a page changes.
Refreshed weekly by the bot.
"""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import lxml.html

import brain
import paths

log = logging.getLogger("nyu_kb")

CONFIG = paths.ROOT / "config" / "nyu_sources.json"
DATASET = "nyu_knowledge"
UA = "VioletNYUHelper/1.0 (NYU student assistant; polite crawler of public pages)"
MAX_TEXT = 12000  # characters kept per page
UPLOAD_TEXT = 6000  # characters per page sent to Cognee

_db = sqlite3.connect(paths.VIOLET_DB, check_same_thread=False, timeout=15)
_db.row_factory = sqlite3.Row
_db.executescript("""
CREATE TABLE IF NOT EXISTS nyu_pages (
    url TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT,
    text TEXT NOT NULL,
    hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    uploaded_name TEXT,            -- content name of the version in Cognee (to replace it later)
    uploaded_hash TEXT,            -- hash of the text that was uploaded (re-upload only when it changes)
    uploaded_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS nyu_fts USING fts5(url UNINDEXED, title, text, tokenize='porter');
CREATE TABLE IF NOT EXISTS nyu_crawls (
    id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, pages INTEGER, changed INTEGER, report TEXT
);
""")
_db.commit()


def config() -> dict:
    return json.loads(CONFIG.read_text())


# ---------- fetching ----------

class _Polite:
    """robots.txt per host, and one request per host every `delay` seconds (or robots Crawl-delay)."""

    def __init__(self, client: httpx.AsyncClient, delay: float):
        self.client, self.delay = client, delay
        self.robots: dict[str, RobotFileParser | None] = {}
        self.last: dict[str, float] = {}

    async def _robots(self, host: str) -> RobotFileParser | None:
        if host not in self.robots:
            rp = RobotFileParser()
            try:
                r = await self.client.get(f"https://{host}/robots.txt")
                if r.status_code in (401, 403):
                    rp.disallow_all = True  # the site refuses us: crawl nothing there (standard robots rule)
                    log.info("nyu crawl: %s refuses this crawler (HTTP %d); skipping the host", host, r.status_code)
                else:
                    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self.robots[host] = rp
        return self.robots[host]

    async def get(self, url: str) -> httpx.Response | None:
        host = urlparse(url).netloc
        rp = await self._robots(host)
        if rp and not rp.can_fetch(UA, url):
            return None
        delay = max(self.delay, float((rp.crawl_delay(UA) if rp else None) or 0))
        wait = delay - (time.monotonic() - self.last.get(host, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await self.client.get(url)
        except httpx.HTTPError:
            return None
        finally:
            self.last[host] = time.monotonic()


_DROP = "//script|//style|//noscript|//nav|//header|//footer|//aside|//form|//*[@role='navigation']|//*[contains(@class,'breadcrumb')]"


def extract(html_text: str, url: str) -> tuple[str, str, list[str]]:
    """(title, clean text, links) from a page."""
    doc = lxml.html.fromstring(html_text)
    title = (doc.findtext(".//title") or "").strip()
    links = []
    for a in doc.xpath("//a[@href]"):
        href = urldefrag(urljoin(url, a.get("href")))[0]
        if href.startswith("http"):
            links.append(href.split("?")[0])
    for el in doc.xpath(_DROP):
        el.drop_tree()
    main = doc.xpath("//main") or doc.xpath("//*[@role='main']") or doc.xpath("//*[@id='content' or @id='main-content']") or [doc]
    text = main[0].text_content()
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    return title, text[:MAX_TEXT], links


async def crawl(only: list[str] | None = None, upload: bool = True) -> dict:
    """Crawl every source (or only the named ones). Returns a per-source report."""
    cfg = config()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report, changed_urls = {}, []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as client:
        polite = _Polite(client, float(cfg.get("delay_seconds", 2.0)))
        for src in cfg["sources"]:
            if only and src["name"] not in only:
                continue
            seen, queue, pages, changed, errors = set(), [(u, 0) for u in src["start"]], 0, 0, 0
            while queue and pages < src.get("max_pages", 30):
                url, depth = queue.pop(0)
                if url in seen:
                    continue
                seen.add(url)
                r = await polite.get(url)
                if r is None or r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                    errors += r is not None and r.status_code != 200
                    continue
                final = str(r.url).split("?")[0]
                try:
                    title, text, links = extract(r.text, final)
                except Exception:
                    continue
                if len(text) >= 200:
                    pages += 1
                    if _store(src["name"], final, title, text):
                        changed += 1
                        changed_urls.append(final)
                if depth < int(cfg.get("max_depth", 3)):
                    for link in links:
                        if link not in seen and any(link.startswith(p) for p in src["allow"]) \
                                and not re.search(r"\.(pdf|jpe?g|png|gif|docx?|xlsx?|zip|mp4)$", link, re.I):
                            queue.append((link, depth + 1))
            report[src["name"]] = {"pages": pages, "changed": changed, "http_errors": errors}
            log.info("nyu crawl %s: %d pages, %d new or changed", src["name"], pages, changed)
    uploaded = await upload_changed() if upload else 0
    total = sum(v["pages"] for v in report.values())
    _db.execute("INSERT INTO nyu_crawls (started_at, finished_at, pages, changed, report) VALUES (?,?,?,?,?)",
                (started, datetime.now(timezone.utc).isoformat(timespec="seconds"), total, len(changed_urls),
                 json.dumps({**report, "_uploaded": uploaded})))
    _db.commit()
    return report


def _store(source: str, url: str, title: str, text: str) -> bool:
    """Save a page; True if it's new or its text changed (so Cognee needs the new version)."""
    h = hashlib.sha256(text.encode()).hexdigest()
    old = _db.execute("SELECT hash FROM nyu_pages WHERE url = ?", (url,)).fetchone()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if old and old["hash"] == h:
        _db.execute("UPDATE nyu_pages SET fetched_at = ? WHERE url = ?", (now, url))
        _db.commit()
        return False
    _db.execute("INSERT INTO nyu_pages (url, source, title, text, hash, fetched_at) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET source=excluded.source, title=excluded.title, text=excluded.text, "
                "hash=excluded.hash, fetched_at=excluded.fetched_at", (url, source, title, text, h, now))
    _db.execute("DELETE FROM nyu_fts WHERE url = ?", (url,))
    _db.execute("INSERT INTO nyu_fts (url, title, text) VALUES (?,?,?)", (url, title, text))
    _db.commit()
    return True


def document(row) -> str:
    """How a page is stored in Cognee: source and date first, so answers can cite them."""
    return (f"NYU page: {row['title']}\nSource: {row['url']}\nFetched: {row['fetched_at'][:10]}\n\n"
            f"{row['text'][:UPLOAD_TEXT]}")


async def upload_changed(batch: int = 10) -> int:
    """Send new or changed pages to the shared Cognee dataset, replacing their old versions."""
    rows = _db.execute("SELECT * FROM nyu_pages WHERE uploaded_hash IS NULL OR uploaded_hash != hash").fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        part = rows[i:i + batch]
        docs = [document(r) for r in part]
        try:
            await brain.shared_put(DATASET, docs, replace_names=[r["uploaded_name"] for r in part if r["uploaded_name"]])
        except Exception as e:
            log.warning("nyu upload batch failed (%s); will retry next refresh", brain._safe(e))
            continue
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _db.executemany("UPDATE nyu_pages SET uploaded_name = ?, uploaded_hash = ?, uploaded_at = ? WHERE url = ?",
                        [(brain.upload_name(d), r["hash"], now, r["url"]) for d, r in zip(docs, part)])
        _db.commit()
        done += len(part)
    log.info("nyu knowledge: uploaded %d page(s) to %s", done, DATASET)
    return done


def due() -> bool:
    r = _db.execute("SELECT MAX(finished_at) FROM nyu_crawls").fetchone()[0]
    days = int(config().get("refresh_days", 7))
    return not r or datetime.now(timezone.utc) - datetime.fromisoformat(r) > timedelta(days=days)


def stats() -> dict:
    n = _db.execute("SELECT COUNT(*), MIN(fetched_at), MAX(fetched_at), SUM(uploaded_at IS NOT NULL) FROM nyu_pages").fetchone()
    return {"pages": n[0], "oldest": n[1], "newest": n[2], "in_cognee": n[3] or 0}


# ---------- lookup ----------

_STOP = {"the", "and", "for", "what", "whats", "when", "where", "who", "how", "does", "can", "with", "this", "that",
         "nyu", "my", "do", "is", "are", "a", "an", "to", "of", "in", "on", "at", "i", "me", "about", "get", "there"}


def search(query: str, k: int = 5) -> list[dict]:
    """Best matching pages (full-text, BM25), with an excerpt around the matching words."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in _STOP and len(w) > 1]
    if not words:
        return []
    q = " OR ".join(f'"{w}"' for w in words)
    rows = _db.execute("SELECT p.url, p.title, p.text, p.fetched_at, p.source FROM nyu_fts f JOIN nyu_pages p ON p.url = f.url "
                       "WHERE nyu_fts MATCH ? ORDER BY bm25(nyu_fts, 0, 5.0, 1.0) LIMIT ?", (q, k)).fetchall()
    out = []
    for r in rows:
        text = r["text"]
        low = text.lower()
        pos = min((low.find(w) for w in words if low.find(w) >= 0), default=0)
        start = max(0, pos - 500)
        out.append({"url": r["url"], "title": r["title"], "fetched": r["fetched_at"][:10], "source": r["source"],
                    "excerpt": text[start:start + 1800]})
    return out
