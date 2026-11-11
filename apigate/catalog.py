"""Model registry (what exists, on which keys), aliases, and price lookup."""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field

from .errors import GateError

ROUTABLE_KINDS = ("chat", "embedding")


@dataclass
class ModelEntry:
    provider: str
    id: str
    kind: str
    enabled: bool
    context_window: int | None = None
    manual: bool = False
    key_ids: set[int] = field(default_factory=set)

    @property
    def public_id(self) -> str:
        return f"{self.provider}/{self.id}"


@dataclass
class Alias:
    name: str
    targets: list[str]
    mode: str = "fallback"        # fallback | round_robin | random


class ModelRegistry:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], ModelEntry] = {}
        self.by_id: dict[str, list[ModelEntry]] = {}
        self.aliases: dict[str, Alias] = {}
        self.providers: set[str] = set()
        self._alias_rr: dict[str, int] = {}

    def load(self, providers: list[str], model_rows: list[dict], key_rows: list[dict], alias_rows: list[dict]) -> None:
        self.providers = set(providers)
        self.entries = {}
        for r in model_rows:
            self.entries[(r["provider"], r["model_id"])] = ModelEntry(
                r["provider"], r["model_id"], r["kind"], bool(r["enabled"]), r["context_window"], bool(r["manual"])
            )
        for r in key_rows:
            e = self.entries.get((r["provider"], r["model_id"]))
            if e:
                e.key_ids.add(r["key_id"])
        self.by_id = {}
        for e in self.entries.values():
            self.by_id.setdefault(e.id, []).append(e)
        self.aliases = {r["name"]: Alias(r["name"], json.loads(r["targets"]), r["mode"]) for r in alias_rows}

    def get(self, provider: str, model_id: str) -> ModelEntry | None:
        return self.entries.get((provider, model_id))

    def split(self, name: str) -> tuple[str, str] | None:
        if "/" in name:
            prov, rest = name.split("/", 1)
            if prov in self.providers:
                return prov, rest
        return None

    def resolve(self, name: str, kind: str, order_hint: int = 0) -> list[tuple[str, str]]:
        """Public model name -> ordered list of (provider, model_id) targets to try."""
        if not isinstance(name, str) or not name or len(name) > 200:
            raise GateError(400, "invalid_model", "'model' must be a non-empty string", param="model")
        al = self.aliases.get(name)
        if al:
            targets = [t for t in (self._resolve_one(x, kind, quiet=True) for x in al.targets) if t]
            if not targets:
                raise GateError(404, "model_not_found", f"alias '{name}' has no usable {kind} targets", param="model")
            if al.mode == "random":
                import random
                random.shuffle(targets)
            elif al.mode == "round_robin":
                i = self._alias_rr.get(name, 0) % len(targets)
                self._alias_rr[name] = i + 1
                targets = targets[i:] + targets[:i]
            return targets
        one = self._resolve_one(name, kind, quiet=False)
        return [one]  # type: ignore[list-item]

    def _resolve_one(self, name: str, kind: str, quiet: bool) -> tuple[str, str] | None:
        sp = self.split(name)
        if sp:
            e = self.entries.get(sp)
            if e and e.enabled and e.kind == kind:
                return sp
            if quiet:
                return None
            raise GateError(404, "model_not_found", f"The model '{name}' does not exist or is not enabled for {kind}", param="model")
        cands = [e for e in self.by_id.get(name, []) if e.enabled and e.kind == kind]
        if len(cands) == 1:
            return cands[0].provider, cands[0].id
        if quiet:
            return None
        if not cands:
            raise GateError(404, "model_not_found", f"The model '{name}' does not exist or is not enabled for {kind}", param="model")
        names = ", ".join(sorted(c.public_id for c in cands))
        raise GateError(400, "ambiguous_model", f"Model '{name}' exists at several providers; use one of: {names}", param="model")


class PriceTable:
    """credits per 1M tokens. Most specific match wins; falls back to defaults."""

    def __init__(self, default_in: float, default_out: float):
        self.default = (default_in, default_out)
        self.rows: list[dict] = []

    def load(self, rows: list[dict], default_in: float, default_out: float) -> None:
        self.rows = rows
        self.default = (default_in, default_out)

    def price(self, provider: str, model: str) -> tuple[float, float]:
        best, score = None, (-1, -1)
        for r in self.rows:
            if r["provider"] not in ("*", provider) or not fnmatch.fnmatchcase(model, r["pattern"]):
                continue
            s = (1 if r["provider"] != "*" else 0, 0 if r["pattern"] == "*" else (1 if any(c in r["pattern"] for c in "*?[") else 2))
            if s > score:
                best, score = r, s
        return (best["price_in"], best["price_out"]) if best else self.default

    def cost_uc(self, provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> int:
        pin, pout = self.price(provider, model)
        # credits/Mtok * tokens == micro-credits
        return int(-(-(prompt_tokens * pin + completion_tokens * pout) // 1))
