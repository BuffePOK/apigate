"""Static settings: defaults < TOML file < environment (APIGATE_*).

Runtime-editable values (signup, default strategy, prices...) live in the DB
(`settings` table) and are read through `Gateway.rt`.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Settings:
    data_dir: str = "./data"
    host: str = "127.0.0.1"
    port: int = 8080

    # --- web / security -------------------------------------------------
    allow_signup: bool = False            # default; editable at runtime by admin
    cookie_secure: bool = False           # set true when served over HTTPS
    hsts: bool = False
    trusted_proxies: str = ""             # comma separated IPs allowed to set X-Forwarded-For
    cors_origins: str = ""                # comma separated; empty = CORS off
    public_origin: str = ""               # e.g. https://llm-apis.example.org (accepted by the CSRF Origin check)
    session_ttl_s: int = 7 * 24 * 3600
    max_body_bytes: int = 20 * 1024 * 1024
    password_min_length: int = 12
    max_api_keys_per_user: int = 20
    login_max_failures: int = 5
    login_window_s: int = 900

    # --- routing --------------------------------------------------------
    default_strategy: str = "round_robin"     # round_robin | random | fill | least_loaded
    fill_threshold: float = 0.7
    max_attempts: int = 4                     # distinct keys tried per request
    max_queue_wait_s: float = 5.0             # wait this long for a limit window to free up
    assumed_output_tokens: int = 1024         # TPM reservation when client sends no max_tokens
    default_max_tokens: int = 8192            # credit hold when client sends no max_tokens
    models_refresh_s: int = 6 * 3600
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 600.0
    allow_insecure_upstream: bool = False     # allow http:// upstreams to non-loopback hosts

    # --- per-caller throttles (0 = unlimited) ---------------------------
    api_key_rpm: int = 600
    api_key_max_concurrent: int = 32
    chat_user_rpm: int = 30
    chat_user_max_concurrent: int = 4

    # --- credits (placeholder pricing: credits per 1M tokens) -----------
    default_price_in: float = 1.0
    default_price_out: float = 3.0

    master_key: str = ""                      # base64url 32 bytes; prefer env APIGATE_MASTER_KEY
    log_level: str = "INFO"

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def db_path(self) -> Path:
        return self.data_path / "gate.db"

    @property
    def proxies(self) -> set[str]:
        return {p.strip() for p in self.trusted_proxies.split(",") if p.strip()}

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Settings":
        data: dict = {}
        path = path or os.environ.get("APIGATE_CONFIG")
        if path and Path(path).exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
        s = cls()
        for fld in fields(cls):
            raw = os.environ.get("APIGATE_" + fld.name.upper(), data.get(fld.name))
            if raw is None:
                continue
            cur = getattr(s, fld.name)
            if isinstance(cur, bool):
                val = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                val = int(raw)
            elif isinstance(cur, float):
                val = float(raw)
            else:
                val = str(raw)
            setattr(s, fld.name, val)
        return s
