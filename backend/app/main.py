"""Capture API. Receives posts from the Chrome extension and links from the iPhone/iPad Shortcut."""
import datetime as dt
import hmac
import html
import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import agent, config, db, digest, index, keywords, mailer, worker

DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("vault")
logging.getLogger("fontTools").setLevel(logging.WARNING)  # PDF font subsetting chatter

QUIET_PATHS = ("/api/items", "/files/", "/dashboard", "/health", "/api/digests", "/favicon", "/items")


class QuietAccess(logging.Filter):
    """Hide successful routine GETs (dashboard polling, file views); keep saves, deletes, asks and all errors."""
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            _client, method, path, _ver, status = record.args
            status = int(status)
        except Exception:
            return True
        if status >= 400 or method != "GET":
            return True
        return not str(path).startswith(QUIET_PATHS)


if config.LOG_ACCESS != "all":
    logging.getLogger("uvicorn.access").addFilter(QuietAccess())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.REQUIRE_MARKER and not config.MARKER.exists():
        # External HDD not mounted → refuse to start (Docker restarts us until it is),
        # so we never silently write the vault onto the SD card.
        log.error("Marker %s missing — is the external drive mounted? Exiting.", config.MARKER)
        sys.exit(3)
    if not config.API_TOKEN:
        log.error("VAULT_TOKEN is not set. Exiting.")
        sys.exit(3)
    db.init()
    index.ensure_built()
    stop = worker.start()
    digest.start(stop)
    log.info("vault ready · data=%s · email=%s · ai=%s", config.DATA_DIR, mailer.enabled(), bool(config.ANTHROPIC_API_KEY))
    yield
    stop.set()


app = FastAPI(title="LinkedIn Vault", version="0.19.0", lifespan=lifespan)


def _ok(supplied: str) -> bool:
    return bool(config.API_TOKEN) and hmac.compare_digest(supplied or "", config.API_TOKEN)


def check_token(x_vault_token: Optional[str] = Header(None), authorization: Optional[str] = Header(None),
                token: Optional[str] = Query(None), vault_token: Optional[str] = Cookie(None)):
    supplied = x_vault_token or (authorization or "").removeprefix("Bearer ").strip() or token or vault_token or ""
    if not _ok(supplied):
        raise HTTPException(401, "bad token")


class Capture(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = ""
    author: Optional[str] = ""
    author_headline: Optional[str] = ""
    posted: Optional[str] = ""
    images: List[str] = Field(default_factory=list)
    documents: List[str] = Field(default_factory=list)
    links: List[str] = Field(default_factory=list)
    source: str = "extension"
    force: bool = False
    debug_html: Optional[str] = None
    doc_title: str = ""
    doc_total: int = 0
    doc_pages: List[dict] = Field(default_factory=list)
    videos: List[dict] = Field(default_factory=list)


class LinkIn(BaseModel):
    url: str            # may be a bare URL or share text like "Check out this post: https://..."
    source: str = "ios"
    force: bool = False


@app.get("/health")
def health():
    return {"ok": True, "hdd_mounted": config.MARKER.exists(), "email": mailer.enabled(),
            "ai": bool(config.ANTHROPIC_API_KEY), "queue": db.stats()}


def _short(url):
    url = re.sub(r"^https?://(www\.)?", "", url or "")
    return url[:80] + ("…" if len(url) > 80 else "")


@app.post("/capture", dependencies=[Depends(check_token)])
def capture(c: Capture):
    if not c.url and not c.text:
        raise HTTPException(400, "need url or text")
    key = db.make_key(c.url, c.text, extra="|".join(c.images[:2]) + "|" + (c.author or ""))
    data = c.model_dump(exclude={"debug_html"})
    if db.GENERIC_LI.search(c.url or ""):
        data["url"] = None  # a feed URL is not the post's address
    res = db.enqueue(key, data["url"], c.source, data)
    if c.debug_html:  # keep the snapshot so selectors can be fixed later
        dbg = config.DATA_DIR / "debug"
        dbg.mkdir(exist_ok=True)
        (dbg / f"item_{res['id']:05d}.html").write_text(c.debug_html, encoding="utf-8")
        log.info("capture %s: saved layout snapshot debug/item_%05d.html", key, res["id"])
    log.info("capture received: #%s from %s — %s (%s)", res["id"], c.source or "extension",
             _short(c.url) or "text only", "duplicate, " + res["status"] if res.get("duplicate") else "queued")
    return res


@app.post("/link", dependencies=[Depends(check_token)])
def link(l: LinkIn):
    url = db.first_url(l.url) or l.url.strip()
    if not url.startswith("http"):
        raise HTTPException(400, "no URL found")
    key = db.make_key(url)
    res = db.enqueue(key, url, l.source, {"url": url, "force": l.force})
    log.info("link received: #%s from %s — %s (%s)", res["id"], l.source or "shortcut", _short(url),
             "duplicate, " + res["status"] if res.get("duplicate") else "queued")
    msg = "Already saved" if res["duplicate"] and res["status"] in ("done", "link_only", "duplicate") else "Saved to Vault ✓"
    return {**res, "message": msg}


@app.get("/items", response_class=HTMLResponse, dependencies=[Depends(check_token)])
def items(limit: int = 100):
    e = html.escape
    rows = "".join(
        f"<tr><td>{r['id']}</td><td>{e(r['status'])}</td><td><a href='{e(r['url'] or '')}'>{e(r['title'] or r['url'] or '')}</a>"
        f"<div class=s>{e(r['summary'] or '')}</div></td><td>{e(r['author'] or '')}</td><td>{e(r['tags'] or '')}</td></tr>"
        for r in db.recent(limit))
    return f"""<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Vault</title><style>body{{font-family:system-ui;margin:16px}}td{{vertical-align:top;padding:6px;border-bottom:1px solid #ddd}}
.s{{white-space:pre-wrap;color:#555;font-size:13px}}</style><h2>LinkedIn Vault</h2>
<table><tr><th>#</th><th>Status</th><th>Post</th><th>Author</th><th>Tags</th></tr>{rows}</table>"""


# ------------------------------------------------------------------ dashboard
@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/dashboard", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(token: Optional[str] = Query(None), vault_token: Optional[str] = Cookie(None)):
    if token:  # first visit: /dashboard?token=... → remember in a cookie, then drop it from the URL
        if not _ok(token):
            raise HTTPException(401, "bad token")
        r = RedirectResponse("/dashboard", status_code=303)
        r.set_cookie("vault_token", token, max_age=3600 * 24 * 365, httponly=True, samesite="lax")
        return r
    if not _ok(vault_token or ""):
        return HTMLResponse("<p style='font-family:system-ui'>Open <code>/dashboard?token=YOUR_TOKEN</code> once on this device.</p>", 401)
    return HTMLResponse(DASHBOARD_HTML)


def _item_json(r: dict) -> dict:
    meta = {}
    folder = Path(r["folder"]) if r.get("folder") else None
    if folder and (folder / "meta.json").exists():
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    text = meta.get("text") or ""
    tags = meta.get("tags") or [t for t in (r.get("tags") or "").split(",") if t]
    if not tags:
        tags = keywords.extract(text, " ".join(p.get("title") or "" for p in meta.get("papers") or []),
                                " ".join(t for _, t in (meta.get("document") or {}).get("text_pages", [])))
    base = f"/files/{r['id']}/"
    return {
        "id": r["id"], "status": r["status"], "url": r.get("url") or meta.get("url"),
        "title": r.get("title") or meta.get("title") or r.get("url"),
        "author": r.get("author") or meta.get("author") or "",
        "captured": dt.datetime.fromtimestamp(r["created_at"]).isoformat(timespec="minutes"),
        "summary": meta.get("summary") or r.get("summary") or "",
        "excerpt": text[:400], "tags": tags, "key_terms": meta.get("key_terms") or [],
        "worth": meta.get("worth_reading") or "",
        "pdf": base + meta["pdf"] if meta.get("pdf") and (folder / meta["pdf"]).exists() else None,
        "document": ({"title": meta["document"].get("title"), "pages": meta["document"].get("pages"),
                      "original": meta["document"].get("original", False),
                      "href": base + meta["document"]["file"] if meta["document"].get("file") else None,
                      "search": meta["document"].get("search")} if meta.get("document") else None),
        "doc_text": " ".join(t for _, t in (meta.get("document") or {}).get("text_pages", []))[:20000],
        "images": [base + n for n in meta.get("images") or []],
        "videos": [{"href": base + v["file"] if v.get("file") else None, "duration": v.get("duration"),
                    "quality": v.get("quality"), "has_transcript": bool(v.get("transcript"))}
                   for v in meta.get("videos") or []],
        "youtube": [{"url": y["url"], "title": y.get("title") or y["url"]} for y in meta.get("youtube") or []],
        "transcript": " ".join(v.get("transcript") or "" for v in meta.get("videos") or [])[:20000],
        "documents": [{"name": n, "href": base + n} for n in meta.get("documents") or []],
        "papers": [{"title": p.get("title") or p.get("id"), "page": p.get("page"),
                    "href": base + p["file"] if p.get("file") else None} for p in meta.get("papers") or []],
    }


@app.get("/api/items", dependencies=[Depends(check_token)])
def api_items(limit: int = 2000):
    rows = [r for r in db.recent(limit) if r["status"] not in ("duplicate",)]
    return [_item_json(r) for r in rows]


@app.get("/files/{item_id}/{name}", dependencies=[Depends(check_token)])
def files(item_id: int, name: str):
    row = next((r for r in db.recent(100000) if r["id"] == item_id), None)
    if not row or not row.get("folder") or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(404)
    folder = Path(row["folder"]).resolve()
    path = (folder / name).resolve()
    if path.parent != folder or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


@app.delete("/api/items/{item_id}", dependencies=[Depends(check_token)])
def delete_item(item_id: int):
    """Remove an item from the vault. Its folder is moved to /data/trash (delete that by hand to free space)."""
    import shutil
    with db.connect() as con:
        row = con.execute("SELECT id, folder FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        folder = Path(row["folder"]) if row["folder"] else None
        if folder and folder.exists():
            trash = config.DATA_DIR / "trash"
            trash.mkdir(exist_ok=True)
            dest = trash / folder.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(folder), str(dest))
        con.execute("DELETE FROM items WHERE id=?", (item_id,))
    index.remove(item_id)
    log.info("item %s deleted (folder moved to trash)", item_id)
    return {"ok": True, "id": item_id}


# ------------------------------------------------------------------ AI: ask + digest
class AskIn(BaseModel):
    question: str


@app.post("/api/ask", dependencies=[Depends(check_token)])
def api_ask(q: AskIn):
    res = agent.ask(q.question)
    res["html"] = digest.md_to_html(res["answer"])
    titles = {r["id"]: r["title"] for r in db.recent(100000)}
    res["sources"] = [{"id": i, "title": titles.get(i) or f"item {i}"} for i in res["citations"]]
    return res


@app.get("/api/digests", dependencies=[Depends(check_token)])
def api_digests():
    return digest.list_digests()


@app.get("/api/digests/{name}", dependencies=[Depends(check_token)])
def api_digest(name: str):
    d = digest.read_digest(name)
    if not d:
        raise HTTPException(404)
    return d


@app.post("/api/digest/run", dependencies=[Depends(check_token)])
def api_digest_run(days: int = 7, email: bool = True):
    d = digest.build(days=days, send=email)
    d["html"] = digest.md_to_html(d["markdown"])
    return d
