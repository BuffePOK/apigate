"""Request helpers: client IP, cookie sessions with CSRF, bearer API keys."""
from __future__ import annotations

import hmac
import ipaddress
import json
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import Request

from ..auth import Principal
from ..core import Gateway
from ..errors import GateError

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def gw_of(request: Request) -> Gateway:
    return request.app.state.gw


def _is_trusted(ip: str, trusted: set[str]) -> bool:
    """`trusted` holds single IPs or CIDR ranges (e.g. 172.16.0.0/12 for a docker network)."""
    if ip in trusted:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for t in trusted:
        if "/" in t:
            try:
                if addr in ipaddress.ip_network(t, strict=False):
                    return True
            except ValueError:
                continue
    return False


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    trusted = gw_of(request).settings.proxies
    if _is_trusted(peer, trusted):
        xff = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
        for ip in reversed(xff):
            if not _is_trusted(ip, trusted):
                return ip
    return peer


def cookie_name(gw: Gateway) -> str:
    return "__Host-gate_session" if gw.settings.cookie_secure else "gate_session"


@dataclass
class Sess:
    sid: str
    csrf: str
    user_id: int
    username: str
    role: str
    unlimited: bool
    balance_uc: int

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _check_origin(request: Request) -> None:
    gw = gw_of(request)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise GateError(403, "csrf", "Cross-site request blocked")
    origin = request.headers.get("origin")
    if origin and origin != "null":
        allowed = {request.headers.get("host", "")}
        if gw.settings.public_origin:
            allowed.add(urlparse(gw.settings.public_origin).netloc)
        if urlparse(origin).netloc not in allowed:
            raise GateError(403, "csrf", "Origin not allowed")
    elif origin == "null":
        raise GateError(403, "csrf", "Origin not allowed")


async def session_of(request: Request, *, admin: bool = False, csrf: bool | None = None) -> Sess:
    gw = gw_of(request)
    sid = request.cookies.get(cookie_name(gw), "")
    row = await gw.auth.get_session(sid)
    if row is None:
        raise GateError(401, "not_authenticated", "Please sign in", type_="authentication_error")
    if csrf is None:
        csrf = request.method in UNSAFE
    if csrf:
        _check_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf"]):
            raise GateError(403, "csrf", "Missing or invalid CSRF token")
    s = Sess(sid, row["csrf"], row["user_id"], row["username"], row["role"], bool(row["unlimited"]), row["balance_uc"])
    if admin and not s.is_admin:
        raise GateError(403, "forbidden", "Administrator access required", type_="permission_error")
    return s


async def bearer_principal(request: Request) -> Principal:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or len(auth) < 15:
        raise GateError(401, "missing_api_key", "You must provide an API key: Authorization: Bearer sk-gate-...", type_="authentication_error")
    return await gw_of(request).auth.verify_api_key(auth[7:].strip(), client_ip(request))


def session_principal(gw: Gateway, s: Sess) -> Principal:
    return Principal(user_id=s.user_id, username=s.username, role=s.role, source="chat")


async def read_json(request: Request) -> dict:
    try:
        body = await request.json()
    except GateError:
        raise
    except (ValueError, UnicodeDecodeError):
        raise GateError(400, "invalid_json", "Request body must be valid JSON") from None
    if not isinstance(body, dict):
        raise GateError(400, "invalid_json", "Request body must be a JSON object")
    return body


def sse(obj: dict | str) -> bytes:
    data = obj if isinstance(obj, str) else json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
    return f"data: {data}\n\n".encode()
