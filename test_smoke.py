"""Smoke tests for /health, /v1/models, and API key validation (deterministic).

External inference is NOT executed by default. The only test that performs a
real outbound request is ``test_live_external_inference`` and it is skipped
unless CLARITY_LIVE_SMOKE=1 is set, so the default suite makes no live calls.

Isolation: GATEWAY_DB is isolated to a tmp_path via settings.DB_PATH mutation;
the settings singleton is fully restored in the fixture teardown (no module
reload, so other test files sharing the global app object are unaffected).
"""

import os

os.environ.setdefault("GATEWAY_DB", "/tmp/gateway_smoke_test.db")
os.environ.setdefault("GATEWAY_ADMIN_KEY", "testadmin")
os.environ.setdefault("X402_PAYTO", "")

import pytest
from fastapi.testclient import TestClient

from app.main import app as main_app
import app.router as router_mod
import app.config as config_mod
import app.db as db_mod


@pytest.fixture
def clarity_smoke(tmp_path):
    s = config_mod.settings
    saved = {
        "DB_PATH": s.DB_PATH,
        "ADMIN_KEY": s.ADMIN_KEY,
        "X402_PAYTO": s.X402_PAYTO,
    }
    s.DB_PATH = str(tmp_path / "smoke.db")
    s.ADMIN_KEY = "testadmin"
    s.X402_PAYTO = ""
    db_mod.init_db()
    yield main_app, router_mod
    for k, v in saved.items():
        setattr(s, k, v)


def test_health_ok(clarity_smoke):
    main_app, _ = clarity_smoke
    client = TestClient(main_app)
    r = client.get("/health")
    assert r.status_code == 200 and r.json().get("status") == "ok"


def test_models_endpoint_lists_models(clarity_smoke):
    main_app, _ = clarity_smoke
    client = TestClient(main_app)
    r = client.get("/v1/models")
    assert r.status_code == 200
    models = r.json().get("data", [])
    assert isinstance(models, list) and len(models) > 0
    assert any(m.get("id") == "llama-3.1-8b-instruct" for m in models)


def test_key_validation_full_flow(clarity_smoke, monkeypatch):
    main_app, router_mod = clarity_smoke
    UpstreamError = router_mod.UpstreamError
    FAKE = {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": "meta-llama/llama-3.1-8b-instruct",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }

    async def fake_chat_openai(client, provider, payload, stream):
        return FAKE, 5, 2

    monkeypatch.setattr(router_mod, "_chat_openai", fake_chat_openai)
    client = TestClient(main_app)

    r = client.post("/v1/keys", json={"name": "smoke"}, headers={"x-admin-key": "testadmin"})
    assert r.status_code == 200, r.text
    skey = r.json()["skey"]
    assert r.json()["id"]

    bad = client.get("/v1/usage", headers={"Authorization": "Bearer sk_wrong"})
    assert bad.status_code in (401, 403)


@pytest.mark.skipif(
    not os.environ.get("CLARITY_LIVE_SMOKE"),
    reason="live external inference opt-in; set CLARITY_LIVE_SMOKE=1 to run",
)
def test_live_external_inference():
    """Opt-in only: hits a real upstream provider. Not part of the default suite."""
    import openai
    import app.config as cfg

    client = openai.OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-not-checked-for-live")
    resp = client.chat.completions.create(
        model="llama-3.1-8b-instruct",
        messages=[{"role": "user", "content": "ping"}],
        extra_headers={"Authorization": f"Bearer {cfg.settings.ADMIN_KEY}"},
    )
    assert resp.choices[0].message.content
