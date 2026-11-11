"""Pure-ASGI middlewares (safe with streaming responses)."""
from __future__ import annotations

import json
import secrets

from starlette.datastructures import MutableHeaders

from ..errors import GateError

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
    "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


class SecurityHeaders:
    def __init__(self, app, hsts: bool = False):
        self.app, self.hsts = app, hsts

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = "req_" + secrets.token_hex(8)
        scope.setdefault("state", {})["request_id"] = rid
        path = scope.get("path", "")

        async def wrapped(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                h["X-Content-Type-Options"] = "nosniff"
                h["X-Frame-Options"] = "DENY"
                h["Referrer-Policy"] = "no-referrer"
                h["Cross-Origin-Opener-Policy"] = "same-origin"
                h["Cross-Origin-Resource-Policy"] = "same-origin"
                h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                h["Content-Security-Policy"] = CSP
                h.setdefault("x-request-id", rid)
                if self.hsts:
                    h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
                if path.startswith(("/api/", "/v1/")):
                    h["Cache-Control"] = "no-store"
            await send(message)

        await self.app(scope, receive, wrapped)


class BodyLimit:
    """Rejects oversized bodies (Content-Length up front, and while streaming for chunked uploads)."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for k, v in scope["headers"]:
            if k == b"content-length":
                try:
                    if int(v) > self.max:
                        return await self._reject(send)
                except ValueError:
                    return await self._reject(send)
        total = 0

        async def limited():
            nonlocal total
            msg = await receive()
            if msg["type"] == "http.request":
                total += len(msg.get("body", b""))
                if total > self.max:
                    raise GateError(413, "request_too_large", f"Request body exceeds {self.max} bytes")
            return msg

        await self.app(scope, limited, send)

    async def _reject(self, send):
        body = json.dumps(GateError(413, "request_too_large", f"Request body exceeds {self.max} bytes").body()).encode()
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
