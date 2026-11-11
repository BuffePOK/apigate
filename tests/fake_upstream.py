"""In-process fake of OpenAI-compatible, Gemini and Anthropic upstreams.

Behaviour is driven by the API key value so tests can script failures:
  good-*     : works
  limited-*  : always 429 with Retry-After: 2
  bad-*      : 401
  flaky-*    : 503
  blip-*     : 503 on the first two calls, then works
  noacc-*    : works, but lacks model 'secret-model' in its model list (others have it)
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
calls: dict[str, list[dict]] = defaultdict(list)   # key -> request log


def reset() -> None:
    calls.clear()


def _key(req: Request) -> str:
    auth = req.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return req.headers.get("x-api-key") or req.headers.get("x-goog-api-key") or ""


def _gate(key: str):
    if key.startswith("bad-") or not key:
        return JSONResponse({"error": {"message": f"Incorrect API key provided: {key}", "type": "invalid_request_error", "code": "invalid_api_key"}}, 401)
    if key.startswith("limited-"):
        return JSONResponse({"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}, 429, headers={"retry-after": "2"})
    if key.startswith("blip-") and calls[key] and calls[key][-1]["path"] == "chat" and sum(1 for c in calls[key] if c["path"] == "chat") <= 2:
        return JSONResponse({"error": {"message": "This model is currently experiencing high demand."}}, 503)
    if key.startswith("flaky-"):
        return JSONResponse({"error": {"message": "overloaded"}}, 503)
    return None


def _text(m) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    return " ".join(p.get("text", "") for p in c or [] if isinstance(p, dict))


# ----------------------------------------------------------------- OpenAI-compat
OA_MODELS = ["gpt-fake", "text-embedding-fake", "whisper-fake", "secret-model"]


@app.get("/oa/v1/models")
async def oa_models(req: Request):
    key = _key(req)
    calls[key].append({"path": "models"})
    if (g := _gate(key)) and not key.startswith(("limited-", "flaky-")):
        return g
    ids = [m for m in OA_MODELS if not (key.startswith("noacc-") and m == "secret-model")]
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in ids]}


@app.post("/oa/v1/chat/completions")
async def oa_chat(req: Request):
    key = _key(req)
    body = await req.json()
    calls[key].append({"path": "chat", "body": body, "t": time.time()})
    if g := _gate(key):
        return g
    last = _text(body["messages"][-1])
    answer = "echo: " + last
    pt = sum(len(_text(m)) for m in body["messages"]) // 4 + 1
    ct = len(answer) // 4 + 1
    if not body.get("stream"):
        return {"id": "c1", "object": "chat.completion", "created": 1, "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}

    async def gen():
        def ev(d):
            return f"data: {json.dumps(d)}\n\n"
        yield ev({"id": "c1", "object": "chat.completion.chunk", "model": body["model"], "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
        for word in answer.split(" "):
            yield ev({"id": "c1", "object": "chat.completion.chunk", "model": body["model"], "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}]})
        yield ev({"id": "c1", "object": "chat.completion.chunk", "model": body["model"], "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        if (body.get("stream_options") or {}).get("include_usage"):
            yield ev({"id": "c1", "object": "chat.completion.chunk", "model": body["model"], "choices": [], "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/oa/v1/embeddings")
async def oa_emb(req: Request):
    key = _key(req)
    body = await req.json()
    calls[key].append({"path": "embeddings", "body": body})
    if g := _gate(key):
        return g
    inp = body["input"] if isinstance(body["input"], list) else [body["input"]]
    return {"object": "list", "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]} for i, _ in enumerate(inp)],
            "model": body["model"], "usage": {"prompt_tokens": 7, "total_tokens": 7}}


# --------------------------------------------------------------------- Gemini native
@app.get("/gem/v1beta/models")
async def gem_models(req: Request):
    key = _key(req)
    calls[key].append({"path": "models", "qs": str(req.url.query)})
    if (g := _gate(key)) and not key.startswith(("limited-", "flaky-")):
        return g
    return {"models": [
        {"name": "models/gemini-flash-fake", "supportedGenerationMethods": ["generateContent"], "inputTokenLimit": 1000000},
        {"name": "models/gemini-embed-fake", "supportedGenerationMethods": ["embedContent"], "inputTokenLimit": 2048},
        {"name": "models/gemini-aqa", "supportedGenerationMethods": ["generateAnswer"]},
    ]}


@app.post("/gem/v1beta/openai/chat/completions")
async def gem_chat(req: Request):
    return await oa_chat(req)


# --------------------------------------------------------------------- Anthropic
@app.get("/an/v1/models")
async def an_models(req: Request):
    key = _key(req)
    calls[key].append({"path": "models"})
    if (g := _gate(key)) and not key.startswith(("limited-", "flaky-")):
        return g
    return {"data": [{"id": "claude-fake", "type": "model"}], "has_more": False, "last_id": "claude-fake"}


@app.post("/an/v1/messages")
async def an_messages(req: Request):
    key = _key(req)
    body = await req.json()
    calls[key].append({"path": "messages", "body": body, "headers": dict(req.headers)})
    if g := _gate(key):
        return g
    user_texts = []
    for m in body["messages"]:
        for b in m["content"]:
            if b["type"] == "text":
                user_texts.append(b["text"])
    use_tool = bool(body.get("tools")) and any("use tool" in t for t in user_texts)
    usage_in = 11
    if not body.get("stream"):
        if use_tool:
            content = [{"type": "text", "text": "Let me check."}, {"type": "tool_use", "id": "toolu_1", "name": body["tools"][0]["name"], "input": {"city": "Paris"}}]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": "claude says hi"}]
            stop = "end_turn"
        return {"id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": content, "stop_reason": stop,
                "usage": {"input_tokens": usage_in, "output_tokens": 5}}

    async def gen():
        def ev(name, d):
            return f"event: {name}\ndata: {json.dumps(d)}\n\n"
        yield ev("message_start", {"type": "message_start", "message": {"id": "msg_1", "role": "assistant", "model": body["model"], "usage": {"input_tokens": usage_in, "output_tokens": 1}}})
        yield ev("ping", {"type": "ping"})
        if use_tool:
            yield ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": body["tools"][0]["name"], "input": {}}})
            yield ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"city":'}})
            yield ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '"Paris"}'}})
            yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            stop = "tool_use"
        else:
            yield ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            for w in ["claude ", "says ", "hi"]:
                yield ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": w}})
            yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            stop = "end_turn"
        yield ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 5}})
        yield ev("message_stop", {"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream")
