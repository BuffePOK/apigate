"""OpenAI-compatible upstreams: OpenAI, DeepSeek, Groq, Mistral, xAI, OpenRouter, Together, Gemini's /openai endpoint..."""
from __future__ import annotations

import json
import time
from typing import AsyncIterator

import httpx

from ..errors import UpstreamError
from .base import ModelInfo, Provider, RateInfo, UpstreamStream, classify_model, parse_duration


def _rate_from_headers(h: httpx.Headers) -> RateInfo:
    """OpenAI-style x-ratelimit-* headers: if a bucket hit zero, remember when it resets."""
    until = None
    for what in ("requests", "tokens"):
        rem = h.get(f"x-ratelimit-remaining-{what}")
        if rem is not None and rem.strip() == "0":
            reset = parse_duration(h.get(f"x-ratelimit-reset-{what}"))
            if reset:
                t = time.time() + reset
                until = max(until or 0.0, t)
    return RateInfo(until)


class OpenAICompatProvider(Provider):
    kind = "openai"
    supports_embeddings = True

    def _headers(self, secret: str) -> dict:
        h = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json", "Accept": "application/json"}
        h.update({str(k): str(v) for k, v in (self.extra.get("headers") or {}).items()})
        return h

    def _payload(self, body: dict, model: str, stream: bool) -> dict:
        p = {k: v for k, v in body.items() if v is not None}
        for k in self.extra.get("drop_params") or []:
            p.pop(k, None)
        p["model"] = model
        if stream:
            p["stream"] = True
            if self.extra.get("stream_usage", True):
                p["stream_options"] = {**(p.get("stream_options") or {}), "include_usage": True}
        else:
            p.pop("stream", None)
            p.pop("stream_options", None)
        return p

    async def list_models(self, secret: str) -> list[ModelInfo]:
        r = await self._request("GET", f"{self.base_url}/models", self._headers(secret))
        data = r.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        out = []
        for it in items:
            mid = it.get("id") if isinstance(it, dict) else None
            if not mid:
                continue
            mid = mid[7:] if mid.startswith("models/") else mid
            out.append(ModelInfo(mid, classify_model(mid), it.get("context_window") or it.get("context_length")))
        return out

    async def chat(self, secret: str, body: dict, model: str) -> tuple[dict, RateInfo]:
        r = await self._request("POST", f"{self.base_url}/chat/completions", self._headers(secret), self._payload(body, model, False))
        try:
            return r.json(), _rate_from_headers(r.headers)
        except ValueError as e:
            raise UpstreamError("server", 502, "upstream returned invalid JSON") from e

    async def embeddings(self, secret: str, body: dict, model: str) -> tuple[dict, RateInfo]:
        p = {k: v for k, v in body.items() if v is not None}
        p["model"] = model
        r = await self._request("POST", f"{self.base_url}/embeddings", self._headers(secret), p)
        try:
            return r.json(), _rate_from_headers(r.headers)
        except ValueError as e:
            raise UpstreamError("server", 502, "upstream returned invalid JSON") from e

    async def chat_stream(self, secret: str, body: dict, model: str) -> UpstreamStream:
        resp = await self._open_stream(f"{self.base_url}/chat/completions", self._headers(secret), self._payload(body, model, True))

        async def it() -> AsyncIterator[dict]:
            try:
                async for line in resp.aiter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        yield json.loads(data)
                    except ValueError:
                        continue
            except httpx.TimeoutException as e:
                raise UpstreamError("timeout", 504, "upstream stream timed out") from e
            except httpx.HTTPError as e:
                raise UpstreamError("network", 502, f"upstream stream error: {type(e).__name__}") from e

        return UpstreamStream(resp, it(), _rate_from_headers(resp.headers))
