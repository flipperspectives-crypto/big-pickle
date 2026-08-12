import os

os.environ["GATEWAY_DB"] = "/tmp/gateway_status_test3.db"
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"

import asyncio  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import app.status as status_mod  # noqa: E402
from app import providers as prov  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

ALL_PROVIDERS = (
    set(prov.OPENAI_COMPATIBLE)
    | set(prov.ANTHROPIC)
    | {p for provs in prov.ROUTES.values() for p in provs}
)
N = len(ALL_PROVIDERS)


def _patch(mapping):
    """Monkeypatch the per-provider probe with deterministic results.

    mapping: provider -> (reachable, latency_ms, reason)
    """
    async def fake_probe(provider):
        return mapping.get(provider, (None, None, "no endpoint configured"))
    status_mod._provider_probe = fake_probe
    status_mod.clear_status_cache()


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
    assert "probed_at" in body and "probe_age_seconds" in body
    provs = body["providers"]
    assert len(provs) > 0
    for name, info in provs.items():
        keys = set(info.keys())
        for required in ("configured", "credentials_configured",
                        "reachable", "probe_latency_ms", "models_in_routes"):
            assert required in keys, f"missing {required} for {name}"
        assert "needs_key" not in info, "ambiguous needs_key must be removed"
    print("PASS status: contract fields present, needs_key removed, probed_at/age added")


def test_status_honest_unreachable_and_unknown():
    _patch({
        "groq": (False, 30.0, "unreachable: ConnectError"),
        "openrouter": (None, None, "no endpoint configured"),
        "local": (False, 2.0, "ollama not running"),
    })
    r = client.get("/v1/status")
    body = r.json()
    provs = body["providers"]
    assert provs["groq"]["reachable"] is False
    assert provs["groq"]["reason"] == "unreachable: ConnectError"
    assert provs["openrouter"]["reachable"] is None
    assert provs["openrouter"]["reason"] == "no endpoint configured"
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


def test_cache_reuse_within_ttl():
    calls = {"n": 0}

    async def counting_probe(provider):
        calls["n"] += 1
        return (True, 10.0, None)
    status_mod._provider_probe = counting_probe
    status_mod.clear_status_cache()
    # two requests within the TTL must share ONE probe batch
    r1 = client.get("/v1/status")
    r2 = client.get("/v1/status")
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] == N, f"expected {N} probe calls (one batch), got {calls['n']}"
    # probe_age_seconds must be present and non-negative
    assert r2.json()["probe_age_seconds"] >= 0
    print(f"PASS cache: {N} probes on 2 requests within TTL (no duplicate storm)")


def test_ttl_refresh_after_expiry():
    calls = {"n": 0}

    async def counting_probe(provider):
        calls["n"] += 1
        return (True, 10.0, None)
    status_mod._provider_probe = counting_probe
    status_mod.clear_status_cache()
    old_ttl = status_mod.PROBE_CACHE_TTL
    status_mod.PROBE_CACHE_TTL = 0.1  # force quick expiry for the test
    try:
        client.get("/v1/status")          # refresh #1
        assert calls["n"] == N
        import time
        time.sleep(0.2)                   # exceed TTL
        client.get("/v1/status")          # refresh #2
        assert calls["n"] == 2 * N, f"expected {2*N} after TTL expiry, got {calls['n']}"
        client.get("/v1/status")          # still within TTL -> cached
        assert calls["n"] == 2 * N, "third request should hit cache"
    finally:
        status_mod.PROBE_CACHE_TTL = old_ttl
        status_mod.clear_status_cache()
    print("PASS ttl: refresh after expiry, cached between")


def test_concurrent_refresh_single_probe_batch():
    calls = {"n": 0}

    async def slow_counting_probe(provider):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return (True, 10.0, None)
    status_mod._provider_probe = slow_counting_probe
    status_mod.clear_status_cache()

    async def run():
        return await asyncio.gather(*[status_mod.get_status() for _ in range(12)])

    results = asyncio.run(run())
    assert len(results) == 12
    # Only ONE refresh runs; all 12 share its probe batch => N probe calls total.
    assert calls["n"] == N, f"concurrent storm! expected {N} probe calls, got {calls['n']}"
    # probed_at present on every result
    assert all("probed_at" in r for r in results)
    print(f"PASS concurrent: 12 simultaneous requests => {N} probes (single shared refresh)")


print("ALL STATUS TESTS PASSED")
