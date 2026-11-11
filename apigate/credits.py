"""One credit balance per user, shared by API keys and the web chat.

Unit: micro-credit (uc). 1 credit = 1_000_000 uc. A request first HOLDS its worst-case
cost (atomic conditional UPDATE: the balance can never go negative, even with many
concurrent requests), then SETTLES to the real cost and refunds the difference.
Holds are persisted, so a crash cannot silently eat credits: stale holds are refunded on start.
"""
from __future__ import annotations

import secrets
import time

from .db import Database
from .errors import GateError

UC = 1_000_000


def to_credits(uc: int) -> float:
    return uc / UC


def to_uc(credits: float) -> int:
    return int(round(credits * UC))


class Credits:
    def __init__(self, db: Database):
        self.db = db

    async def hold(self, user_id: int, amount_uc: int) -> str | None:
        """Returns hold id, or None for unlimited users. Raises 402 when funds are insufficient."""
        hid = secrets.token_hex(12)

        def f(c):
            u = c.execute("SELECT active, unlimited, balance_uc FROM users WHERE id=?", (user_id,)).fetchone()
            if u is None or not u["active"]:
                raise GateError(403, "account_disabled", "This account is disabled")
            if u["unlimited"]:
                return None
            cur = c.execute("UPDATE users SET balance_uc = balance_uc - ? WHERE id=? AND balance_uc >= ?", (amount_uc, user_id, amount_uc))
            if cur.rowcount != 1:
                raise GateError(402, "insufficient_quota", "Insufficient credits for this request. Ask an administrator to top up your balance.")
            c.execute("INSERT INTO holds(id,user_id,amount_uc,created_at) VALUES(?,?,?,?)", (hid, user_id, amount_uc, time.time()))
            return hid

        return await self.db.transaction(f)

    async def settle(self, hold_id: str | None, user_id: int, cost_uc: int, **meta) -> int:
        """Charge the real cost. Returns balance after. `meta` goes to the ledger row."""

        def f(c):
            u = c.execute("SELECT balance_uc, unlimited FROM users WHERE id=?", (user_id,)).fetchone()
            bal = u["balance_uc"]
            held = 0
            if hold_id:
                h = c.execute("SELECT amount_uc FROM holds WHERE id=?", (hold_id,)).fetchone()
                if h:
                    held = h["amount_uc"]
                    c.execute("DELETE FROM holds WHERE id=?", (hold_id,))
            if u["unlimited"]:
                charge, new_bal = 0, bal
            else:
                charge = min(cost_uc, held + bal)
                new_bal = bal + held - charge
                c.execute("UPDATE users SET balance_uc=? WHERE id=?", (new_bal, user_id))
            c.execute(
                "INSERT INTO ledger(user_id,ts,kind,delta_uc,balance_after,cost_uc,source,api_key_id,provider,model,provider_key_id,"
                "prompt_tokens,completion_tokens,request_id,latency_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, time.time(), "usage", -charge, new_bal, cost_uc, meta.get("source"), meta.get("api_key_id"), meta.get("provider"),
                 meta.get("model"), meta.get("provider_key_id"), meta.get("prompt_tokens"), meta.get("completion_tokens"),
                 meta.get("request_id"), meta.get("latency_ms")),
            )
            return new_bal

        return await self.db.transaction(f)

    async def release(self, hold_id: str | None) -> None:
        if not hold_id:
            return

        def f(c):
            h = c.execute("SELECT user_id, amount_uc FROM holds WHERE id=?", (hold_id,)).fetchone()
            if h:
                c.execute("DELETE FROM holds WHERE id=?", (hold_id,))
                c.execute("UPDATE users SET balance_uc = balance_uc + ? WHERE id=?", (h["amount_uc"], h["user_id"]))

        await self.db.transaction(f)

    async def release_all(self) -> int:
        def f(c):
            rows = c.execute("SELECT id, user_id, amount_uc FROM holds").fetchall()
            for h in rows:
                c.execute("UPDATE users SET balance_uc = balance_uc + ? WHERE id=?", (h["amount_uc"], h["user_id"]))
            c.execute("DELETE FROM holds")
            return len(rows)

        return await self.db.transaction(f)

    async def adjust(self, user_id: int, delta_uc: int, actor_id: int | None, note: str = "", kind: str = "grant") -> int:
        """Admin top-up (positive) or correction (negative). The balance never goes below zero."""

        def f(c):
            u = c.execute("SELECT balance_uc FROM users WHERE id=?", (user_id,)).fetchone()
            if u is None:
                raise GateError(404, "user_not_found", "No such user")
            new_bal = u["balance_uc"] + delta_uc
            if new_bal < 0:
                raise GateError(400, "balance_negative", "Adjustment would make the balance negative")
            c.execute("UPDATE users SET balance_uc=? WHERE id=?", (new_bal, user_id))
            c.execute(
                "INSERT INTO ledger(user_id,ts,kind,delta_uc,balance_after,note,actor_id) VALUES(?,?,?,?,?,?,?)",
                (user_id, time.time(), kind, delta_uc, new_bal, note[:300], actor_id),
            )
            return new_bal

        return await self.db.transaction(f)

    async def balance(self, user_id: int) -> int:
        r = await self.db.fetchone("SELECT balance_uc FROM users WHERE id=?", (user_id,))
        return r["balance_uc"] if r else 0

    async def ledger(self, user_id: int, limit: int = 50, before_id: int | None = None) -> list[dict]:
        limit = max(1, min(limit, 200))
        if before_id:
            return await self.db.fetchall("SELECT * FROM ledger WHERE user_id=? AND id<? ORDER BY id DESC LIMIT ?", (user_id, before_id, limit))
        return await self.db.fetchall("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit))
