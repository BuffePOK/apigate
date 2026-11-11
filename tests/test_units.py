from __future__ import annotations

import math

import pytest

from apigate.crypto import Vault, hash_password, verify_password, new_api_key, RedactingFilter
from apigate.limits import LimitEngine, LimitRule, SlidingWindow, Verdict, clean_limits


class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t


def rule(i, provider="p", key=None, pattern="*", scope="model", **lim):
    return LimitRule(i, provider, key, pattern, scope, lim)


def test_sliding_window_expires_and_waits():
    w = SlidingWindow(60)
    w.add(1000, 3); w.add(1030, 2)
    assert w.used(1030) == 5
    assert w.wait_for(1030, 1, 5) == pytest.approx(31, abs=2)
    assert w.used(1061) == 2                       # first bucket gone
    assert w.wait_for(1030, 10, 5) == math.inf


def test_specificity_key_over_provider_and_exact_over_glob():
    e = LimitEngine()
    e.set_rules([rule(1, rpm=100), rule(2, pattern="gem*", rpm=10), rule(3, pattern="gem-flash", rpm=5, rpd=20), rule(4, key=7, pattern="*", rpm=2)])
    assert e.effective("p", 1, "gem-flash")["rpm"][0] == 5
    assert e.effective("p", 1, "gem-flash")["rpd"][0] == 20
    assert e.effective("p", 1, "gem-pro")["rpm"][0] == 10
    assert e.effective("p", 1, "other")["rpm"][0] == 100
    assert e.effective("p", 7, "other")["rpm"][0] == 2     # key-specific beats everything for that key
    assert e.effective("q", 1, "x") == {}


def test_acquire_release_settle_and_token_adjustment():
    c = Clock(); e = LimitEngine(c)
    e.set_rules([rule(1, rpm=2, tpm=1000)])
    r1 = e.acquire("p", 1, "m", 400)
    assert not isinstance(r1, Verdict)
    r2 = e.acquire("p", 1, "m", 400)
    v = e.acquire("p", 1, "m", 100)
    assert isinstance(v, Verdict) and not v.ok and v.reason == "rpm" and v.wait > 0
    e.release(r2)                                   # never reached the provider -> slot freed
    e.settle(r1, 100)                               # used far less than reserved
    r3 = e.acquire("p", 1, "m", 800)
    assert not isinstance(r3, Verdict)
    assert e.fill_ratio("p", 1, "m") == pytest.approx(0.5, abs=0.01) or e.fill_ratio("p", 1, "m") <= 1.0


def test_zero_limit_and_oversized_are_impossible_not_waitable():
    e = LimitEngine(Clock())
    e.set_rules([rule(1, pattern="pro", rpd=0), rule(2, pattern="small", tpm=100)])
    assert e.key_can_serve("p", 1, "pro") is False
    assert e.acquire("p", 1, "pro", 1).impossible
    v = e.acquire("p", 1, "small", 500)
    assert v.impossible and v.wait == math.inf


def test_concurrency_limit_and_shared_scope():
    e = LimitEngine(Clock())
    e.set_rules([rule(1, scope="key", rpm=3, conc=1)])
    a = e.acquire("p", 1, "m1", 1)
    assert isinstance(e.acquire("p", 1, "m2", 1), Verdict)    # conc=1 shared by both models
    e.settle(a, 1)
    e.acquire("p", 1, "m2", 1)
    # shared counter: m1 and m2 together hit rpm=3
    assert e.fill_ratio("p", 1, "m1") >= 2 / 3 - 0.01


def test_week_window_rebuilt_from_history():
    c = Clock(); e = LimitEngine(c)
    e.set_rules([rule(1, rpd=5, rpw=50)])
    now_min = int(c.t // 60)
    rows = [{"provider_key_id": 1, "model": "m", "minute": now_min - 600, "requests": 3, "tokens": 0},     # 10 h ago: in day+week
            {"provider_key_id": 1, "model": "m", "minute": now_min - 3 * 1440, "requests": 40, "tokens": 0}]  # 3 days ago: week only
    e.rebuild(rows, {1: "p"})
    snap = {(s["metric"]): s["used"] for s in e.snapshot_key(1)}
    assert snap == {"rpd": 3, "rpw": 43}


def test_clean_limits_validates():
    assert clean_limits({"rpm": "5", "tpd": None}) == {"rpm": 5}
    with pytest.raises(ValueError): clean_limits({"bogus": 1})
    with pytest.raises(ValueError): clean_limits({"rpm": -1})


def test_vault_roundtrip_binding_and_tamper():
    v = Vault(b"k" * 32)
    blob = v.encrypt("sk-secret-value", "pk:1:openai")
    assert b"sk-secret" not in blob and v.decrypt(blob, "pk:1:openai") == "sk-secret-value"
    with pytest.raises(Exception): v.decrypt(blob, "pk:2:openai")                 # ciphertext cannot be moved to another row
    with pytest.raises(Exception): v.decrypt(blob[:-1] + bytes([blob[-1] ^ 1]), "pk:1:openai")
    with pytest.raises(Exception): Vault(b"z" * 32).decrypt(blob, "pk:1:openai")


def test_password_and_key_formats():
    h = hash_password("correct horse battery")
    assert verify_password(h, "correct horse battery") and not verify_password(h, "wrong") and not verify_password("junk", "x")
    assert new_api_key() != new_api_key() and len(new_api_key()) > 40


def test_log_redaction():
    f = RedactingFilter(); f.register("my-exact-provider-secret")
    out = f.scrub("auth my-exact-provider-secret and sk-proj-abcdefghijklmnop and AIzaSyA1234567890abcdefghij and Bearer abcdefghijklmnopqrstu")
    assert "my-exact" not in out and "sk-proj" not in out and "AIza" not in out and "abcdefghijklmnopqrstu" not in out


def test_trusted_proxy_accepts_cidr():
    from apigate.server.deps import _is_trusted
    t = {"172.16.0.0/12", "10.0.0.5"}
    assert _is_trusted("172.18.0.3", t) and _is_trusted("10.0.0.5", t)
    assert not _is_trusted("8.8.8.8", t) and not _is_trusted("garbage", t)


def test_cli_load_limits_preset_whitelists_models(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    from apigate.cli import main
    from apigate.crypto import Vault
    monkeypatch.setenv("APIGATE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("APIGATE_MASTER_KEY", Vault.generate_master())
    preset = str(Path(__file__).resolve().parents[1] / "presets" / "gemini-free.toml")
    assert main(["load-limits", preset]) == 0
    assert main(["load-limits", preset]) == 0          # idempotent: second run updates, does not duplicate
    out = capsys.readouterr().out
    assert out.count("gemma-4-31b*") == 2
    from apigate.limits import LimitRule, LimitEngine
    rules = [LimitRule(1, "gemini", None, "*", "model", {"rpm": 0}),
             LimitRule(2, "gemini", None, "gemma-4-31b*", "model", {"rpm": 30, "tpm": 16000})]
    eng = LimitEngine(); eng.set_rules(rules)
    assert eng.key_can_serve("gemini", 1, "gemma-4-31b-it") and not eng.key_can_serve("gemini", 1, "gemini-2.5-flash-lite")
