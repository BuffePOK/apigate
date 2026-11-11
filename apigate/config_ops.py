"""Admin-side configuration operations (providers, keys, limits, aliases, prices, models).
Mixed into Gateway. Every mutation bumps `config_version`, so other processes (CLI) and the
running server pick the change up."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from .catalog import ROUTABLE_KINDS
from .crypto import redactor, scrub
from .errors import GateError, UpstreamError
from .limits import clean_limits
from .pool import STRATEGIES
from .providers import KINDS, ModelInfo, validate_base_url

log = logging.getLogger("apigate")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ALIAS_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _clean_secret(s: str) -> str:
    if not isinstance(s, str):
        raise GateError(400, "invalid_secret", "key must be a string")
    s = s.strip()
    if not (8 <= len(s) <= 1024) or any(c.isspace() or ord(c) < 32 for c in s):
        raise GateError(400, "invalid_secret", "That does not look like an API key (8-1024 chars, no whitespace)")
    return s


PROVIDER_ALIASES = {"claude": "anthropic", "google": "gemini", "gpt": "openai", "chatgpt": "openai", "grok": "xai"}


class ConfigOps:
    # attributes provided by Gateway
    db: object
    vault: object

    def resolve_provider(self, name: str) -> str:
        """Accept friendly names (claude -> anthropic) and fail with the list of valid providers."""
        n = (name or "").strip().lower()
        n = PROVIDER_ALIASES.get(n, n) if n not in self.provider_rows else n  # type: ignore[attr-defined]
        if n not in self.provider_rows:  # type: ignore[attr-defined]
            raise GateError(404, "provider_not_found", f"No such provider '{name}'. Available: {', '.join(sorted(self.provider_rows))}")  # type: ignore[attr-defined]
        return n

    async def _changed(self) -> None:
        await self.db.bump_config()  # type: ignore[attr-defined]
        await self.reload()  # type: ignore[attr-defined]

    # ------------------------------------------------------------- providers
    async def add_provider(self, name: str, kind: str, base_url: str, extra: dict | None = None) -> None:
        if not NAME_RE.match(name or ""):
            raise GateError(400, "invalid_name", "Provider name: lowercase letters, digits, - _ (max 32)")
        if kind not in KINDS:
            raise GateError(400, "invalid_kind", f"kind must be one of {sorted(KINDS)}")
        try:
            base_url = validate_base_url(base_url, self.settings.allow_insecure_upstream)  # type: ignore[attr-defined]
        except ValueError as e:
            raise GateError(400, "invalid_base_url", str(e)) from e
        try:
            await self.db.execute("INSERT INTO providers(name,kind,base_url,extra) VALUES(?,?,?,?)", (name, kind, base_url, json.dumps(extra or {})))  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            if "UNIQUE" in str(e):
                raise GateError(409, "provider_exists", "Provider already exists") from e
            raise
        await self._changed()

    async def update_provider(self, name: str, **f) -> None:
        if name not in self.provider_rows:  # type: ignore[attr-defined]
            raise GateError(404, "provider_not_found", "No such provider")
        sets, vals = [], []
        if f.get("base_url") is not None:
            try:
                sets.append("base_url=?"); vals.append(validate_base_url(f["base_url"], self.settings.allow_insecure_upstream))  # type: ignore[attr-defined]
            except ValueError as e:
                raise GateError(400, "invalid_base_url", str(e)) from e
        if f.get("enabled") is not None:
            sets.append("enabled=?"); vals.append(1 if f["enabled"] else 0)
        if "strategy" in f:
            if f["strategy"] not in (None, "") and f["strategy"] not in STRATEGIES:
                raise GateError(400, "invalid_strategy", f"strategy must be one of {STRATEGIES}")
            sets.append("strategy=?"); vals.append(f["strategy"] or None)
        if "fill_threshold" in f:
            v = f["fill_threshold"]
            if v is not None and not (0.05 <= float(v) <= 1.0):
                raise GateError(400, "invalid_threshold", "fill_threshold must be between 0.05 and 1.0")
            sets.append("fill_threshold=?"); vals.append(float(v) if v is not None else None)
        if f.get("extra") is not None:
            if not isinstance(f["extra"], dict):
                raise GateError(400, "invalid_extra", "extra must be an object")
            sets.append("extra=?"); vals.append(json.dumps(f["extra"]))
        if sets:
            await self.db.execute(f"UPDATE providers SET {', '.join(sets)} WHERE name=?", (*vals, name))  # type: ignore[attr-defined]
            await self._changed()

    async def delete_provider(self, name: str) -> None:
        if name not in self.provider_rows:  # type: ignore[attr-defined]
            raise GateError(404, "provider_not_found", "No such provider")
        await self.db.execute("DELETE FROM providers WHERE name=?", (name,))  # type: ignore[attr-defined]
        await self._changed()

    # ---------------------------------------------------------- provider keys
    async def add_provider_key(self, provider: str, label: str, secret: str, *, verify: bool = True, priority: int = 100) -> dict:
        provider = self.resolve_provider(provider)
        label = (label or "").strip()[:80] or "key"
        secret = _clean_secret(secret)
        digest = self.vault.digest("pk:" + secret)  # type: ignore[attr-defined]
        if await self.db.fetchone("SELECT id FROM provider_keys WHERE fp_digest=?", (digest,)):  # type: ignore[attr-defined]
            raise GateError(409, "duplicate_key", "This key is already registered")
        models: list[ModelInfo] | None = None
        redactor.register(secret)        # upstream error text must never echo this key back to the admin
        if verify:
            prov = self.providers.get(provider)  # type: ignore[attr-defined]
            if prov is None:
                raise GateError(400, "provider_disabled", "Provider is disabled")
            try:
                models = await prov.list_models(secret)
            except UpstreamError as e:
                redactor.forget(secret)
                if e.kind in ("auth", "permission"):
                    raise GateError(400, "key_rejected", f"The provider rejected this key: {scrub(e.message)}") from None
                raise GateError(502, "verify_failed", f"Could not verify the key ({scrub(e.message)}). Retry, or add with verify=false.") from None
        fp = "…" + secret[-4:]

        def f(c):
            cur = c.execute(
                "INSERT INTO provider_keys(provider,label,secret,fingerprint,fp_digest,priority,created_at) VALUES(?,?,?,?,?,?,?)",
                (provider, label, b"", fp, digest, priority, time.time()))
            kid = cur.lastrowid
            c.execute("UPDATE provider_keys SET secret=? WHERE id=?", (self.vault.encrypt(secret, f"pk:{kid}:{provider}"), kid))  # type: ignore[attr-defined]
            return kid

        kid = await self.db.transaction(f)  # type: ignore[attr-defined]
        if models is not None:
            await self._store_models(kid, provider, models)
        await self._changed()
        return {"id": kid, "provider": provider, "label": label, "fingerprint": fp, "models": len(models) if models is not None else None}

    async def update_provider_key(self, kid: int, *, label: str | None = None, enabled: bool | None = None,
                                  priority: int | None = None, reset_status: bool = False) -> None:
        sets, vals = [], []
        if label is not None:
            sets.append("label=?"); vals.append(label.strip()[:80] or "key")
        if enabled is not None:
            sets.append("enabled=?"); vals.append(1 if enabled else 0)
        if priority is not None:
            sets.append("priority=?"); vals.append(int(priority))
        if reset_status:
            sets += ["status='ok'", "status_note=NULL"]
        if not sets:
            return
        n = await self.db.execute(f"UPDATE provider_keys SET {', '.join(sets)} WHERE id=?", (*vals, kid))  # type: ignore[attr-defined]
        if not n:
            raise GateError(404, "key_not_found", "No such key")
        if reset_status:
            for pool in self.pools.values():  # type: ignore[attr-defined]
                k = pool.keys.get(kid)
                if k:
                    k.cooldown_until, k.model_cooldown, k.fail_streak = 0.0, {}, 0
        await self._changed()

    async def delete_provider_key(self, kid: int) -> None:
        n = await self.db.execute("DELETE FROM provider_keys WHERE id=?", (kid,))  # type: ignore[attr-defined]
        if not n:
            raise GateError(404, "key_not_found", "No such key")
        await self.db.execute("DELETE FROM usage_minute WHERE provider_key_id=?", (kid,))  # type: ignore[attr-defined]
        await self._changed()

    # ------------------------------------------------------------ models
    async def _store_models(self, kid: int, provider: str, models: list[ModelInfo]) -> None:
        now = time.time()

        def f(c):
            for m in models:
                c.execute(
                    "INSERT INTO models(provider,model_id,kind,enabled,context_window,manual,first_seen,last_seen) VALUES(?,?,?,?,?,0,?,?) "
                    "ON CONFLICT(provider,model_id) DO UPDATE SET last_seen=excluded.last_seen, "
                    "context_window=COALESCE(excluded.context_window, models.context_window), "
                    "kind=CASE WHEN models.manual=1 THEN models.kind ELSE excluded.kind END",
                    (provider, m.id, m.kind, 1 if m.kind in ROUTABLE_KINDS else 0, m.context_window, now, now))
            c.execute("DELETE FROM model_keys WHERE key_id=?", (kid,))
            c.executemany("INSERT OR IGNORE INTO model_keys(provider,model_id,key_id) VALUES(?,?,?)", [(provider, m.id, kid) for m in models])

        await self.db.transaction(f)  # type: ignore[attr-defined]

    async def refresh_models(self, provider: str | None = None, key_id: int | None = None) -> dict:
        """Ask every healthy key which models it can use. Returns a per-provider summary."""
        if provider:
            provider = self.resolve_provider(provider)
        async with self._refresh_lock:  # type: ignore[attr-defined]
            sem = asyncio.Semaphore(4)
            summary: dict[str, dict] = {}

            async def one(prov_name: str, key) -> None:
                s = summary.setdefault(prov_name, {"keys_ok": 0, "keys_failed": 0, "models": 0, "errors": []})
                async with sem:
                    try:
                        models = await self.providers[prov_name].list_models(key.secret)  # type: ignore[attr-defined]
                    except UpstreamError as e:
                        s["keys_failed"] += 1
                        s["errors"].append(f"{key.label}: {scrub(e.message)[:120]}")
                        key.last_error = f"model listing failed: {e.kind}"
                        if e.kind == "auth":
                            self.pools[prov_name].on_auth_failed(key, e.message)  # type: ignore[attr-defined]
                            await self._persist_key_status(key.id, "auth_failed", e.message)  # type: ignore[attr-defined]
                        return
                    except Exception as e:  # noqa: BLE001
                        s["keys_failed"] += 1
                        s["errors"].append(f"{key.label}: {type(e).__name__}")
                        return
                    await self._store_models(key.id, prov_name, models)
                    s["keys_ok"] += 1
                    s["models"] = max(s["models"], len(models))

            jobs = []
            for pname, pool in self.pools.items():  # type: ignore[attr-defined]
                if (provider and pname != provider) or pname not in self.providers:  # type: ignore[attr-defined]
                    continue
                for key in pool.keys.values():
                    if key.enabled and key.status == "ok" and key.secret and (key_id is None or key.id == key_id):
                        jobs.append(one(pname, key))
            await asyncio.gather(*jobs)
            if jobs:
                await self._changed()
            return summary

    async def set_model_enabled(self, provider: str, model_id: str, enabled: bool) -> None:
        n = await self.db.execute("UPDATE models SET enabled=? WHERE provider=? AND model_id=?", (1 if enabled else 0, provider, model_id))  # type: ignore[attr-defined]
        if not n:
            raise GateError(404, "model_not_found", "No such model")
        await self._changed()

    async def add_manual_model(self, provider: str, model_id: str, kind: str = "chat") -> None:
        if provider not in self.provider_rows:  # type: ignore[attr-defined]
            raise GateError(404, "provider_not_found", "No such provider")
        if kind not in ROUTABLE_KINDS or not model_id or len(model_id) > 200:
            raise GateError(400, "invalid_model", "kind must be chat or embedding; model id required")
        now = time.time()
        await self.db.execute(  # type: ignore[attr-defined]
            "INSERT OR REPLACE INTO models(provider,model_id,kind,enabled,context_window,manual,first_seen,last_seen) VALUES(?,?,?,1,NULL,1,?,?)",
            (provider, model_id, kind, now, now))
        # a manual model has no discovery data: allow every key of the provider
        await self.db.execute("INSERT OR IGNORE INTO model_keys(provider,model_id,key_id) SELECT provider,?,id FROM provider_keys WHERE provider=?", (model_id, provider))  # type: ignore[attr-defined]
        await self._changed()

    async def delete_manual_model(self, provider: str, model_id: str) -> None:
        n = await self.db.execute("DELETE FROM models WHERE provider=? AND model_id=? AND manual=1", (provider, model_id))  # type: ignore[attr-defined]
        if not n:
            raise GateError(404, "model_not_found", "No such manual model")
        await self._changed()

    # ------------------------------------------------------------- limits
    async def set_limit_rule(self, rule_id: int | None, provider: str, key_id: int | None, pattern: str, scope: str,
                             limits: dict, note: str = "") -> int:
        provider = self.resolve_provider(provider)
        if scope not in ("model", "key"):
            raise GateError(400, "invalid_scope", "scope must be model or key")
        pattern = (pattern or "*").strip()
        if not pattern or len(pattern) > 200:
            raise GateError(400, "invalid_pattern", "pattern required (glob, e.g. gemini-*)")
        try:
            lim = clean_limits(limits or {})
        except (ValueError, TypeError) as e:
            raise GateError(400, "invalid_limits", str(e)) from e
        if not lim:
            raise GateError(400, "invalid_limits", "set at least one limit")
        if rule_id:
            n = await self.db.execute(  # type: ignore[attr-defined]
                "UPDATE limit_rules SET provider=?, key_id=?, pattern=?, scope=?, limits=?, note=? WHERE id=?",
                (provider, key_id, pattern, scope, json.dumps(lim), note[:200], rule_id))
            if not n:
                raise GateError(404, "rule_not_found", "No such rule")
        else:
            def f(c):
                return c.execute("INSERT INTO limit_rules(provider,key_id,pattern,scope,limits,note) VALUES(?,?,?,?,?,?)",
                                 (provider, key_id, pattern, scope, json.dumps(lim), note[:200])).lastrowid
            rule_id = await self.db.transaction(f)  # type: ignore[attr-defined]
        await self._changed()
        return rule_id  # type: ignore[return-value]

    async def delete_limit_rule(self, rule_id: int) -> None:
        n = await self.db.execute("DELETE FROM limit_rules WHERE id=?", (rule_id,))  # type: ignore[attr-defined]
        if not n:
            raise GateError(404, "rule_not_found", "No such rule")
        await self._changed()

    # ------------------------------------------------------ aliases / prices
    async def set_alias(self, name: str, targets: list[str], mode: str = "fallback") -> None:
        if not ALIAS_RE.match(name or ""):
            raise GateError(400, "invalid_alias", "Alias: letters, digits, . _ : - (no slash), max 64")
        if mode not in ("fallback", "round_robin", "random"):
            raise GateError(400, "invalid_mode", "mode must be fallback, round_robin or random")
        if not isinstance(targets, list) or not targets or len(targets) > 20:
            raise GateError(400, "invalid_targets", "targets: 1-20 model ids")
        for t in targets:
            if self.registry._resolve_one(str(t), "chat", True) is None:  # type: ignore[attr-defined]
                raise GateError(400, "unknown_target", f"'{t}' is not an enabled chat model (use provider/model)")
        await self.db.execute("INSERT OR REPLACE INTO aliases(name,targets,mode) VALUES(?,?,?)", (name, json.dumps([str(t) for t in targets]), mode))  # type: ignore[attr-defined]
        await self._changed()

    async def delete_alias(self, name: str) -> None:
        n = await self.db.execute("DELETE FROM aliases WHERE name=?", (name,))  # type: ignore[attr-defined]
        if not n:
            raise GateError(404, "alias_not_found", "No such alias")
        await self._changed()

    async def set_price(self, price_id: int | None, provider: str, pattern: str, price_in: float, price_out: float) -> int:
        if price_in < 0 or price_out < 0:
            raise GateError(400, "invalid_price", "prices must be >= 0")
        if provider != "*" and provider not in self.provider_rows:  # type: ignore[attr-defined]
            raise GateError(404, "provider_not_found", "No such provider")
        pattern = (pattern or "*")[:200]
        if price_id:
            n = await self.db.execute("UPDATE prices SET provider=?, pattern=?, price_in=?, price_out=? WHERE id=?", (provider, pattern, price_in, price_out, price_id))  # type: ignore[attr-defined]
            if not n:
                raise GateError(404, "price_not_found", "No such price")
        else:
            def f(c):
                return c.execute("INSERT INTO prices(provider,pattern,price_in,price_out) VALUES(?,?,?,?)", (provider, pattern, price_in, price_out)).lastrowid
            price_id = await self.db.transaction(f)  # type: ignore[attr-defined]
        await self._changed()
        return price_id  # type: ignore[return-value]

    async def delete_price(self, price_id: int) -> None:
        n = await self.db.execute("DELETE FROM prices WHERE id=?", (price_id,))  # type: ignore[attr-defined]
        if not n:
            raise GateError(404, "price_not_found", "No such price")
        await self._changed()

    # ------------------------------------------------------ runtime settings
    async def set_rt(self, name: str, value) -> None:
        from .core import RT_TYPES
        if name not in RT_TYPES:
            raise GateError(400, "unknown_setting", f"unknown setting; allowed: {sorted(RT_TYPES)}")
        typ = RT_TYPES[name]
        try:
            v = (bool(value) if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")) if typ is bool else typ(value)
        except (TypeError, ValueError) as e:
            raise GateError(400, "invalid_value", f"{name} must be {typ.__name__}") from e
        if name == "default_strategy" and v not in STRATEGIES:
            raise GateError(400, "invalid_value", f"strategy must be one of {STRATEGIES}")
        if name == "fill_threshold" and not (0.05 <= v <= 1.0):
            raise GateError(400, "invalid_value", "fill_threshold must be between 0.05 and 1.0")
        if typ in (int, float) and v < 0:
            raise GateError(400, "invalid_value", "must be >= 0")
        await self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (name, str(v)))  # type: ignore[attr-defined]
        await self._changed()

    # ------------------------------------------------------------ reporting
    def status(self) -> dict:
        now = self.clock()  # type: ignore[attr-defined]
        provs = []
        counts: dict[str, int] = {}
        for e in self.registry.entries.values():  # type: ignore[attr-defined]
            counts[e.provider] = counts.get(e.provider, 0) + 1
        for name, row in self.provider_rows.items():  # type: ignore[attr-defined]
            pool = self.pools.get(name)  # type: ignore[attr-defined]
            strat, thr = self._strategy(name)  # type: ignore[attr-defined]
            keys = []
            for k in (pool.keys.values() if pool else []):
                d = k.public(now)
                d["limits"] = self.engine.snapshot_key(k.id, now)  # type: ignore[attr-defined]
                keys.append(d)
            provs.append({
                "name": name, "kind": row["kind"], "base_url": row["base_url"], "enabled": bool(row["enabled"]),
                "strategy": row["strategy"], "effective_strategy": strat, "fill_threshold": row["fill_threshold"], "effective_threshold": thr,
                "models": counts.get(name, 0), "keys": keys,
            })
        return {"providers": provs, "strategies": list(STRATEGIES)}
