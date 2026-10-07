"""SQLite job queue + index. Lives on the external HDD so it survives reboots."""
import hashlib
import json
import re
import sqlite3
import threading
import time
from typing import Optional

from . import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT UNIQUE NOT NULL,
    url         TEXT,
    source      TEXT,
    payload     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|done|link_only|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try_at REAL NOT NULL DEFAULT 0,
    last_error  TEXT,
    folder      TEXT,
    title       TEXT,
    author      TEXT,
    summary     TEXT,
    tags        TEXT,
    emailed     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_status ON items(status, next_try_at);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

URN_RE = re.compile(r"urn:li:(?:activity|share|ugcPost):(\d{10,})")
ACT_RE = re.compile(r"(?:activity|share|ugcpost)[-:](\d{15,})", re.I)   # /posts/...-activity-123 or ...-share-123
URL_RE = re.compile(r"https?://[^\s<>\"']+")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as con:
        con.executescript(SCHEMA)
        # jobs interrupted by a reboot go back to the queue
        con.execute("UPDATE items SET status='pending' WHERE status='processing'")
        # v0.9 bug: feed/update post links were dropped — rebuild them from the stored activity id
        con.execute("""UPDATE items SET url='https://www.linkedin.com/feed/update/urn:li:activity:' || substr(key, 4) || '/'
                       WHERE url IS NULL AND key LIKE 'li:%'""")


def first_url(text: str) -> Optional[str]:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,;") if m else None


POST_PATH = re.compile(r"linkedin\.com/(posts|feed/update|pulse)/|lnkd\.in/", re.I)


class _GenericLI:
    """A LinkedIn address that is NOT a specific post (the feed, a profile, search…)."""
    @staticmethod
    def search(url):
        return bool(url) and "linkedin.com" in url and not POST_PATH.search(url)


GENERIC_LI = _GenericLI()


def make_key(url: Optional[str], text: Optional[str] = None, extra: str = "") -> str:
    """Stable de-dup key: LinkedIn activity id if we can find one, else normalized post URL, else content hash.
    Generic LinkedIn pages (the feed, a profile…) are never used as a key — many posts share them."""
    for src in (url or "", text or ""):
        m = URN_RE.search(src) or ACT_RE.search(src)
        if m:
            return f"li:{m.group(1)}"
    if url and not GENERIC_LI.search(url):
        return "url:" + url.split("?")[0].split("#")[0].rstrip("/").lower()
    blob = ((text or "")[:1000] + "|" + extra) if (text or extra) else (url or "")
    return "txt:" + hashlib.sha1(blob.encode()).hexdigest()[:16]


def _has_original(folder) -> bool:
    """True if the saved item already holds a real document file (not one rebuilt from page pictures)."""
    try:
        from pathlib import Path
        m = json.loads((Path(folder) / "meta.json").read_text(encoding="utf-8"))
        return bool((m.get("document") or {}).get("original")) or (bool(m.get("documents")) and not m.get("document"))
    except Exception:
        return False


def enqueue(key: str, url: Optional[str], source: str, payload: dict) -> dict:
    now = time.time()
    with _lock, connect() as con:
        row = con.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()
        if row is None:
            cur = con.execute(
                "INSERT INTO items(key,url,source,payload,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                (key, url, source, json.dumps(payload), now, now),
            )
            return {"id": cur.lastrowid, "status": "queued", "duplicate": False}
        # Already known. A richer capture (from the extension) upgrades a link-only item.
        old = json.loads(row["payload"] or "{}")
        richer = bool(payload.get("text")) and not old.get("text")
        # a re-save that brings the original PDF (or a video) the stored copy lacks also upgrades it
        if not richer and row["status"] in ("done", "link_only"):
            richer = (bool(payload.get("documents")) and not _has_original(row["folder"])) or \
                     (bool(payload.get("videos")) and not old.get("videos"))
        if richer or payload.get("force"):
            con.execute(
                "UPDATE items SET payload=?, status='pending', attempts=0, next_try_at=0, emailed=0, updated_at=? WHERE id=?",
                (json.dumps({**old, **payload}), now, row["id"]),
            )
            return {"id": row["id"], "status": "requeued", "duplicate": True}
        return {"id": row["id"], "status": row["status"], "duplicate": True, "title": row["title"]}


def find_by_id(item_id: int):
    with connect() as con:
        return con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def find_by_key(key: str):
    with connect() as con:
        return con.execute("SELECT * FROM items WHERE key=?", (key,)).fetchone()


def claim_next() -> Optional[sqlite3.Row]:
    with _lock, connect() as con:
        row = con.execute(
            "SELECT * FROM items WHERE status='pending' AND next_try_at<=? ORDER BY id LIMIT 1",
            (time.time(),),
        ).fetchone()
        if row:
            con.execute("UPDATE items SET status='processing', updated_at=? WHERE id=?", (time.time(), row["id"]))
        return row


def update(item_id: int, **fields) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock, connect() as con:
        con.execute(f"UPDATE items SET {cols} WHERE id=?", (*fields.values(), item_id))


def fail(item_id: int, attempts: int, err: str) -> None:
    attempts += 1
    if attempts >= config.MAX_ATTEMPTS:
        update(item_id, status="failed", attempts=attempts, last_error=err[:2000])
    else:
        backoff = 60 * (2 ** attempts)  # 2, 4, 8, 16 min
        update(item_id, status="pending", attempts=attempts, last_error=err[:2000], next_try_at=time.time() + backoff)


def stats() -> dict:
    with connect() as con:
        return {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM items GROUP BY status")}


def recent(limit: int = 50) -> list:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM items ORDER BY id DESC LIMIT ?", (limit,))]


def get_setting(key: str, default=None):
    with connect() as con:
        r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default


def set_setting(key: str, value: str) -> None:
    with _lock, connect() as con:
        con.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
