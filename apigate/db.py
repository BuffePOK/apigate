"""SQLite storage. One writer thread; all access is serialised through it, so
multi-statement transactions are atomic with respect to every other caller."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Sequence

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value BLOB);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE users(
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('admin','user')),
  active INTEGER NOT NULL DEFAULT 1,
  unlimited INTEGER NOT NULL DEFAULT 0,
  balance_uc INTEGER NOT NULL DEFAULT 0 CHECK(balance_uc >= 0),
  created_at REAL NOT NULL,
  last_login_at REAL
);
CREATE TABLE sessions(
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  last_seen REAL NOT NULL,
  ip TEXT, ua TEXT
);
CREATE TABLE api_keys(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL,
  created_by INTEGER,
  last_used_at REAL,
  revoked_at REAL,
  expires_at REAL,
  rpm INTEGER,
  max_concurrent INTEGER,
  allowed_models TEXT
);

CREATE TABLE providers(
  name TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  base_url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  strategy TEXT,
  fill_threshold REAL,
  extra TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE provider_keys(
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL REFERENCES providers(name) ON DELETE CASCADE,
  label TEXT NOT NULL,
  secret BLOB NOT NULL,
  fingerprint TEXT NOT NULL,
  fp_digest TEXT NOT NULL UNIQUE,
  enabled INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'ok',
  status_note TEXT,
  priority INTEGER NOT NULL DEFAULT 100,
  created_at REAL NOT NULL
);
CREATE TABLE models(
  provider TEXT NOT NULL REFERENCES providers(name) ON DELETE CASCADE,
  model_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  context_window INTEGER,
  manual INTEGER NOT NULL DEFAULT 0,
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL,
  PRIMARY KEY(provider, model_id)
);
CREATE TABLE model_keys(
  provider TEXT NOT NULL,
  model_id TEXT NOT NULL,
  key_id INTEGER NOT NULL REFERENCES provider_keys(id) ON DELETE CASCADE,
  PRIMARY KEY(provider, model_id, key_id)
);
CREATE TABLE limit_rules(
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL REFERENCES providers(name) ON DELETE CASCADE,
  key_id INTEGER REFERENCES provider_keys(id) ON DELETE CASCADE,
  pattern TEXT NOT NULL DEFAULT '*',
  scope TEXT NOT NULL DEFAULT 'model' CHECK(scope IN ('model','key')),
  limits TEXT NOT NULL,
  note TEXT
);
CREATE TABLE aliases(
  name TEXT PRIMARY KEY,
  targets TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'fallback'
);
CREATE TABLE prices(
  id INTEGER PRIMARY KEY,
  provider TEXT NOT NULL DEFAULT '*',
  pattern TEXT NOT NULL DEFAULT '*',
  price_in REAL NOT NULL,
  price_out REAL NOT NULL
);

CREATE TABLE ledger(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  delta_uc INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  cost_uc INTEGER,
  source TEXT,
  api_key_id INTEGER,
  provider TEXT, model TEXT, provider_key_id INTEGER,
  prompt_tokens INTEGER, completion_tokens INTEGER,
  request_id TEXT, latency_ms INTEGER,
  note TEXT, actor_id INTEGER
);
CREATE INDEX ledger_user ON ledger(user_id, id DESC);
CREATE INDEX ledger_ts ON ledger(ts);
CREATE TABLE holds(
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  amount_uc INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE usage_minute(
  provider_key_id INTEGER NOT NULL,
  model TEXT NOT NULL,
  minute INTEGER NOT NULL,
  requests INTEGER NOT NULL,
  tokens INTEGER NOT NULL,
  PRIMARY KEY(provider_key_id, model, minute)
) WITHOUT ROWID;

CREATE TABLE conversations(
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  model TEXT,
  system_prompt TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX conv_user ON conversations(user_id, updated_at DESC);
CREATE TABLE messages(
  id INTEGER PRIMARY KEY,
  conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  model TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX msg_conv ON messages(conv_id, id);
CREATE TABLE audit(
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  actor_id INTEGER,
  action TEXT NOT NULL,
  target TEXT,
  ip TEXT,
  detail TEXT
);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON", "busy_timeout=5000", "secure_delete=ON"):
            self._conn.execute(f"PRAGMA {pragma}")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self._ex = ThreadPoolExecutor(1, thread_name_prefix="apigate-db")

    # -- low level ---------------------------------------------------------
    async def run(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._ex, fn, self._conn)

    async def transaction(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        def wrapped(conn: sqlite3.Connection):
            conn.execute("BEGIN IMMEDIATE")
            try:
                out = fn(conn)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return out

        return await self.run(wrapped)

    async def execute(self, sql: str, params: Sequence = ()) -> int:
        def f(c):
            return c.execute(sql, params).rowcount

        return await self.run(f)

    async def fetchall(self, sql: str, params: Sequence = ()) -> list[dict]:
        return await self.run(lambda c: [dict(r) for r in c.execute(sql, params).fetchall()])

    async def fetchone(self, sql: str, params: Sequence = ()) -> dict | None:
        def f(c):
            r = c.execute(sql, params).fetchone()
            return dict(r) if r else None

        return await self.run(f)

    # -- lifecycle ---------------------------------------------------------
    async def init(self) -> None:
        def f(c: sqlite3.Connection):
            ver = c.execute("PRAGMA user_version").fetchone()[0]
            if ver == 0:
                c.execute("BEGIN")
                for stmt in _split(SCHEMA):
                    c.execute(stmt)
                c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                c.execute("INSERT INTO meta(key,value) VALUES('config_version', 1)")
                c.execute("COMMIT")
            elif ver > SCHEMA_VERSION:
                raise RuntimeError(f"database schema {ver} is newer than this build ({SCHEMA_VERSION})")

        await self.run(f)

    async def close(self) -> None:
        def f(c):
            try:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                c.close()

        await self.run(f)
        self._ex.shutdown(wait=True)

    # -- helpers ------------------------------------------------------------
    async def bump_config(self) -> None:
        await self.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key='config_version'")

    async def config_version(self) -> int:
        r = await self.fetchone("SELECT value FROM meta WHERE key='config_version'")
        return int(r["value"]) if r else 0

    async def audit(self, actor_id: int | None, action: str, target: str = "", ip: str = "", detail: str = "") -> None:
        await self.execute(
            "INSERT INTO audit(ts,actor_id,action,target,ip,detail) VALUES(?,?,?,?,?,?)",
            (time.time(), actor_id, action, target, ip, detail[:1000]),
        )


def _split(script: str) -> list[str]:
    # tiny splitter: schema has no semicolons inside statements
    return [s.strip() for s in script.split(";") if s.strip()]
