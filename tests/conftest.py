from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import fake_upstream  # noqa: E402
from apigate import Gateway, Settings  # noqa: E402
from apigate.crypto import Vault  # noqa: E402

FAKE = "http://fake.test"


def make_settings(tmp_path, **kw) -> Settings:
    s = Settings(data_dir=str(tmp_path / "data"), master_key=Vault.generate_master(), allow_insecure_upstream=True,
                 max_queue_wait_s=0.0, password_min_length=10, api_key_rpm=0, chat_user_rpm=0)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


@pytest_asyncio.fixture
async def gw(tmp_path):
    fake_upstream.reset()
    g = Gateway(make_settings(tmp_path), transport=httpx.ASGITransport(app=fake_upstream.app))
    await g.start(background=False)
    await g.update_provider("openai", base_url=f"{FAKE}/oa/v1")
    await g.update_provider("deepseek", base_url=f"{FAKE}/oa/v1")
    await g.update_provider("anthropic", base_url=f"{FAKE}/an/v1")
    await g.update_provider("gemini", base_url=f"{FAKE}/gem/v1beta/openai")
    yield g
    await g.stop()


@pytest_asyncio.fixture
async def user(gw):
    u = await gw.auth.create_user("alice", "correct horse battery", "user")
    await gw.credits.adjust(u["id"], 5_000_000_000, None, "test credits")
    return u


async def principal_for(gw, user, source="api"):
    plain, rec = await gw.auth.create_api_key(user["id"], "t", None)
    return await gw.auth.verify_api_key(plain, "127.0.0.1"), plain
