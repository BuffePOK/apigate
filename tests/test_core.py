from __future__ import annotations

import asyncio
import json

import pytest

import fake_upstream
from apigate.errors import GateError
from conftest import principal_for


def chat_calls(key):
    return [c for c in fake_upstream.calls[key] if c["path"] == "chat"]


async def add_keys(gw, provider, keys):
    for k in keys:
        await gw.add_provider_key(provider, k, k)


MSG = {"model": "openai/gpt-fake", "messages": [{"role": "user", "content": "hello"}]}


async def test_models_discovered_and_filtered(gw, user):
    await add_keys(gw, "openai", ["good-0001", "noacc-0001"])
    ids = {m["id"] for m in gw.public_models()}
    assert "openai/gpt-fake" in ids and "openai/text-embedding-fake" in ids
    assert "openai/whisper-fake" not in ids          # audio is listed in admin but not routable
    assert "openai/secret-model" in ids
    # the model only one key can serve must never be sent to the other key
    pr, _ = await principal_for(gw, user)
    for _ in range(4):
        await gw.chat(pr, {"model": "openai/secret-model", "messages": [{"role": "user", "content": "x"}]})
    assert chat_calls("noacc-0001") == [] and len(chat_calls("good-0001")) == 4


async def test_round_robin_alternates(gw, user):
    await add_keys(gw, "openai", ["good-0001", "good-0002", "good-0003"])
    pr, _ = await principal_for(gw, user)
    for _ in range(9):
        r = await gw.chat(pr, dict(MSG))
        assert r.body["choices"][0]["message"]["content"] == "echo: hello"
    assert [len(chat_calls(k)) for k in ("good-0001", "good-0002", "good-0003")] == [3, 3, 3]


async def test_random_uses_all_keys(gw, user):
    await add_keys(gw, "openai", ["good-0001", "good-0002"])
    await gw.update_provider("openai", strategy="random")
    pr, _ = await principal_for(gw, user)
    for _ in range(40):
        await gw.chat(pr, dict(MSG))
    a, b = len(chat_calls("good-0001")), len(chat_calls("good-0002"))
    assert a + b == 40 and a > 5 and b > 5


async def test_fill_to_70_percent_then_next(gw, user):
    await add_keys(gw, "openai", ["good-0001", "good-0002"])
    await gw.update_provider("openai", strategy="fill", fill_threshold=0.7)
    await gw.set_limit_rule(None, "openai", None, "gpt-fake", "model", {"rpm": 10})
    pr, _ = await principal_for(gw, user)
    seq = []
    for _ in range(14):
        await gw.chat(pr, dict(MSG))
        seq.append("A" if len(chat_calls("good-0001")) > seq.count("A") else "B")
    assert seq == ["A"] * 7 + ["B"] * 7


async def test_failover_and_cooldown_on_429(gw, user):
    await add_keys(gw, "openai", ["limited-0001", "good-0001"])
    pr, _ = await principal_for(gw, user)
    for _ in range(5):
        r = await gw.chat(pr, dict(MSG))
        assert r.body["choices"]
    assert len(chat_calls("limited-0001")) == 1          # tried once, then cooled down
    assert len(chat_calls("good-0001")) == 5
    ks = gw.pools["openai"].keys
    limited = [k for k in ks.values() if k.label == "limited-0001"][0]
    assert limited.model_cooldown["gpt-fake"] > gw.clock()


async def test_bad_key_is_disabled_persistently(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    # a key that was valid when added but is revoked later
    await gw.add_provider_key("openai", "bad-0001", "bad-0001", verify=False)
    pr, _ = await principal_for(gw, user)
    for _ in range(3):
        await gw.chat(pr, dict(MSG))
    row = await gw.db.fetchone("SELECT status FROM provider_keys WHERE label='bad-0001'")
    await asyncio.sleep(0.05)
    row = await gw.db.fetchone("SELECT status FROM provider_keys WHERE label='bad-0001'")
    assert row["status"] == "auth_failed"
    assert len(chat_calls("bad-0001")) == 1


async def test_all_keys_at_capacity_returns_429_with_retry_after(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    await gw.set_limit_rule(None, "openai", None, "*", "model", {"rpm": 2})
    pr, _ = await principal_for(gw, user)
    await gw.chat(pr, dict(MSG))
    await gw.chat(pr, dict(MSG))
    with pytest.raises(GateError) as e:
        await gw.chat(pr, dict(MSG))
    assert e.value.status == 429 and e.value.retry_after and e.value.retry_after >= 1
    assert len(chat_calls("good-0001")) == 2             # the 3rd request never reached the provider


async def test_limit_zero_hides_model_and_oversized_request_rejected(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    await gw.set_limit_rule(None, "openai", None, "secret-model", "model", {"rpd": 0})
    assert "openai/secret-model" not in {m["id"] for m in gw.public_models()}
    await gw.set_limit_rule(None, "openai", None, "gpt-fake", "model", {"tpm": 100})
    pr, _ = await principal_for(gw, user)
    big = {"model": "openai/gpt-fake", "messages": [{"role": "user", "content": "x" * 5000}]}
    with pytest.raises(GateError) as e:
        await gw.chat(pr, big)
    assert e.value.status == 400 and e.value.code == "request_too_large"


async def test_bare_name_alias_and_ambiguity(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    await add_keys(gw, "deepseek", ["good-0002"])
    pr, _ = await principal_for(gw, user)
    with pytest.raises(GateError) as e:                # gpt-fake exists at two providers
        await gw.chat(pr, {"model": "gpt-fake", "messages": [{"role": "user", "content": "x"}]})
    assert e.value.code == "ambiguous_model"
    await gw.set_alias("smart", ["deepseek/gpt-fake", "openai/gpt-fake"], "fallback")
    r = await gw.chat(pr, {"model": "smart", "messages": [{"role": "user", "content": "x"}]})
    assert r.meta["provider"] == "deepseek"


async def test_alias_fallback_when_first_provider_dead(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    await gw.add_provider_key("deepseek", "flaky-0001", "flaky-0001")
    await gw.set_alias("smart", ["deepseek/gpt-fake", "openai/gpt-fake"], "fallback")
    pr, _ = await principal_for(gw, user)
    r = await gw.chat(pr, {"model": "smart", "messages": [{"role": "user", "content": "x"}]})
    assert r.meta["provider"] == "openai"


async def test_embeddings_and_kind_mismatch(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    pr, _ = await principal_for(gw, user)
    r = await gw.embeddings(pr, {"model": "openai/text-embedding-fake", "input": ["a", "b"]})
    assert len(r.body["data"]) == 2
    with pytest.raises(GateError) as e:
        await gw.chat(pr, {"model": "openai/text-embedding-fake", "messages": [{"role": "user", "content": "x"}]})
    assert e.value.status == 404


async def test_api_key_model_allowlist(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    plain, _ = await gw.auth.create_api_key(user["id"], "restricted", None, allowed_models=["anthropic/*"])
    pr = await gw.auth.verify_api_key(plain, "1.1.1.1")
    with pytest.raises(GateError) as e:
        await gw.chat(pr, dict(MSG))
    assert e.value.status == 403
    assert gw.public_models(pr) == []


# ------------------------------------------------------------------ streaming
async def collect(result):
    out = []
    async for ch in result.stream:
        out.append(ch)
    return out


async def test_stream_openai_compat_usage_hidden_unless_requested(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    pr, _ = await principal_for(gw, user)
    r = await gw.chat(pr, dict(MSG), stream=True)
    chunks = await collect(r)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
    assert text.strip() == "echo: hello"
    assert not any(c.get("usage") for c in chunks)       # client did not ask for it
    assert all(c["model"] == "openai/gpt-fake" for c in chunks)
    # the upstream was nevertheless asked for usage so we can bill exactly
    assert chat_calls("good-0001")[0]["body"]["stream_options"] == {"include_usage": True}
    r = await gw.chat(pr, {**MSG, "stream_options": {"include_usage": True}}, stream=True)
    assert any(c.get("usage") for c in await collect(r))


async def test_stream_client_disconnect_still_settles(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    pr, _ = await principal_for(gw, user)
    before = await gw.credits.balance(user["id"])
    r = await gw.chat(pr, dict(MSG), stream=True)
    it = r.stream.__aiter__()
    await it.__anext__()
    await it.aclose()                                    # consumer walks away
    await asyncio.sleep(0.1)
    after = await gw.credits.balance(user["id"])
    assert after < before                                 # charged something for what was consumed
    assert not await gw.db.fetchall("SELECT * FROM holds")
    assert gw.engine.inflight == {k: 0 for k in gw.engine.inflight}


async def test_stream_never_started_is_released(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    pr, _ = await principal_for(gw, user)
    r = await gw.chat(pr, dict(MSG), stream=True)
    await r.stream.aclose()
    await asyncio.sleep(0.1)
    assert not await gw.db.fetchall("SELECT * FROM holds")


# ------------------------------------------------------------------ credits
async def test_usage_is_charged_and_ledgered(gw, user):
    await add_keys(gw, "openai", ["good-0001"])
    pr, _ = await principal_for(gw, user)
    b0 = await gw.credits.balance(user["id"])
    r = await gw.chat(pr, dict(MSG))
    u = r.body["usage"]
    expect = u["prompt_tokens"] * 1.0 + u["completion_tokens"] * 3.0
    b1 = await gw.credits.balance(user["id"])
    assert b0 - b1 == pytest.approx(expect, abs=1)
    rows = await gw.credits.ledger(user["id"], 5)
    assert rows[0]["kind"] == "usage" and rows[0]["provider"] == "openai" and rows[0]["prompt_tokens"] == u["prompt_tokens"]
    assert rows[0]["balance_after"] == b1


async def test_insufficient_credits_blocks_before_upstream(gw):
    u = await gw.auth.create_user("bob", "another long password", "user")
    await add_keys(gw, "openai", ["good-0001"])
    pr, _ = await principal_for(gw, u)
    with pytest.raises(GateError) as e:
        await gw.chat(pr, dict(MSG))
    assert e.value.status == 402 and e.value.code == "insufficient_quota"
    assert chat_calls("good-0001") == []


async def test_concurrent_requests_cannot_overspend(gw):
    u = await gw.auth.create_user("carol", "yet another password", "user")
    await add_keys(gw, "openai", ["good-0001"])
    # hold per request = est_prompt + default_max_tokens(8192)*3 ~= 24.6k uc; fund exactly 3 holds
    gw.settings.default_max_tokens = 8192
    await gw.credits.adjust(u["id"], 3 * 24_700, None, "fund")
    pr, _ = await principal_for(gw, u)
    results = await asyncio.gather(*[gw.chat(pr, dict(MSG)) for _ in range(12)], return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    denied = [r for r in results if isinstance(r, GateError) and r.status == 402]
    assert len(ok) + len(denied) == 12 and len(denied) >= 1
    bal = await gw.credits.balance(u["id"])
    assert bal >= 0 and not await gw.db.fetchall("SELECT * FROM holds")


async def test_failed_request_costs_nothing(gw, user):
    await gw.add_provider_key("openai", "flaky-0001", "flaky-0001")
    pr, _ = await principal_for(gw, user)
    b0 = await gw.credits.balance(user["id"])
    with pytest.raises(GateError):
        await gw.chat(pr, dict(MSG))
    assert await gw.credits.balance(user["id"]) == b0
    assert not await gw.db.fetchall("SELECT * FROM holds")


async def test_unlimited_user_not_charged(gw):
    u = await gw.auth.create_user("root2", "a very long admin pw", "admin")
    await gw.db.execute("UPDATE users SET unlimited=1 WHERE id=?", (u["id"],))
    await add_keys(gw, "openai", ["good-0001"])
    pr, _ = await principal_for(gw, u)
    await gw.chat(pr, dict(MSG))
    assert await gw.credits.balance(u["id"]) == 0
    row = (await gw.credits.ledger(u["id"], 1))[0]
    assert row["delta_uc"] == 0 and row["cost_uc"] > 0


async def test_stale_holds_refunded_on_restart(gw, user):
    hid = await gw.credits.hold(user["id"], 1000)
    assert await gw.db.fetchone("SELECT 1 FROM holds WHERE id=?", (hid,))
    b = await gw.credits.balance(user["id"])
    assert await gw.credits.release_all() == 1
    assert await gw.credits.balance(user["id"]) == b + 1000


# ---------------------------------------------------------------- gemini / claude
async def test_gemini_native_listing_classifies_models(gw, user):
    await gw.add_provider_key("gemini", "good-0001", "good-0001")
    ids = {m["id"]: m for m in gw.public_models()}
    assert ids["gemini/gemini-flash-fake"]["kind"] == "chat"
    assert ids["gemini/gemini-embed-fake"]["kind"] == "embedding"
    assert "gemini/gemini-aqa" not in ids
    # the key must go in a header, never the URL
    assert all("good-0001" not in c.get("qs", "") for c in fake_upstream.calls["good-0001"])


async def test_anthropic_text_and_tools(gw, user):
    await gw.add_provider_key("anthropic", "good-0001", "good-0001")
    pr, _ = await principal_for(gw, user)
    r = await gw.chat(pr, {"model": "anthropic/claude-fake", "temperature": 1.7,
                           "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]})
    assert r.body["choices"][0]["message"]["content"] == "claude says hi"
    assert r.body["usage"] == {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
    sent = [c for c in fake_upstream.calls["good-0001"] if c["path"] == "messages"][0]
    assert sent["body"]["system"] == "be brief" and sent["body"]["temperature"] == 1.0 and sent["body"]["max_tokens"] > 0
    assert sent["headers"]["anthropic-version"] and "authorization" not in sent["headers"]

    tools = [{"type": "function", "function": {"name": "weather", "description": "d", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    body = {"model": "anthropic/claude-fake", "tools": tools, "messages": [{"role": "user", "content": "please use tool"}]}
    r = await gw.chat(pr, body)
    ch = r.body["choices"][0]
    assert ch["finish_reason"] == "tool_calls"
    tc = ch["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "weather" and json.loads(tc["function"]["arguments"]) == {"city": "Paris"}
    # round trip: feed the tool result back
    follow = {"model": "anthropic/claude-fake", "tools": tools, "messages": [
        {"role": "user", "content": "please use tool"},
        {"role": "assistant", "content": None, "tool_calls": [tc]},
        {"role": "tool", "tool_call_id": tc["id"], "content": "sunny"}]}
    await gw.chat(pr, follow)
    sent = [c for c in fake_upstream.calls["good-0001"] if c["path"] == "messages"][-1]["body"]
    assert sent["messages"][1]["content"][0]["type"] == "tool_use"
    assert sent["messages"][2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tc["id"], "content": "sunny"}]}


async def test_anthropic_stream_translation(gw, user):
    await gw.add_provider_key("anthropic", "good-0001", "good-0001")
    pr, _ = await principal_for(gw, user)
    r = await gw.chat(pr, {"model": "anthropic/claude-fake", "stream_options": {"include_usage": True},
                           "messages": [{"role": "user", "content": "hi"}]}, stream=True)
    chunks = await collect(r)
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"]) == "claude says hi"
    assert [c["choices"][0]["finish_reason"] for c in chunks if c["choices"] and c["choices"][0]["finish_reason"]] == ["stop"]
    assert chunks[-1]["usage"] == {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}

    tools = [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object", "properties": {}}}}]
    r = await gw.chat(pr, {"model": "anthropic/claude-fake", "tools": tools, "messages": [{"role": "user", "content": "use tool"}]}, stream=True)
    chunks = await collect(r)
    deltas = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    first = next(d for d in deltas if d.get("tool_calls"))["tool_calls"][0]
    assert first["function"]["name"] == "weather" and first["index"] == 0
    args = "".join(tc["function"]["arguments"] for d in deltas for tc in d.get("tool_calls", []))
    assert json.loads(args) == {"city": "Paris"}


async def test_provider_name_aliases_and_helpful_error(gw):
    out = await gw.add_provider_key("claude", "x", "good-key-secret-7777")
    assert out["provider"] == "anthropic"
    with pytest.raises(GateError) as e:
        await gw.add_provider_key("nope", "x", "good-key-secret-8888")
    assert "anthropic" in e.value.message and "gemini" in e.value.message


async def test_transient_5xx_is_retried_on_the_same_key(gw, user):
    await add_keys(gw, "openai", ["blip-0001"])
    pr, _ = await principal_for(gw, user)
    out = await gw.chat(pr, dict(MSG))
    assert out is not None
    assert len(chat_calls("blip-0001")) == 3
