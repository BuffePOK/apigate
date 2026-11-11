"""Rate-limit engine.

Metrics: r|t (requests|tokens) + p + s|m|h|d|w  ->  rps rpm rph rpd rpw tps tpm tph tpd tpw, plus `conc`
(in-flight requests). A limit of 0 means "this key cannot serve this model".

Rules are matched per (provider, key, model); for every metric the MOST SPECIFIC
rule wins (key-specific > provider-wide, exact model > glob > '*'). A rule's
`scope` decides how counters are shared:
  model : separate counter per model            (Gemini, OpenAI style)
  key   : one counter shared by all models the rule matches

Windows are bucketed sliding windows (60 buckets) that expire *conservatively*
(a bucket leaves the window only when it is entirely outside). All code runs in
the event loop thread, so check+reserve is atomic without locks.
"""
from __future__ import annotations

import fnmatch
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

WINDOWS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
METRICS = tuple(f"{k}p{w}" for k in "rt" for w in "smhdw") + ("conc",)


def valid_metric(m: str) -> bool:
    return m in METRICS


class SlidingWindow:
    __slots__ = ("span", "res", "b", "total", "limit")

    def __init__(self, span: float):
        self.span = float(span)
        self.res = self.span / 60.0
        self.b: deque[list] = deque()  # [bucket_idx, amount]
        self.total = 0
        self.limit = 0

    def _prune(self, now: float) -> None:
        cutoff = now - self.span
        b, res = self.b, self.res
        while b and (b[0][0] + 1) * res <= cutoff:
            self.total -= b.popleft()[1]

    def used(self, now: float) -> int:
        self._prune(now)
        return max(self.total, 0)

    def add(self, now: float, amount: int) -> None:
        self._prune(now)
        idx = int(now // self.res)
        b = self.b
        if b and b[-1][0] == idx:
            b[-1][1] += amount
        elif not b or idx > b[-1][0]:
            b.append([idx, amount])
        else:
            for i in range(len(b) - 1, -1, -1):
                if b[i][0] == idx:
                    b[i][1] += amount
                    break
                if b[i][0] < idx:
                    b.insert(i + 1, [idx, amount])
                    break
            else:
                b.appendleft([idx, amount])
        self.total += amount

    def adjust(self, ts: float, delta: int, now: float) -> None:
        """Correct an earlier add() made at `ts`; if that bucket already expired, only grow."""
        if delta == 0:
            return
        self._prune(now)
        idx = int(ts // self.res)
        for e in reversed(self.b):
            if e[0] == idx:
                e[1] += delta
                self.total += delta
                return
            if e[0] < idx:
                break
        if delta > 0:
            self.add(now, delta)

    def wait_for(self, now: float, need: int, limit: int) -> float:
        used = self.used(now)
        over = used + need - limit
        if over <= 0:
            return 0.0
        if need > limit:
            return math.inf
        freed = 0
        for idx, amt in self.b:
            freed += amt
            if freed >= over:
                return max(0.0, (idx + 1) * self.res + self.span - now)
        return self.span


@dataclass
class LimitRule:
    id: int
    provider: str
    key_id: int | None
    pattern: str
    scope: str
    limits: dict[str, int]
    note: str = ""

    @property
    def specificity(self) -> tuple[int, int, int]:
        glob = any(c in self.pattern for c in "*?[")
        pat = 0 if self.pattern == "*" else (1 if glob else 2)
        literal = sum(1 for c in self.pattern if c not in "*?[]")   # among globs the one with more literal text wins
        return (1 if self.key_id is not None else 0, pat, literal)

    @classmethod
    def from_row(cls, r: dict) -> "LimitRule":
        return cls(r["id"], r["provider"], r["key_id"], r["pattern"], r["scope"], json.loads(r["limits"]), r.get("note") or "")


def clean_limits(raw: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for k, v in raw.items():
        if v is None or v == "":
            continue
        if not valid_metric(k):
            raise ValueError(f"unknown limit metric: {k}")
        iv = int(v)
        if iv < 0:
            raise ValueError(f"{k} must be >= 0")
        out[k] = iv
    return out


@dataclass(slots=True)
class Verdict:
    ok: bool
    wait: float = 0.0
    impossible: bool = False
    reason: str = ""


@dataclass(slots=True)
class Reservation:
    key_id: int
    provider: str
    model: str
    ts: float
    tokens: int
    req_wins: list = field(default_factory=list)
    tok_wins: list = field(default_factory=list)
    conc: list = field(default_factory=list)
    done: bool = False


class LimitEngine:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.rules: list[LimitRule] = []
        self._eff: dict[tuple, dict] = {}
        self.win: dict[tuple, SlidingWindow] = {}
        self.inflight: dict[tuple, int] = {}
        self._pending: dict[tuple, list] = {}   # (key_id, model, minute) -> [req, tok]
        self.rules_signature = ""

    # ------------------------------------------------------------------ rules
    def set_rules(self, rules: list[LimitRule]) -> bool:
        """Returns True when the rule set changed (caller should rebuild windows)."""
        sig = json.dumps([(r.id, r.provider, r.key_id, r.pattern, r.scope, sorted(r.limits.items())) for r in rules], sort_keys=True)
        changed = sig != self.rules_signature
        self.rules_signature = sig
        self.rules = list(rules)
        self._eff.clear()
        return changed

    def effective(self, provider: str, key_id: int, model: str) -> dict[str, tuple[int, tuple]]:
        ck = (provider, key_id, model)
        hit = self._eff.get(ck)
        if hit is not None:
            return hit
        matching = [
            r for r in self.rules
            if r.provider == provider and (r.key_id is None or r.key_id == key_id) and fnmatch.fnmatchcase(model, r.pattern)
        ]
        matching.sort(key=lambda r: r.specificity, reverse=True)
        out: dict[str, tuple[int, tuple]] = {}
        for r in matching:
            ident = (key_id, ("k", r.id)) if r.scope == "key" else (key_id, ("m", model))
            for m, v in r.limits.items():
                if m not in out:
                    out[m] = (int(v), ident)
        self._eff[ck] = out
        return out

    def key_can_serve(self, provider: str, key_id: int, model: str) -> bool:
        return all(v > 0 for v, _ in self.effective(provider, key_id, model).values())

    # ---------------------------------------------------------------- windows
    def _window(self, ident: tuple, metric: str, limit: int) -> SlidingWindow:
        k = (ident, metric)
        w = self.win.get(k)
        if w is None:
            w = self.win[k] = SlidingWindow(WINDOWS[metric[2]])
        w.limit = limit
        return w

    # ------------------------------------------------------------- admission
    def acquire(self, provider: str, key_id: int, model: str, tokens: int, now: float | None = None) -> Reservation | Verdict:
        now = self.clock() if now is None else now
        eff = self.effective(provider, key_id, model)
        wait = 0.0
        blocked = False
        reason = ""
        for m, (val, ident) in eff.items():
            if val == 0:
                return Verdict(False, math.inf, True, f"{m}=0")
            if m == "conc":
                if self.inflight.get((ident, "conc"), 0) >= val:
                    blocked, wait, reason = True, max(wait, 0.2), "conc"
                continue
            need = 1 if m[0] == "r" else tokens
            if need > val:
                return Verdict(False, math.inf, True, f"request needs {need} > {m}={val}")
            w = self.win.get((ident, m))
            if w is None:
                continue
            w.limit = val
            if w.used(now) + need > val:
                blocked, reason = True, m
                wait = max(wait, w.wait_for(now, need, val))
        if blocked:
            return Verdict(False, wait, False, reason)
        res = Reservation(key_id, provider, model, now, tokens)
        for m, (val, ident) in eff.items():
            if m == "conc":
                k = (ident, "conc")
                self.inflight[k] = self.inflight.get(k, 0) + 1
                res.conc.append(k)
                continue
            w = self._window(ident, m, val)
            if m[0] == "r":
                w.add(now, 1)
                res.req_wins.append(w)
            else:
                w.add(now, tokens)
                res.tok_wins.append(w)
        return res

    def settle(self, res: Reservation, actual_tokens: int, now: float | None = None) -> None:
        """Request finished (successfully or counted by the provider anyway)."""
        if res.done:
            return
        res.done = True
        now = self.clock() if now is None else now
        delta = int(actual_tokens) - res.tokens
        for w in res.tok_wins:
            w.adjust(res.ts, delta, now)
        for k in res.conc:
            self.inflight[k] = max(self.inflight.get(k, 1) - 1, 0)
        self._record(res.key_id, res.model, now, 1, int(actual_tokens))

    def release(self, res: Reservation, now: float | None = None) -> None:
        """Request never reached the provider (or was rejected without cost): undo."""
        if res.done:
            return
        res.done = True
        now = self.clock() if now is None else now
        for w in res.req_wins:
            w.adjust(res.ts, -1, now)
        for w in res.tok_wins:
            w.adjust(res.ts, -res.tokens, now)
        for k in res.conc:
            self.inflight[k] = max(self.inflight.get(k, 1) - 1, 0)

    # ------------------------------------------------------------- reporting
    def fill_ratio(self, provider: str, key_id: int, model: str, now: float | None = None) -> float:
        now = self.clock() if now is None else now
        r = 0.0
        for m, (val, ident) in self.effective(provider, key_id, model).items():
            if val <= 0:
                return 1.0
            if m == "conc":
                u = self.inflight.get((ident, "conc"), 0)
            else:
                w = self.win.get((ident, m))
                u = w.used(now) if w else 0
            r = max(r, u / val)
        return r

    def snapshot_key(self, key_id: int, now: float | None = None) -> list[dict]:
        now = self.clock() if now is None else now
        out = []
        for (ident, metric), w in self.win.items():
            if ident[0] != key_id or w.limit <= 0:
                continue
            kind, ref = ident[1]
            out.append({
                "scope": ref if kind == "m" else f"rule#{ref}",
                "metric": metric,
                "used": w.used(now),
                "limit": w.limit,
            })
        for (ident, _), n in self.inflight.items():
            if ident[0] == key_id and n:
                out.append({"scope": ident[1][1] if ident[1][0] == "m" else f"rule#{ident[1][1]}", "metric": "conc", "used": n, "limit": 0})
        out.sort(key=lambda d: (d["scope"], d["metric"]))
        return out

    # ----------------------------------------------------------- persistence
    def _record(self, key_id: int, model: str, now: float, req: int, tok: int) -> None:
        k = (key_id, model, int(now // 60))
        e = self._pending.get(k)
        if e is None:
            self._pending[k] = [req, tok]
        else:
            e[0] += req
            e[1] += tok

    def drain_pending(self) -> list[tuple]:
        rows = [(k[0], k[1], k[2], v[0], v[1]) for k, v in self._pending.items()]
        self._pending.clear()
        return rows

    def rebuild(self, rows: list[dict], provider_of_key: dict[int, str], now: float | None = None) -> None:
        """Re-create minute-and-larger windows from persisted per-minute usage."""
        now = self.clock() if now is None else now
        self.win.clear()
        for r in sorted(rows, key=lambda r: r["minute"]):
            prov = provider_of_key.get(r["provider_key_id"])
            if prov is None:
                continue
            ts = min(now, r["minute"] * 60 + 59.0)
            for m, (val, ident) in self.effective(prov, r["provider_key_id"], r["model"]).items():
                if m == "conc" or m[2] == "s":
                    continue
                w = self._window(ident, m, val)
                if ts > now - w.span:
                    w.add(ts, r["requests"] if m[0] == "r" else r["tokens"])
