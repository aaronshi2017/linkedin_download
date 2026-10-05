"""Network helpers: public LinkedIn post fetch, file downloads, link resolving, paper lookup."""
import json
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from . import config

log = logging.getLogger("vault.fetch")

HEADERS = {"User-Agent": config.BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
# arXiv / APIs: identify honestly (arXiv answers spoofed browser agents with HTTP 406)
BOT_HEADERS = {"User-Agent": "LinkedInVault/0.2 (personal research archiver)", "Accept": "*/*"}


def _is_linkedin(url: str) -> bool:
    return any(d in (url or "") for d in ("linkedin.com", "lnkd.in", "licdn.com"))
ARXIV_RE = re.compile(r"arxiv\.org/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>?#]+)", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"']+")


class NotPublic(Exception):
    """LinkedIn wants a login (authwall) or blocked us — we keep the link only."""


def client(url: str = "linkedin.com") -> httpx.Client:
    # IPv4 only (the container has no IPv6 route) + retry flaky connects
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=3)
    headers = HEADERS if _is_linkedin(url) else BOT_HEADERS
    return httpx.Client(headers=headers, follow_redirects=True, timeout=30, transport=transport)


# ---------------------------------------------------------------- LinkedIn public page
def parse_post_html(html: str) -> dict:
    """Extract text/author/images from a public LinkedIn post page (JSON-LD first, OpenGraph fallback)."""
    soup = BeautifulSoup(html, "html.parser")
    out = {"text": "", "author": "", "images": [], "posted": "", "title": ""}

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if n.get("@type") in ("SocialMediaPosting", "DiscussionForumPosting", "Article", "NewsArticle", "BlogPosting"):
                out["text"] = out["text"] or n.get("articleBody") or n.get("text") or ""
                out["title"] = out["title"] or n.get("headline") or ""
                out["posted"] = out["posted"] or n.get("datePublished") or ""
                a = n.get("author")
                if isinstance(a, list):
                    a = a[0] if a else {}
                if isinstance(a, dict):
                    out["author"] = out["author"] or a.get("name", "")
                imgs = n.get("image") or []
                imgs = imgs if isinstance(imgs, list) else [imgs]
                for im in imgs:
                    u = im.get("url") if isinstance(im, dict) else im
                    if isinstance(u, str) and u not in out["images"]:
                        out["images"].append(u)

    def meta(prop):
        t = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return t.get("content", "") if t else ""

    out["text"] = out["text"] or meta("og:description") or meta("description")
    out["title"] = out["title"] or meta("og:title") or (soup.title.string if soup.title else "")
    og_img = meta("og:image")
    if og_img and og_img not in out["images"]:
        out["images"].append(og_img)
    # external links shown in the post body
    out["links"] = [a["href"] for a in soup.select("a[href]") if "lnkd.in" in a["href"]][:20]
    return out


def fetch_public_post(url: str) -> dict:
    with client() as c:
        r = c.get(url)
    final = str(r.url)
    if r.status_code != 200 or "authwall" in final or "/login" in final or "/signup" in final:
        raise NotPublic(f"HTTP {r.status_code} at {final}")
    data = parse_post_html(r.text)
    if not data["text"] and not data["images"]:
        raise NotPublic("page had no readable post content")
    return data


# ---------------------------------------------------------------- downloads
def _ext_for(content_type: str, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "application/pdf": ".pdf", "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
        "text/vtt": ".vtt", "image/avif": ".avif",
    }.get(ct) or (Path(urlparse(url).path).suffix[:5] or ".bin")


def download(url: str, dest_stem: Path, max_mb: float = 30, want_pdf: bool = False) -> Optional[Path]:
    """Stream a file to dest_stem + detected extension. Returns path or None."""
    try:
        with client(url) as c, c.stream("GET", url) as r:
            if r.status_code != 200:
                log.warning("download %s -> HTTP %s", url, r.status_code)
                return None
            ext = _ext_for(r.headers.get("content-type", ""), url)
            path = dest_stem.with_suffix(ext)
            size, limit = 0, max_mb * 1024 * 1024
            with open(path, "wb") as f:
                for chunk in r.iter_bytes(65536):
                    size += len(chunk)
                    if size > limit:
                        f.close()
                        path.unlink(missing_ok=True)
                        log.warning("download %s exceeded %s MB", url, max_mb)
                        return None
                    f.write(chunk)
        if want_pdf and path.read_bytes()[:5] != b"%PDF-":
            path.unlink(missing_ok=True)
            return None
        return path
    except Exception as e:
        log.warning("download %s failed: %s", url, e)
        return None


# ---------------------------------------------------------------- links & papers
def extract_urls(text: str) -> list:
    return list(dict.fromkeys(u.rstrip(").,;]") for u in URL_RE.findall(text or "")))


def resolve(url: str, strict: bool = False) -> str:
    """Follow lnkd.in and other short links to the real destination.
    strict=True raises on network errors (so the job is retried) instead of returning the input."""
    try:
        with client() as c:
            r = c.get(url)
        final = str(r.url)
        if "lnkd.in" in final or "linkedin.com/safety/go" in final:
            # LinkedIn sometimes shows an interstitial page with the real link inside
            soup = BeautifulSoup(r.text, "html.parser")
            a = soup.select_one("a[data-tracking-control-name='external_url_click']") or next(
                (x for x in soup.select("a[href^='http']") if "linkedin.com" not in x["href"] and "lnkd.in" not in x["href"]), None
            )
            if a:
                return a["href"]
        return final
    except Exception as e:
        log.info("resolve %s failed: %s", url, e)
        if strict:
            raise RuntimeError(f"could not open {url}: {e}")
        return url


def arxiv_info(arxiv_id: str) -> dict:
    title = ""
    try:
        with client("arxiv.org") as c:
            r = c.get("https://export.arxiv.org/api/query", params={"id_list": arxiv_id})
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entry = ET.fromstring(r.text).find("a:entry", ns)
        if entry is not None:
            title = " ".join((entry.findtext("a:title", "", ns) or "").split())
    except Exception as e:
        log.info("arxiv meta %s failed: %s", arxiv_id, e)
    if not title:  # fallback: the abstract page carries <meta name="citation_title">
        try:
            with client("arxiv.org") as c:
                r = c.get(f"https://arxiv.org/abs/{arxiv_id}")
            m = BeautifulSoup(r.text, "html.parser").find("meta", attrs={"name": "citation_title"})
            title = (m.get("content") or "").strip() if m else ""
        except Exception as e:
            log.info("arxiv abs %s failed: %s", arxiv_id, e)
    return {"kind": "arxiv", "id": arxiv_id, "title": title,
            "page": f"https://arxiv.org/abs/{arxiv_id}", "pdf": f"https://arxiv.org/pdf/{arxiv_id}"}


def doi_info(doi: str) -> dict:
    info = {"kind": "doi", "id": doi, "title": "", "page": f"https://doi.org/{doi}", "pdf": None}
    if not config.UNPAYWALL_EMAIL:
        return info
    try:
        with client("unpaywall.org") as c:
            r = c.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": config.UNPAYWALL_EMAIL})
        if r.status_code == 200:
            j = r.json()
            info["title"] = j.get("title") or ""
            loc = j.get("best_oa_location") or {}
            info["pdf"] = loc.get("url_for_pdf")
    except Exception as e:
        log.info("unpaywall %s failed: %s", doi, e)
    return info


def find_papers(urls: list) -> list:
    """Turn resolved URLs into paper records (arXiv, DOI, or direct PDF)."""
    papers, seen = [], set()
    for u in urls:
        m = ARXIV_RE.search(u)
        if m and m.group(1) not in seen:
            seen.add(m.group(1)); papers.append(arxiv_info(m.group(1))); continue
        m = DOI_RE.search(u)
        if m and ("doi" in u or "ieeexplore" in u or "sciencedirect" in u):
            doi = m.group(1).rstrip(".")
            if doi not in seen:
                seen.add(doi); papers.append(doi_info(doi)); continue
        if urlparse(u).path.lower().endswith(".pdf") and u not in seen:
            seen.add(u)
            papers.append({"kind": "pdf", "id": u, "title": Path(urlparse(u).path).name, "page": u, "pdf": u})
    return papers


# ---------------------------------------------------------------- videos / YouTube
YT_RE = re.compile(r"(youtube\.com/(watch|shorts|embed|live)|youtu\.be/)", re.I)


def youtube_info(url: str) -> dict:
    """Title/channel/thumbnail via YouTube's public oEmbed endpoint (no video download)."""
    info = {"url": url, "title": "", "channel": "", "thumb": None}
    try:
        with client("youtube.com") as c:
            r = c.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"})
        if r.status_code == 200:
            j = r.json()
            info.update(title=j.get("title", ""), channel=j.get("author_name", ""), thumb=j.get("thumbnail_url"))
    except Exception as e:
        log.info("youtube oembed %s failed: %s", url, e)
    return info


def vtt_to_text(vtt: str) -> str:
    """WebVTT captions → plain transcript text (drops timestamps, cue numbers, duplicates)."""
    lines, last = [], ""
    for ln in vtt.splitlines():
        ln = re.sub(r"<[^>]+>", "", ln).strip()
        if not ln or ln == "WEBVTT" or "-->" in ln or ln.isdigit() or ln.startswith(("NOTE", "STYLE", "Kind:", "Language:")):
            continue
        if ln != last:
            lines.append(ln)
            last = ln
    return " ".join(lines)
