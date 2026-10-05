# LinkedIn Vault (prototype v0.1)

Save useful LinkedIn technical posts in one tap. A Raspberry Pi archives the text, images, PDFs and
linked papers to your external drive, writes an AI summary, and emails everything to you.

```
iPhone / iPad ──Share ▸ "Save to Vault" Shortcut──┐
                                                    ├─ HTTPS over Tailscale ─▶  Pi: linkedin-vault (Docker)
Personal PC Chrome ──"📥 Vault" button────────────┘                             │
                                                     ┌──────────────────────────┼───────────────────────┐
                                                     ▼                          ▼                       ▼
                                          /mnt/vault (external HDD)     Email to your Gmail     /items web page
                                          post.md, meta.json, images,   summary + attachments   (quick list)
                                          PDFs, papers, vault.db
```

| Folder | What |
|---|---|
| `backend/` | FastAPI capture API, SQLite queue, worker (fetch → download → papers → AI → vault → email), Docker files |
| `extension/` | Chrome/Edge extension (Manifest V3) that adds a **📥 Vault** button to every post |
| `backend/tests/` | Offline tests (`pytest -q`) |

---

## 1. Prepare the Pi

Works on a Pi 4 (4 GB+) or Pi 5 with 64-bit Raspberry Pi OS.

### 1a. External drive

> ⚠️ Formatting erases the drive. Copy anything you need off it first.

```bash
lsblk -f                                   # find it, e.g. /dev/sda1
sudo mkfs.ext4 -L vault /dev/sda1
sudo mkdir -p /mnt/vault
sudo blkid /dev/sda1                       # copy the UUID
echo 'UUID=<uuid> /mnt/vault ext4 defaults,nofail,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a && df -h /mnt/vault
sudo touch /mnt/vault/.vault-marker        # the app refuses to start if this file is missing
```

The marker file stops the app from writing to the SD card when the drive isn't mounted: the
container keeps restarting until the drive is back. A 2.5" USB-powered drive needs the official
Pi power supply or a powered USB hub.

### 1b. Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
```

### 1c. Install the app

```bash
# copy the linkedin-vault folder to the Pi (scp, USB stick, or git), then:
cd linkedin-vault/backend
cp .env.example .env
openssl rand -hex 24          # paste the result into VAULT_TOKEN
nano .env                     # fill in the keys (see sections 2 and 3)
docker compose up -d --build
docker compose logs -f        # look for "vault ready"
curl http://127.0.0.1:8000/health
```

### 1d. Tailscale (HTTPS for your devices)

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname vault-pi --advertise-tags=tag:vault
sudo tailscale serve --bg http://127.0.0.1:8000
tailscale serve status        # shows https://vault-pi.<your-tailnet>.ts.net
```

In the Tailscale admin console:

* **DNS**: turn on MagicDNS and HTTPS Certificates (needed for `tailscale serve`).
* **Access controls**: keep the vault and the probe project apart. Merge this into your policy and
  **keep your existing probe rules**:

```jsonc
"tagOwners": { "tag:vault": ["autogroup:admin"], "tag:probe": ["autogroup:admin"] },
"acls": [
  { "action": "accept", "src": ["autogroup:member"], "dst": ["tag:vault:443"] },
  { "action": "accept", "src": ["autogroup:member"], "dst": ["tag:probe:*"] }
  // + your existing probe ↔ collector rules
]
```

The app itself only listens on `127.0.0.1`, so the only way in is through Tailscale on port 443.

---

## 2. Email (Gmail)

1. Create a separate Gmail account for the bot, e.g. `aaron.vault.bot@gmail.com`.
2. Turn on 2-Step Verification for that account, then go to **Google Account → Security → App passwords**
   and create one named "vault-pi".
3. In `.env`: `SMTP_USER=` the bot address, `SMTP_PASSWORD=` the 16-character app password, and
   `MAIL_TO=` your own Gmail.
4. Optional: in your own Gmail, add a filter `subject:"[Vault]"` that applies the label **Vault**.

Each email contains the title, author, link, AI summary, tags, linked papers and full post text,
with the images, PDFs and papers attached up to `MAX_ATTACH_MB` (20 MB). Anything larger stays on
the drive and the email says so.

## 3. AI summary (optional)

Put an Anthropic API key in `ANTHROPIC_API_KEY`. The worker sends the post text plus the first two
images, so infographics get summarized too. `CLAUDE_MODEL` sets the model; use any current model ID
from docs.claude.com. Leave the key empty to skip summaries; everything else still works.

---

## 4. iPhone / iPad: "Save to Vault" Shortcut

Install Tailscale on the device and turn on **VPN On Demand** in the Tailscale app settings, so the
tailnet is reachable whenever you share.

In the **Shortcuts** app, tap **+** and build:

1. Tap the **ⓘ** (Details) → turn on **Show in Share Sheet**. Set *Receives* to **URLs** and **Text** only.
2. Add **Get Contents of URL**:
   * URL: `https://vault-pi.<your-tailnet>.ts.net/link`
   * Method: **POST**
   * Headers: `X-Vault-Token` = *your VAULT_TOKEN*
   * Request Body: **JSON**, field `url` (Text) = **Shortcut Input**, field `source` (Text) = `ios`
3. Add **Get Dictionary Value**: key `message` from **Contents of URL**.
4. Add **Show Notification** with the **Dictionary Value**.
5. Name it **Save to Vault** and pick an icon.

Use it from the LinkedIn app: on a post, tap **Send/Share → Share via… → Save to Vault**. You'll see
"Saved to Vault ✓" and the email follows shortly after.

> From an iPhone share, the Pi only receives a link. It reads the public version of the post. If
> LinkedIn requires a login for that post, the item is saved as **link only** and the email says so.
> Capturing the same post later with the Chrome extension upgrades it to a full capture.

---

## 5. Chrome extension (personal PC)

> Use this on a personal computer. Don't install it or Tailscale on your corporate laptop without IT
> approval. On that laptop, use LinkedIn's own **⋯ → Save** and share from *Saved items* on your
> phone later.

1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and pick the `extension/` folder.
2. Click the extension icon, enter `https://vault-pi.<tailnet>.ts.net` and your token, then **Save** → allow → **Test**.
3. On linkedin.com, every post gets a **📥 Vault** button next to Like / Comment / Repost / Send.
   Click it and the button shows ✅ Saved. The extension expands "…more" first so the full text is captured.
4. If the Pi can't be reached, captures wait in a local queue (the orange badge shows how many) and
   are resent every 5 minutes or when you click **Send queued**.

---

## 6. What lands on the drive

```
/mnt/vault/
  vault.db                          # index + job queue
  vault/2026-09-28_00012_one-sky-three-satellite-technologies/
      post.md                       # readable page (Obsidian-friendly)
      meta.json                     # everything structured
      img_01.jpg ...                # full-resolution post images
      document_01.pdf               # PDF carousel (when available)
      paper_01_<title>.pdf          # arXiv / open-access papers found in the post
```

Useful endpoints (all need the token, as the `X-Vault-Token` header or `?token=`):

* `GET /health`: drive mounted, email and AI enabled, queue counts (no token needed)
* `GET /items?token=...`: a simple list of saved items with summaries
* `POST /link` `{url}` and `POST /capture` `{url,text,author,images[],links[]}`

Backup (recommended): `rclone sync /mnt/vault gdrive:linkedin-vault` from a nightly cron job.

---

## 7. Known limits of this prototype

* **LinkedIn markup changes.** The extension has fallbacks, but if the button disappears, the selectors in
  `content.js` need an update.
* **PDF carousels.** LinkedIn often renders document posts as page images inside a viewer rather than
  as a PDF link. Those pages are captured as images; a real PDF is saved only when a download link exists.
* **Paywalled papers** (IEEE and others) are saved as links; only open-access PDFs are downloaded
  (arXiv always, DOIs through Unpaywall).
* Captures are processed one at a time with automatic retries (2 → 4 → 8 → 16 min, then marked failed).
  Jobs interrupted by a reboot resume automatically.

## 8. Development

```bash
cd backend
pip install -r requirements.txt pytest
pytest -q
```
