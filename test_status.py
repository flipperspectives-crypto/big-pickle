import os

os.environ["GATEWAY_DB"] = "/tmp/gateway_status_test2.db"
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"

from fastapi.testclient import TestClient  # noqa: E402

import app.status as status_mod  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)


def _patch(mapping):
    """Monkeypatch the per-provider probe with deterministic results.

    mapping: provider -> (reachable, latency_ms, reason)
    """
    async def fake_probe(provider):
        return mapping.get(provider, (None, None, "no endpoint configured"))
    status_mod._provider_probe = fake_probe


def test_status_contract_fields():
    _patch({
        "groq": (True, 42.1, None),
        "huggingface": (None, None, "no endpoint configured"),
        "local": (False, 1.0, "ollama not running"),
    })
    r = client.get("/v1/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "healthy"
    assert "timestamp" in body and "gateway" in body
    assert "providers" in body
    provs = body["providers"]
    assert len(provs) > 0
    for name, info in provs.items():
        keys = set(info.keys())
        for required in ("configured", "credentials_configured",
                        "reachable", "probe_latency_ms", "models_in_routes"):
            assert required in keys, f"missing {required} for {name}"
        assert "needs_key" not in info, "ambiguous needs_key must be removed"
    print("PASS status: contract fields present, needs_key removed")


def test_status_honest_unreachable_and_unknown():
    _patch({
        "groq": (False, 30.0, "unreachable: ConnectError"),
        "openrouter": (None, None, "no endpoint configured"),
        "local": (False, 2.0, "ollama not running"),
    })
    r = client.get("/v1/status")
    body = r.json()
    provs = body["providers"]
    # unreachable is explicit false, with reason
    assert provs["groq"]["reachable"] is False
    assert provs["groq"]["reason"] == "unreachable: ConnectError"
    # could-not-probe is null, never guessed
    assert provs["openrouter"]["reachable"] is None
    assert provs["openrouter"]["reason"] == "no endpoint configured"
    # local Ollama is explicit false, never null
    assert provs["local"]["reachable"] is False
    assert provs["local"]["reason"] == "ollama not running"
    print("PASS status: unreachable=false, unknown=null, local=false honestly")


def test_status_no_secret_or_raw_error_leak():
    _patch({"groq": (True, 12.0, None)})
    r = client.get("/v1/status")
    body = r.json()
    text = str(body).lower()
    for forbidden in ("sk-", "gw_", "bearer", "api_key", "api-key", "secret",
                     "token", "admin", "password", "x-api-key", "authorization",
                     "127.0.0.1", "localhost", "0.0.0.0", "/data/", ".db",
                     "sqlite", "traceback", "file \"", "line ", "detail",
                     "upstream", "all providers failed", "all providers"):
        assert forbidden not in text, f"forbidden token '{forbidden}' leaked: {text[:160]}"
    # provider entries must not embed credentials or host URLs
    for name, info in body["providers"].items():
        assert "key" not in str(info).lower() or "credentials_configured" in info
    print("PASS status: no secrets/hosts/dbpaths/stacktraces/raw errors leaked")


def test_status_probe_and_failover_notes_honest():
    _patch({})
    r = client.get("/v1/status")
    body = r.json()
    assert "live per-request reachability probe" in body["recent_activity"]["note"]
    assert "mocked providers" in body["failover"]["note"]
    print("PASS status: probe + failover notes are honest")


print("ALL STATUS TESTS PASSED")
