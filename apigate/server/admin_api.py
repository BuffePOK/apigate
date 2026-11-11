"""Administrator API (cookie session + CSRF, role=admin). API keys can never reach these routes."""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from ..credits import UC, to_uc
from ..errors import GateError
from ..pool import STRATEGIES
from .deps import client_ip, gw_of, session_of
from .user_api import _ledger_public, public_user

router = APIRouter(prefix="/api/admin")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserIn(Strict):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    role: str = "user"
    initial_credits: float = Field(default=0, ge=0, le=1e9)


class UserPatch(Strict):
    active: bool | None = None
    role: str | None = None
    unlimited: bool | None = None
    password: str | None = Field(default=None, max_length=256)


class CreditsIn(Strict):
    credits: float = Field(ge=-1e9, le=1e9)
    note: str = Field(default="", max_length=300)


class AdminKeyIn(Strict):
    name: str = Field(min_length=1, max_length=80)
    expires_days: int | None = Field(default=None, ge=1, le=3650)
    rpm: int | None = Field(default=None, ge=1, le=1_000_000)
    max_concurrent: int | None = Field(default=None, ge=1, le=10_000)
    allowed_models: list[str] | None = Field(default=None, max_length=100)


class ProviderIn(Strict):
    name: str
    kind: str
    base_url: str = Field(max_length=300)
    extra: dict | None = None


class ProviderPatch(Strict):
    base_url: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None
    strategy: str | None = None
    fill_threshold: float | None = None
    extra: dict | None = None


class KeyAdd(Strict):
    label: str = Field(max_length=80)
    secret: str = Field(max_length=1024)
    verify: bool = True
    priority: int = Field(default=100, ge=0, le=100000)


class KeyPatch(Strict):
    label: str | None = Field(default=None, max_length=80)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=100000)
    reset_status: bool = False


class ModelPatch(Strict):
    enabled: bool


class ModelAdd(Strict):
    provider: str
    model_id: str = Field(max_length=200)
    kind: str = "chat"


class LimitIn(Strict):
    id: int | None = None
    provider: str
    key_id: int | None = None
    pattern: str = Field(default="*", max_length=200)
    scope: str = "model"
    limits: dict[str, int | None]
    note: str = Field(default="", max_length=200)


class AliasIn(Strict):
    targets: list[str] = Field(min_length=1, max_length=20)
    mode: str = "fallback"


class PriceIn(Strict):
    id: int | None = None
    provider: str = "*"
    pattern: str = Field(default="*", max_length=200)
    price_in: float = Field(ge=0, le=1e9)
    price_out: float = Field(ge=0, le=1e9)


class SettingIn(Strict):
    value: str | int | float | bool


async def admin(request: Request):
    s = await session_of(request, admin=True)
    return s, gw_of(request), client_ip(request)


# ------------------------------------------------------------------ overview
@router.get("/overview")
async def overview(request: Request):
    _, gw, _ = await admin(request)
    since = time.time() - 86400
    by_model = await gw.db.fetchall(
        "SELECT provider, model, COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens),0) AS completion_tokens, COALESCE(SUM(cost_uc),0) AS cost_uc "
        "FROM ledger WHERE kind='usage' AND ts>? GROUP BY provider, model ORDER BY requests DESC LIMIT 50", (since,))
    totals = await gw.db.fetchone(
        "SELECT (SELECT COUNT(*) FROM users) AS users, (SELECT COALESCE(SUM(balance_uc),0) FROM users) AS balances, "
        "(SELECT COUNT(*) FROM ledger WHERE kind='usage' AND ts>?) AS requests_24h", (since,))
    for r in by_model:
        r["cost_credits"] = r.pop("cost_uc") / UC
    return {**gw.status(), "usage_24h": by_model, "totals": {"users": totals["users"], "credits_outstanding": totals["balances"] / UC,
                                                              "requests_24h": totals["requests_24h"]}}


# --------------------------------------------------------------------- users
@router.get("/users")
async def users(request: Request):
    _, gw, _ = await admin(request)
    rows = await gw.db.fetchall("SELECT id,username,role,active,unlimited,balance_uc,created_at,last_login_at FROM users ORDER BY id")
    return {"users": [{**public_user(r), "created_at": r["created_at"], "last_login_at": r["last_login_at"]} for r in rows]}


@router.post("/users")
async def create_user(body: UserIn, request: Request):
    s, gw, ip = await admin(request)
    u = await gw.auth.create_user(body.username, body.password, body.role)
    if body.initial_credits:
        await gw.credits.adjust(u["id"], to_uc(body.initial_credits), s.user_id, "initial credits", "grant")
    await gw.db.audit(s.user_id, "user_create", u["username"], ip, f"role={body.role}")
    return {"id": u["id"]}


async def _last_admin_guard(gw, uid: int, new_active: bool | None, new_role: str | None) -> None:
    losing = (new_active is False) or (new_role == "user")
    if not losing:
        return
    u = await gw.auth.get_user(uid)
    if u and u["role"] == "admin" and u["active"]:
        n = await gw.db.fetchone("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1 AND id<>?", (uid,))
        if n["n"] == 0:
            raise GateError(400, "last_admin", "You cannot remove the last active administrator")


@router.patch("/users/{uid}")
async def patch_user(uid: int, body: UserPatch, request: Request):
    s, gw, ip = await admin(request)
    if not await gw.auth.get_user(uid):
        raise GateError(404, "user_not_found", "No such user")
    if body.role is not None and body.role not in ("admin", "user"):
        raise GateError(400, "invalid_role", "role must be admin or user")
    await _last_admin_guard(gw, uid, body.active, body.role)
    sets, vals = [], []
    for f in ("active", "role", "unlimited"):
        v = getattr(body, f)
        if v is not None:
            sets.append(f"{f}=?")
            vals.append(int(v) if isinstance(v, bool) else v)
    if sets:
        await gw.db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*vals, uid))
    if body.active is False:
        await gw.auth.deactivate_effects(uid)       # kills sessions, drops cached API keys
    elif body.role is not None or body.active is True:
        gw.auth._key_cache.clear()
    if body.password:
        await gw.auth.set_password(uid, body.password)
    await gw.db.audit(s.user_id, "user_update", str(uid), ip, body.model_dump_json(exclude={"password"}))
    return {"ok": True}


@router.post("/users/{uid}/credits")
async def grant(uid: int, body: CreditsIn, request: Request):
    s, gw, ip = await admin(request)
    bal = await gw.credits.adjust(uid, to_uc(body.credits), s.user_id, body.note or "admin adjustment", "grant" if body.credits >= 0 else "adjust")
    await gw.db.audit(s.user_id, "credits", str(uid), ip, f"{body.credits:+} {body.note}")
    return {"balance_credits": bal / UC}


@router.get("/users/{uid}/ledger")
async def user_ledger(uid: int, request: Request, limit: int = 100):
    _, gw, _ = await admin(request)
    return {"entries": [_ledger_public(r) for r in await gw.credits.ledger(uid, limit)]}


@router.get("/api-keys")
async def all_keys(request: Request):
    _, gw, _ = await admin(request)
    return {"keys": await gw.auth.list_api_keys(None)}


@router.post("/users/{uid}/api-keys")
async def admin_create_key(uid: int, body: AdminKeyIn, request: Request):
    s, gw, ip = await admin(request)
    if not await gw.auth.get_user(uid):
        raise GateError(404, "user_not_found", "No such user")
    exp = time.time() + body.expires_days * 86400 if body.expires_days else None
    plain, rec = await gw.auth.create_api_key(uid, body.name, s.user_id, expires_at=exp, rpm=body.rpm, max_concurrent=body.max_concurrent,
                                              allowed_models=body.allowed_models, max_keys=gw.settings.max_api_keys_per_user)
    await gw.db.audit(s.user_id, "api_key_create_for_user", f"{uid}:{rec['prefix']}", ip)
    return {"key": plain, "info": rec, "notice": "Copy this key now. It will never be shown again."}


@router.delete("/api-keys/{key_id}")
async def admin_revoke_key(key_id: int, request: Request):
    s, gw, ip = await admin(request)
    if not await gw.auth.revoke_api_key(key_id):
        raise GateError(404, "key_not_found", "No such key")
    await gw.db.audit(s.user_id, "api_key_revoke", str(key_id), ip)
    return {"ok": True}


# ----------------------------------------------------------------- providers
@router.post("/providers")
async def add_provider(body: ProviderIn, request: Request):
    s, gw, ip = await admin(request)
    await gw.add_provider(body.name, body.kind, body.base_url, body.extra)
    await gw.db.audit(s.user_id, "provider_add", body.name, ip, body.base_url)
    return {"ok": True}


@router.patch("/providers/{name}")
async def patch_provider(name: str, body: ProviderPatch, request: Request):
    s, gw, ip = await admin(request)
    await gw.update_provider(name, **{k: getattr(body, k) for k in body.model_fields_set})
    await gw.db.audit(s.user_id, "provider_update", name, ip, body.model_dump_json(exclude_none=True))
    return {"ok": True}


@router.delete("/providers/{name}")
async def delete_provider(name: str, request: Request):
    s, gw, ip = await admin(request)
    await gw.delete_provider(name)
    await gw.db.audit(s.user_id, "provider_delete", name, ip)
    return {"ok": True}


@router.post("/providers/{name}/keys")
async def add_key(name: str, body: KeyAdd, request: Request):
    s, gw, ip = await admin(request)
    out = await gw.add_provider_key(name, body.label, body.secret, verify=body.verify, priority=body.priority)
    await gw.db.audit(s.user_id, "provider_key_add", f"{name}:{out['id']}", ip, body.label)
    return out


@router.post("/refresh-models")
async def refresh_all(request: Request, provider: str | None = None):
    s, gw, ip = await admin(request)
    out = await gw.refresh_models(provider)
    await gw.db.audit(s.user_id, "models_refresh", provider or "*", ip)
    return {"summary": out}


@router.patch("/keys/{kid}")
async def patch_key(kid: int, body: KeyPatch, request: Request):
    s, gw, ip = await admin(request)
    await gw.update_provider_key(kid, label=body.label, enabled=body.enabled, priority=body.priority, reset_status=body.reset_status)
    await gw.db.audit(s.user_id, "provider_key_update", str(kid), ip, body.model_dump_json(exclude_none=True))
    return {"ok": True}


@router.delete("/keys/{kid}")
async def delete_key(kid: int, request: Request):
    s, gw, ip = await admin(request)
    await gw.delete_provider_key(kid)
    await gw.db.audit(s.user_id, "provider_key_delete", str(kid), ip)
    return {"ok": True}


# -------------------------------------------------------------------- models
@router.get("/models")
async def all_models(request: Request):
    _, gw, _ = await admin(request)
    out = []
    for e in sorted(gw.registry.entries.values(), key=lambda e: (e.provider, e.id)):
        pin, pout = gw.prices.price(e.provider, e.id)
        out.append({"provider": e.provider, "id": e.id, "kind": e.kind, "enabled": e.enabled, "manual": e.manual,
                    "context_window": e.context_window, "keys": len(e.key_ids), "price_in": pin, "price_out": pout,
                    "servable": gw._model_servable(e.provider, e.id, gw.clock())})
    return {"models": out}


@router.patch("/models/{provider}/{model_id:path}")
async def patch_model(provider: str, model_id: str, body: ModelPatch, request: Request):
    s, gw, ip = await admin(request)
    await gw.set_model_enabled(provider, model_id, body.enabled)
    await gw.db.audit(s.user_id, "model_enable" if body.enabled else "model_disable", f"{provider}/{model_id}", ip)
    return {"ok": True}


@router.post("/models")
async def add_model(body: ModelAdd, request: Request):
    s, gw, ip = await admin(request)
    await gw.add_manual_model(body.provider, body.model_id, body.kind)
    await gw.db.audit(s.user_id, "model_add", f"{body.provider}/{body.model_id}", ip)
    return {"ok": True}


@router.delete("/models/{provider}/{model_id:path}")
async def del_model(provider: str, model_id: str, request: Request):
    s, gw, ip = await admin(request)
    await gw.delete_manual_model(provider, model_id)
    await gw.db.audit(s.user_id, "model_delete", f"{provider}/{model_id}", ip)
    return {"ok": True}


# -------------------------------------------------------------------- limits
@router.get("/limits")
async def limits(request: Request):
    _, gw, _ = await admin(request)
    return {"rules": [{"id": r.id, "provider": r.provider, "key_id": r.key_id, "pattern": r.pattern, "scope": r.scope, "limits": r.limits, "note": r.note}
                      for r in gw.engine.rules]}


@router.put("/limits")
async def put_limit(body: LimitIn, request: Request):
    s, gw, ip = await admin(request)
    rid = await gw.set_limit_rule(body.id, body.provider, body.key_id, body.pattern, body.scope, body.limits, body.note)
    await gw.db.audit(s.user_id, "limit_set", str(rid), ip, f"{body.provider} {body.pattern} {body.limits}")
    return {"id": rid}


@router.delete("/limits/{rid}")
async def del_limit(rid: int, request: Request):
    s, gw, ip = await admin(request)
    await gw.delete_limit_rule(rid)
    await gw.db.audit(s.user_id, "limit_delete", str(rid), ip)
    return {"ok": True}


# ------------------------------------------------------------ aliases / prices
@router.get("/aliases")
async def aliases(request: Request):
    _, gw, _ = await admin(request)
    return {"aliases": [{"name": a.name, "targets": a.targets, "mode": a.mode} for a in gw.registry.aliases.values()]}


@router.put("/aliases/{name}")
async def put_alias(name: str, body: AliasIn, request: Request):
    s, gw, ip = await admin(request)
    await gw.set_alias(name, body.targets, body.mode)
    await gw.db.audit(s.user_id, "alias_set", name, ip, ",".join(body.targets))
    return {"ok": True}


@router.delete("/aliases/{name}")
async def del_alias(name: str, request: Request):
    s, gw, ip = await admin(request)
    await gw.delete_alias(name)
    await gw.db.audit(s.user_id, "alias_delete", name, ip)
    return {"ok": True}


@router.get("/prices")
async def prices(request: Request):
    _, gw, _ = await admin(request)
    return {"prices": gw.prices.rows, "default": {"price_in": gw.prices.default[0], "price_out": gw.prices.default[1]}}


@router.put("/prices")
async def put_price(body: PriceIn, request: Request):
    s, gw, ip = await admin(request)
    pid = await gw.set_price(body.id, body.provider, body.pattern, body.price_in, body.price_out)
    await gw.db.audit(s.user_id, "price_set", str(pid), ip, f"{body.provider} {body.pattern} {body.price_in}/{body.price_out}")
    return {"id": pid}


@router.delete("/prices/{pid}")
async def del_price(pid: int, request: Request):
    s, gw, ip = await admin(request)
    await gw.delete_price(pid)
    await gw.db.audit(s.user_id, "price_delete", str(pid), ip)
    return {"ok": True}


# ------------------------------------------------------- settings and audit
@router.get("/settings")
async def get_settings(request: Request):
    _, gw, _ = await admin(request)
    from ..core import RT_TYPES
    return {"settings": {k: gw.rt(k) for k in RT_TYPES}, "strategies": list(STRATEGIES)}


@router.put("/settings/{name}")
async def put_setting(name: str, body: SettingIn, request: Request):
    s, gw, ip = await admin(request)
    await gw.set_rt(name, body.value)
    await gw.db.audit(s.user_id, "setting", name, ip, str(body.value))
    return {"ok": True}


@router.get("/audit")
async def audit(request: Request, limit: int = 200):
    _, gw, _ = await admin(request)
    rows = await gw.db.fetchall(
        "SELECT a.id, a.ts, a.action, a.target, a.ip, a.detail, u.username AS actor FROM audit a LEFT JOIN users u ON u.id=a.actor_id "
        "ORDER BY a.id DESC LIMIT ?", (max(1, min(limit, 1000)),))
    return {"entries": rows}
