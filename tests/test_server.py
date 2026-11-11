from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

import fake_upstream
from apigate.server import create_app

H = {"Origin": "http://test"}


@pytest_asyncio.fixture
async def app_client(gw):
    app = create_app(gw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def login(client, username, password):
    r = await client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"X-CSRF-Token": r.json()["csrf"], **H}


@pytest_asyncio.fixture
async def admin_h(gw, app_client):
    await gw.auth.create_user("root", "very long admin password", "admin")
    return await login(app_client, "root", "very long admin password")


@pytest_asyncio.fixture
async def with_keys(gw):
    await gw.add_provider_key("openai", "k1", "good-key-secret-0001")
    await gw.add_provider_key("anthropic", "c1", "good-key-secret-0002")


async def test_v1_requires_valid_key_in_openai_error_format(app_client):
    r = await app_client.get("/v1/models")
    assert r.status_code == 401 and r.json()["error"]["type"] == "authentication_error"
    r = await app_client.get("/v1/models", headers={"Authorization": "Bearer sk-gate-" + "x" * 43})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_api_key"


async def test_invalid_key_spam_is_throttled(app_client):
    codes = []
    for _ in range(40):
        r = await app_client.get("/v1/models", headers={"Authorization": "Bearer sk-gate-" + "y" * 43})
        codes.append(r.status_code)
    assert 429 in codes and codes[0] == 401


async def test_end_to_end_api_flow(gw, app_client, admin_h, with_keys):
    r = await app_client.post("/api/admin/users", headers=admin_h, json={"username": "dave", "password": "dave's long password", "initial_credits": 50})
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    r = await app_client.post(f"/api/admin/users/{uid}/api-keys", headers=admin_h, json={"name": "for dave"})
    key = r.json()["key"]
    assert key.startswith("sk-gate-")

    auth = {"Authorization": f"Bearer {key}"}
    r = await app_client.get("/v1/models", headers=auth)
    ids = [m["id"] for m in r.json()["data"]]
    assert "openai/gpt-fake" in ids and "anthropic/claude-fake" in ids

    r = await app_client.post("/v1/chat/completions", headers=auth, json={"model": "openai/gpt-fake", "messages": [{"role": "user", "content": "ping"}]})
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "echo: ping"
    assert r.headers["x-gate-model"] == "openai/gpt-fake" and r.headers["x-request-id"]

    r = await app_client.post("/v1/chat/completions", headers=auth, json={"model": "anthropic/claude-fake", "stream": True, "stream_options": {"include_usage": True},
                                                                      "messages": [{"role": "user", "content": "ping"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    lines = [ln for ln in r.text.split("\n\n") if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(ln[6:]) for ln in lines[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"]) == "claude says hi"

    r = await app_client.post("/v1/embeddings", headers=auth, json={"model": "openai/text-embedding-fake", "input": "x"})
    assert r.status_code == 200 and r.json()["data"][0]["embedding"]

    r = await app_client.post("/v1/chat/completions", headers=auth, json={"model": "nope/none", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    r = await app_client.post("/v1/chat/completions", headers=auth, json={"model": "openai/gpt-fake"})
    assert r.status_code == 400
    r = await app_client.post("/v1/chat/completions", headers=auth, content=b"{not json", )
    assert r.status_code == 400


async def test_api_key_cannot_reach_admin_or_session_routes(app_client, gw, admin_h):
    u = await gw.auth.create_user("eve", "eve long password 1", "user")
    plain, _ = await gw.auth.create_api_key(u["id"], "k", None)
    async with httpx.AsyncClient(transport=app_client._transport, base_url="http://test") as anon:   # no cookies
        for path in ("/api/admin/users", "/api/admin/overview", "/api/me/api-keys", "/api/auth/me"):
            r = await anon.get(path, headers={"Authorization": f"Bearer {plain}"})
            assert r.status_code == 401


async def test_user_cannot_use_admin_routes(app_client, gw):
    await gw.auth.create_user("mallory", "mallory long pass 1", "user")
    h = await login(app_client, "mallory", "mallory long pass 1")
    assert (await app_client.get("/api/admin/users", headers=h)).status_code == 403
    assert (await app_client.post("/api/admin/users", headers=h, json={"username": "x" * 5, "password": "y" * 14, "role": "admin"})).status_code == 403
    assert (await app_client.put("/api/admin/settings/allow_signup", headers=h, json={"value": True})).status_code == 403


async def test_csrf_and_origin_enforced(app_client, gw):
    await gw.auth.create_user("frank", "frank long password 1", "user")
    h = await login(app_client, "frank", "frank long password 1")
    body = {"name": "x"}
    assert (await app_client.post("/api/me/api-keys", json=body, headers={"Origin": "http://test"})).status_code == 403          # no CSRF token
    assert (await app_client.post("/api/me/api-keys", json=body, headers={**h, "X-CSRF-Token": "wrong"})).status_code == 403
    assert (await app_client.post("/api/me/api-keys", json=body, headers={**h, "Origin": "http://evil.example"})).status_code == 403   # foreign origin
    assert (await app_client.post("/api/me/api-keys", json=body, headers={**h, "Sec-Fetch-Site": "cross-site"})).status_code == 403
    assert (await app_client.post("/api/me/api-keys", json=body, headers=h)).status_code == 200


async def test_login_throttle_and_generic_errors(app_client, gw):
    await gw.auth.create_user("grace", "grace long password 1", "user")
    # unknown user and wrong password are indistinguishable
    r = await app_client.post("/api/auth/login", json={"username": "nobody-here", "password": "whatever long"})
    assert r.status_code == 401 and r.json()["error"]["message"] == "Wrong username or password"
    codes = []
    for _ in range(8):
        r = await app_client.post("/api/auth/login", json={"username": "grace", "password": "wrong password!!"})
        codes.append(r.status_code)
        if r.status_code == 401:
            assert r.json()["error"]["message"] == "Wrong username or password"
    assert codes[:4] == [401] * 4 and codes[4] == 429       # 5 failures from one address -> locked
    r = await app_client.post("/api/auth/login", json={"username": "grace", "password": "grace long password 1"})
    assert r.status_code == 429 and "Retry-After" in r.headers   # even the right password is refused while locked


async def test_secrets_never_leak_and_are_encrypted_at_rest(gw, app_client, admin_h, with_keys, tmp_path):
    secret = "good-key-secret-0001"
    for path in ("/api/admin/overview", "/api/admin/models", "/api/admin/audit", "/api/admin/limits"):
        assert secret not in (await app_client.get(path, headers=admin_h)).text
    r = await app_client.post("/api/me/api-keys", headers=admin_h, json={"name": "mine"})
    plain = r.json()["key"]
    listing = (await app_client.get("/api/me/api-keys", headers=admin_h)).text
    assert plain not in listing and "key_hash" not in listing
    # raw bytes of the database + WAL contain neither the provider key nor the gateway key
    await gw.db.run(lambda c: c.execute("PRAGMA wal_checkpoint(FULL)"))
    raw = b"".join(p.read_bytes() for p in (tmp_path / "data").glob("gate.db*"))
    assert secret.encode() not in raw and plain.encode() not in raw


async def test_revoked_and_expired_keys_rejected(gw, app_client, admin_h, with_keys):
    r = await app_client.post("/api/me/api-keys", headers=admin_h, json={"name": "tmp"})
    key, kid = r.json()["key"], r.json()["info"]["id"]
    auth = {"Authorization": f"Bearer {key}"}
    assert (await app_client.get("/v1/models", headers=auth)).status_code == 200
    assert (await app_client.delete(f"/api/me/api-keys/{kid}", headers=admin_h)).status_code == 200
    assert (await app_client.get("/v1/models", headers=auth)).status_code == 401


async def test_users_cannot_revoke_each_others_keys(gw, app_client):
    a = await gw.auth.create_user("ann", "ann long password 11", "user")
    await gw.auth.create_user("bobby", "bobby long password 111", "user")
    plain, rec = await gw.auth.create_api_key(a["id"], "annkey", None)
    h = await login(app_client, "bobby", "bobby long password 111")
    assert (await app_client.delete(f"/api/me/api-keys/{rec['id']}", headers=h)).status_code == 404
    assert (await gw.auth.verify_api_key(plain, "1.2.3.4")).username == "ann"


async def test_body_limit_and_headers(gw, app_client):
    gw.settings.max_body_bytes = 1000
    app = create_app(gw)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/chat/completions", content=b"x" * 5000, headers={"Authorization": "Bearer sk-gate-" + "z" * 43})
        assert r.status_code == 413
        r = await c.get("/healthz")
        for h in ("x-content-type-options", "content-security-policy", "x-frame-options", "referrer-policy"):
            assert h in r.headers
        assert "script-src 'self'" in r.headers["content-security-policy"] and "unsafe-inline" not in r.headers["content-security-policy"]


async def test_signup_toggle_and_last_admin_guard(gw, app_client, admin_h):
    r = await app_client.post("/api/auth/signup", json={"username": "newbie", "password": "newbie long password"})
    assert r.status_code == 403
    assert (await app_client.put("/api/admin/settings/allow_signup", headers=admin_h, json={"value": True})).status_code == 200
    async with httpx.AsyncClient(transport=app_client._transport, base_url="http://test") as c2:
        r = await c2.post("/api/auth/signup", json={"username": "newbie", "password": "newbie long password"})
        assert r.status_code == 200 and r.json()["user"]["balance_credits"] == 0
    me = (await app_client.get("/api/auth/me")).json()["user"]
    r = await app_client.patch(f"/api/admin/users/{me['id']}", headers=admin_h, json={"active": False})
    assert r.status_code == 400 and r.json()["error"]["code"] == "last_admin"
    r = await app_client.patch(f"/api/admin/users/{me['id']}", headers=admin_h, json={"role": "user"})
    assert r.status_code == 400


async def test_deactivated_user_loses_session_and_keys(gw, app_client, admin_h):
    u = await gw.auth.create_user("temp", "temp long password 1", "user")
    plain, _ = await gw.auth.create_api_key(u["id"], "k", None)
    assert (await gw.auth.verify_api_key(plain, "1.1.1.1")).username == "temp"
    r = await app_client.patch(f"/api/admin/users/{u['id']}", headers=admin_h, json={"active": False})
    assert r.status_code == 200
    from apigate.errors import GateError
    with pytest.raises(GateError):
        await gw.auth.verify_api_key(plain, "1.1.1.1")


async def test_web_chat_flow_shares_credits_with_api(gw, app_client, with_keys):
    u = await gw.auth.create_user("chatty", "chatty long password", "user")
    await gw.credits.adjust(u["id"], 10_000_000, None, "grant")
    h = await login(app_client, "chatty", "chatty long password")
    models = (await app_client.get("/api/models", headers=h)).json()["models"]
    assert any(m["id"] == "openai/gpt-fake" for m in models)
    cid = (await app_client.post("/api/conversations", headers=h, json={})).json()["id"]
    r = await app_client.post(f"/api/conversations/{cid}/messages", headers=h, json={"content": "hello there", "model": "openai/gpt-fake"})
    assert r.status_code == 200
    events = [json.loads(e[6:]) for e in r.text.split("\n\n") if e.startswith("data: ")]
    assert "".join(e.get("delta", "") for e in events).strip() == "echo: hello there"
    assert events[-1].get("done") is True
    import asyncio
    await asyncio.sleep(0.1)
    conv = (await app_client.get(f"/api/conversations/{cid}", headers=h)).json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"] and conv["conversation"]["title"].startswith("hello")
    ledger = (await app_client.get("/api/me/ledger", headers=h)).json()["entries"]
    assert ledger[0]["source"] == "chat" and ledger[0]["cost_credits"] > 0           # same ledger as API usage
    # strangers cannot read it
    await gw.auth.create_user("snoop", "snoop long password 1", "user")
    async with httpx.AsyncClient(transport=app_client._transport, base_url="http://test") as c2:
        h2 = await login(c2, "snoop", "snoop long password 1")
        assert (await c2.get(f"/api/conversations/{cid}", headers=h2)).status_code == 404
        assert (await c2.post(f"/api/conversations/{cid}/messages", headers=h2, json={"content": "x", "model": "openai/gpt-fake"})).status_code == 404


async def test_admin_can_manage_providers_limits_and_aliases(gw, app_client, admin_h):
    r = await app_client.post("/api/admin/providers/openai/keys", headers=admin_h, json={"label": "a", "secret": "good-key-secret-0009"})
    assert r.status_code == 200 and r.json()["models"] == 4
    r = await app_client.post("/api/admin/providers/openai/keys", headers=admin_h, json={"label": "dup", "secret": "good-key-secret-0009"})
    assert r.status_code == 409
    r = await app_client.post("/api/admin/providers/openai/keys", headers=admin_h, json={"label": "b", "secret": "bad-key-secret-0001"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "key_rejected" and "bad-key-secret-0001" not in r.text
    r = await app_client.put("/api/admin/limits", headers=admin_h, json={"provider": "openai", "pattern": "gpt-*", "limits": {"rpm": 5, "tpm": 1000, "rpd": 20}})
    assert r.status_code == 200
    r = await app_client.put("/api/admin/limits", headers=admin_h, json={"provider": "openai", "pattern": "x", "limits": {"bogus": 1}})
    assert r.status_code == 400
    r = await app_client.put("/api/admin/aliases/smart", headers=admin_h, json={"targets": ["openai/gpt-fake"], "mode": "fallback"})
    assert r.status_code == 200
    r = await app_client.put("/api/admin/aliases/bad", headers=admin_h, json={"targets": ["openai/does-not-exist"]})
    assert r.status_code == 400
    r = await app_client.patch("/api/admin/providers/openai", headers=admin_h, json={"strategy": "fill", "fill_threshold": 0.7})
    assert r.status_code == 200
    r = await app_client.patch("/api/admin/providers/openai", headers=admin_h, json={"base_url": "http://169.254.169.254/latest"})
    assert r.status_code == 400
    audit = (await app_client.get("/api/admin/audit", headers=admin_h)).json()["entries"]
    assert any(a["action"] == "provider_key_add" for a in audit)
