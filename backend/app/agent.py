"""'Ask your vault' — a small tool-using agent (Claude) over your own saved posts.

The model decides which searches to run (search_vault), opens items it wants to read in full
(read_item), can look at what you saved recently (recent_items), and then answers with
citations like [#12] that link back to the saved items. Nothing here touches LinkedIn."""
import datetime as dt
import json
import logging
import time
from pathlib import Path

from . import config, db, index

log = logging.getLogger("vault.agent")
MAX_STEPS = 6

SYSTEM = """You are the research assistant for a personal archive ("the vault") of technical LinkedIn posts,
white papers, arXiv papers and videos saved by a senior RAN / O-RAN / 5G engineer moving into RAN AI.

Answer the user's question using ONLY what is in the vault. Use the tools:
- search_vault: keyword search (BM25). Run several focused searches with different wording, acronyms
  and synonyms (e.g. "energy saving", "cell sleep", "power consumption").
- read_item: open one saved item to read its full text, summary, document pages and transcript.
- recent_items: list what was saved in the last N days.

Rules:
- Cite every claim with the item id in square brackets, e.g. [#12]. Only cite items you actually saw.
- If the vault does not contain the answer, say so plainly and suggest what to save or search for.
- Be concise and technical. Use short sections or bullets. Mention when a source is a paper or white paper.
{interests}"""

TOOLS = [
    {"name": "search_vault",
     "description": "Full-text search over all saved items (post text, summaries, document pages, video transcripts, PDF papers). Returns the best matching passages with item ids.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "keywords, acronyms or a short phrase"},
         "limit": {"type": "integer", "description": "max passages (default 8, max 15)"}},
         "required": ["query"]}},
    {"name": "read_item",
     "description": "Read one saved item in full: title, author, date, link, summary, tags, post text, document page text, video transcript, papers.",
     "input_schema": {"type": "object", "properties": {"item_id": {"type": "integer"}}, "required": ["item_id"]}},
    {"name": "recent_items",
     "description": "List items saved in the last N days (id, date, title, tags).",
     "input_schema": {"type": "object", "properties": {"days": {"type": "integer"}}, "required": ["days"]}},
]


def _meta(item_id: int) -> dict:
    row = db.find_by_id(item_id)
    if not row or not row["folder"]:
        return {}
    f = Path(row["folder"]) / "meta.json"
    try:
        m = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    m["id"], m["url"] = row["id"], row["url"] or m.get("url")
    m["saved"] = dt.datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d")
    return m


def tool_search(query: str, limit: int = 8) -> str:
    hits = index.search(query, max(1, min(int(limit or 8), 15)))
    if not hits:
        return "No matches."
    return "\n\n".join(f"[#{h['item_id']}] {h['title'][:100]} — {h['kind']} ({h['ref']})\n{h['text'][:900]}" for h in hits)


def tool_read(item_id: int) -> str:
    m = _meta(int(item_id))
    if not m:
        return f"Item #{item_id} not found."
    parts = [f"[#{m['id']}] {m.get('title')}", f"Author: {m.get('author') or '-'}   Saved: {m.get('saved')}",
             f"Link: {m.get('url') or '-'}", f"Tags: {', '.join(m.get('tags') or [])}"]
    if m.get("summary"):
        parts.append("Summary:\n" + m["summary"])
    parts.append("Post text:\n" + (m.get("text") or "")[:6000])
    doc = m.get("document") or {}
    if doc.get("text_pages"):
        parts.append(f"Document '{doc.get('title')}' ({doc.get('pages')} pages):\n" +
                     "\n".join(f"p{n}: {t}" for n, t in doc["text_pages"])[:9000])
    for v in m.get("videos") or []:
        if v.get("transcript"):
            parts.append("Video transcript:\n" + v["transcript"][:4000])
    for p in m.get("papers") or []:
        parts.append(f"Linked paper: {p.get('title') or p.get('id')} {p.get('page')}")
    for y in m.get("youtube") or []:
        parts.append(f"YouTube: {y.get('title')} {y.get('url')}")
    return "\n\n".join(parts)[:16000]


def tool_recent(days: int = 7) -> str:
    since = time.time() - 86400 * max(1, min(int(days or 7), 365))
    rows = [r for r in db.recent(500) if r["created_at"] >= since and r["status"] in ("done", "link_only")]
    if not rows:
        return "Nothing saved in that period."
    return "\n".join(f"[#{r['id']}] {dt.datetime.fromtimestamp(r['created_at']):%Y-%m-%d} {r['title'] or r['url']}  ({r['tags'] or ''})" for r in rows)


def run_tool(name: str, args: dict) -> str:
    try:
        if name == "search_vault":
            return tool_search(args.get("query", ""), args.get("limit", 8))
        if name == "read_item":
            return tool_read(args.get("item_id"))
        if name == "recent_items":
            return tool_recent(args.get("days", 7))
    except Exception as e:
        return f"Tool error: {e}"
    return f"Unknown tool {name}"


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _cited(text: str) -> list:
    import re
    return sorted({int(x) for x in re.findall(r"\[#(\d+)\]", text or "")})


def ask(question: str) -> dict:
    question = (question or "").strip()
    if not question:
        return {"answer": "Ask a question about what you've saved.", "citations": [], "steps": []}
    if not config.ANTHROPIC_API_KEY:
        # no AI key: plain search results so the feature still helps
        hits = index.search(question, 8)
        ans = ("AI is off (no ANTHROPIC_API_KEY), so here are the best matching passages:\n\n" +
               "\n".join(f"- [#{h['item_id']}] **{h['title'][:90]}** — {' '.join(h['snippet'].split())}" for h in hits)) if hits \
            else "AI is off (no ANTHROPIC_API_KEY) and no saved item matches those words."
        return {"answer": ans, "citations": sorted({h["item_id"] for h in hits}), "steps": [f"search: {question}"]}

    interests = f"\nThe user's current focus areas: {config.INTERESTS}." if config.INTERESTS else ""
    messages = [{"role": "user", "content": question}]
    steps, client = [], _client()
    for _ in range(MAX_STEPS):
        resp = client.messages.create(model=config.CLAUDE_MODEL, max_tokens=1800,
                                      system=SYSTEM.format(interests=interests), tools=TOOLS, messages=messages)
        if resp.stop_reason != "tool_use":
            answer = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            return {"answer": answer, "citations": _cited(answer), "steps": steps}
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if getattr(b, "type", "") == "tool_use":
                steps.append(f"{b.name}: {json.dumps(b.input, ensure_ascii=False)}")
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": run_tool(b.name, b.input or {})})
        messages.append({"role": "user", "content": results})
    # step budget used up: ask for a final answer without tools
    messages.append({"role": "user", "content": "Please give your best answer now from what you found, with [#id] citations."})
    resp = client.messages.create(model=config.CLAUDE_MODEL, max_tokens=1800, system=SYSTEM.format(interests=interests),
                                  tools=TOOLS, tool_choice={"type": "none"}, messages=messages)
    answer = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    return {"answer": answer, "citations": _cited(answer), "steps": steps}
