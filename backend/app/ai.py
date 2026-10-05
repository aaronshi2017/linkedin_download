"""AI summary + tags via the Claude API. Reads the post text AND the main image (infographics)."""
import base64
import io
import json
import logging
from pathlib import Path

from . import config

log = logging.getLogger("vault.ai")

PROMPT = """You are helping a senior RAN / O-RAN / 5G engineer archive useful technical LinkedIn posts.
Read the post text and any attached image (often an infographic or paper figure).

Return ONLY a JSON object with these keys:
  "title":      short descriptive title (max 90 chars)
  "summary":    3 concise bullet points as one string, each line starting with "- "
  "key_terms":  up to 8 technical terms / specs mentioned (e.g. "SIB19", "3GPP Rel-17")
  "tags":       3-6 lowercase topic tags (e.g. "ntn", "o-ran", "ai-ml", "energy-saving", "csi")
  "worth_reading": one of "high", "medium", "low" for a RAN AI engineer

Author: {author}
Post text:
\"\"\"{text}\"\"\"
"""


def _image_block(path: Path):
    try:
        from PIL import Image
        img = Image.open(path)
        img = img.convert("RGB")
        img.thumbnail((1568, 1568))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(buf.getvalue()).decode()}}
    except Exception as e:
        log.info("skip image %s: %s", path, e)
        return None


def fallback(text: str, author: str) -> dict:
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else "LinkedIn post"
    return {"title": first[:90], "summary": "", "key_terms": [], "tags": [], "worth_reading": "",
            "ai": False, "author": author}


def summarize(text: str, author: str, images: list) -> dict:
    if not config.ANTHROPIC_API_KEY or (not text and not images):
        return fallback(text, author)
    try:
        import anthropic
        content = [b for b in (_image_block(p) for p in images[:2]) if b]
        content.append({"type": "text", "text": PROMPT.format(author=author or "unknown", text=(text or "")[:12000])})
        resp = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY).messages.create(
            model=config.CLAUDE_MODEL, max_tokens=800, messages=[{"role": "user", "content": content}]
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        data["ai"] = True
        return data
    except Exception as e:
        log.warning("AI summary failed: %s", e)
        return fallback(text, author)
