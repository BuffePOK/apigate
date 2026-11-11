"""Session-based API used by the web UI: auth, account, API keys, conversations, chat."""
from __future__ import annotations

import json
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..credits import UC, to_credits
from ..errors import GateError
from ..tokens import est_text
from .deps import client_ip, cookie_name, gw_of, session_of, session_principal, sse

router = APIRouter(prefix="/api")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Strict):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordIn(Strict):
    current: str = Field(max_length=256)
    new: str = Field(max_length=256)


class KeyIn(Strict):
    name: str = Field(min_length=1, max_length=80)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


class ConvIn(Strict):
    title: str | None = Field(default=None, max_length=120)
    model: str | None = Field(default=None, max_length=200)
    system_prompt: str | None = Field(default=None, max_length=20000)


class MsgIn(Strict):
    content: str = Field(min_length=1, max_length=200_000)
    model: str | None = Field(default=None, max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)


def public_user(u: dict) -> dict:
    return {"id": u["id"], "username": u["username"], "role": u["role"], "unlimited": bool(u.get("unlimited")),
            "balance_credits": to_credits(u.get("balance_uc", 0)), "active": bool(u.get("active", 1))}


def _set_cookie(gw, response: Response, sid: str) -> None:
    response.set_cookie(cookie_name(gw), sid, max_age=gw.settings.session_ttl_s, httponly=True, samesite="strict",
                        secure=gw.settings.cookie_secure, path="/")


# ----------------------------------------------------------------------- auth
@router.get("/public")
async def public(request: Request):
    gw = gw_of(request)
    return {"signup_enabled": bool(gw.rt("allow_signup")), "name": "apigate"}


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, response: Response):
    gw = gw_of(request)
    ip = client_ip(request)
    user, sid, csrf = await gw.auth.login(body.username, body.password, ip, request.headers.get("user-agent", ""))
    _set_cookie(gw, response, sid)
    await gw.db.audit(user["id"], "login", user["username"], ip)
    return {"user": public_user(user), "csrf": csrf}


@router.post("/auth/signup")
async def signup(body: LoginIn, request: Request, response: Response):
    gw = gw_of(request)
    ip = client_ip(request)
    if not gw.rt("allow_signup"):
        raise GateError(403, "signup_disabled", "Self-registration is disabled. Ask an administrator for an account.")
    if gw.auth.signup_throttle.blocked_for(ip):
        raise GateError(429, "too_many_attempts", "Too many sign-ups from this address", retry_after=600)
    gw.auth.signup_throttle.fail(ip)
    user = await gw.auth.create_user(body.username, body.password, "user")
    _, sid, csrf = await gw.auth.login(body.username, body.password, ip, request.headers.get("user-agent", ""))
    _set_cookie(gw, response, sid)
    await gw.db.audit(user["id"], "signup", user["username"], ip)
    return {"user": public_user(user), "csrf": csrf}


@router.get("/auth/me")
async def me(request: Request):
    s = await session_of(request)
    u = await gw_of(request).auth.get_user(s.user_id)
    return {"user": public_user(u), "csrf": s.csrf}  # type: ignore[arg-type]


@router.post("/auth/logout")
async def logout(request: Request, response: Response):
    s = await session_of(request)
    gw = gw_of(request)
    await gw.auth.logout(s.sid)
    response.delete_cookie(cookie_name(gw), path="/")
    return {"ok": True}


@router.post("/auth/password")
async def change_password(body: PasswordIn, request: Request):
    from ..crypto import verify_password
    import asyncio
    s = await session_of(request)
    gw = gw_of(request)
    row = await gw.db.fetchone("SELECT pw_hash FROM users WHERE id=?", (s.user_id,))
    if not row or not await asyncio.to_thread(verify_password, row["pw_hash"], body.current):
        raise GateError(403, "wrong_password", "Current password is incorrect")
    await gw.auth.set_password(s.user_id, body.new, keep_session=s.sid)
    await gw.db.audit(s.user_id, "password_change", s.username, client_ip(request))
    return {"ok": True}


# -------------------------------------------------------------------- account
@router.get("/me/ledger")
async def my_ledger(request: Request, limit: int = 50, before: int | None = None):
    s = await session_of(request)
    rows = await gw_of(request).credits.ledger(s.user_id, limit, before)
    return {"entries": [_ledger_public(r) for r in rows]}


def _ledger_public(r: dict) -> dict:
    return {
        "id": r["id"], "ts": r["ts"], "kind": r["kind"], "delta_credits": r["delta_uc"] / UC, "balance_credits": r["balance_after"] / UC,
        "cost_credits": (r["cost_uc"] or 0) / UC, "source": r["source"], "provider": r["provider"], "model": r["model"],
        "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"], "note": r["note"],
    }


@router.get("/me/api-keys")
async def my_keys(request: Request):
    s = await session_of(request)
    return {"keys": await gw_of(request).auth.list_api_keys(s.user_id)}


@router.post("/me/api-keys")
async def create_my_key(body: KeyIn, request: Request):
    s = await session_of(request)
    gw = gw_of(request)
    exp = time.time() + body.expires_days * 86400 if body.expires_days else None
    plain, rec = await gw.auth.create_api_key(s.user_id, body.name, s.user_id, expires_at=exp, max_keys=gw.settings.max_api_keys_per_user)
    await gw.db.audit(s.user_id, "api_key_create", f"{rec['id']}:{rec['prefix']}", client_ip(request))
    return {"key": plain, "info": rec, "notice": "Copy this key now. It will never be shown again."}


@router.delete("/me/api-keys/{key_id}")
async def revoke_my_key(key_id: int, request: Request):
    s = await session_of(request)
    gw = gw_of(request)
    if not await gw.auth.revoke_api_key(key_id, s.user_id):
        raise GateError(404, "key_not_found", "No such key")
    await gw.db.audit(s.user_id, "api_key_revoke", str(key_id), client_ip(request))
    return {"ok": True}


@router.get("/models")
async def models_for_chat(request: Request):
    s = await session_of(request)
    gw = gw_of(request)
    data = [m for m in gw.public_models(session_principal(gw, s)) if m["kind"] == "chat"]
    return {"models": data}


# --------------------------------------------------------------- conversations
async def _conv(gw, s, cid: str) -> dict:
    c = await gw.db.fetchone("SELECT * FROM conversations WHERE id=? AND user_id=?", (cid, s.user_id))
    if not c:
        raise GateError(404, "conversation_not_found", "No such conversation")
    return c


@router.get("/conversations")
async def list_convs(request: Request):
    s = await session_of(request)
    rows = await gw_of(request).db.fetchall(
        "SELECT id,title,model,updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT 300", (s.user_id,))
    return {"conversations": rows}


@router.post("/conversations")
async def new_conv(body: ConvIn, request: Request):
    s = await session_of(request)
    gw = gw_of(request)
    n = await gw.db.fetchone("SELECT COUNT(*) AS n FROM conversations WHERE user_id=?", (s.user_id,))
    if n and n["n"] >= 1000:
        raise GateError(400, "too_many_conversations", "Delete some old conversations first")
    cid, now = secrets.token_urlsafe(12), time.time()
    await gw.db.execute("INSERT INTO conversations(id,user_id,title,model,system_prompt,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (cid, s.user_id, body.title or "New chat", body.model, body.system_prompt, now, now))
    return {"id": cid}


@router.get("/conversations/{cid}")
async def get_conv(cid: str, request: Request):
    s = await session_of(request)
    gw = gw_of(request)
    c = await _conv(gw, s, cid)
    msgs = await gw.db.fetchall("SELECT id,role,content,model,created_at FROM messages WHERE conv_id=? ORDER BY id", (cid,))
    return {"conversation": c, "messages": msgs}


@router.patch("/conversations/{cid}")
async def patch_conv(cid: str, body: ConvIn, request: Request):
    s = await session_of(request)
    gw = gw_of(request)
    await _conv(gw, s, cid)
    sets, vals = [], []
    for f in ("title", "model", "system_prompt"):
        v = getattr(body, f)
        if f in body.model_fields_set:
            sets.append(f"{f}=?")
            vals.append(v)
    if sets:
        await gw.db.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id=?", (*vals, cid))
    return {"ok": True}


@router.delete("/conversations/{cid}")
async def del_conv(cid: str, request: Request):
    s = await session_of(request)
    gw = gw_of(request)
    await _conv(gw, s, cid)
    await gw.db.execute("DELETE FROM conversations WHERE id=?", (cid,))
    return {"ok": True}


MAX_CONTEXT_TOKENS = 120_000


@router.post("/conversations/{cid}/messages")
async def chat_message(cid: str, body: MsgIn, request: Request):
    s = await session_of(request)
    gw = gw_of(request)
    conv = await _conv(gw, s, cid)
    model = body.model or conv["model"]
    if not model:
        raise GateError(400, "model_required", "Pick a model first", param="model")
    history = await gw.db.fetchall("SELECT role,content FROM messages WHERE conv_id=? ORDER BY id DESC LIMIT 400", (cid,))
    history.reverse()
    history.append({"role": "user", "content": body.content})
    budget, kept = MAX_CONTEXT_TOKENS, []
    for m in reversed(history):                      # keep the newest turns that fit
        t = est_text(m["content"]) + 4
        if budget - t < 0 and kept:
            break
        budget -= t
        kept.append(m)
    kept.reverse()
    messages = ([{"role": "system", "content": conv["system_prompt"]}] if conv["system_prompt"] else []) + kept
    req: dict = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
    if body.temperature is not None:
        req["temperature"] = body.temperature
    res = await gw.chat(session_principal(gw, s), req, stream=True)   # raises 402/429/... as JSON before streaming starts

    now = time.time()
    await gw.db.execute("INSERT INTO messages(conv_id,role,content,created_at) VALUES(?,?,?,?)", (cid, "user", body.content, now))
    if conv["title"] == "New chat":
        await gw.db.execute("UPDATE conversations SET title=? WHERE id=?", (body.content.strip().replace("\n", " ")[:48] or "New chat", cid))
    await gw.db.execute("UPDATE conversations SET model=? WHERE id=?", (model, cid))

    async def events():
        parts: list[str] = []
        failed = ""
        saved = False
        try:
            yield sse({"meta": {"model": res.meta["model"]}})
            async for chunk in res.stream:  # type: ignore[union-attr]
                if "error" in chunk:
                    failed = chunk["error"].get("message", "stream failed")
                    yield sse({"error": failed})
                    break
                for ch in chunk.get("choices") or []:
                    d = (ch.get("delta") or {}).get("content")
                    if d:
                        parts.append(d)
                        yield sse({"delta": d})
            text = "".join(parts)
            if text:
                mid = await _save_assistant(gw, cid, text, res.meta["model"])
                saved = True
                yield sse({"done": True, "message_id": mid})
            elif not failed:
                yield sse({"done": True})
        finally:
            await res.stream.aclose()  # type: ignore[union-attr]
            if parts and not saved:   # client left mid-way: keep what was generated
                gw._bg(_save_assistant(gw, cid, "".join(parts) + "\n\n[stopped]", res.meta["model"]))

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def _save_assistant(gw, cid: str, text: str, model: str) -> int:
    now = time.time()

    def f(c):
        cur = c.execute("INSERT INTO messages(conv_id,role,content,model,created_at) VALUES(?,?,?,?,?)", (cid, "assistant", text, model, now))
        c.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, cid))
        return cur.lastrowid

    return await gw.db.transaction(f)
