"""Failover tests for POST /v1/chat/completions.

These verify provider failover and usage accounting without any real network
calls. The upstream chat function (``app.router._chat_openai``) is monkeypatched
per test, so no provider is contacted.

Isolation (no module reload, so the shared global app object used by other test
files is never disturbed):
- GATEWAY_DB is isolated to a tmp_path by mutating ``settings.DB_PATH``
- GATEWAY_ADMIN_KEY is set directly on the settings singleton
- every mutated setting attribute is restored in the fixture teardown
"""

import os

os.environ.setdefault("GATEWAY_DB", "/tmp/gateway_failover_test.db")
os.environ.setdefault("GATEWAY_ADMIN_KEY", "testadmin")
os.environ.setdefault("X402_PAYTO", "")

import pytest
from fastapi.testclient import TestClient

from app.main import app as main_app
import app.router as router_mod
import app.config as config_mod
import app.db as db_mod


@pytest.fixture
def clarity_failover(tmp_path):
    s = config_mod.settings
    saved = {
        "DB_PATH": s.DB_PATH,
        "ADMIN_KEY": s.ADMIN_KEY,
        "X402_PAYTO": s.X402_PAYTO,
    }
    s.DB_PATH = str(tmp_path / "failover.db")
    s.ADMIN_KEY = "testadmin"
    s.X402_PAYTO = ""
    db_mod.init_db()
    yield main_app, router_mod
    for k, v in saved.items():
        setattr(s, k, v)


FAKE = {
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "model": "meta-llama/llama-3.1-8b-instruct",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "failover works"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
}


def test_failover_first_provider_down_then_fallback_and_usage(clarity_failover, monkeypatch):
    main_app, router_mod = clarity_failover
    UpstreamError = router_mod.UpstreamError

    async def fake_chat_openai(client, provider, payload, stream):
        if provider == "deepinfra":
            raise UpstreamError(500, "deepinfra is down")
        assert provider == "together", f"expected fallback to together, got {provider}"
        return FAKE, 12, 3

    monkeypatch.setattr(router_mod, "_chat_openai", fake_chat_openai)
    client = TestClient(main_app)

    r = client.post("/v1/keys", json={"name": "failover-test"}, headers={"x-admin-key": "testadmin"})
    assert r.status_code == 200, r.text
    skey = r.json()["skey"]
    kid = r.json()["id"]
    r = client.post("/v1/credits", json={"key_id": kid, "amount": 10.0}, headers={"x-admin-key": "testadmin"})
    assert r.status_code == 200

    r = client.post(
        "/v1/chat/completions",
        json={"model": "llama-3.1-8b-instruct", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "failover works"

    usage = client.get("/v1/usage", headers={"Authorization": f"Bearer {skey}"}).json()
    assert usage["prompt_tokens"] == 12 and usage["completion_tokens"] == 3
    assert usage["balance_usd"] < 10.0


def test_failover_zero_balance_rejected(clarity_failover, monkeypatch):
    main_app, router_mod = clarity_failover
    UpstreamError = router_mod.UpstreamError

    async def fake_chat_openai(client, provider, payload, stream):
        if provider == "deepinfra":
            raise UpstreamError(500, "deepinfra is down")
        assert provider == "together"
        return FAKE, 12, 3

    monkeypatch.setattr(router_mod, "_chat_openai", fake_chat_openai)
    client = TestClient(main_app)

    r0 = client.post("/v1/keys", json={"name": "nofunds"}, headers={"x-admin-key": "testadmin"})
    assert r0.status_code == 200
    skey0 = r0.json()["skey"]
    r = client.post(
        "/v1/chat/completions",
        json={"model": "llama-3.1-8b-instruct", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey0}"},
    )
    assert r.status_code == 402


def test_failover_all_providers_down_returns_502(clarity_failover, monkeypatch):
    main_app, router_mod = clarity_failover
    UpstreamError = router_mod.UpstreamError

    async def fake_all_down(client, provider, payload, stream):
        raise UpstreamError(500, "down")

    monkeypatch.setattr(router_mod, "_chat_openai", fake_all_down)
    client = TestClient(main_app)

    r = client.post("/v1/keys", json={"name": "funded"}, headers={"x-admin-key": "testadmin"})
    assert r.status_code == 200
    skey = r.json()["skey"]
    client.post("/v1/credits", json={"key_id": r.json()["id"], "amount": 10.0}, headers={"x-admin-key": "testadmin"})
    r = client.post(
        "/v1/chat/completions",
        json={"model": "llama-3.1-8b-instruct", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 502
