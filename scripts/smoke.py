#!/usr/bin/env python3
"""End-to-end smoke test of a running gateway (only needs httpx).

  python scripts/smoke.py --base https://llm-apis.example.org --key sk-gate-... [--model gemini/gemini-3.5-flash-lite]
  (key may also come from APIGATE_SMOKE_KEY - avoids it landing in shell history)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

ok_all = True


def check(name: str, ok: bool, detail: str = "") -> None:
    global ok_all
    ok_all &= ok
    print(f"[{'OK ' if ok else 'FAIL'}] {name}" + (f"  - {detail}" if detail else ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18731")
    ap.add_argument("--key", default=os.environ.get("APIGATE_SMOKE_KEY", ""))
    ap.add_argument("--model", default="", help="model id to test (default: tries cheap models in turn until one answers)")
    a = ap.parse_args()
    base, h = a.base.rstrip("/"), {"Authorization": f"Bearer {a.key}"}
    c = httpx.Client(timeout=120)

    r = c.get(f"{base}/healthz")
    check("healthz", r.status_code == 200)
    r = c.get(f"{base}/v1/models")
    check("no key -> 401", r.status_code == 401, str(r.status_code))
    r = c.get(f"{base}/v1/models", headers={"Authorization": "Bearer sk-gate-definitely-wrong"})
    check("wrong key -> 401", r.status_code == 401, str(r.status_code))
    r = c.get(f"{base}/v1/models", headers=h)
    check("list models", r.status_code == 200, str(r.status_code))
    if r.status_code != 200:
        return 1
    models = [m["id"] for m in r.json()["data"]]
    provs = sorted({m.split("/")[0] for m in models if "/" in m})
    print(f"      {len(models)} models, providers: {', '.join(provs)}")
    def rank(m: str) -> tuple:
        # prefer cheap/small chat models; newest names first
        pref = 0 if "flash-lite" in m else 1 if ("flash" in m or "mini" in m or "chat" in m or "haiku" in m) else 2
        return (pref, [-ord(ch) for ch in m])
    cands = [a.model] if a.model else sorted(models, key=rank)
    cands = [m for m in cands if not any(t in m for t in ("embed", "tts", "image", "audio", "realtime", "transcribe", "moderation", "live"))][:6]
    model, r, t = "", None, 0.0
    for m in cands:
        t = time.time()
        r = c.post(f"{base}/v1/chat/completions", headers=h, json={"model": m, "messages": [{"role": "user", "content": "Reply with exactly: pong"}], "max_tokens": 64})
        if r.status_code == 200:
            model = m
            break
        print(f"      [skip] {m}: {r.status_code} {r.text[:110]}")
    check("chat (non-stream)", bool(model), f"model={model} {time.time()-t:.1f}s " + (r.json()["choices"][0]["message"]["content"][:60] if model else "no model answered"))
    if not model:
        return 1

    t, chunks, text, done = time.time(), 0, "", False
    with c.stream("POST", f"{base}/v1/chat/completions", headers=h, json={"model": model, "stream": True, "stream_options": {"include_usage": True},
                  "messages": [{"role": "user", "content": "Count from 1 to 5, separated by spaces."}], "max_tokens": 64}) as s:
        for line in s.iter_lines():
            if line.startswith("data: "):
                d = line[6:]
                if d == "[DONE]":
                    done = True
                    break
                j = json.loads(d)
                chunks += 1
                for ch in j.get("choices") or []:
                    text += (ch.get("delta") or {}).get("content") or ""
        check("chat (stream)", s.status_code == 200 and done and chunks > 1, f"{s.status_code} chunks={chunks} done={done} {time.time()-t:.1f}s text={text[:40]!r}")

    r = c.post(f"{base}/v1/chat/completions", headers=h, json={"model": "nope/does-not-exist", "messages": [{"role": "user", "content": "hi"}]})
    check("unknown model -> 4xx JSON error", 400 <= r.status_code < 500 and "error" in r.json(), str(r.status_code))
    r = c.post(f"{base}/v1/chat/completions", headers=h, content=b"{not json", )
    check("malformed body -> 4xx", 400 <= r.status_code < 500, str(r.status_code))
    r = c.post(f"{base}/v1/chat/completions", headers={**h, "Content-Type": "application/json"}, content=b"x" * (20 * 1024 * 1024))
    check("oversized body rejected", r.status_code in (400, 413), str(r.status_code))

    hdr = c.get(f"{base}/").headers
    check("security headers", "content-security-policy" in hdr and hdr.get("x-content-type-options") == "nosniff")
    if base.startswith("https"):
        check("HSTS", "strict-transport-security" in hdr)
    print("\nALL OK" if ok_all else "\nSOME CHECKS FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
