"""Send one email per saved post, with images/PDFs attached (within Gmail's size limit)."""
import html
import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

from . import config

log = logging.getLogger("vault.mail")


def enabled() -> bool:
    return bool(config.MAIL_TO and config.SMTP_USER and config.SMTP_PASSWORD)


def build(meta: dict, files: list) -> EmailMessage:
    title = meta.get("title") or "LinkedIn post"
    msg = EmailMessage()
    msg["Subject"] = f"[Vault] {title}"
    msg["From"] = config.SMTP_USER or "vault@localhost"
    msg["To"] = config.MAIL_TO or "you@localhost"

    # attach in priority order until the size budget is used
    budget, used, attached, skipped = config.MAX_ATTACH_MB * 1024 * 1024, 0, [], []
    for f in files:
        f = Path(f)
        size = f.stat().st_size
        if used + size <= budget:
            used += size
            attached.append(f)
        else:
            skipped.append(f.name)

    papers = meta.get("papers") or []
    lines = [
        f"{title}",
        f"Author: {meta.get('author') or '-'}",
        f"Post:   {meta.get('url') or '-'}",
        "",
    ]
    if meta.get("status") == "link_only":
        lines += ["⚠️ LinkedIn did not show this post publicly, so only the link was saved.",
                  "Open it on your device, or capture it with the Chrome extension to archive the full content.", ""]
    if meta.get("summary"):
        lines += ["Summary:", meta["summary"], ""]
    if meta.get("tags"):
        lines += ["Tags: " + ", ".join(meta["tags"]), ""]
    if papers:
        lines += ["Linked papers:"] + [f"- {p.get('title') or p['id']}  {p['page']}" +
                                       ("  (PDF attached/saved)" if p.get("file") else "") for p in papers] + [""]
    for v in meta.get("videos") or []:
        if v.get("file"):
            lines += [f"Video saved on the Pi: {v['file']}" + (f" ({v['quality']}p)" if v.get("quality") else "")]
    for y in meta.get("youtube") or []:
        lines += [f"YouTube: {y.get('title') or ''} {y['url']}"]
    if meta.get("videos") or meta.get("youtube"):
        lines += [""]
    doc = meta.get("document") or {}
    if doc:
        how = f"original PDF attached ({doc.get('file')})" if doc.get("original") else "attached as document.pdf"
        lines += [f"Document: {doc.get('title') or '-'} ({doc.get('pages')} pages) — {how}; full page text is in post.pdf"]
        if doc.get("search"):
            lines += ["Find the original PDF: " + doc["search"]]
        lines += [""]
    if skipped:
        lines += ["Too large to attach (saved on the Pi vault): " + ", ".join(skipped), ""]
    lines += [f"Vault folder: {meta.get('folder')}", "", "— Post text —", meta.get("text") or "(none)"]
    body = "\n".join(lines)
    msg.set_content(body)

    # simple HTML version with inline preview of the first image
    first_img = next((f for f in attached if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif")), None)
    esc = html.escape
    summary_html = "".join(f"<li>{esc(l[2:] if l.startswith('- ') else l)}</li>"
                           for l in (meta.get("summary") or "").splitlines() if l.strip())
    papers_html = "".join(f"<li><a href='{esc(p['page'])}'>{esc(p.get('title') or p['id'])}</a></li>" for p in papers)
    img_html = "<p><img src='cid:preview' style='max-width:100%'></p>" if first_img else ""
    html_body = f"""<div style='font-family:Arial,sans-serif;max-width:720px'>
<h2 style='margin-bottom:4px'>{esc(title)}</h2>
<p style='color:#555;margin-top:0'>{esc(meta.get('author') or '')} · <a href='{esc(meta.get('url') or '')}'>Open post on LinkedIn</a></p>
{'<ul>' + summary_html + '</ul>' if summary_html else ''}
{'<p><b>Tags:</b> ' + esc(', '.join(meta.get('tags') or [])) + '</p>' if meta.get('tags') else ''}
{'<p><b>Linked papers</b></p><ul>' + papers_html + '</ul>' if papers_html else ''}
{img_html}
<details><summary>Full post text</summary><pre style='white-space:pre-wrap'>{esc(meta.get('text') or '')}</pre></details>
<p style='color:#888;font-size:12px'>Vault folder: {esc(meta.get('folder') or '')}</p></div>"""
    msg.add_alternative(html_body, subtype="html")
    if first_img:
        ctype = mimetypes.guess_type(first_img.name)[0] or "image/jpeg"
        maintype, subtype = ctype.split("/")
        msg.get_payload()[1].add_related(first_img.read_bytes(), maintype, subtype, cid="<preview>")

    for f in attached:
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(f.read_bytes(), maintype=maintype, subtype=subtype, filename=f.name)
    return msg


def send(meta: dict, files: list) -> bool:
    if not enabled():
        log.info("email disabled (MAIL_TO/SMTP_USER/SMTP_PASSWORD not set)")
        return False
    msg = build(meta, files)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=60) as s:
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.send_message(msg)
    log.info("item #%s emailed (%.1f MB attached)", meta.get("id"), sum(len(p.get_payload(decode=True) or b"") for p in msg.iter_attachments()) / 1e6)
    return True
