"""Gateway: the composition root and the request pipeline.

request -> resolve model -> permission -> caller throttle -> credit HOLD ->
  loop { pick key by strategy -> reserve limits -> call upstream -> on failure cool down + try next key } ->
  SETTLE (limits + credits ledger)
"""
from __future__ import annotations

import asyncio
import logging
import math
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .auth import Auth, Principal
from .catalog import ModelRegistry, PriceTable, ROUTABLE_KINDS
from .config import Settings
from .config_ops import ConfigOps
from .credits import Credits
from .crypto import Vault, redactor, scrub
from .db import Database
from .errors import GateError, UpstreamError
from .limits import LimitEngine, LimitRule, Reservation, SlidingWindow, Verdict
from .pool import STRATEGIES, KeyState, Pool
from .providers import DEFAULT_PROVIDERS, Provider
from .tokens import est_chat, est_embedding, est_text

log = logging.getLogger("apigate")

RT_TYPES: dict[str, type] = {
    "allow_signup": bool, "default_strategy": str, "fill_threshold": float, "default_price_in": float, "default_price_out": float,
    "max_queue_wait_s": float, "assumed_output_tokens": int, "default_max_tokens": int, "max_attempts": int,
}


@dataclass
class _Gate:
    win: SlidingWindow = field(default_factory=lambda: SlidingWindow(60))
    inflight: int = 0


@dataclass
class _Selected:
    result: Any
    key: KeyState
    provider: str
    model: str
    res: Reservation
    pool: Pool


@dataclass
class _Call:
    pr: Principal
    rid: str
    t0: float
    name: str
    kind: str
    est_prompt: int
    hold_id: str | None
    gate: tuple[tuple, _Gate]


@dataclass
class Result:
    body: dict | None = None
    stream: "StreamHandle | None" = None
    meta: dict = field(default_factory=dict)


class Gateway(ConfigOps):
    def __init__(self, settings: Settings | None = None, *, transport: httpx.AsyncBaseTransport | None = None, clock=time.time):
        self.settings = settings or Settings.load()
        self.settings.data_path.mkdir(parents=True, exist_ok=True)
        self.db = Database(self.settings.db_path)
        self.vault = Vault.load(self.settings.master_key, self.settings.data_path)
        self.engine = LimitEngine(clock)
        self.clock = clock
        self.pools: dict[str, Pool] = {}
        self.provider_rows: dict[str, dict] = {}
        self.providers: dict[str, Provider] = {}
        self.registry = ModelRegistry()
        self.prices = PriceTable(self.settings.default_price_in, self.settings.default_price_out)
        self.credits = Credits(self.db)
        s = self.settings
        self.auth = Auth(self.db, self.vault, s.session_ttl_s, s.password_min_length, s.login_max_failures, s.login_window_s)
        self.http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(connect=s.connect_timeout_s, read=s.read_timeout_s, write=30.0, pool=30.0),
            limits=httpx.Limits(max_connections=1000, max_keepalive_connections=200),
            follow_redirects=False,  # never forward Authorization headers to a redirect target
        )
        self._gates: dict[tuple, _Gate] = {}
        self._tasks: set[asyncio.Task] = set()
        self._open_streams: set[StreamHandle] = set()
        self._loops: list[asyncio.Task] = []
        self._rt: dict[str, str] = {}
        self._cfg_version = -1
        self.started = False
        self._reload_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

    # ================================================================ lifecycle
    async def start(self, *, background: bool = True) -> None:
        if self.started:
            return
        await self.db.init()
        await self._check_canary()
        if not await self.db.fetchone("SELECT 1 AS x FROM providers LIMIT 1"):
            for name, kind, url in DEFAULT_PROVIDERS:
                await self.db.execute("INSERT INTO providers(name,kind,base_url) VALUES(?,?,?)", (name, kind, url))
        n = await self.credits.release_all()
        if n:
            log.warning("refunded %d stale credit holds from a previous run", n)
        await self.reload(force=True)
        self.started = True
        if background:
            self._loops = [asyncio.create_task(self._loop(f, p), name=f.__name__) for f, p in (
                (self._flush_tick, 5), (self._watch_tick, 3), (self._housekeeping_tick, 60))]
            self._loops.append(asyncio.create_task(self._refresh_loop(), name="refresh_models"))

    async def stop(self) -> None:
        for t in self._loops:
            t.cancel()
        await asyncio.gather(*self._loops, return_exceptions=True)
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=10)
        try:
            await self.flush_usage()
            await self.auth.flush_last_used()
        finally:
            await self.http.aclose()
            await self.db.close()
        self.started = False

    async def _check_canary(self) -> None:
        r = await self.db.fetchone("SELECT value FROM meta WHERE key='canary'")
        if r is None:
            await self.db.execute("INSERT INTO meta(key,value) VALUES('canary', ?)", (self.vault.encrypt("apigate", "canary"),))
            return
        try:
            assert self.vault.decrypt(r["value"], "canary") == "apigate"
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("Master key does not match this database (wrong APIGATE_MASTER_KEY / master.key?). Refusing to start.") from e

    async def _loop(self, fn, period: float) -> None:
        while True:
            await asyncio.sleep(period)
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("background task %s failed", fn.__name__)

    async def _refresh_loop(self) -> None:
        await asyncio.sleep(1)
        try:
            await self.refresh_models()
        except Exception:  # noqa: BLE001
            log.exception("initial model refresh failed")
        while True:
            await asyncio.sleep(max(60, self.settings.models_refresh_s))
            try:
                await self.refresh_models()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("model refresh failed")

    async def _flush_tick(self) -> None:
        await self.flush_usage()
        await self.auth.flush_last_used()

    async def _watch_tick(self) -> None:
        if await self.db.config_version() != self._cfg_version:
            await self.reload()

    async def _housekeeping_tick(self) -> None:
        await self.auth.purge_expired()
        now = self.clock()
        for k in [k for k, g in self._gates.items() if g.inflight == 0 and g.win.used(now) == 0]:
            self._gates.pop(k, None)
        await self.db.execute("DELETE FROM usage_minute WHERE minute < ?", (int(now // 60) - 8 * 1440,))
        mono = time.monotonic()
        for h in [h for h in self._open_streams if not h.started and mono - h.created > 30]:
            h.close_bg()  # response never started (client vanished): settle with zero usage

    def _bg(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)

        def done(task: asyncio.Task) -> None:
            self._tasks.discard(task)
            if not task.cancelled() and task.exception():
                log.error("background job failed: %r", task.exception())

        t.add_done_callback(done)

    # =============================================================== config load
    def rt(self, name: str):
        typ = RT_TYPES[name]
        if name in self._rt:
            v = self._rt[name]
            return (v.lower() in ("1", "true", "yes", "on")) if typ is bool else typ(v)
        return getattr(self.settings, name)

    async def reload(self, force: bool = False) -> None:
        async with self._reload_lock:
            db = self.db
            ver = await db.config_version()
            if ver == self._cfg_version and not force:
                return
            prov = await db.fetchall("SELECT * FROM providers")
            keys = await db.fetchall("SELECT * FROM provider_keys ORDER BY id")
            mk = await db.fetchall("SELECT provider, model_id, key_id FROM model_keys")
            models = await db.fetchall("SELECT * FROM models")
            rules = await db.fetchall("SELECT * FROM limit_rules")
            aliases = await db.fetchall("SELECT * FROM aliases")
            prices = await db.fetchall("SELECT * FROM prices")
            st = await db.fetchall("SELECT key, value FROM settings")
            self._rt = {r["key"]: r["value"] for r in st if r["key"] in RT_TYPES}
            self.provider_rows = {p["name"]: p for p in prov}

            import json
            from .providers import build
            self.providers = {}
            for p in prov:
                if p["enabled"]:
                    self.providers[p["name"]] = build(p["kind"], p["name"], p["base_url"], self.http, json.loads(p["extra"] or "{}"))

            models_by_key: dict[int, set[str]] = {}
            for r in mk:
                models_by_key.setdefault(r["key_id"], set()).add(r["model_id"])
            seen: set[int] = set()
            for p in prov:
                pool = self.pools.setdefault(p["name"], Pool(p["name"]))
                pool.keys = {k: v for k, v in pool.keys.items() if k in {r["id"] for r in keys}}
            for name in list(self.pools):
                if name not in self.provider_rows:
                    del self.pools[name]
            for r in keys:
                pool = self.pools.get(r["provider"])
                if pool is None:
                    continue
                ks = pool.keys.get(r["id"])
                if ks is None:
                    ks = pool.keys[r["id"]] = KeyState(r["id"], r["provider"], r["label"], "")
                try:
                    ks.secret = self.vault.decrypt(r["secret"], f"pk:{r['id']}:{r['provider']}")
                    redactor.register(ks.secret)
                except Exception:  # noqa: BLE001
                    ks.secret, ks.enabled, ks.status, ks.note = "", False, "auth_failed", "cannot decrypt (master key changed?)"
                    log.error("provider key %s (%s) cannot be decrypted", r["id"], r["label"])
                    continue
                ks.label, ks.fingerprint, ks.enabled, ks.priority = r["label"], r["fingerprint"], bool(r["enabled"]), r["priority"]
                ks.status, ks.note = r["status"], r["status_note"]
                ks.models = models_by_key.get(r["id"], set())
                ks.models_known = bool(ks.models)
                seen.add(r["id"])

            self.registry.load(list(self.providers), models, mk, aliases)
            self.prices.load(prices, self.rt("default_price_in"), self.rt("default_price_out"))
            changed = self.engine.set_rules([LimitRule.from_row(r) for r in rules])
            first = self._cfg_version < 0
            self._cfg_version = ver
            if changed or first:
                await self._rebuild_windows()

    async def _rebuild_windows(self) -> None:
        await self.flush_usage()
        since = int(self.clock() // 60) - 7 * 1440
        rows = await self.db.fetchall("SELECT * FROM usage_minute WHERE minute >= ?", (since,))
        owner = {k.id: k.provider for p in self.pools.values() for k in p.keys.values()}
        self.engine.rebuild(rows, owner)

    async def flush_usage(self) -> None:
        rows = self.engine.drain_pending()
        if not rows:
            return

        def f(c):
            c.execute("BEGIN")
            c.executemany(
                "INSERT INTO usage_minute(provider_key_id,model,minute,requests,tokens) VALUES(?,?,?,?,?) "
                "ON CONFLICT(provider_key_id,model,minute) DO UPDATE SET requests=requests+excluded.requests, tokens=tokens+excluded.tokens",
                rows)
            c.execute("COMMIT")

        await self.db.run(f)

    # ================================================================ throttles
    def _enter_gate(self, pr: Principal) -> tuple[tuple, _Gate]:
        s = self.settings
        if pr.source == "internal":
            return (("i", 0), _Gate())
        rpm = pr.rpm if pr.rpm is not None else (s.api_key_rpm if pr.source == "api" else s.chat_user_rpm)
        conc = pr.max_concurrent if pr.max_concurrent is not None else (s.api_key_max_concurrent if pr.source == "api" else s.chat_user_max_concurrent)
        gid = pr.gate_id
        g = self._gates.setdefault(gid, _Gate())
        now = self.clock()
        if rpm and g.win.used(now) >= rpm:
            raise GateError(429, "rate_limit_exceeded", f"Rate limit: {rpm} requests per minute", retry_after=g.win.wait_for(now, 1, rpm) or 1)
        if conc and g.inflight >= conc:
            raise GateError(429, "too_many_concurrent_requests", f"Too many concurrent requests (max {conc})", retry_after=1)
        g.win.add(now, 1)
        g.inflight += 1
        return gid, g

    @staticmethod
    def _exit_gate(gate: tuple[tuple, _Gate]) -> None:
        gate[1].inflight = max(gate[1].inflight - 1, 0)

    # =============================================================== public API
    def internal_principal(self, username: str = "internal") -> Principal:
        return Principal(user_id=None, username=username, role="admin", source="internal")

    async def chat(self, pr: Principal, body: dict, *, stream: bool = False) -> Result:
        return await self._execute(pr, body, "chat", stream)

    async def embeddings(self, pr: Principal, body: dict) -> Result:
        return await self._execute(pr, body, "embedding", False)

    def public_models(self, pr: Principal | None = None) -> list[dict]:
        now = self.clock()
        out = []
        for e in self.registry.entries.values():
            if not e.enabled or e.kind not in ROUTABLE_KINDS or e.provider not in self.providers:
                continue
            if not self._model_servable(e.provider, e.id, now):
                continue
            if pr and not self._allowed(pr, e.public_id, [(e.provider, e.id)]):
                continue
            out.append({"id": e.public_id, "object": "model", "created": 0, "owned_by": e.provider, "kind": e.kind, "context_window": e.context_window})
        for a in self.registry.aliases.values():
            tg = [t for t in (self.registry._resolve_one(x, "chat", True) for x in a.targets) if t]
            if tg and (not pr or self._allowed(pr, a.name, tg)):
                out.append({"id": a.name, "object": "model", "created": 0, "owned_by": "gateway", "kind": "chat", "context_window": None, "alias_of": a.targets})
        out.sort(key=lambda m: m["id"])
        return out

    def _model_servable(self, provider: str, model: str, now: float) -> bool:
        pool = self.pools.get(provider)
        if not pool:
            return False
        for k in pool.keys.values():
            if k.enabled and k.status == "ok" and (not k.models_known or model in k.models) and self.engine.key_can_serve(provider, k.id, model):
                return True
        return False

    @staticmethod
    def _allowed(pr: Principal, name: str, targets: list[tuple[str, str]]) -> bool:
        if pr.allowed_models is None:
            return True
        import fnmatch
        pats = pr.allowed_models
        cands = [name] + [f"{p}/{m}" for p, m in targets]
        return any(fnmatch.fnmatchcase(c, pat) for c in cands for pat in pats)

    def _strategy(self, provider: str) -> tuple[str, float]:
        row = self.provider_rows.get(provider) or {}
        strat = row.get("strategy") or self.rt("default_strategy")
        if strat not in STRATEGIES:
            strat = "round_robin"
        thr = row.get("fill_threshold") or self.rt("fill_threshold")
        return strat, float(thr)

    # ============================================================== the pipeline
    async def _execute(self, pr: Principal, body: dict, kind: str, stream: bool) -> Result:
        t0 = time.monotonic()
        rid = "req_" + secrets.token_hex(8)
        name = body.get("model")
        targets = self.registry.resolve(name, kind)  # type: ignore[arg-type]
        if not self._allowed(pr, name, targets):  # type: ignore[arg-type]
            raise GateError(403, "model_not_allowed", f"This API key may not use model '{name}'", param="model")
        est_prompt = est_chat(body) if kind == "chat" else est_embedding(body)
        max_out = int(body.get("max_completion_tokens") or body.get("max_tokens") or 0) if kind == "chat" else 0
        reserve = est_prompt + ((max_out or self.rt("assumed_output_tokens")) if kind == "chat" else 0)
        hold_out = (max_out or self.rt("default_max_tokens")) if kind == "chat" else 0
        hold_uc = max(self.prices.cost_uc(p, m, est_prompt, hold_out) for p, m in targets)

        gate = self._enter_gate(pr)
        hold_id: str | None = None
        try:
            if pr.user_id is not None:
                hold_id = await self.credits.hold(pr.user_id, hold_uc)
            sel = await self._select_and_call(body, kind, targets, reserve, stream)
        except BaseException as e:
            self._exit_gate(gate)
            await self._refund(hold_id, e)
            raise

        call = _Call(pr, rid, t0, name, kind, est_prompt, hold_id, gate)  # type: ignore[arg-type]
        public_name = name if name not in self.registry.aliases else f"{sel.provider}/{sel.model}"
        meta = {"request_id": rid, "provider": sel.provider, "model": f"{sel.provider}/{sel.model}"}
        if stream:
            return Result(stream=self._open_stream_handle(call, sel, body, public_name), meta=meta)
        try:
            out = sel.result[0]
            pt, ct = self._usage_of(out, kind, est_prompt)
            out["model"] = public_name
            await self._finish(call, sel, pt, ct)
        except BaseException as e:
            self.engine.release(sel.res)
            self._exit_gate(gate)
            await self._refund(hold_id, e)
            raise
        return Result(body=out, meta=meta)

    async def _refund(self, hold_id: str | None, exc: BaseException) -> None:
        if not hold_id:
            return
        if isinstance(exc, asyncio.CancelledError):
            self._bg(self.credits.release(hold_id))   # we are being cancelled: awaiting here would be cancelled too
        else:
            await self.credits.release(hold_id)

    @staticmethod
    def _usage_of(out: dict, kind: str, est_prompt: int) -> tuple[int, int]:
        u = out.get("usage") or {}
        pt = int(u.get("prompt_tokens") or 0) or est_prompt
        if kind != "chat":
            return pt, 0
        ct = int(u.get("completion_tokens") or 0)
        tt = int(u.get("total_tokens") or 0)
        if not u:
            text = "".join((c.get("message") or {}).get("content") or "" for c in out.get("choices", []))
            ct = est_text(text) if text else 1
        if tt > pt + ct:        # reasoning tokens etc. are billed as output
            ct = tt - pt
        return pt, ct

    async def _finish(self, call: _Call, sel: _Selected, pt: int, ct: int, *, error: str = "") -> None:
        """Settle limits + credits. Never raises into the client path for bookkeeping hiccups."""
        self.engine.settle(sel.res, pt + ct)
        self._exit_gate(call.gate)
        cost = self.prices.cost_uc(sel.provider, sel.model, pt, ct)
        lat = int((time.monotonic() - call.t0) * 1000)
        try:
            if call.pr.user_id is not None:
                await self.credits.settle(
                    call.hold_id, call.pr.user_id, cost, source=call.pr.source, api_key_id=call.pr.api_key_id,
                    provider=sel.provider, model=sel.model, provider_key_id=sel.key.id, prompt_tokens=pt,
                    completion_tokens=ct, request_id=call.rid, latency_ms=lat)
        except Exception:  # noqa: BLE001
            log.exception("credit settlement failed for %s", call.rid)
            self._bg(self.credits.release(call.hold_id))
        log.info("%s user=%s src=%s %s/%s key=%s in=%d out=%d cost_uc=%d %dms%s", call.rid, call.pr.username, call.pr.source,
                 sel.provider, sel.model, sel.key.label, pt, ct, cost, lat, f" ERR={error}" if error else "")

    def _open_stream_handle(self, call: _Call, sel: _Selected, body: dict, public_name: str) -> "StreamHandle":
        h = StreamHandle(self, call, sel, body, public_name)
        self._open_streams.add(h)
        return h

    async def _close_stream(self, h: "StreamHandle") -> None:
        sel, call = h.sel, h.call
        await sel.result.aclose()
        if h.usage:
            pt = int(h.usage.get("prompt_tokens") or 0) or call.est_prompt
            ct = int(h.usage.get("completion_tokens") or 0)
            tt = int(h.usage.get("total_tokens") or 0)
            if tt > pt + ct:
                ct = tt - pt
        else:
            pt, ct = call.est_prompt, (int(h.chars / 3.2) + 1 if h.chars else 0)
        if not h.err:
            sel.pool.on_success(sel.key, sel.model)
        await self._finish(call, sel, pt, ct, error=h.err)

    # ------------------------------------------------------------ key selection
    async def _select_and_call(self, body: dict, kind: str, targets: list[tuple[str, str]], reserve: int, stream: bool) -> _Selected:
        tried: set[tuple[int, str]] = set()
        attempts = 0
        max_attempts = max(1, self.rt("max_attempts"))
        deadline = self.clock() + self.rt("max_queue_wait_s")
        last_err: UpstreamError | None = None
        saw_candidate = False
        transient: dict[tuple[int, str], int] = {}
        while True:
            min_wait = math.inf
            blocked_any = False
            retry_now = False
            for prov_name, model_id in targets:
                pool, provider = self.pools.get(prov_name), self.providers.get(prov_name)
                if not pool or not provider:
                    continue
                now = self.clock()
                cands = [k for k in pool.candidates(model_id, now, tried) if self.engine.key_can_serve(prov_name, k.id, model_id)]
                if not cands:
                    continue
                saw_candidate = True
                strat, thr = self._strategy(prov_name)
                ordered = pool.order(cands, model_id, strat, thr, lambda k: self.engine.fill_ratio(prov_name, k.id, model_id))
                for key in ordered:
                    res = self.engine.acquire(prov_name, key.id, model_id, reserve)
                    if isinstance(res, Verdict):
                        if not res.impossible:
                            blocked_any = True
                            min_wait = min(min_wait, res.wait)
                        continue
                    tried.add((key.id, model_id))
                    attempts += 1
                    pool.mark_used(key, model_id, now)
                    try:
                        if kind == "embedding":
                            out = await provider.embeddings(key.secret, body, model_id)
                        elif stream:
                            out = await provider.chat_stream(key.secret, body, model_id)
                        else:
                            out = await provider.chat(key.secret, body, model_id)
                    except UpstreamError as e:
                        last_err = e
                        self._apply_policy(pool, key, model_id, e, res)
                        if not e.retryable:
                            raise self._client_error(e) from None
                        if attempts >= max_attempts:
                            break
                        # transient 5xx / network blips ("high demand"): retry the same key briefly
                        tk = (key.id, model_id)
                        if e.kind in ("server", "network", "timeout") and transient.get(tk, 0) < 2 and attempts < max_attempts:
                            transient[tk] = transient.get(tk, 0) + 1
                            tried.discard(tk)
                            await asyncio.sleep(0.4 * 2 ** (transient[tk] - 1))
                            retry_now = True
                            break
                        continue
                    except BaseException:
                        self.engine.release(res)
                        raise
                    pool.on_success(key, model_id)
                    rate = out.rate if stream else out[1]
                    if rate.exhausted_until and rate.exhausted_until > self.clock():
                        key.model_cooldown[model_id] = rate.exhausted_until
                    return _Selected(out, key, prov_name, model_id, res, pool)
                if attempts >= max_attempts:
                    break
            if attempts >= max_attempts:
                break
            if retry_now:
                continue
            if blocked_any and math.isfinite(min_wait) and self.clock() + min_wait <= deadline:
                await asyncio.sleep(min_wait + 0.02)
                continue
            break
        raise self._exhausted_error(targets, last_err, saw_candidate, blocked_any, min_wait, reserve)

    def _apply_policy(self, pool: Pool, key: KeyState, model: str, e: UpstreamError, res: Reservation | None = None) -> None:
        now = self.clock()
        k = e.kind
        if k == "rate_limit":
            if res:
                self.engine.settle(res, 0)
            cd = pool.on_rate_limited(key, model, e.retry_after, now)
            log.warning("429 from %s key=%s model=%s -> cooldown %.0fs", key.provider, key.label, model, cd)
            return
        if res:
            if k == "quota":
                self.engine.settle(res, 0)
            else:
                self.engine.release(res)
        if k == "quota":
            pool.on_quota(key, now)
        elif k == "auth":
            pool.on_auth_failed(key, e.message)
            self._bg(self._persist_key_status(key.id, "auth_failed", e.message))
            log.error("provider key %s (%s) rejected by upstream: disabled until an admin re-enables it", key.id, key.label)
        elif k == "permission":
            pool.on_model_unavailable(key, model, now, 3600, "permission denied")
        elif k == "not_found":
            pool.on_model_unavailable(key, model, now, 1800, "model not found on this key")
        elif k in ("server", "network", "timeout"):
            pool.on_server_error(key, now, e.message)

    async def _persist_key_status(self, key_id: int, status: str, note: str) -> None:
        await self.db.execute("UPDATE provider_keys SET status=?, status_note=? WHERE id=?", (status, scrub(note)[:200], key_id))

    @staticmethod
    def _client_error(e: UpstreamError) -> GateError:
        status = e.status if 400 <= e.status < 500 and e.status not in (401, 403, 404, 429) else 400
        return GateError(status, "upstream_rejected", scrub(e.message) or "The upstream provider rejected the request")

    def _exhausted_error(self, targets, last_err, saw_candidate, blocked, min_wait, reserve) -> GateError:
        now = self.clock()
        if last_err is not None:
            if last_err.kind in ("rate_limit", "quota"):
                return GateError(429, "rate_limit_exceeded", "All upstream keys for this model are rate limited. Try again shortly.", retry_after=last_err.retry_after or 5)
            return GateError(502, "upstream_error", f"Upstream error: {scrub(last_err.message)}")
        if saw_candidate and blocked and math.isfinite(min_wait):
            return GateError(429, "rate_limit_exceeded", "All upstream keys for this model are at capacity. Try again shortly.", retry_after=max(1.0, math.ceil(min_wait)))
        if saw_candidate:
            return GateError(400, "request_too_large", f"This request (~{reserve} tokens) exceeds the per-minute token limit of every available key for this model.")
        waits = []
        for prov, model in targets:
            pool = self.pools.get(prov)
            for k in (pool.keys.values() if pool else []):
                if k.enabled and k.status == "ok":
                    w = max(k.cooldown_until, k.model_cooldown.get(model, 0.0)) - now
                    if w > 0:
                        waits.append(w)
        if waits:
            return GateError(429, "rate_limit_exceeded", "All upstream keys for this model are cooling down after upstream limits.", retry_after=max(1.0, math.ceil(min(waits))))
        return GateError(503, "no_available_key", "No upstream key is currently available for this model.")


class StreamHandle:
    """An open upstream stream. Settles exactly once, whether it finishes, errors, is cancelled, or never starts."""

    def __init__(self, gw: Gateway, call: _Call, sel: _Selected, body: dict, public_name: str):
        self.gw, self.call, self.sel = gw, call, sel
        self.public_name = public_name
        self.include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        self.usage: dict | None = None
        self.chars = 0
        self.err = ""
        self.started = False
        self.closed = False
        self.created = time.monotonic()

    def __aiter__(self) -> AsyncIterator[dict]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[dict]:
        self.started = True
        try:
            async for chunk in self.sel.result:
                if chunk.get("usage"):
                    self.usage = chunk["usage"]
                choices = chunk.get("choices") or []
                for ch in choices:
                    d = ch.get("delta") or {}
                    self.chars += len(d.get("content") or "") + len(d.get("reasoning_content") or "")
                    for tc in d.get("tool_calls") or []:
                        self.chars += len((tc.get("function") or {}).get("arguments") or "")
                if not choices and not self.include_usage:
                    continue
                chunk["model"] = self.public_name
                yield chunk
        except UpstreamError as e:
            self.err = e.message
            self.gw._apply_policy(self.sel.pool, self.sel.key, self.sel.model, e)
            yield {"error": {"message": f"Upstream stream failed: {scrub(e.message)}", "type": "server_error", "code": "upstream_error"}}
        except (asyncio.CancelledError, GeneratorExit):
            self.err = "client disconnected"
            raise
        finally:
            self.close_bg()

    def close_bg(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.gw._open_streams.discard(self)
        self.gw._bg(self.gw._close_stream(self))

    async def aclose(self) -> None:
        self.close_bg()
