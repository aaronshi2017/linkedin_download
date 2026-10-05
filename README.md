# LinkedIn Vault

Save useful LinkedIn technical posts with one click, before the feed washes them away. A Raspberry Pi
keeps the text, images, original PDFs and videos together with the link back to the post, writes an AI
summary, emails you a copy, and gives you a searchable dashboard, an "Ask your vault" assistant and a
weekly digest.

Everything is stored on your own hardware. Saving is always started by you (a click or a share); nothing
browses or scrapes LinkedIn on its own.

```
Chrome (personal PC) ── 📥 Vault button ───────┐
                                                ├─ HTTPS over Tailscale ─▶ Raspberry Pi (Docker)
iPhone / iPad ── Share ▸ "Save to Vault" ──────┘                              │
                                                     ┌────────────────────────┼─────────────────────┐
                                                     ▼                        ▼                     ▼
                                              USB drive (files)        Email to you          Dashboard
                                              text, images, PDFs,      summary + PDF copy    search, Ask,
                                              videos, search index     + attachments         weekly digest
```

## What it saves

| Post type | What is kept |
|---|---|
| Text and images | Full text, author, full-size images, link to the post, a PDF copy of the post |
| Document posts (PDF carousels) | The original PDF and the text of every page |
| Video posts | The MP4, a thumbnail and the captions as a transcript |
| Posts with YouTube links | The link, title and thumbnail (YouTube videos are not downloaded) |
| Posts linking to papers | arXiv PDFs, and open-access PDFs for DOI links (via Unpaywall); paywalled papers stay as links |

## Features

- **📥 Vault button** on every LinkedIn post (Chrome/Edge extension). If the Pi can't be reached, saves wait in a queue and are resent every 5 minutes.
- **iPhone / iPad Shortcut** in the share sheet.
- **Dashboard**: posts grouped by day, keyword filter, search across post text, document pages, transcripts and paper titles, delete button.
- **Email per save** with the summary, PDF copy and attachments (up to `MAX_ATTACH_MB`).
- **AI summary and tags** for each post (text plus the first two images).
- **Ask your vault**: a question box; the assistant searches everything you saved and answers with `[#id]` links to the posts.
- **Weekly digest** by email: themes, what to read first, links to your interests.

The AI parts need an Anthropic API key. Without one, saving, the dashboard, plain search and a simple digest still work.

## Repository layout

| Folder | What |
|---|---|
| `backend/` | FastAPI app, SQLite queue and search index, worker, Docker files |
| `backend/tests/` | Offline tests (`pytest -q`) |
| `extension/` | Chrome/Edge extension (Manifest V3) |

## Requirements

- Raspberry Pi 4 or 5 (64-bit OS) with Docker, and a USB drive for storage
- [Tailscale](https://tailscale.com) on the Pi and on each device you save from, with MagicDNS and HTTPS certificates turned on
- Optional: a Gmail account with an app password (email), an Anthropic API key (AI)

## Setup

### 1. Storage

The app writes to `/mnt/usb1/linkedin-vault` (change the path in `backend/docker-compose.yml`). It refuses
to start unless a marker file is there, so it can never fill the SD card when the drive is unplugged.

```bash
sudo mkdir -p /mnt/usb1/linkedin-vault
sudo touch /mnt/usb1/linkedin-vault/.vault-marker
```

### 2. Configure

```bash
cd backend
cp .env.example .env
openssl rand -hex 24      # paste the result into VAULT_TOKEN
nano .env
```

| Setting | Purpose |
|---|---|
| `VAULT_TOKEN` | Required. The password the extension, Shortcut and dashboard use |
| `ANTHROPIC_API_KEY`, `CLAUDE_MODEL` | AI summaries, Ask and digest |
| `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_TO` | Email (Gmail app password) |
| `PUBLIC_URL` | Your dashboard address, used for links in digest emails |
| `INTERESTS` | Your focus areas, used by the digest and Ask |
| `DIGEST_WEEKDAY`, `DIGEST_HOUR` | When the weekly digest is sent (default Monday 08:00) |
| `LOG_ACCESS` | `quiet` (default) or `all` |

Never commit `.env`; it is listed in `.gitignore`.

### 3. Start

```bash
docker build --network host -t backend-vault .
docker compose up -d --no-build
docker compose logs -f                 # wait for "vault ready"
curl http://127.0.0.1:8000/health
```

### 4. HTTPS through Tailscale

```bash
sudo tailscale serve --bg http://127.0.0.1:8000
tailscale serve status                 # shows https://<pi-name>.<tailnet>.ts.net
```

The app listens only on `127.0.0.1`, so Tailscale is the only way in.

### 5. Dashboard

Open `https://<pi-name>.<tailnet>.ts.net/dashboard?token=YOUR_TOKEN` once on each device. The token is
then kept in a cookie, so bookmark plain `/dashboard`.

### 6. Chrome extension

Use a personal computer. Don't install it on a work laptop without IT approval; company proxies usually
block the connection to the Pi anyway.

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, pick the `extension/` folder.
2. Click the extension icon, enter the Pi address and token, then **Save** and **Test**.
3. On linkedin.com, click **📥 Vault** under any post.

### 7. iPhone / iPad Shortcut

In the Shortcuts app, create **Save to Vault**:

1. In the shortcut's details, turn on **Show in Share Sheet**; receive **URLs** and **Text**.
2. Add **Get Contents of URL**: URL `https://<pi-name>.<tailnet>.ts.net/link`, method **POST**, header
   `X-Vault-Token` = your token, JSON body with `url` = **Shortcut Input** and `source` = `ios`.
3. Add **Get Dictionary Value** (`message`) and **Show Notification**.

A share from the phone sends only the link, so the Pi reads the public version of the post. Saving the
same post later with the extension upgrades it to a full capture.

## Everyday use

**Logs**

```
capture received: #52 from chrome — lnkd.in/p/abc123 (queued)
item #52 done: "RL Playbook for RAN" — pdf, 3 image(s), AI summary
item #52 emailed (2.1 MB attached)
```

If you click 📥 Vault and no `received` line appears, the save never reached the Pi.

**Update**

```bash
cd backend
docker build --network host -t backend-vault .
docker compose up -d --no-build --force-recreate
```

A change to `.env` only needs the second command.

**Files on the drive**

```
/mnt/usb1/linkedin-vault/
  vault.db                 queue and search index
  vault/2026-10-01_00052_<title>/
      post.pdf  post.md  meta.json  img_01.jpg  <document>.pdf  video_01.mp4  paper_01_<title>.pdf
  digests/                 weekly digests (Markdown)
  trash/                   deleted items (remove by hand when sure)
```

## Limits

- LinkedIn changes its page layout from time to time. If the button disappears or links go missing, the
  selectors in `extension/content.js` need an update.
- Posts that need a login can only be captured in full by the extension, not by the phone Shortcut.
- One save is processed at a time; failed steps are retried with growing delays.

## Development

```bash
cd backend
pip install -r requirements.txt pytest
pytest -q
```

## Disclaimer

A personal tool for keeping your own copy of posts you can already see. Respect LinkedIn's terms and the
authors' copyright: don't republish saved content. Not affiliated with LinkedIn.
