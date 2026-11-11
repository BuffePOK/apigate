"""The OpenAI-compatible surface: /v1/models, /v1/chat/completions, /v1/embeddings."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..core import Result
from ..errors import GateError
from .deps import bearer_principal, gw_of, read_json, sse

router = APIRouter()


def _validate_chat(body: dict) -> None:
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs or len(msgs) > 5000:
        raise GateError(400, "invalid_messages", "'messages' must be a non-empty array", param="messages")
    for m in msgs:
        if not isinstance(m, dict) or not isinstance(m.get("role"), str):
            raise GateError(400, "invalid_messages", "each message needs a 'role'", param="messages")
    for k in ("temperature", "top_p"):
        if body.get(k) is not None and not isinstance(body[k], (int, float)):
            raise GateError(400, "invalid_parameter", f"'{k}' must be a number", param=k)
    for k in ("max_tokens", "max_completion_tokens", "n"):
        if body.get(k) is not None and (not isinstance(body[k], int) or isinstance(body[k], bool) or body[k] < 0):
            raise GateError(400, "invalid_parameter", f"'{k}' must be a non-negative integer", param=k)
    if body.get("stream") is not None and not isinstance(body["stream"], bool):
        raise GateError(400, "invalid_parameter", "'stream' must be a boolean", param="stream")
    if body.get("tools") is not None and not isinstance(body["tools"], list):
        raise GateError(400, "invalid_parameter", "'tools' must be an array", param="tools")


def meta_headers(res: Result) -> dict:
    return {"x-request-id": res.meta["request_id"], "x-gate-model": res.meta["model"]}


@router.get("/v1/models")
async def list_models(request: Request):
    pr = await bearer_principal(request)
    return {"object": "list", "data": gw_of(request).public_models(pr)}


@router.get("/v1/models/{model_id:path}")
async def get_model(model_id: str, request: Request):
    pr = await bearer_principal(request)
    for m in gw_of(request).public_models(pr):
        if m["id"] == model_id:
            return m
    raise GateError(404, "model_not_found", f"The model '{model_id}' does not exist", param="model")


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    gw = gw_of(request)
    pr = await bearer_principal(request)
    body = await read_json(request)
    _validate_chat(body)
    stream = bool(body.get("stream"))
    res = await gw.chat(pr, body, stream=stream)
    if not stream:
        return JSONResponse(res.body, headers=meta_headers(res))

    async def events():
        try:
            async for chunk in res.stream:  # type: ignore[union-attr]
                yield sse(chunk)
            yield sse("[DONE]")
        finally:
            await res.stream.aclose()  # type: ignore[union-attr]

    return StreamingResponse(events(), media_type="text/event-stream", headers={**meta_headers(res), "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/embeddings")
async def embeddings(request: Request):
    gw = gw_of(request)
    pr = await bearer_principal(request)
    body = await read_json(request)
    inp = body.get("input")
    if not (isinstance(inp, str) and inp) and not (isinstance(inp, list) and inp and len(inp) <= 2048):
        raise GateError(400, "invalid_input", "'input' must be a non-empty string or array", param="input")
    res = await gw.embeddings(pr, body)
    return JSONResponse(res.body, headers=meta_headers(res))
