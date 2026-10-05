"""All settings come from environment variables (see .env.example)."""
import os
from pathlib import Path


def _bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(os.getenv("VAULT_DATA_DIR", "/data"))
VAULT_DIR = DATA_DIR / "vault"
DB_PATH = DATA_DIR / "vault.db"
MARKER = DATA_DIR / ".vault-marker"          # proves the external HDD is mounted
REQUIRE_MARKER = _bool("REQUIRE_MARKER", "1")

API_TOKEN = os.getenv("VAULT_TOKEN", "")

# AI summary (optional — leave key empty to skip)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")

# What you care about — steers the digest and "Ask your vault" (comma-separated)
INTERESTS = os.getenv("INTERESTS", "AI/ML for RAN, O-RAN rApps/xApps and RIC, energy saving, CSI feedback compression, NTN, 5G-Advanced/6G, network automation")

# Weekly digest: weekday 0=Mon … 6=Sun, local hour; DIGEST_ENABLED=0 turns it off
DIGEST_ENABLED = _bool("DIGEST_ENABLED", "1")
DIGEST_WEEKDAY = int(os.getenv("DIGEST_WEEKDAY", "0"))
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "8"))
DIGEST_DAYS = int(os.getenv("DIGEST_DAYS", "7"))
DIGESTS_DIR = DATA_DIR / "digests"
# your dashboard address, so [#id] links in digest emails open the item (e.g. https://<pi>.<tailnet>.ts.net)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

# Email delivery (optional — leave MAIL_TO empty to skip)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
MAIL_TO = os.getenv("MAIL_TO", "")
MAX_ATTACH_MB = float(os.getenv("MAX_ATTACH_MB", "20"))

# Open-access paper lookup for DOIs (Unpaywall asks for a contact email)
UNPAYWALL_EMAIL = os.getenv("UNPAYWALL_EMAIL", "")

MAX_IMAGES = int(os.getenv("MAX_IMAGES", "12"))
MAX_PDF_MB = float(os.getenv("MAX_PDF_MB", "60"))
MAX_VIDEO_MB = float(os.getenv("MAX_VIDEO_MB", "300"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "5"))
# "quiet" (default) hides routine dashboard requests in the log; "all" shows every request
LOG_ACCESS = os.getenv("LOG_ACCESS", "quiet").strip().lower()
WORKER_POLL_SEC = float(os.getenv("WORKER_POLL_SEC", "3"))

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
