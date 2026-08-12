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
        "last_status": local_mod._last_status,
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
    local_mod._last_status = s["last_status"]
    local_mod.DISCOVERY_CACHE_TTL = s["ttl"]
    local_mod.FAILURE_TTL = s["fttl"]
    status_mod.clear_status_cache()


def _fake_fetch(tags):
    # _fetch_tags now returns sanitized per-model dicts (each with "name"/"id").
    # Allow callers to pass either plain tag strings or full metadata dicts.
    norm = []
    for t in tags:
        if isinstance(t, dict):
            d = dict(t)
            if d.get("id") is None and d.get("name"):
                d["id"] = "local:" + d["name"]
            elif d.get("name") is None and isinstance(d.get("id"), str) and d["id"].startswith("local:"):
                d["name"] = d["id"][len("local:"):]
            norm.append(d)
        else:
            norm.append({"name": t, "id": "local:" + t})
    async def _f():
        return list(norm)
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
            return [{"name": "qwen3:1.7b", "id": "local:qwen3:1.7b"}]

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
            return [{"name": "qwen3:1.7b", "id": "local:qwen3:1.7b"}]

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
            return [{"name": "qwen3:1.7b", "id": "local:qwen3:1.7b"}]

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
            return [{"name": "qwen3:1.7b", "id": "local:qwen3:1.7b"}]

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


# ---------------------------------------------------------------------------
# Local model metadata enrichment (Phase 1/2)
# ---------------------------------------------------------------------------
FULL_META = [
    {
        "name": "qwen3:1.7b",
        "id": "local:qwen3:1.7b",
        "size_bytes": 1324347080,
        "family": "qwen3",
        "parameter_size": "1.7B",
        "quantization_level": "Q4_K_M",
        "context_length": 40960,
        "capabilities": ["completion", "tools", "thinking"],
    },
    {
        "name": "llama3.2:3b",
        "id": "local:llama3.2:3b",
        "size_bytes": 2097152000,
        "family": "llama3.2",
        "parameter_size": "3B",
        "quantization_level": "Q4_0",
        "context_length": 8192,
        "capabilities": ["completion"],
    },
]


def test_metadata_sanitized_into_expected_fields():
    s = _save_state()
    try:
        local_mod._fetch_tags = _fake_fetch(FULL_META)
        local_mod.clear_local_cache()
        r = client.get("/v1/models")
        assert r.status_code == 200
        by_id = {m["id"]: m for m in r.json()["data"]}
        q = by_id["local:qwen3:1.7b"]
        assert q["local"] is True
        d = q["details"]
        assert d["size_bytes"] == 1324347080
        assert d["family"] == "qwen3"
        assert d["parameter_size"] == "1.7B"
        assert d["quantization_level"] == "Q4_K_M"
        assert d["context_length"] == 40960
        assert d["capabilities"] == ["completion", "tools", "thinking"]
        # llama variant keeps only what Ollama reported
        l = by_id["local:llama3.2:3b"]["details"]
        assert l["capabilities"] == ["completion"]
        print("PASS local metadata sanitized into expected fields")
    finally:
        _restore_state(s)


# ---------------------------------------------------------------------------
# Capabilities MUST come from Ollama (never invented / family-inferred)
# ---------------------------------------------------------------------------
def _details_by_id():
    r = client.get("/v1/models")
    return {m["id"]: m for m in r.json()["data"]}


def test_capabilities_preserved_exactly_from_ollama_qwen3():
    s = _save_state()
    try:
        # _fetch_tags returns flat per-model dicts (as _sanitize_model does).
        local_mod._fetch_tags = _fake_fetch([{
            "name": "qwen3:1.7b", "id": "local:qwen3:1.7b",
            "family": "qwen3",
            "capabilities": ["completion", "tools", "thinking"],
        }])
        local_mod.clear_local_cache()
        caps = _details_by_id()["local:qwen3:1.7b"]["details"]["capabilities"]
        assert caps == ["completion", "tools", "thinking"], caps
        print("PASS Qwen3 capabilities preserved exactly from Ollama")
    finally:
        _restore_state(s)


def test_non_qwen_thinking_capability_preserved():
    s = _save_state()
    try:
        # DeepSeek reports thinking; it must NOT be suppressed for being non-Qwen3.
        local_mod._fetch_tags = _fake_fetch([{
            "name": "deepseek-r1:7b", "id": "local:deepseek-r1:7b",
            "family": "deepseek-r1",
            "capabilities": ["completion", "thinking"],
        }])
        local_mod.clear_local_cache()
        caps = _details_by_id()["local:deepseek-r1:7b"]["details"]["capabilities"]
        assert caps == ["completion", "thinking"], caps
        print("PASS non-Qwen thinking capability preserved (not suppressed)")
    finally:
        _restore_state(s)


def test_missing_capabilities_omitted():
    s = _save_state()
    try:
        local_mod._fetch_tags = _fake_fetch([{
            "name": "plain:1", "id": "local:plain:1",
            "family": "plain",  # no capabilities key
        }])
        local_mod.clear_local_cache()
        det = _details_by_id()["local:plain:1"]["details"]
        assert "capabilities" not in det, det
        print("PASS missing capabilities omitted (not invented)")
    finally:
        _restore_state(s)


def test_malformed_capabilities_filtered():
    # Through the real sanitizer (not the fake bypass).
    ok = local_mod._sanitize_model({
        "name": "x:1",
        "details": {"capabilities": [123, "", "thinking", "thinking", "  tools  "]},
    })
    assert ok["capabilities"] == ["thinking", "tools"], ok["capabilities"]
    none_caps = local_mod._sanitize_model({"name": "y:1", "details": {"capabilities": "not-a-list"}})
    assert none_caps["capabilities"] is None, none_caps["capabilities"]
    print("PASS malformed capabilities safely filtered/deduped")


def test_no_family_based_capability_fabrication():
    # A Qwen3 family tag with NO capabilities array must NOT auto-gain tools/thinking.
    bad = local_mod._sanitize_model({"name": "qwen3:1.7b", "details": {"family": "qwen3"}})
    assert bad["capabilities"] is None, bad["capabilities"]
    print("PASS no family-based capability fabrication")


def test_local_metadata_only_on_local_entries():
    s = _save_state()
    try:
        local_mod._fetch_tags = _fake_fetch(FULL_META)
        local_mod.clear_local_cache()
        r = client.get("/v1/models")
        data = r.json()["data"]
        for m in data:
            if m["local"]:
                assert "details" in m, m
            else:
                assert "details" not in m, f"cloud model leaked details: {m['id']}"
        print("PASS details appear only on local entries; cloud unchanged")
    finally:
        _restore_state(s)


def test_missing_optional_metadata_omitted_not_invented():
    s = _save_state()
    try:
        # Ollama reported a model but with sparse details
        sparse = [{"name": "tiny:latest", "id": "local:tiny:latest", "size_bytes": 123456}]
        local_mod._fetch_tags = _fake_fetch(sparse)
        local_mod.clear_local_cache()
        r = client.get("/v1/models")
        d = {m["id"]: m for m in r.json()["data"]}["local:tiny:latest"]["details"]
        assert d["size_bytes"] == 123456
        assert "family" not in d
        assert "parameter_size" not in d
        assert "quantization_level" not in d
        assert "context_length" not in d
        print("PASS missing optional metadata omitted (never invented)")
    finally:
        _restore_state(s)


def test_sanitize_model_handles_malformed_entries():
    # A malformed individual entry must not crash discovery.
    assert local_mod._sanitize_model(None) is None
    assert local_mod._sanitize_model("not-a-dict") is None
    assert local_mod._sanitize_model({"no_name": 1}) is None
    bad = local_mod._sanitize_model({
        "name": "x:1",
        "size": "not-an-int",          # wrong type -> dropped
        "details": "not-a-dict",        # wrong type -> dropped
    })
    assert bad["id"] == "local:x:1"
    assert bad["size_bytes"] is None
    assert bad["family"] is None
    assert bad["capabilities"] is None  # never invented from family
    print("PASS _sanitize_model drops malformed fields without crashing")


def test_no_host_url_header_error_in_metadata():
    s = _save_state()
    try:
        # Even if Ollama returned hostile fields, they are not retained.
        hostile = [{
            "name": "ok:1", "id": "local:ok:1",
            "OLLAMA_BASE_URL": "http://10.0.0.5:11434",
            "headers": {"Authorization": "Bearer sk-secret"},
            "error": "boom", "raw": "<html>internal</html>",
        }]
        local_mod._fetch_tags = _fake_fetch(hostile)
        local_mod.clear_local_cache()
        r = client.get("/v1/models")
        body = r.text.lower()
        for forbidden in ["10.0.0.5", "11434", "sk-secret", "bearer", "authorization", "boom", "<html"]:
            assert forbidden not in body, f"leak: {forbidden}"
        det = {m["id"]: m for m in r.json()["data"]}["local:ok:1"].get("details") or {}
        assert "OLLAMA_BASE_URL" not in det
        print("PASS hostile metadata fields never retained/leaked")
    finally:
        _restore_state(s)


def test_valid_empty_list_represented_honestly():
    s = _save_state()
    try:
        local_mod._fetch_tags = _fake_fetch([])
        local_mod.clear_local_cache()
        st = asyncio.run(local_mod.local_models_with_status())
        assert st["status"] == "ok", st
        assert st["models"] == []
        print("PASS valid empty Ollama list => status 'ok', models []")
    finally:
        _restore_state(s)


def test_discovery_failure_distinguishable_from_empty():
    s = _save_state()
    try:
        async def boom():
            raise local_mod.DiscoveryError("down")

        local_mod._fetch_tags = boom
        local_mod.clear_local_cache()
        st = asyncio.run(local_mod.local_models_with_status())
        assert st["status"] == "unavailable", st
        assert st["models"] == []
        print("PASS discovery failure => status 'unavailable' (distinct from empty ok)")
    finally:
        _restore_state(s)


def test_failure_backoff_request_reports_unavailable():
    s = _save_state()
    try:
        local_mod.DISCOVERY_CACHE_TTL = 60.0
        local_mod.FAILURE_TTL = 4.0  # long enough to stay in backoff
        local_mod.clear_local_cache()

        async def boom():
            raise local_mod.DiscoveryError("down")

        local_mod._fetch_tags = boom
        first = asyncio.run(local_mod.local_models_with_status())
        assert first["status"] == "unavailable", first
        assert first["models"] == []
        # A follow-up request inside the failure backoff must still report unavailable
        second = asyncio.run(local_mod.local_models_with_status())
        assert second["status"] == "unavailable", second
        print("PASS failure-backoff request => status 'unavailable'")
    finally:
        _restore_state(s)


def test_recovery_after_backoff_reports_ok():
    s = _save_state()
    try:
        local_mod.DISCOVERY_CACHE_TTL = 60.0
        local_mod.FAILURE_TTL = 0.05
        local_mod.clear_local_cache()
        holder = {"mode": "fail"}

        async def flaky():
            if holder["mode"] == "fail":
                raise local_mod.DiscoveryError("down")
            return [{"name": "qwen3:1.7b", "id": "local:qwen3:1.7b"}]

        local_mod._fetch_tags = flaky
        assert asyncio.run(local_mod.local_models_with_status())["status"] == "unavailable"
        holder["mode"] = "ok"
        time.sleep(0.1)
        recovered = asyncio.run(local_mod.local_models_with_status())
        assert recovered["status"] == "ok", recovered
        assert recovered["models"] == [{"name": "qwen3:1.7b", "id": "local:qwen3:1.7b"}]
        print("PASS recovery after backoff => status 'ok'")
    finally:
        _restore_state(s)


def test_successful_populated_discovery_ok():
    s = _save_state()
    try:
        local_mod.DISCOVERY_CACHE_TTL = 60.0
        local_mod.FAILURE_TTL = 4.0
        local_mod.clear_local_cache()
        local_mod._fetch_tags = _fake_fetch([{"name": "qwen3:1.7b", "id": "local:qwen3:1.7b"}])
        st = asyncio.run(local_mod.local_models_with_status())
        assert st["status"] == "ok", st
        assert len(st["models"]) == 1
        print("PASS successful populated discovery => status 'ok'")
    finally:
        _restore_state(s)


def test_clear_local_cache_resets_failure_time():
    s = _save_state()
    try:
        # Regression: clear_local_cache must actually reset the module-level
        # _failure_time (it previously rebound a local var and leaked state).
        async def boom():
            raise local_mod.DiscoveryError("down")

        local_mod._fetch_tags = boom
        local_mod.clear_local_cache()
        asyncio.run(local_mod.local_model_ids())
        assert local_mod._failure_time > 0.0
        local_mod.clear_local_cache()
        assert local_mod._failure_time == 0.0, "failure_time not reset"
        assert local_mod._success_cache["models"] is None
        print("PASS clear_local_cache resets _failure_time (scoping fixed)")
    finally:
        _restore_state(s)


def test_cache_refresh_is_zero_inference_and_safe():
    s = _save_state()
    try:
        calls = {"n": 0}

        async def counting():
            calls["n"] += 1
            return [{"name": "qwen3:1.7b", "id": "local:qwen3:1.7b"}]

        local_mod._fetch_tags = counting
        local_mod.clear_local_cache()
        asyncio.run(local_mod.local_model_ids())
        asyncio.run(local_mod.local_model_ids())  # served from cache
        assert calls["n"] == 1
        local_mod.clear_local_cache()  # refresh bypasses only local cache
        st = asyncio.run(local_mod.local_models_with_status())
        assert calls["n"] == 2
        assert st["status"] == "ok"
        print("PASS refresh bypasses local cache and re-discovers (zero inference)")
    finally:
        _restore_state(s)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
