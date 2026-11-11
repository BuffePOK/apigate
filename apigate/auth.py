"""Users, sessions, gateway API keys, login throttling."""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass

from .crypto import Vault, hash_password, looks_like_api_key, new_api_key, new_token, verify_dummy, verify_password
from .db import Database
from .errors import GateError

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,64}$")


@dataclass(slots=True)
class Principal:
    user_id: int | None            # None = trusted in-process caller (no credits)
    username: str
    role: str
    source: str                    # api | chat | internal
    api_key_id: int | None = None
    allowed_models: list[str] | None = None
    rpm: int | None = None
    max_concurrent: int | None = None

    @property
    def gate_id(self) -> tuple:
        return ("k", self.api_key_id) if self.api_key_id else ("u", self.user_id)


class Throttle:
    """Failure counter per key within a sliding time window (in-memory, bounded)."""

    def __init__(self, max_failures: int, window_s: float, max_entries: int = 50_000):
        self.max, self.window, self.cap = max_failures, window_s, max_entries
        self.events: dict[str, deque[float]] = {}

    def _clean(self, key: str, now: float) -> deque:
        dq = self.events.get(key)
        if dq is None:
            return deque()
        while dq and dq[0] < now - self.window:
            dq.popleft()
        if not dq:
            self.events.pop(key, None)
        return dq

    def blocked_for(self, key: str) -> float:
        now = time.time()
        dq = self._clean(key, now)
        if len(dq) >= self.max:
            return max(1.0, dq[0] + self.window - now)
        return 0.0

    def fail(self, key: str) -> None:
        now = time.time()
        if len(self.events) > self.cap:
            for k in list(self.events)[: self.cap // 10]:
                self.events.pop(k, None)
        dq = self._clean(key, now)
        dq.append(now)
        self.events[key] = dq

    def clear(self, key: str) -> None:
        self.events.pop(key, None)


class Auth:
    def __init__(self, db: Database, vault: Vault, session_ttl_s: int, min_pw: int, max_login_failures: int, login_window_s: int):
        self.db, self.vault = db, vault
        self.session_ttl = session_ttl_s
        self.min_pw = min_pw
        self.login_throttle = Throttle(max_login_failures, login_window_s)
        self.bearer_throttle = Throttle(30, 300)
        self.signup_throttle = Throttle(10, 3600)
        self._key_cache: dict[str, tuple[float, dict]] = {}
        self._touch: dict[int, float] = {}

    # ------------------------------------------------------------ passwords
    def check_password_policy(self, pw: str) -> None:
        if not isinstance(pw, str) or len(pw) < self.min_pw:
            raise GateError(400, "weak_password", f"Password must be at least {self.min_pw} characters")
        if len(pw) > 256:
            raise GateError(400, "weak_password", "Password is too long")
        if pw.lower() in {"password1234", "123456789012", "qwertyuiopas"} or len(set(pw)) < 5:
            raise GateError(400, "weak_password", "Password is too easy to guess")

    @staticmethod
    def check_username(name: str) -> str:
        if not isinstance(name, str) or not USERNAME_RE.match(name):
            raise GateError(400, "invalid_username", "Username must be 3-64 characters: letters, digits, . _ @ -")
        return name

    # ----------------------------------------------------------------- users
    async def create_user(self, username: str, password: str, role: str = "user") -> dict:
        self.check_username(username)
        self.check_password_policy(password)
        if role not in ("admin", "user"):
            raise GateError(400, "invalid_role", "role must be admin or user")
        pw = await asyncio.to_thread(hash_password, password)
        try:
            await self.db.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,?)", (username, pw, role, time.time()))
        except Exception as e:  # noqa: BLE001
            if "UNIQUE" in str(e):
                raise GateError(409, "username_taken", "That username is already taken") from e
            raise
        return await self.get_user_by_name(username)  # type: ignore[return-value]

    async def get_user(self, uid: int) -> dict | None:
        return await self.db.fetchone("SELECT id,username,role,active,unlimited,balance_uc,created_at,last_login_at FROM users WHERE id=?", (uid,))

    async def get_user_by_name(self, name: str) -> dict | None:
        return await self.db.fetchone("SELECT id,username,role,active,unlimited,balance_uc,created_at,last_login_at FROM users WHERE username=?", (name,))

    async def set_password(self, uid: int, password: str, keep_session: str | None = None) -> None:
        self.check_password_policy(password)
        pw = await asyncio.to_thread(hash_password, password)
        await self.db.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw, uid))
        # invalidate every other session
        if keep_session:
            await self.db.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (uid, self.vault.digest(keep_session)))
        else:
            await self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))

    async def deactivate_effects(self, uid: int) -> None:
        await self.db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        self._key_cache.clear()

    # ----------------------------------------------------------------- login
    async def login(self, username: str, password: str, ip: str, ua: str) -> tuple[dict, str, str]:
        tk_ip, tk_user = f"ip:{ip}", f"user:{str(username).lower()[:64]}"
        wait = max(self.login_throttle.blocked_for(tk_ip), self.login_throttle.blocked_for(tk_user))
        if wait:
            raise GateError(429, "too_many_attempts", "Too many failed logins. Try again later.", retry_after=wait)
        row = None
        if isinstance(username, str) and isinstance(password, str) and len(password) <= 256:
            row = await self.db.fetchone("SELECT id,pw_hash,active FROM users WHERE username=?", (username,))
        if row is None:
            await asyncio.to_thread(verify_dummy, str(password)[:256])
            ok = False
        else:
            ok = await asyncio.to_thread(verify_password, row["pw_hash"], password) and bool(row["active"])
        if not ok:
            self.login_throttle.fail(tk_ip)
            self.login_throttle.fail(tk_user)
            raise GateError(401, "invalid_credentials", "Wrong username or password")
        self.login_throttle.clear(tk_user)
        await self.db.execute("UPDATE users SET last_login_at=? WHERE id=?", (time.time(), row["id"]))
        sid, csrf = await self.create_session(row["id"], ip, ua)
        return (await self.get_user(row["id"])), sid, csrf  # type: ignore[return-value]

    async def create_session(self, uid: int, ip: str, ua: str) -> tuple[str, str]:
        sid, csrf, now = new_token(), new_token(), time.time()
        await self.db.execute(
            "INSERT INTO sessions(id,user_id,csrf,created_at,expires_at,last_seen,ip,ua) VALUES(?,?,?,?,?,?,?,?)",
            (self.vault.digest(sid), uid, csrf, now, now + self.session_ttl, now, ip, ua[:200]),
        )
        return sid, csrf

    async def get_session(self, sid: str) -> dict | None:
        if not sid or len(sid) > 128:
            return None
        now = time.time()
        row = await self.db.fetchone(
            "SELECT s.csrf, s.expires_at, s.last_seen, u.id AS user_id, u.username, u.role, u.active, u.unlimited, u.balance_uc "
            "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?",
            (self.vault.digest(sid),),
        )
        if not row or row["expires_at"] < now or not row["active"]:
            return None
        if now - row["last_seen"] > 300:
            await self.db.execute("UPDATE sessions SET last_seen=? WHERE id=?", (now, self.vault.digest(sid)))
        return row

    async def logout(self, sid: str) -> None:
        await self.db.execute("DELETE FROM sessions WHERE id=?", (self.vault.digest(sid),))

    async def purge_expired(self) -> None:
        await self.db.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))

    # -------------------------------------------------------------- API keys
    async def create_api_key(self, user_id: int, name: str, created_by: int | None, *, expires_at: float | None = None,
                             rpm: int | None = None, max_concurrent: int | None = None, allowed_models: list[str] | None = None,
                             max_keys: int = 20) -> tuple[str, dict]:
        name = (name or "").strip()[:80]
        if not name:
            raise GateError(400, "invalid_name", "Key name is required")
        n = await self.db.fetchone("SELECT COUNT(*) AS n FROM api_keys WHERE user_id=? AND revoked_at IS NULL", (user_id,))
        if n and n["n"] >= max_keys:
            raise GateError(400, "too_many_keys", f"At most {max_keys} active API keys per user")
        if allowed_models is not None:
            allowed_models = [str(x)[:200] for x in allowed_models][:100]
        plain = new_api_key()
        now = time.time()
        await self.db.execute(
            "INSERT INTO api_keys(user_id,name,prefix,key_hash,created_at,created_by,expires_at,rpm,max_concurrent,allowed_models) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, name, plain[:14], self.vault.digest(plain), now, created_by, expires_at, rpm, max_concurrent,
             json.dumps(allowed_models) if allowed_models is not None else None),
        )
        row = await self.db.fetchone("SELECT * FROM api_keys WHERE key_hash=?", (self.vault.digest(plain),))
        return plain, self._public_key(row)  # type: ignore[arg-type]

    @staticmethod
    def _public_key(r: dict) -> dict:
        r = dict(r)
        r.pop("key_hash", None)
        r["allowed_models"] = json.loads(r["allowed_models"]) if r.get("allowed_models") else None
        return r

    async def list_api_keys(self, user_id: int | None = None) -> list[dict]:
        if user_id is None:
            rows = await self.db.fetchall(
                "SELECT k.*, u.username FROM api_keys k JOIN users u ON u.id=k.user_id ORDER BY k.id DESC LIMIT 1000")
        else:
            rows = await self.db.fetchall("SELECT * FROM api_keys WHERE user_id=? ORDER BY id DESC", (user_id,))
        return [self._public_key(r) for r in rows]

    async def revoke_api_key(self, key_id: int, user_id: int | None = None) -> bool:
        if user_id is None:
            n = await self.db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (time.time(), key_id))
        else:
            n = await self.db.execute("UPDATE api_keys SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL", (time.time(), key_id, user_id))
        self._key_cache.clear()
        return n > 0

    async def verify_api_key(self, token: str, ip: str) -> Principal:
        if ip and self.bearer_throttle.blocked_for(ip):
            raise GateError(429, "too_many_attempts", "Too many invalid API keys from this address", retry_after=60)
        if not looks_like_api_key(token):
            self._bad(ip)
        h = self.vault.digest(token)
        now = time.time()
        hit = self._key_cache.get(h)
        if hit and now - hit[0] < 20:
            row = hit[1]
        else:
            row = await self.db.fetchone(
                "SELECT k.id, k.user_id, k.revoked_at, k.expires_at, k.rpm, k.max_concurrent, k.allowed_models, "
                "u.username, u.role, u.active FROM api_keys k JOIN users u ON u.id=k.user_id WHERE k.key_hash=?", (h,))
            if row is None:
                self._bad(ip)
            self._key_cache[h] = (now, row)
            if len(self._key_cache) > 10_000:
                self._key_cache.clear()
        if row["revoked_at"] or not row["active"] or (row["expires_at"] and row["expires_at"] < now):
            raise GateError(401, "invalid_api_key", "Incorrect API key provided", type_="authentication_error")
        self._touch[row["id"]] = now
        return Principal(
            user_id=row["user_id"], username=row["username"], role=row["role"], source="api", api_key_id=row["id"],
            allowed_models=json.loads(row["allowed_models"]) if row["allowed_models"] else None,
            rpm=row["rpm"], max_concurrent=row["max_concurrent"],
        )

    def _bad(self, ip: str) -> None:
        if ip:
            self.bearer_throttle.fail(ip)
        raise GateError(401, "invalid_api_key", "Incorrect API key provided", type_="authentication_error")

    async def flush_last_used(self) -> None:
        if not self._touch:
            return
        items, self._touch = self._touch, {}

        def f(c):
            c.execute("BEGIN")
            c.executemany("UPDATE api_keys SET last_used_at=? WHERE id=?", [(t, i) for i, t in items.items()])
            c.execute("COMMIT")

        await self.db.run(f)
