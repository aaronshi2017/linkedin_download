"""Full-text search index over everything in the vault (SQLite FTS5, BM25 ranking).

Each saved item is split into chunks: post text/summary, every document page, video transcripts,
YouTube titles and the text of downloaded PDFs (papers, original documents)."""
import json
import logging
import re
import threading
from pathlib import Path

from . import config, db

log = logging.getLogger("vault.index")
_lock = threading.Lock()
CHUNK = 1400  # characters per chunk

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    text, title UNINDEXED, item_id UNINDEXED, kind UNINDEXED, ref UNINDEXED,
    tokenize = 'porter unicode61'
);
"""


def _split(text: str, size: int = CHUNK):
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        cut = text.rfind(". ", start + size // 2, end)          # prefer sentence boundaries
        end = cut + 1 if cut > 0 and end < len(text) else end
        out.append(text[start:end].strip())
        start = end
    return [c for c in out if c]


def pdf_text(path: Path, max_pages: int = 40) -> list:
    """[(page_no, text)] from a PDF; empty list if unreadable."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return [(i + 1, (p.extract_text() or "").strip()) for i, p in enumerate(reader.pages[:max_pages])]
    except Exception as e:
        log.info("pdf text %s: %s", path.name, e)
        return []


def chunks_for(meta: dict) -> list:
    """(kind, ref, text) tuples for one item."""
    folder = Path(meta.get("folder") or "")
    out = []
    head = "\n".join(x for x in [meta.get("title"), meta.get("author"), meta.get("summary"),
                                 " ".join(meta.get("tags") or [])] if x)
    for i, c in enumerate(_split(head + "\n" + (meta.get("text") or ""))):
        out.append(("post", f"part {i + 1}", c))
    doc = meta.get("document") or {}
    for n, t in doc.get("text_pages") or []:
        for c in _split(t):
            out.append(("document", f"page {n}", c))
    for i, v in enumerate(meta.get("videos") or [], 1):
        for c in _split(v.get("transcript") or ""):
            out.append(("video", f"video {i} transcript", c))
    for y in meta.get("youtube") or []:
        if y.get("title"):
            out.append(("youtube", y.get("url", ""), f"{y['title']} {y.get('channel', '')}"))
    # text inside downloaded PDFs (papers; original documents if their page text wasn't captured)
    pdfs = [(p.get("title") or p.get("id"), p["file"]) for p in meta.get("papers") or [] if p.get("file")]
    if doc.get("original") and doc.get("file") and not doc.get("text_pages"):
        pdfs.append((doc.get("title") or "document", doc["file"]))
    for label, name in pdfs:
        for page, t in pdf_text(folder / name):
            for c in _split(t):
                out.append(("pdf", f"{label} p.{page}", c))
    return out


def init() -> None:
    with db.connect() as con:
        con.executescript(SCHEMA)


def remove(item_id: int) -> None:
    with _lock, db.connect() as con:
        con.execute("DELETE FROM chunks WHERE item_id = ?", (item_id,))


def index_meta(meta: dict) -> int:
    rows = chunks_for(meta)
    with _lock, db.connect() as con:
        con.execute("DELETE FROM chunks WHERE item_id = ?", (meta["id"],))
        con.executemany("INSERT INTO chunks(text, title, item_id, kind, ref) VALUES (?,?,?,?,?)",
                        [(t, meta.get("title") or "", meta["id"], k, r) for k, r, t in rows])
    return len(rows)


def reindex_all() -> int:
    """Rebuild from every meta.json on the drive (runs at startup if the index is empty)."""
    n = 0
    for r in db.recent(100000):
        f = Path(r["folder"] or "") / "meta.json"
        if r["status"] in ("done", "link_only") and f.exists():
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                meta["id"] = r["id"]
                n += index_meta(meta)
            except Exception as e:
                log.warning("reindex item %s: %s", r["id"], e)
    log.info("index rebuilt: %s chunks", n)
    return n


def ensure_built() -> None:
    init()
    with db.connect() as con:
        empty = con.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        has_items = con.execute("SELECT count(*) FROM items WHERE status IN ('done','link_only')").fetchone()[0] > 0
    if empty and has_items:
        threading.Thread(target=reindex_all, daemon=True, name="reindex").start()


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[\w\-\.]+", q.lower()) if len(w) > 1]
    words = [w.strip(".-") for w in words if w.strip(".-")]
    return " OR ".join(f'"{w}"' for w in words[:24])


def search(q: str, limit: int = 8) -> list:
    """Best-matching chunks: [{item_id, title, kind, ref, snippet, score}]"""
    fq = _fts_query(q)
    if not fq:
        return []
    with db.connect() as con:
        rows = con.execute(
            "SELECT item_id, title, kind, ref, snippet(chunks, 0, '**', '**', ' … ', 40) AS snip, "
            "bm25(chunks) AS score, text FROM chunks WHERE chunks MATCH ? ORDER BY score LIMIT ?",
            (fq, limit)).fetchall()
    return [{"item_id": r["item_id"], "title": r["title"], "kind": r["kind"], "ref": r["ref"],
             "snippet": r["snip"], "text": r["text"], "score": round(-r["score"], 2)} for r in rows]
