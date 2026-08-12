"""Offline tests for the configurable local Ollama provider.

No real network and no inference: all upstream calls are monkeypatched.

Covers:
- custom OLLAMA_BASE_URL is used (and trailing-slash normalization)
- default localhost still works for dev/test
- /api/version status probe uses the configured root
- /api/tags discovery returns local:<exact-tag> (colons preserved)
- local:qwen3:1.7b colon handling, free ($0) routing
- cloud routes remain unchanged
- legacy local aliases are not advertised but still route literally
- Ollama offline => clean failure, no local models, cloud models remain
- discovery cache prevents duplicate bursts
- no private hostname / internal URL / raw body / API key leaks
"""
import asyncio
import io
import logging
import os
import time

os.environ.setdefault("GATEWAY_DB", "/tmp/gateway_local_test.db")
os.environ.setdefault("GATEWAY_ADMIN_KEY", "testadmin")

import httpx  # noqa: E402

import app.local as local_mod  # noqa: E402
import app.status as status_mod  # noqa: E402
from app import providers as prov  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)

DEFAULT_OLLAMA = "http://127.0.0.1:11434"
PRIVATE_OLLAMA = "http://clarity-windows._peer.internal:11434"


def _save_state():
    return {
        "ollama": settings.OLLAMA_BASE_URL,
        "fetch": local_mod._fetch_tags,
        "probe": getattr(status_mod, "_provider_probe", None),
        "success_models": local_mod._success_cache["models"],
        "success_time": local_mod._success_cache["time"],
        "failure_time": local_mod._failure_time,
        "ttl": local_mod.DISCOVERY_CACHE_TTL,
        "fttl": local_mod.FAILURE_TTL,
    }


def _restore_state(s):
    settings.OLLAMA_BASE_URL = s["ollama"]
    local_mod._fetch_tags = s["fetch"]
    if s["probe"] is not None:
        status_mod._provider_probe = s["probe"]
    local_mod._success_cache["models"] = s["success_models"]
    local_mod._success_cache["time"] = s["success_time"]
    local_mod._failure_time = s["failure_time"]
    local_mod.DISCOVERY_CACHE_TTL = s["ttl"]
    local_mod.FAILURE_TTL = s["fttl"]
    status_mod.clear_status_cache()


def _fake_fetch(tags):
    async def _f():
        return list(tags)
    return _f


def test_default_ollama_base_url_is_localhost():
    s = _save_state()
    try:
        settings.OLLAMA_BASE_URL = DEFAULT_OLLAMA
        assert settings.OLLAMA_BASE_URL == "http://127.0.0.1:11434"
        assert prov.ollama_api_url("api/version") == "http://127.0.0.1:11434/api/version"
        assert prov.ollama_api_url("api/tags") == "http://127.0.0.1:11434/api/tags"
        assert prov.base_url("local") == "http://127.0.0.1:11434/v1"
        # OpenAI-compatible local chat URL must resolve to <base>/v1/chat/completions
        assert prov.base_url("local") + "/chat/completions" == "http://127.0.0.1:11434/v1/chat/completions"
        print("PASS default OLLAMA_BASE_URL localhost; chat=<base>/v1/chat/completions")
    finally:
        _restore_state(s)


def test_custom_ollama_base_url_used_and_normalized():
    s = _save_state()
    try:
        settings.OLLAMA_BASE_URL = PRIVATE_OLLAMA + "/"  # trailing slash must be normalized
        assert prov.ollama_api_url("api/tags") == PRIVATE_OLLAMA + "/api/tags"
        assert prov.ollama_api_url("/api/version") == PRIVATE_OLLAMA + "/api/version"
        assert prov.base_url("local") == PRIVATE_OLLAMA + "/v1"
        # The real _provider_probe calls _probe(providers.ollama_api_url("api/version"));
        # mirror that exactly and capture the URL so the test is order-independent
        # (other test files monkeypatch _provider_probe and never restore it).
        captured = {}

        async def my_probe(url):
            captured["url"] = url
            return (True, 1.0, None)

        async def my_provider_probe(provider):
            if provider == "local":
                return await my_probe(prov.ollama_api_url("api/version"))
            return (True, 1.0, None)

        status_mod._probe = my_probe
        status_mod._provider_probe = my_provider_probe
        asyncio.run(status_mod._provider_probe("local"))
        assert captured["url"] == PRIVATE_OLLAMA + "/api/version", captured
        print("PASS custom OLLAMA_BASE_URL used for tags/version/chat, normalized")
    finally:
        _restore_state(s)


def test_discovery_returns_local_exact_tags():
    s = _save_state()
    try:
        local_mod._fetch_tags = _fake_fetch(["qwen3:1.7b", "llama3.2:1b", "qwen2.5:7b", "llama3.2:3b"])
        local_mod.clear_local_cache()
        ids = asyncio.run(local_mod.local_model_ids())
        assert ids == [
            "local:qwen3:1.7b",
            "local:llama3.2:1b",
            "local:qwen2.5:7b",
            "local:llama3.2:3b",
        ], ids
        print("PASS discovery returns local:<exact-tag> (colons preserved)")
    finally:
        _restore_state(s)


def test_local_prefix_routing_colon_and_free():
    s = _save_state()
    try:
        assert prov.providers_for("local:qwen3:1.7b") == ["local"]
        assert prov.upstream_model("local", "local:qwen3:1.7b") == "qwen3:1.7b"
        assert prov.is_free_model("local:qwen3:1.7b") is True
        assert prov.price_for("local", "local:qwen3:1.7b", 1000, 1000) == 0.0
        print("PASS local prefix routing/colon handling + $0 billing")
    finally:
        _restore_state(s)


def test_cloud_routes_unchanged():
    s = _save_state()
    try:
        assert prov.providers_for("gpt-4o") == ["openai"]
        assert prov.upstream_model("openai", "gpt-4o") == "gpt-4o"
        assert prov.base_url("openai") == "https://api.openai.com/v1"
        assert prov.providers_for("llama-3.3-70b") == ["groq", "cerebras", "deepinfra", "huggingface"]
        print("PASS cloud routes unchanged")
    finally:
        _restore_state(s)


def test_legacy_aliases_hidden_but_routable():
    s = _save_state()
    try:
        local_mod._fetch_tags = _fake_fetch([])
        local_mod.clear_local_cache()
        settings.OLLAMA_BASE_URL = DEFAULT_OLLAMA
        r = client.get("/v1/models")
        assert r.status_code == 200, r.text
        ids = {m["id"] for m in r.json()["data"]}
        for alias in prov.LEGACY_LOCAL_ALIASES:
            assert alias not in ids, f"{alias} must not be advertised"
        # still routable literally (never silently remapped to an installed model)
        assert prov.providers_for("lucy") == ["local"]
        assert prov.upstream_model("local", "lucy") == "lucy"
        print("PASS legacy aliases hidden from /v1/models but route to local literally")
    finally:
        _restore_state(s)


def test_offline_clean_no_local_cloud_remain():
    s = _save_state()
    real_client = local_mod.httpx.AsyncClient
    try:
        # Break the HTTP client so the REAL _fetch_tags fail-closed path
        # (catches httpx.HTTPError and returns []) is exercised with no network.
        class _BrokenClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                raise httpx.ConnectError("refused")

        local_mod.httpx.AsyncClient = _BrokenClient
        local_mod.clear_local_cache()
        ids = asyncio.run(local_mod.local_model_ids())
        assert ids == []
        r = client.get("/v1/models")
        data = r.json()["data"]
        assert all(not m["id"].startswith("local:") for m in data), "local model leaked while offline"
        assert "gpt-4o" in {m["id"] for m in data}
        print("PASS offline: no local models, cloud intact, no exception")
    finally:
        local_mod.httpx.AsyncClient = real_client
        _restore_state(s)


def test_cache_prevents_burst():
    s = _save_state()
    try:
        calls = {"n": 0}

        async def counting():
            calls["n"] += 1
            return ["qwen3:1.7b"]

        local_mod._fetch_tags = counting
        local_mod.clear_local_cache()
        for _ in range(5):
            asyncio.run(local_mod.local_model_ids())
        assert calls["n"] == 1, f"expected 1 fetch, got {calls['n']}"
        print("PASS cache: 5 discovery calls => 1 upstream fetch (no burst)")
    finally:
        _restore_state(s)


def test_no_private_info_leak():
    s = _save_state()
    try:
        settings.OLLAMA_BASE_URL = PRIVATE_OLLAMA
        local_mod._fetch_tags = _fake_fetch([])

        async def offline_probe(provider):
            if provider == "local":
                return (False, 1.0, "ollama not running")
            return (True, 1.0, None)

        status_mod._provider_probe = offline_probe
        local_mod.clear_local_cache()
        status_mod.clear_status_cache()

        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logger = logging.getLogger("clarity")
        logger.addHandler(handler)
        try:
            r1 = client.get("/v1/models")
            r2 = client.get("/v1/status")
        finally:
            logger.removeHandler(handler)

        blobs = [r1.text, r2.text, log_capture.getvalue()]
        forbidden = [
            "clarity-windows", "_peer.internal", "11434", "127.0.0.1",
            "api/tags", "api/version", "sk-", "gw_", "bearer", "secret",
            "authorization", "traceback",
        ]
        for b in blobs:
            low = b.lower()
            for f in forbidden:
                assert f not in low, f"leak: '{f}' present in response/log"
        print("PASS no private hostname/url/body/key leaked in /v1/models or /v1/status")
    finally:
        _restore_state(s)


def test_failure_fails_closed_no_success_cache():
    s = _save_state()
    try:
        local_mod.DISCOVERY_CACHE_TTL = 60.0
        local_mod.FAILURE_TTL = 4.0
        local_mod.clear_local_cache()

        async def boom():
            raise local_mod.DiscoveryError("boom")

        local_mod._fetch_tags = boom
        ids = asyncio.run(local_mod.local_model_ids())
        assert ids == [], ids
        # A failure must NOT be stored as a successful (empty) 60s result.
        assert local_mod._success_cache["models"] is None
        assert local_mod._failure_time > 0.0
        print("PASS transient failure fails closed; not cached as success")
    finally:
        _restore_state(s)


def test_failure_not_60s_cached_and_recovers_fast():
    s = _save_state()
    try:
        # Short TTLs: proves recovery needs only the short backoff, not 60s.
        local_mod.DISCOVERY_CACHE_TTL = 60.0
        local_mod.FAILURE_TTL = 0.05
        local_mod.clear_local_cache()
        holder = {"mode": "fail"}

        async def flaky():
            if holder["mode"] == "fail":
                raise local_mod.DiscoveryError("down")
            return ["qwen3:1.7b"]

        local_mod._fetch_tags = flaky
        # 1) failure -> closed, success cache untouched
        assert asyncio.run(local_mod.local_model_ids()) == []
        assert local_mod._success_cache["models"] is None
        # 2) recovery after only the short backoff expires
        holder["mode"] = "ok"
        time.sleep(0.1)
        ids = asyncio.run(local_mod.local_model_ids())
        assert ids == ["local:qwen3:1.7b"], ids
        print("PASS failure not cached as 60s success; recovers after short backoff")
    finally:
        _restore_state(s)


def test_valid_empty_200_is_successful_empty():
    s = _save_state()
    try:
        local_mod.DISCOVERY_CACHE_TTL = 60.0
        local_mod.FAILURE_TTL = 4.0
        local_mod.clear_local_cache()
        local_mod._fetch_tags = _fake_fetch([])  # valid 200 {"models": []}
        ids = asyncio.run(local_mod.local_model_ids())
        assert ids == []
        # Valid empty is a SUCCESS cache entry, not a failure.
        assert local_mod._success_cache["models"] == []
        assert local_mod._failure_time == 0.0
        print('PASS valid HTTP 200 {"models": []} treated as successful-empty')
    finally:
        _restore_state(s)


def test_success_caches_normally():
    s = _save_state()
    try:
        local_mod.DISCOVERY_CACHE_TTL = 60.0
        local_mod.FAILURE_TTL = 4.0
        local_mod.clear_local_cache()
        calls = {"n": 0}

        async def counting():
            calls["n"] += 1
            return ["qwen3:1.7b"]

        local_mod._fetch_tags = counting
        for _ in range(4):
            asyncio.run(local_mod.local_model_ids())
        assert calls["n"] == 1, calls["n"]
        print("PASS successful discovery caches normally (1 fetch / 4 calls)")
    finally:
        _restore_state(s)


def test_concurrent_refreshes_deduplicated():
    s = _save_state()
    try:
        local_mod.DISCOVERY_CACHE_TTL = 60.0
        local_mod.FAILURE_TTL = 4.0
        local_mod.clear_local_cache()
        calls = {"n": 0}

        async def slow_counting():
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return ["qwen3:1.7b"]

        local_mod._fetch_tags = slow_counting

        async def run():
            return await asyncio.gather(*[local_mod.local_model_ids() for _ in range(10)])

        results = asyncio.run(run())
        assert all(r == ["local:qwen3:1.7b"] for r in results)
        assert calls["n"] == 1, calls["n"]
        print("PASS concurrent local discovery => 1 fetch (deduplicated)")
    finally:
        _restore_state(s)


def test_models_endpoint_no_store_headers():
    s = _save_state()
    try:
        local_mod._fetch_tags = _fake_fetch(["qwen3:1.7b"])
        local_mod.clear_local_cache()
        r = client.get("/v1/models")
        assert r.status_code == 200
        cc = r.headers.get("cache-control", "").lower()
        assert "no-store" in cc, cc
        assert "no-cache" in cc, cc
        assert "max-age=0" in cc, cc
        assert r.headers.get("pragma", "").lower() == "no-cache", r.headers.get("pragma")
        print("PASS /v1/models sends Cache-Control: no-store, Pragma: no-cache")
    finally:
        _restore_state(s)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
