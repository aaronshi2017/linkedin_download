"""Keyword tagging without AI: finds known RAN / AI / telecom topics in the post text.
Used when no AI key is set, and merged with AI tags when it is."""
import re

# tag -> regex (case-insensitive). Add your own lines here.
TOPICS = {
    "o-ran":          r"\bo-?ran\b|open\s?ran|\bopenran\b",
    "ric":            r"\b(near-rt|non-rt)?\s?ric\b|\bxapps?\b|\brapps?\b|\bsmo\b",
    "xapp":           r"\bxapps?\b",
    "rapp":           r"\brapps?\b",
    "5g":             r"\b5g\b|\bnr\b|new radio",
    "6g":             r"\b6g\b",
    "lte":            r"\blte\b|\b4g\b",
    "ntn":            r"\bntn\b|non-terrestrial|satellite|direct-to-(cell|device)",
    "ai-ml":          r"\bai\b|\bml\b|machine learning|deep learning|neural|\bllms?\b|genai|agentic|transformer",
    "llm":            r"\bllms?\b|large language model|genai|agentic",
    "mimo":           r"\bm?mimo\b|massive mimo|beamform",
    "csi":            r"\bcsi\b|channel state",
    "energy-saving":  r"energy (saving|efficien)|power saving|\bsleep mode\b|cell (switch|sleep)",
    "security":       r"secur|jamming|attack|threat|zero trust|encrypt",
    "3gpp":           r"\b3gpp\b|\brel(ease)?[- ]?1[5-9]\b|\brel(ease)?[- ]?2[0-9]\b",
    "ran-optimization": r"optimi[sz]ation|\bkpis?\b|link adaptation|\bmcs\b|\bbler\b|handover|mobility",
    "core":           r"\b5gc\b|\bamf\b|\bsmf\b|\bupf\b|core network|open5gs",
    "fwa":            r"\bfwa\b|fixed wireless",
    "ims-volte":      r"\bims\b|volte|vonr",
    "testing":        r"\btest(ing|bed)?\b|emulator|keysight|\blab\b|drive test",
    "paper":          r"arxiv|\bpaper\b|preprint|ieee|journal|conference|workshop",
    "digital-twin":   r"digital twin",
    "cloud-ran":      r"\bv?ran\b.*cloud|cloud-?ran|\bvran\b|kubernetes|\bcaas\b",
    "spectrum":       r"spectrum|\bmhz\b|\bghz\b|mmwave|\bfr[12]\b|\bband\s?n?\d+",
}
_COMPILED = {tag: re.compile(rx, re.I) for tag, rx in TOPICS.items()}


def extract(*texts: str, limit: int = 8) -> list:
    import unicodedata
    blob = unicodedata.normalize("NFKC", "\n".join(t for t in texts if t))
    hits = []
    for tag, rx in _COMPILED.items():
        n = len(rx.findall(blob))
        if n:
            hits.append((n, tag))
    hits.sort(key=lambda x: -x[0])
    return [t for _, t in hits[:limit]]
