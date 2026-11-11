"""Anthropic Messages API <-> OpenAI chat-completions translation (text, images, tools, streaming)."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import AsyncIterator

import httpx

from ..errors import GateError, UpstreamError
from .base import ModelInfo, Provider, RateInfo, UpstreamStream, classify_model, map_http_error

ANTHROPIC_VERSION = "2023-06-01"
STOP_MAP = {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop", "tool_use": "tool_calls", "refusal": "content_filter", "pause_turn": "stop"}
_DATA_URI = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.S)


# ---------------------------------------------------------------- request side
def _text_of(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(p.get("text", ""))
    return "\n".join(parts)


def _blocks(content) -> list[dict]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out: list[dict] = []
    for p in content:
        t = p.get("type") if isinstance(p, dict) else None
        if t == "text":
            if p.get("text"):
                out.append({"type": "text", "text": p["text"]})
        elif t == "image_url":
            url = (p.get("image_url") or {}).get("url", "") if isinstance(p.get("image_url"), dict) else p.get("image_url", "")
            m = _DATA_URI.match(url)
            if m:
                out.append({"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}})
            elif url.startswith("https://"):
                out.append({"type": "image", "source": {"type": "url", "url": url}})
            else:
                raise GateError(400, "invalid_image", "image_url must be a data: URI or https URL")
        else:
            raise GateError(400, "unsupported_content", f"content part type '{t}' is not supported for this model")
    return out


def _push(msgs: list[dict], role: str, blocks: list[dict]) -> None:
    if not blocks:
        return
    if msgs and msgs[-1]["role"] == role:
        msgs[-1]["content"].extend(blocks)
    else:
        msgs.append({"role": role, "content": list(blocks)})
    if role == "user":  # tool_result blocks must come first
        c = msgs[-1]["content"]
        c.sort(key=lambda b: 0 if b["type"] == "tool_result" else 1)


def _loads(s) -> dict:
    if isinstance(s, dict):
        return s
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        return {}


def to_anthropic_request(body: dict, model: str, stream: bool, default_max: int = 4096) -> dict:
    if (body.get("n") or 1) != 1:
        raise GateError(400, "unsupported_parameter", "n > 1 is not supported for Anthropic models", param="n")
    system: list[str] = []
    msgs: list[dict] = []
    for m in body.get("messages", []):
        role = m.get("role")
        if role in ("system", "developer"):
            t = _text_of(m.get("content"))
            if t:
                system.append(t)
        elif role == "user":
            _push(msgs, "user", _blocks(m.get("content")))
        elif role == "assistant":
            blocks = _blocks(m.get("content"))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                blocks.append({"type": "tool_use", "id": tc.get("id"), "name": fn.get("name"), "input": _loads(fn.get("arguments"))})
            _push(msgs, "assistant", blocks)
        elif role == "tool":
            _push(msgs, "user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": _text_of(m.get("content"))}])
        else:
            raise GateError(400, "invalid_role", f"unsupported message role '{role}'", param="messages")
    rf = body.get("response_format") or {}
    if rf.get("type") in ("json_object", "json_schema"):
        system.append("Respond with a single valid JSON value and nothing else.")
    req: dict = {
        "model": model,
        "max_tokens": int(body.get("max_completion_tokens") or body.get("max_tokens") or default_max),
        "messages": msgs,
    }
    if system:
        req["system"] = "\n\n".join(system)
    if body.get("temperature") is not None:
        req["temperature"] = min(max(float(body["temperature"]), 0.0), 1.0)
    if body.get("top_p") is not None:
        req["top_p"] = float(body["top_p"])
    stop = body.get("stop")
    if stop:
        req["stop_sequences"] = [s for s in ([stop] if isinstance(stop, str) else stop) if s][:4]
    tools = body.get("tools")
    tc = body.get("tool_choice")
    if tools and tc != "none":
        req["tools"] = [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
            for t in tools if t.get("type") == "function"
        ]
        choice: dict | None = None
        if tc == "required":
            choice = {"type": "any"}
        elif isinstance(tc, dict) and tc.get("function"):
            choice = {"type": "tool", "name": tc["function"]["name"]}
        elif tc in (None, "auto"):
            choice = {"type": "auto"}
        if choice is not None:
            if body.get("parallel_tool_calls") is False:
                choice["disable_parallel_tool_use"] = True
            req["tool_choice"] = choice
    if stream:
        req["stream"] = True
    return req


# --------------------------------------------------------------- response side
def _usage(u: dict) -> tuple[int, int]:
    pt = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
    return pt, int(u.get("output_tokens") or 0)


def from_anthropic_response(r: dict, public_model: str) -> dict:
    text, tool_calls = [], []
    for blk in r.get("content", []):
        if blk.get("type") == "text":
            text.append(blk.get("text", ""))
        elif blk.get("type") == "tool_use":
            tool_calls.append({"id": blk["id"], "type": "function", "function": {"name": blk["name"], "arguments": json.dumps(blk.get("input", {}))}})
    pt, ct = _usage(r.get("usage", {}))
    msg: dict = {"role": "assistant", "content": "".join(text) if text else None}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-" + str(r.get("id", "")), "object": "chat.completion", "created": int(time.time()), "model": public_model,
        "choices": [{"index": 0, "message": msg, "finish_reason": STOP_MAP.get(r.get("stop_reason"), "stop")}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


class StreamTranslator:
    """Feeds Anthropic SSE events, emits OpenAI chat.completion.chunk dicts."""

    def __init__(self, public_model: str):
        self.model = public_model
        self.id = "chatcmpl-" + str(int(time.time() * 1000))
        self.created = int(time.time())
        self.tools: dict[int, int] = {}
        self.pt = 0
        self.ct = 0
        self.finish: str | None = None

    def _chunk(self, delta: dict, finish: str | None = None, usage: dict | None = None, choices: bool = True) -> dict:
        c = {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
             "choices": [{"index": 0, "delta": delta, "finish_reason": finish}] if choices else []}
        if usage is not None:
            c["usage"] = usage
        return c

    def feed(self, ev: dict) -> list[dict]:
        t = ev.get("type")
        if t == "message_start":
            msg = ev.get("message", {})
            self.id = "chatcmpl-" + str(msg.get("id", self.id))
            self.pt, _ = _usage(msg.get("usage", {}))
            return [self._chunk({"role": "assistant", "content": ""})]
        if t == "content_block_start":
            blk = ev.get("content_block", {})
            if blk.get("type") == "tool_use":
                idx = len(self.tools)
                self.tools[ev["index"]] = idx
                return [self._chunk({"tool_calls": [{"index": idx, "id": blk["id"], "type": "function", "function": {"name": blk["name"], "arguments": ""}}]})]
            return []
        if t == "content_block_delta":
            d = ev.get("delta", {})
            if d.get("type") == "text_delta":
                return [self._chunk({"content": d.get("text", "")})]
            if d.get("type") == "input_json_delta" and ev.get("index") in self.tools:
                return [self._chunk({"tool_calls": [{"index": self.tools[ev["index"]], "function": {"arguments": d.get("partial_json", "")}}]})]
            return []
        if t == "message_delta":
            u = ev.get("usage") or {}
            if "output_tokens" in u:
                self.ct = int(u["output_tokens"])
            if "input_tokens" in u and u["input_tokens"]:
                self.pt = max(self.pt, _usage(u)[0])
            sr = ev.get("delta", {}).get("stop_reason")
            if sr:
                self.finish = STOP_MAP.get(sr, "stop")
                return [self._chunk({}, self.finish)]
            return []
        if t == "message_stop":
            return [self._chunk({}, usage={"prompt_tokens": self.pt, "completion_tokens": self.ct, "total_tokens": self.pt + self.ct}, choices=False)]
        if t == "error":
            e = ev.get("error", {})
            kind = {"overloaded_error": "server", "rate_limit_error": "rate_limit", "api_error": "server"}.get(e.get("type"), "server")
            raise UpstreamError(kind, 529 if kind == "server" else 429, str(e.get("message", "upstream stream error"))[:300])
        return []


def _rate(h: httpx.Headers) -> RateInfo:
    until = None
    for name in ("requests", "tokens", "input-tokens", "output-tokens"):
        rem = h.get(f"anthropic-ratelimit-{name}-remaining")
        reset = h.get(f"anthropic-ratelimit-{name}-reset")
        if rem is not None and rem.strip() == "0" and reset:
            try:
                ts = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
                until = max(until or 0.0, ts)
            except ValueError:
                pass
    return RateInfo(until)


class AnthropicProvider(Provider):
    kind = "anthropic"

    def _headers(self, secret: str) -> dict:
        h = {"x-api-key": secret, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        h.update({str(k): str(v) for k, v in (self.extra.get("headers") or {}).items()})
        return h

    async def list_models(self, secret: str) -> list[ModelInfo]:
        out, after = [], ""
        for _ in range(20):
            url = f"{self.base_url}/models?limit=1000" + (f"&after_id={after}" if after else "")
            j = (await self._request("GET", url, self._headers(secret))).json()
            for m in j.get("data", []):
                out.append(ModelInfo(m["id"], classify_model(m["id"]), m.get("max_input_tokens")))
            if not j.get("has_more"):
                break
            after = j.get("last_id", "")
            if not after:
                break
        return out

    async def chat(self, secret: str, body: dict, model: str) -> tuple[dict, RateInfo]:
        req = to_anthropic_request(body, model, False, int(self.extra.get("default_max_tokens", 4096)))
        r = await self._request("POST", f"{self.base_url}/messages", self._headers(secret), req)
        try:
            return from_anthropic_response(r.json(), body.get("model", model)), _rate(r.headers)
        except (ValueError, KeyError) as e:
            raise UpstreamError("server", 502, "upstream returned an unexpected response") from e

    async def chat_stream(self, secret: str, body: dict, model: str) -> UpstreamStream:
        req = to_anthropic_request(body, model, True, int(self.extra.get("default_max_tokens", 4096)))
        resp = await self._open_stream(f"{self.base_url}/messages", self._headers(secret), req)
        tr = StreamTranslator(body.get("model", model))

        async def it() -> AsyncIterator[dict]:
            try:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    for chunk in tr.feed(ev):
                        yield chunk
            except httpx.TimeoutException as e:
                raise UpstreamError("timeout", 504, "upstream stream timed out") from e
            except httpx.HTTPError as e:
                raise UpstreamError("network", 502, f"upstream stream error: {type(e).__name__}") from e

        return UpstreamStream(resp, it(), _rate(resp.headers))
