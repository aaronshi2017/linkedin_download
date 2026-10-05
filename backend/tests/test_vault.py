"""Offline tests: run with  `pytest -q`  from the backend/ folder."""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ.update(VAULT_DATA_DIR=str(TMP), VAULT_TOKEN="t0k", REQUIRE_MARKER="1",
                  ANTHROPIC_API_KEY="", MAIL_TO="", WORKER_POLL_SEC="0.2")
(TMP / ".vault-marker").touch()

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import db, fetch, mailer, main  # noqa: E402

SAMPLE_HTML = """<html><head>
<meta property="og:title" content="Untangling Satellite Techs | Harvinder">
<meta property="og:image" content="https://media.licdn.com/dms/image/feedshare-og.jpg">
<script type="application/ld+json">{"@context":"http://schema.org","@type":"SocialMediaPosting",
"articleBody":"One sky. Three satellite technologies. See https://arxiv.org/abs/2401.12345 for details.",
"datePublished":"2026-09-21T10:00:00Z","author":{"@type":"Person","name":"Harvinder Singh Saini"},
"image":{"url":"https://media.licdn.com/dms/image/feedshare-1.jpg"}}</script></head><body></body></html>"""


def test_keys():
    a = db.make_key("https://www.linkedin.com/posts/harvinder_ntn-activity-7375012345678901234-AbCd?utm_source=share")
    b = db.make_key("https://www.linkedin.com/feed/update/urn:li:activity:7375012345678901234/")
    assert a == b == "li:7375012345678901234"
    c = db.make_key("https://www.linkedin.com/posts/oran-openran-5g-share-7506689402639634432-UAB0/?utm_source=share")
    assert c == "li:7506689402639634432"
    assert db.first_url("Check out this post: https://lnkd.in/abc123 via LinkedIn") == "https://lnkd.in/abc123"


def test_feed_url_is_not_a_key():
    a = db.make_key("https://www.linkedin.com/feed/", "Post A about NTN", "img1")
    b = db.make_key("https://www.linkedin.com/feed/", "Post B about O-RAN", "img2")
    assert a != b and a.startswith("txt:")
    assert db.make_key("https://www.linkedin.com/feed/", "", "imgX") != db.make_key("https://www.linkedin.com/feed/", "", "imgY")


def test_parse_public_html():
    d = fetch.parse_post_html(SAMPLE_HTML)
    assert d["author"] == "Harvinder Singh Saini"
    assert "Three satellite" in d["text"]
    assert d["images"][0].endswith("feedshare-1.jpg") and len(d["images"]) == 2


def test_find_papers(monkeypatch):
    monkeypatch.setattr(fetch, "arxiv_info", lambda i: {"kind": "arxiv", "id": i, "title": "T", "page": "p", "pdf": "x"})
    ps = fetch.find_papers(["https://arxiv.org/pdf/2401.12345v2", "https://arxiv.org/abs/2401.12345",
                            "https://example.com/files/oran-whitepaper.pdf"])
    assert [p["kind"] for p in ps] == ["arxiv", "pdf"]


def test_end_to_end(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [("ok",)])
    monkeypatch.setattr(fetch, "resolve", lambda u, strict=False: "https://www.linkedin.com/posts/h_ntn-activity-7375012345678901234-AbCd" if "lnkd.in" in u else u)
    img_src = TMP / "src.png"
    Image.new("RGB", (800, 600), "navy").save(img_src)

    def fake_download(url, stem, max_mb=30, want_pdf=False):
        if want_pdf:
            p = stem.with_suffix(".pdf"); p.write_bytes(b"%PDF-1.4 fake"); return p
        p = stem.with_suffix(".png"); shutil.copy(img_src, p); return p

    monkeypatch.setattr(fetch, "download", fake_download)
    monkeypatch.setattr(fetch, "fetch_public_post", lambda u: fetch.parse_post_html(SAMPLE_HTML))
    monkeypatch.setattr(fetch, "arxiv_info", lambda i: {"kind": "arxiv", "id": i, "title": "NTN paper",
                                                        "page": f"https://arxiv.org/abs/{i}", "pdf": "https://arxiv.org/pdf/x"})
    sent = []
    monkeypatch.setattr(mailer, "send", lambda meta, files: sent.append((meta, files)) or True)

    with TestClient(main.app) as c:
        assert c.post("/link", json={"url": "x"}).status_code == 401
        # 1) iPhone share: link only
        r = c.post("/link", headers={"X-Vault-Token": "t0k"},
                   json={"url": "Check out this post https://www.linkedin.com/posts/h_ntn-activity-7375012345678901234-AbCd"})
        assert r.json()["status"] == "queued"
        # 2) extension capture of a different post, with image + profile photo that must be skipped
        r = c.post("/capture", headers={"X-Vault-Token": "t0k"}, json={
            "url": "https://www.linkedin.com/feed/update/urn:li:activity:7375099999999999999/",
            "text": "O-RAN energy saving rApp results. Paper: https://arxiv.org/abs/2409.00001",
            "author": "Jane Doe",
            "images": ["https://media.licdn.com/dms/image/profile-displayphoto-shrink_100/x.jpg",
                       "https://media.licdn.com/dms/image/feedshare-shrink_2048/big.jpg"]})
        assert r.json()["status"] == "queued"
        # duplicate is detected
        r = c.post("/link", headers={"X-Vault-Token": "t0k"},
                   json={"url": "https://www.linkedin.com/feed/update/urn:li:activity:7375012345678901234/"})
        assert r.json()["duplicate"] is True

        # 3) iPhone short link to the same post as #1 -> marked duplicate by the worker
        r = c.post("/link", headers={"X-Vault-Token": "t0k"}, json={"url": "https://lnkd.in/p/guDCgDJT"})
        assert r.json()["status"] == "queued"
        for _ in range(100):
            if db.stats() == {"done": 2, "duplicate": 1}:
                break
            time.sleep(0.1)
        assert db.stats() == {"done": 2, "duplicate": 1}, db.recent()
        assert c.get("/health").json()["hdd_mounted"] is True
        assert "O-RAN energy" in c.get("/items?token=t0k").text
        # dashboard: token → cookie → API + files
        assert c.get("/dashboard").status_code == 401
        r = c.get("/dashboard?token=t0k")
        assert r.status_code == 200 and "LinkedIn Vault" in r.text
        items = c.get("/api/items").json()
        assert len(items) == 2 and all(i["tags"] for i in items)
        rich = next(i for i in items if i["images"])
        assert "o-ran" in rich["tags"] and "energy-saving" in rich["tags"]
        assert c.get(rich["images"][0]).status_code == 200
        assert c.get(f"/files/{rich['id']}/..%2Fvault.db").status_code == 404
        # delete → gone from list, folder moved to trash
        assert c.delete(f"/api/items/{rich['id']}").status_code == 200
        assert len(c.get("/api/items").json()) == 1
        assert any((TMP / "trash").iterdir())

    folders = sorted((TMP / "vault").iterdir()) + sorted((TMP / "trash").iterdir())
    folders = sorted(folders, key=lambda f: f.name)
    assert len(folders) == 2
    meta = json.loads((folders[1] / "meta.json").read_text())
    assert meta["images"] == ["img_01.png"]            # profile photo skipped
    assert meta["pdf"] == "post.pdf" and (folders[1] / "post.pdf").read_bytes()[:5] == b"%PDF-"
    assert meta["papers"][0]["file"].startswith("paper_01")
    assert (folders[0] / "post.md").read_text().startswith("# ")
    assert len(sent) == 2


def test_email_build():
    f = TMP / "a.png"
    Image.new("RGB", (50, 50)).save(f)
    msg = mailer.build({"title": "T", "url": "u", "summary": "- a\n- b", "tags": ["ntn"], "papers": [],
                        "text": "hello", "folder": "/data/x"}, [f])
    assert msg["Subject"] == "[Vault] T"
    assert any(p.get_filename() == "a.png" for p in msg.iter_attachments())
