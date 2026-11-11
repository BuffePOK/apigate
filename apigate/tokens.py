"""Cheap, conservative token estimates (used only for reservations; real usage settles later)."""
from __future__ import annotations

import json


def est_text(s: str) -> int:
    # utf-8 bytes / 3.2 is slightly pessimistic for English and about right for Cyrillic/Polish
    return int(len(s.encode("utf-8", "ignore")) / 3.2) + 1


def est_content(content) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return est_text(content)
    n = 0
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            n += est_text(p.get("text", ""))
        elif p.get("type") in ("image_url", "input_image"):
            n += 800
        else:
            n += 200
    return n


def est_chat(body: dict) -> int:
    n = 3
    for m in body.get("messages", []):
        n += 4 + est_content(m.get("content"))
        for tc in m.get("tool_calls") or []:
            n += est_text(json.dumps(tc.get("function", {}), ensure_ascii=False))
    if body.get("tools"):
        n += est_text(json.dumps(body["tools"], ensure_ascii=False))
    return n


def est_embedding(body: dict) -> int:
    inp = body.get("input", "")
    if isinstance(inp, str):
        return est_text(inp)
    n = 0
    for x in inp:
        n += est_text(x) if isinstance(x, str) else len(x) if isinstance(x, list) else 1
    return n
