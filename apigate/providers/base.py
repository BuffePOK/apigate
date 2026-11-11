"""Provider adapter interface. Every adapter speaks OpenAI format on its public side."""
from __future__ import annotations

import ipaddress
import json
import re
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from ..crypto import scrub
from ..errors import UpstreamError


@dataclass
class ModelInfo:
    id: str
    kind: str = "chat"            # chat | embedding | image | audio | video | moderation | other
    context_window: int | None = None


@dataclass
class RateInfo:
    """What response headers told us about remaining capacity."""
    exhausted_until: float | None = None   # wall-clock time; None = nothing known


class UpstreamStream:
    """An opened (status 200) upstream stream yielding OpenAI chunk dicts."""

    def __init__(self, resp: httpx.Response | None, it: AsyncIterator[dict], rate: RateInfo):
        self._resp = resp
        self._it = it
        self.rate = rate

    def __aiter__(self) -> AsyncIterator[dict]:
        return self._it

    async def aclose(self) -> None:
        try:
            await self._it.aclose()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        if self._resp is not None:
            try:
                await self._resp.aclose()
            except Exception:  # noqa: BLE001
                pass


class Provider:
    kind = "base"
    supports_embeddings = False

    def __init__(self, name: str, base_url: str, client: httpx.AsyncClient, extra: dict | None = None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.client = client
        self.extra = extra or {}

    async def list_models(self, secret: str) -> list[ModelInfo]:
        raise NotImplementedError

    async def chat(self, secret: str, body: dict, model: str) -> tuple[dict, RateInfo]:
        raise NotImplementedError

    async def chat_stream(self, secret: str, body: dict, model: str) -> UpstreamStream:
        raise NotImplementedError

    async def embeddings(self, secret: str, body: dict, model: str) -> tuple[dict, RateInfo]:
        raise UpstreamError("bad_request", 400, f"provider '{self.name}' has no embeddings endpoint")

    # ---- shared plumbing ----------------------------------------------
    async def _request(self, method: str, url: str, headers: dict, json_body: dict | None = None) -> httpx.Response:
        try:
            resp = await self.client.request(method, url, headers=headers, json=json_body)
        except httpx.TimeoutException as e:
            raise UpstreamError("timeout", 504, f"upstream timeout: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise UpstreamError("network", 502, f"upstream connection error: {type(e).__name__}") from e
        if resp.status_code >= 400:
            raise map_http_error(resp.status_code, resp.text, resp.headers)
        return resp

    async def _open_stream(self, url: str, headers: dict, json_body: dict) -> httpx.Response:
        req = self.client.build_request("POST", url, headers=headers, json=json_body)
        try:
            resp = await self.client.send(req, stream=True)
        except httpx.TimeoutException as e:
            raise UpstreamError("timeout", 504, f"upstream timeout: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise UpstreamError("network", 502, f"upstream connection error: {type(e).__name__}") from e
        if resp.status_code >= 400:
            try:
                text = (await resp.aread()).decode("utf-8", "replace")
            finally:
                await resp.aclose()
            raise map_http_error(resp.status_code, text, resp.headers)
        return resp


# --------------------------------------------------------------------------
_DUR = re.compile(r"(?:(\d+(?:\.\d+)?)(ms|h|m|s))")


def parse_duration(s: str | None) -> float | None:
    """'1s', '6m0s', '20ms', '1.5s' or plain seconds -> seconds."""
    if not s:
        return None
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    parts = _DUR.findall(s)
    if not parts:
        return None
    mult = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}
    return sum(float(v) * mult[u] for v, u in parts)


def parse_retry_after(headers: httpx.Headers, body: str = "") -> float | None:
    ra = headers.get("retry-after") or headers.get("retry-after-ms")
    if ra:
        try:
            v = float(ra)
            return v / 1000 if headers.get("retry-after") is None else v
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(ra).timestamp() - time.time())
            except Exception:  # noqa: BLE001
                pass
    # Google style: "retryDelay": "34s"
    m = re.search(r'"retryDelay"\s*:\s*"([^"]+)"', body)
    if m:
        return parse_duration(m.group(1))
    m = re.search(r"retry in ([0-9.]+)s", body, re.I)
    if m:
        return float(m.group(1))
    return None


def extract_message(text: str) -> str:
    try:
        j = json.loads(text)
        if isinstance(j, list) and j:
            j = j[0]
        err = j.get("error", j) if isinstance(j, dict) else {}
        if isinstance(err, dict):
            return str(err.get("message") or err.get("msg") or text)[:500]
        return str(err)[:500]
    except Exception:  # noqa: BLE001
        return text[:300]


def map_http_error(status: int, text: str, headers: httpx.Headers) -> UpstreamError:
    msg = scrub(extract_message(text))
    low = text.lower()
    ra = parse_retry_after(headers, text)
    if status == 429:
        if "insufficient_quota" in low or "billing" in low and "exceeded" not in low:
            return UpstreamError("quota", status, msg, retry_after=ra)
        return UpstreamError("rate_limit", status, msg, retry_after=ra)
    if status == 402:
        return UpstreamError("quota", status, msg)
    if status == 401:
        return UpstreamError("auth", status, msg)
    if status == 403:
        return UpstreamError("permission", status, msg)
    if status == 404:
        return UpstreamError("not_found", status, msg)
    if status in (408, 504):
        return UpstreamError("timeout", status, msg)
    if status >= 500 or status == 529:
        return UpstreamError("server", status, msg, retry_after=ra)
    return UpstreamError("bad_request", status, msg)


def classify_model(model_id: str) -> str:
    m = model_id.lower()
    if "embed" in m:
        return "embedding"
    if "moderation" in m or "guard" in m:
        return "moderation"
    if any(t in m for t in ("whisper", "tts", "transcribe", "speech", "audio", "lyria", "realtime", "live")):
        return "audio"
    if any(t in m for t in ("dall-e", "imagen", "gpt-image", "-image", "nano-banana", "flux", "stable-diffusion")):
        return "image"
    if any(t in m for t in ("veo", "sora", "video")):
        return "video"
    return "chat"


def validate_base_url(url: str, allow_insecure: bool = False) -> str:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("base_url must be http(s)://host/...")
    if u.username or u.password:
        raise ValueError("base_url must not contain credentials")
    host = u.hostname
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_link_local or str(ip) == "169.254.169.254"):
        raise ValueError("link-local / metadata addresses are not allowed")
    loopback = host == "localhost" or (ip is not None and ip.is_loopback)
    if u.scheme == "http" and not loopback and not allow_insecure:
        raise ValueError("http:// upstreams are only allowed for loopback (or set allow_insecure_upstream)")
    return url.rstrip("/")
