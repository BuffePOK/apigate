"""Per-provider key pools: health state and selection strategies."""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

STRATEGIES = ("round_robin", "random", "fill", "least_loaded")

BASE_COOLDOWN_S = 15.0
MAX_COOLDOWN_S = 900.0
MAX_RETRY_AFTER_S = 86400.0


@dataclass(eq=False)
class KeyState:
    id: int
    provider: str
    label: str
    secret: str = field(repr=False)
    fingerprint: str = ""
    enabled: bool = True
    priority: int = 100
    status: str = "ok"                 # ok | auth_failed
    note: str | None = None
    models: set[str] = field(default_factory=set)
    models_known: bool = False
    cooldown_until: float = 0.0
    model_cooldown: dict[str, float] = field(default_factory=dict)
    rate_streak: dict[str, int] = field(default_factory=dict)
    fail_streak: int = 0
    last_used: float = 0.0
    last_error: str = ""

    def usable(self, model: str, now: float) -> bool:
        if not self.enabled or self.status != "ok" or now < self.cooldown_until:
            return False
        if now < self.model_cooldown.get(model, 0.0):
            return False
        return (not self.models_known) or model in self.models

    def public(self, now: float) -> dict:
        return {
            "id": self.id, "provider": self.provider, "label": self.label, "fingerprint": self.fingerprint,
            "enabled": self.enabled, "priority": self.priority, "status": self.status, "note": self.note,
            "cooldown_s": max(0, round(self.cooldown_until - now)),
            "model_cooldowns": {m: round(t - now) for m, t in self.model_cooldown.items() if t > now},
            "models": len(self.models), "last_error": self.last_error,
        }


class Pool:
    def __init__(self, provider: str, rng: random.Random | None = None):
        self.provider = provider
        self.keys: dict[int, KeyState] = {}
        self.cursor: dict[str, int] = {}   # model -> last used key id (round robin)
        self.rng = rng or random.Random()

    # ---------------------------------------------------------------- choose
    def candidates(self, model: str, now: float, exclude: set[tuple[int, str]]) -> list[KeyState]:
        return [k for k in self.keys.values() if (k.id, model) not in exclude and k.usable(model, now)]

    def order(
        self,
        cands: list[KeyState],
        model: str,
        strategy: str,
        threshold: float,
        ratio: Callable[[KeyState], float],
    ) -> list[KeyState]:
        if len(cands) <= 1:
            return list(cands)
        by_prio = sorted(cands, key=lambda k: (k.priority, k.id))
        if strategy == "random":
            out = list(cands)
            self.rng.shuffle(out)
            return out
        if strategy == "least_loaded":
            return sorted(by_prio, key=ratio)
        if strategy == "fill":
            ratios = {k.id: ratio(k) for k in by_prio}
            below = [k for k in by_prio if ratios[k.id] < threshold]
            above = sorted((k for k in by_prio if ratios[k.id] >= threshold), key=lambda k: ratios[k.id])
            return below + above
        # round_robin (default)
        last = self.cursor.get(model)
        ids = [k.id for k in by_prio]
        start = 0
        if last is not None:
            for i, kid in enumerate(ids):
                if kid > last:
                    start = i
                    break
            else:
                start = 0
        return by_prio[start:] + by_prio[:start]

    def mark_used(self, key: KeyState, model: str, now: float) -> None:
        self.cursor[model] = key.id
        key.last_used = now

    # ---------------------------------------------------------------- health
    def on_success(self, key: KeyState, model: str) -> None:
        key.fail_streak = 0
        key.rate_streak.pop(model, None)
        key.last_error = ""

    def on_rate_limited(self, key: KeyState, model: str, retry_after: float | None, now: float) -> float:
        n = key.rate_streak.get(model, 0) + 1
        key.rate_streak[model] = n
        if retry_after and retry_after > 0:
            cd = min(retry_after + 0.5, MAX_RETRY_AFTER_S)
        else:
            cd = min(BASE_COOLDOWN_S * 2 ** (n - 1), MAX_COOLDOWN_S)
        key.model_cooldown[model] = max(key.model_cooldown.get(model, 0.0), now + cd)
        key.last_error = "rate limited"
        return cd

    def on_model_unavailable(self, key: KeyState, model: str, now: float, seconds: float = 600.0, why: str = "") -> None:
        key.model_cooldown[model] = now + seconds
        key.last_error = why or "model unavailable"

    def on_quota(self, key: KeyState, now: float, seconds: float = 3600.0) -> None:
        key.cooldown_until = max(key.cooldown_until, now + seconds)
        key.last_error = "quota exhausted / billing"

    def on_auth_failed(self, key: KeyState, note: str) -> None:
        key.status = "auth_failed"
        key.note = note[:200]
        key.last_error = "authentication failed"

    def on_server_error(self, key: KeyState, now: float, why: str = "") -> float:
        key.fail_streak += 1
        key.last_error = why or "upstream error"
        if key.fail_streak >= 3:
            cd = min(5.0 * 2 ** (key.fail_streak - 3), 300.0)
            key.cooldown_until = max(key.cooldown_until, now + cd)
            return cd
        return 0.0
