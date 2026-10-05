"""Build a self-contained 'printed' copy of each saved post: post.pdf
(title, author, dates, link, summary, full text, full-size images, linked papers).
It never depends on LinkedIn still having the post."""
import datetime as dt
import logging
from pathlib import Path

from fontTools.ttLib import TTFont
from fpdf import FPDF
from PIL import Image

log = logging.getLogger("vault.pdf")

FONT_DIR = Path(__file__).parent / "fonts"
REG, BOLD = FONT_DIR / "DejaVuSans.ttf", FONT_DIR / "DejaVuSans-Bold.ttf"
_CMAP = set(TTFont(str(REG))["cmap"].getBestCmap().keys())
ACCENT = (10, 102, 194)
MUTED = (102, 112, 133)


def clean(text: str) -> str:
    """Make text printable: LinkedIn's 'bold' is really Unicode math letters (𝗔𝗜 → AI), so fold
    those back with NFKC; then drop what the font can't draw (mostly emoji)."""
    import unicodedata
    text = "".join(unicodedata.normalize("NFKC", ch) if 0x1D400 <= ord(ch) <= 0x1D7FF else ch for ch in (text or ""))
    out = []
    for ch in text:
        if ch in "\n\t" or ord(ch) in _CMAP:
            out.append(ch)
        elif ord(ch) in (0x200B, 0xFE0F):
            continue
        else:
            out.append("")
    return "".join(out).replace("\t", "    ")


class PostPDF(FPDF):
    footer_text = ""

    def para(self, h, text, link="", align="L"):
        """multi_cell that always continues on the next line at the left margin."""
        self.multi_cell(self.epw, h, text, link=link, align=align, new_x="LMARGIN", new_y="NEXT")

    def footer(self):
        self.set_y(-12)
        self.set_font("dv", size=7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 6, f"{self.footer_text}   ·   page {self.page_no()}/{{nb}}", align="C")


def build(folder: Path, meta: dict) -> Path:
    pdf = PostPDF(format="A4")
    pdf.add_font("dv", "", str(REG))
    pdf.add_font("dv", "B", str(BOLD))
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(True, margin=16)
    pdf.alias_nb_pages()
    pdf.footer_text = f"Saved by LinkedIn Vault · item #{meta.get('id')} · {meta.get('captured_at', '')[:16].replace('T', ' ')}"
    pdf.set_title(clean(meta.get("title") or "LinkedIn post"))
    pdf.set_author(clean(meta.get("author") or ""))
    pdf.add_page()
    w = pdf.epw

    # title
    pdf.set_font("dv", "B", 15)
    pdf.set_text_color(20, 24, 33)
    pdf.para(7.5, clean(meta.get("title") or "LinkedIn post"))
    pdf.ln(1)

    # author / dates / link
    pdf.set_font("dv", size=9.5)
    pdf.set_text_color(*MUTED)
    who = clean(" — ".join(x for x in [meta.get("author"), meta.get("author_headline")] if x))
    if who:
        pdf.para(5, who)
    dates = [f"Saved {meta.get('captured_at', '')[:16].replace('T', ' ')}"]
    if meta.get("posted"):
        dates.insert(0, f"Posted {meta['posted'][:10]}")
    pdf.para(5, "  ·  ".join(dates))
    if meta.get("url"):
        pdf.set_text_color(*ACCENT)
        pdf.para(5, meta["url"], link=meta["url"])
    if meta.get("tags"):
        pdf.set_text_color(*MUTED)
        pdf.para(5, "Tags: " + ", ".join(meta["tags"]))
    pdf.ln(3)
    pdf.set_draw_color(228, 231, 236)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + w, pdf.get_y())
    pdf.ln(4)

    # AI summary
    if meta.get("summary"):
        pdf.set_font("dv", "B", 11)
        pdf.set_text_color(20, 24, 33)
        pdf.cell(w, 6, "Summary", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("dv", size=10)
        for line in meta["summary"].splitlines():
            if line.strip():
                pdf.para(5.2, "•  " + clean(line.lstrip("-• ").strip()))
        pdf.ln(3)

    # full post text
    if meta.get("text"):
        pdf.set_font("dv", size=10.5)
        pdf.set_text_color(30, 34, 43)
        pdf.para(5.6, clean(meta["text"]))
        pdf.ln(4)

    # videos + YouTube
    for i, v in enumerate(meta.get("videos") or [], 1):
        pdf.set_font("dv", "B", 11)
        pdf.set_text_color(20, 24, 33)
        dur = f"{int(v['duration'] // 60)}:{int(v['duration'] % 60):02d}" if v.get("duration") else "?"
        pdf.para(6, f"Video {i} ({dur}{', ' + str(v['quality']) + 'p' if v.get('quality') else ''})")
        pdf.set_font("dv", size=9.5)
        pdf.set_text_color(*MUTED)
        pdf.para(5, f"Saved as {v['file']} in this folder" if v.get("file") else "Video file could not be saved")
        if v.get("transcript"):
            pdf.set_font("dv", size=10)
            pdf.set_text_color(30, 34, 43)
            pdf.para(5.4, "Transcript: " + clean(v["transcript"]))
        pdf.ln(3)
    for y in meta.get("youtube") or []:
        pdf.set_font("dv", "B", 11)
        pdf.set_text_color(20, 24, 33)
        pdf.para(6, "YouTube video")
        pdf.set_font("dv", size=10)
        pdf.set_text_color(*ACCENT)
        pdf.para(5.2, clean(f"{y.get('title') or y['url']}" + (f" — {y['channel']}" if y.get("channel") else "")), link=y["url"])
        pdf.ln(3)

    # document post: the text of every page (crisp + searchable, unlike the small page images)
    doc = meta.get("document") or {}
    if doc.get("text_pages"):
        pdf.set_font("dv", "B", 11)
        pdf.set_text_color(20, 24, 33)
        pdf.para(6, clean(f"Document: {doc.get('title') or 'attached document'} ({doc.get('pages')} pages)"))
        if doc.get("search"):
            pdf.set_font("dv", size=9)
            pdf.set_text_color(*ACCENT)
            pdf.para(5, "Search the web for the original PDF", link=doc["search"])
        for n, t in doc["text_pages"]:
            pdf.ln(2)
            pdf.set_font("dv", "B", 9.5)
            pdf.set_text_color(*MUTED)
            pdf.para(5, f"Page {n}")
            pdf.set_font("dv", size=10)
            pdf.set_text_color(30, 34, 43)
            pdf.para(5.4, clean(t))
        pdf.ln(4)

    # images at full page width
    for name in meta.get("images") or []:
        p = folder / name
        try:
            with Image.open(p) as im:
                iw, ih = im.size
                if im.format not in ("JPEG", "PNG"):
                    p = folder / (Path(name).stem + "_pdf.png")
                    im.convert("RGB").save(p)
            h = w * ih / iw
            max_h = pdf.eph
            draw_w, draw_h = (w, h) if h <= max_h else (w * max_h / h, max_h)
            if pdf.get_y() + draw_h > pdf.h - pdf.b_margin:
                pdf.add_page()
            pdf.image(str(p), x=pdf.l_margin + (w - draw_w) / 2, w=draw_w, h=draw_h)
            pdf.ln(4)
        except Exception as e:
            log.info("pdf: skip image %s: %s", name, e)

    # linked papers
    papers = meta.get("papers") or []
    if papers:
        pdf.set_font("dv", "B", 11)
        pdf.set_text_color(20, 24, 33)
        pdf.cell(w, 6, "Linked papers", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("dv", size=9.5)
        for pp in papers:
            pdf.set_text_color(*ACCENT)
            pdf.para(5, clean(pp.get("title") or pp.get("id") or ""), link=pp.get("page") or "")
            if pp.get("file"):
                pdf.set_text_color(*MUTED)
                pdf.para(4.6, f"   saved as {pp['file']}")

    out = folder / "post.pdf"
    pdf.output(str(out))
    return out


def build_document(folder: Path, page_files: list, document: dict) -> Path:
    """Rebuild the shared document as a PDF, one page image per page (as LinkedIn shows it)."""
    pdf = FPDF(unit="mm")
    pdf.set_auto_page_break(False)
    pdf.set_title(clean(document.get("title") or "document"))
    for f in page_files:
        with Image.open(f) as im:
            w, h = im.size
            src = f
            if im.format not in ("JPEG", "PNG"):
                src = f.with_name(f.stem + "_pdf.png")
                im.convert("RGB").save(src)
        pw = 210.0
        ph = pw * h / w
        pdf.add_page(format=(pw, ph))
        pdf.image(str(src), x=0, y=0, w=pw, h=ph)
    out = folder / "document.pdf"
    pdf.output(str(out))
    return out


def safe_build_document(folder: Path, page_files: list, document: dict):
    try:
        return build_document(folder, page_files, document) if page_files else None
    except Exception as e:
        log.warning("document pdf for %s failed: %s", folder, e)
        return None


def safe_build(folder: Path, meta: dict):
    try:
        return build(folder, meta)
    except Exception as e:
        log.warning("pdf for %s failed: %s", folder, e)
        return None


def rebuild_all(vault_dir: Path) -> int:
    """(Re)create post.pdf for every saved item — run once after upgrading."""
    import json
    n = 0
    for meta_file in sorted(vault_dir.glob("*/meta.json")):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        out = safe_build(meta_file.parent, meta)
        if out:
            meta["pdf"] = out.name
            meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            n += 1
    return n


if __name__ == "__main__":
    from . import config
    logging.basicConfig(level=logging.INFO)
    print(f"{rebuild_all(config.VAULT_DIR)} PDFs written ({dt.datetime.now():%H:%M})")
