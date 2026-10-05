"""Background worker: turns queued captures into vault folders + emails."""
import datetime as dt
import json
import logging
import re
import socket
import threading
import time
import traceback
from urllib.parse import quote_plus
from pathlib import Path

from . import ai, config, db, fetch, index, keywords, mailer, pdfgen

log = logging.getLogger("vault.worker")

LI_SKIP = ("profile-displayphoto", "company-logo", "profile-framedphoto", "ghost", "static.licdn.com/aero")


def slugify(s: str, n: int = 50) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s or "").strip("-").lower()
    return (s[:n].rstrip("-")) or "post"


def process(row) -> None:
    payload = json.loads(row["payload"] or "{}")
    url = row["url"] or payload.get("url")
    status = "done"

    # 0) no internet/DNS → raise so the job is retried later instead of saved half-empty
    try:
        socket.getaddrinfo("www.linkedin.com", 443)
    except OSError as e:
        raise RuntimeError(f"no DNS/internet: {e}")

    # iPhone shares arrive as lnkd.in short links → expand to the real post URL first
    if url and "lnkd.in" in url:
        real = fetch.resolve(url, strict=True)
        if real != url:
            log.info("item %s: %s -> %s", row["id"], url, real)
            new_key = db.make_key(real)
            other = db.find_by_key(new_key)
            if other and other["id"] != row["id"]:
                if payload.get("force"):  # "force" on a short link → re-process the original item
                    db.update(other["id"], status="pending", attempts=0, next_try_at=0, emailed=0)
                db.update(row["id"], status="duplicate", url=real, title=other["title"], last_error=f"same post as item {other['id']}")
                log.info("item %s is a duplicate of item %s", row["id"], other["id"])
                return
            db.update(row["id"], key=new_key, url=real)
            url = real

    # 1) content: from the extension (already rich) or fetched from the public page
    if not payload.get("text") and url and "linkedin.com" in url:
        try:
            pub = fetch.fetch_public_post(url)
            payload = {**pub, **{k: v for k, v in payload.items() if v}}
        except fetch.NotPublic as e:
            log.info("item %s not public: %s", row["id"], e)
            status = "link_only"
    elif not payload.get("text") and url:
        status = "link_only"  # non-LinkedIn link: archive it + look for papers below

    text = payload.get("text") or ""
    author = payload.get("author") or ""

    # 2) folder on the HDD (deterministic → re-runs overwrite, no duplicates)
    url_slug = re.sub(r"^https?://(www\.)?", "", url or "").split("?")[0]
    first_line = text.strip().splitlines()[0] if text.strip() else (author or url_slug or "post")
    folder = config.VAULT_DIR / f"{dt.date.today().isoformat()}_{row['id']:05d}_{slugify(first_line)}"
    folder.mkdir(parents=True, exist_ok=True)

    # 3) images + documents (LinkedIn media URLs expire → download now)
    images = []
    img_urls = [u for u in payload.get("images", []) if not any(s in u for s in LI_SKIP)]
    for i, u in enumerate(img_urls[: config.MAX_IMAGES], 1):
        p = fetch.download(u, folder / f"img_{i:02d}", max_mb=20)
        if p:
            images.append(p)
    docs = []
    dt_raw = (payload.get("doc_title") or "").strip()
    if dt_raw.endswith("…"):  # truncated label → drop the cut-off word
        dt_raw = dt_raw[:-1].rsplit(" ", 1)[0]
    doc_slug = slugify(dt_raw, 60)
    for i, u in enumerate(payload.get("documents", [])[:5], 1):
        name = f"{doc_slug}" if (i == 1 and doc_slug != "post") else f"document_{i:02d}"
        p = fetch.download(u, folder / name, max_mb=config.MAX_PDF_MB, want_pdf=True)
        if p:
            docs.append(p)

    # 3b) document posts: every page image + the page text LinkedIn stores in each image's alt text
    document = None
    doc_pages = sorted(payload.get("doc_pages") or [], key=lambda d: d.get("n", 0))
    if doc_pages:
        page_files = []
        # the original PDF was downloaded → page images are not needed (only their text is kept)
        for d in ([] if docs else doc_pages[:150]):
            f = fetch.download(d["src"], folder / f"doc_page_{int(d['n']):03d}", max_mb=10)
            if f:
                page_files.append(f)
        text_pages = [[int(d["n"]), d["alt"].strip()] for d in doc_pages if (d.get("alt") or "").strip()]
        title_hint = (payload.get("doc_title") or "").strip()
        if title_hint.endswith("…"):  # LinkedIn truncates the label: drop the cut-off last word
            title_hint = title_hint[:-1].rsplit(" ", 1)[0].strip()
        first_page = text_pages[0][1][:160] if text_pages else ""
        query = first_page or title_hint
        document = {"title": title_hint, "pages": payload.get("doc_total") or len(doc_pages),
                    "captured_pages": len(page_files), "text_pages": text_pages, "original": bool(docs),
                    "search": None if docs else
                    (f"https://www.google.com/search?q={quote_plus(query + ' filetype:pdf')}" if query else None)}
        if docs:
            document["file"] = docs[0].name
        else:
            dpdf = pdfgen.safe_build_document(folder, page_files, document)
            document["file"] = dpdf.name if dpdf else None

    # 3c) LinkedIn videos: progressive MP4 (best quality), thumbnail, captions → transcript
    videos, video_files = [], []
    for i, v in enumerate((payload.get("videos") or [])[:3], 1):
        rec = {"duration": v.get("duration"), "quality": v.get("quality"), "file": None, "transcript": ""}
        if v.get("thumb"):
            t = fetch.download(v["thumb"], folder / f"video_{i:02d}_thumb", max_mb=10)
            if t:
                images.insert(0, t)          # the video frame leads the card, PDF and email
        if v.get("mp4"):
            f = fetch.download(v["mp4"], folder / f"video_{i:02d}", max_mb=config.MAX_VIDEO_MB)
            if f:
                rec["file"] = f.name
                video_files.append(f)
        for c in (v.get("captions") or [])[:1]:
            cf = fetch.download(c, folder / f"video_{i:02d}_captions", max_mb=5)
            if cf:
                rec["transcript"] = fetch.vtt_to_text(cf.read_text(encoding="utf-8", errors="ignore"))
        videos.append(rec)

    # 4) linked papers (arXiv / DOI / direct PDF)
    raw_links = list(dict.fromkeys((payload.get("links") or []) + fetch.extract_urls(text) + ([url] if status == "link_only" and url else [])))
    resolved = [fetch.resolve(u) if ("lnkd.in" in u or "linkedin.com/safety" in u) else u for u in raw_links[:25]]
    papers = fetch.find_papers(resolved)
    # YouTube: keep the link + title + thumbnail (downloading YouTube videos isn't allowed by their terms)
    youtube = []
    for j, u in enumerate([u for u in dict.fromkeys(resolved) if fetch.YT_RE.search(u)][:3], 1):
        y = fetch.youtube_info(u)
        if y.get("thumb"):
            t = fetch.download(y["thumb"], folder / f"youtube_{j:02d}_thumb", max_mb=5)
            y["thumb_file"] = t.name if t else None
            if t:
                images.append(t)
        youtube.append(y)
    paper_files = []
    for i, p in enumerate(papers, 1):
        if p.get("pdf"):
            f = fetch.download(p["pdf"], folder / f"paper_{i:02d}_{slugify(p.get('title') or p['id'], 40)}",
                               max_mb=config.MAX_PDF_MB, want_pdf=True)
            if f:
                p["file"] = f.name
                paper_files.append(f)

    # 5) AI summary (text + first images)
    s = ai.summarize(text, author, images)
    # keyword tags always (AI tags first, then built-in topic list)
    kw = keywords.extract(text, " ".join(p.get("title") or "" for p in papers),
                          " ".join(v["transcript"] for v in videos), " ".join(y.get("title", "") for y in youtube),
                          " ".join(t for _, t in (document or {}).get("text_pages", [])))
    s["tags"] = list(dict.fromkeys([t.lower() for t in (s.get("tags") or [])] + kw))[:10]
    title = (s.get("title") if text else "") or (url if status == "link_only" else first_line[:90])

    meta = {
        "id": row["id"], "key": row["key"], "url": url, "source": row["source"], "status": status,
        "title": title, "author": author, "author_headline": payload.get("author_headline", ""),
        "posted": payload.get("posted", ""), "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": s.get("summary", ""), "tags": s.get("tags", []), "key_terms": s.get("key_terms", []),
        "worth_reading": s.get("worth_reading", ""), "ai_summary": s.get("ai", False),
        "images": [p.name for p in images], "documents": [p.name for p in docs], "papers": papers,
        "links": resolved, "text": text, "folder": str(folder), "document": document,
        "videos": videos, "youtube": youtube,
    }
    # printable copy of the post (text + full-size images) that never depends on LinkedIn
    pdf = pdfgen.safe_build(folder, meta)
    meta["pdf"] = pdf.name if pdf else None
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    (folder / "post.md").write_text(render_md(meta), encoding="utf-8")
    try:
        index.index_meta(meta)   # make it searchable for "Ask your vault" and the digest
    except Exception as e:
        log.warning("indexing item %s failed: %s", row["id"], e)

    # 6) email (retry-safe: only once per item)
    emailed = row["emailed"]
    if not emailed:
        doc_file = [folder / document["file"]] if document and document.get("file") and not document.get("original") else []
        emailed = 1 if mailer.send(meta, ([pdf] if pdf else []) + doc_file + images + docs + paper_files + video_files) else 0

    db.update(row["id"], status=status, folder=str(folder), title=title, author=author,
              summary=meta["summary"], tags=",".join(meta["tags"]), emailed=emailed,
              payload=json.dumps(payload), last_error=None)
    parts = [("pdf" if pdf else None), (f"{len(images)} image(s)" if images else None),
             ("original document" if document and document.get("original") else ("document" if document else None)),
             (f"{len(video_files)} video(s)" if video_files else None), (f"{len(paper_files)} paper(s)" if paper_files else None),
             ("AI summary" if meta.get("summary") else None)]
    log.info("item #%s %s: \"%s\" — %s", row["id"], status, (title or "")[:80], ", ".join(x for x in parts if x) or "text only")


def render_md(m: dict) -> str:
    out = [f"# {m['title']}", "",
           f"- **Author:** {m['author'] or '-'} {('— ' + m['author_headline']) if m['author_headline'] else ''}",
           f"- **Post:** {m['url'] or '-'}",
           f"- **Captured:** {m['captured_at']}  ·  **Source:** {m['source']}",
           f"- **Tags:** {', '.join(m['tags']) or '-'}  ·  **Worth reading:** {m['worth_reading'] or '-'}", ""]
    if m["summary"]:
        out += ["## Summary", m["summary"], ""]
    if m["key_terms"]:
        out += ["**Key terms:** " + ", ".join(m["key_terms"]), ""]
    if m["papers"]:
        out += ["## Linked papers"] + [f"- [{p.get('title') or p['id']}]({p['page']})" +
                                       (f" — `{p['file']}`" if p.get("file") else "") for p in m["papers"]] + [""]
    if m["images"]:
        out += ["## Images"] + [f"![]({n})" for n in m["images"]] + [""]
    if m["documents"]:
        out += ["## Documents"] + [f"- `{n}`" for n in m["documents"]] + [""]
    out += ["## Post text", "", m["text"] or "_(not available — link only)_", ""]
    return "\n".join(out)


def loop(stop: threading.Event) -> None:
    log.info("worker started")
    while not stop.is_set():
        row = db.claim_next()
        if row is None:
            stop.wait(config.WORKER_POLL_SEC)
            continue
        try:
            process(row)
        except Exception as e:
            log.error("item %s failed: %s", row["id"], e)
            db.fail(row["id"], row["attempts"], f"{e}\n{traceback.format_exc()}")


def start() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=loop, args=(stop,), daemon=True, name="vault-worker").start()
    return stop
