"""Offline tests for the Clarity Local Runtime surface (zero-inference).

No real network and no inference: all upstream calls are monkeypatched.

Covers:
- populated /api/ps  -> status "ok", sanitized models, id = local:<exact-tag>
- empty /api/ps      -> status "ok", models []
- unavailable runtime -> status "unavailable"
- malformed fields   -> dropped without crashing, no fabrication
- no sensitive leakage (no host/url/key/path/header/error text)
- runtime refresh never invokes inference (only GET /api/ps)
- cache + async lock deduplication (no burst; concurrent refresh => 1 fetch)
- last local success telemetry (recorded, well-shaped, no prompt/key/url)
- failed / cloud / streaming requests do NOT populate telemetry
- runtime module is INDEPENDENT from the /api/tags discovery cache
"""
import asyncio
import io
import logging
import os

os.environ.setdefault("GATEWAY_DB", "/tmp/gateway_runtime_test.db")
os.environ.setdefault("GATEWAY_ADMIN_KEY", "testadmin")

import app.runtime as rt  # noqa: E402
from app import providers as prov  # noqa: E402
from app import router as router_mod  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.router import UpstreamError  # noqa: E402

client = TestClient(app)

DEFAULT_OLLAMA = "http://127.0.0.1:11434"
PRIVATE_OLLAMA = "http://clarity-windows._peer.internal:11434"


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _save_state():
    return {
        "ollama": settings.OLLAMA_BASE_URL,
        "do_get": rt._do_get,
        "fetch_ps": rt._fetch_ps,
        "models": rt._runtime_cache["models"],
        "time": rt._runtime_cache["time"],
        "failure_time": rt._failure_time,
        "last_status": rt._last_status,
        "ttl": rt.RUNTIME_CACHE_TTL,
        "fttl": rt.FAILURE_TTL,
        "last_local": rt._last_local_request,
    }


def _restore_state(s):
    settings.OLLAMA_BASE_URL = s["ollama"]
    rt._do_get = s["do_get"]
    rt._fetch_ps = s["fetch_ps"]
    rt._runtime_cache["models"] = s["models"]
    rt._runtime_cache["time"] = s["time"]
    rt._failure_time = s["failure_time"]
    rt._last_status = s["last_status"]
    rt.RUNTIME_CACHE_TTL = s["ttl"]
    rt.FAILURE_TTL = s["fttl"]
    rt._last_local_request = s["last_local"]


def _fake_do_get(status, payload):
    async def _f(url, timeout):
        return FakeResp(status, payload)
    return _f


# ---------------------------------------------------------------------------
# status semantics
# ---------------------------------------------------------------------------
def test_populated_runtime_ok():
    s = _save_state()
    try:
        settings.OLLAMA_BASE_URL = DEFAULT_OLLAMA
        rt._do_get = _fake_do_get(200, {"models": [
            {
                "name": "qwen3:1.7b", "size": 1324347080, "size_vram": 1324347080,
                "details": {"family": "qwen3", "parameter_size": "1.7B",
                            "quantization_level": "Q4_K_M", "context_length": 40960},
                "expires_at": "2026-01-01T00:00:00Z",
            }
        ]})
        rt.clear_runtime_cache()
        r = client.get("/v1/local/runtime")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok", body
        m = body["models"][0]
        assert m["id"] == "local:qwen3:1.7b"
        assert m["name"] == "qwen3:1.7b"
        assert m["size_bytes"] == 1324347080
        assert m["size_vram_bytes"] == 1324347080
        assert m["context_length"] == 40960
        assert m["expires_at"] == "2026-01-01T00:00:00Z"
        assert m["family"] == "qwen3"
        assert m["parameter_size"] == "1.7B"
        assert m["quantization_level"] == "Q4_K_M"
        print("PASS populated /api/ps -> status ok, sanitized fields exposed")
    finally:
        _restore_state(s)


def test_empty_runtime_ok():
    s = _save_state()
    try:
        rt._do_get = _fake_do_get(200, {"models": []})
        rt.clear_runtime_cache()
        body = client.get("/v1/local/runtime").json()
        assert body["status"] == "ok", body
        assert body["models"] == []
        print("PASS empty /api/ps -> status ok, models []")
    finally:
        _restore_state(s)


def test_unavailable_runtime():
    s = _save_state()
    try:
        rt.RUNTIME_CACHE_TTL = 60.0
        rt.FAILURE_TTL = 4.0
        rt.clear_runtime_cache()

        async def boom(url, timeout):
            raise rt.RuntimeProbeError("down")

        rt._do_get = boom
        body = client.get("/v1/local/runtime").json()
        assert body["status"] == "unavailable", body
        assert body["models"] == []
        # second call inside backoff stays unavailable
        body2 = client.get("/v1/local/runtime").json()
        assert body2["status"] == "unavailable", body2
        print("PASS runtime unavailable -> status unavailable (cached backoff)")
    finally:
        _restore_state(s)


def test_non_200_unavailable():
    s = _save_state()
    try:
        rt._do_get = _fake_do_get(503, {"error": "boom"})
        rt.clear_runtime_cache()
        body = client.get("/v1/local/runtime").json()
        assert body["status"] == "unavailable", body
        print("PASS non-200 /api/ps -> status unavailable")
    finally:
        _restore_state(s)


# ---------------------------------------------------------------------------
# malformed fields
# ---------------------------------------------------------------------------
def test_malformed_runtime_entries_dropped():
    s = _save_state()
    try:
        rt._do_get = _fake_do_get(200, {"models": [
            {"name": "ok:1", "size": 123, "size_vram": 200, "details": {"family": "ok"}},
            None,
            "not-a-dict",
            {"no_name": 1},
            {"name": "badsize:1", "size": "huge", "size_vram": "nope", "details": "nope"},
        ]})
        rt.clear_runtime_cache()
        body = client.get("/v1/local/runtime").json()
        assert body["status"] == "ok", body
        ids = [m["id"] for m in body["models"]]
        assert "local:ok:1" in ids
        assert "local:badsize:1" in ids  # name valid -> kept, bad ints -> null
        bad = next(m for m in body["models"] if m["id"] == "local:badsize:1")
        assert bad["size_bytes"] is None
        assert bad["size_vram_bytes"] is None
        print("PASS malformed runtime entries dropped/coerced without crash")
    finally:
        _restore_state(s)


# ---------------------------------------------------------------------------
# no sensitive leakage
# ---------------------------------------------------------------------------
def test_no_sensitive_leak_in_runtime():
    s = _save_state()
    try:
        settings.OLLAMA_BASE_URL = PRIVATE_OLLAMA
        rt._do_get = _fake_do_get(200, {"models": [
            {
                "name": "ok:1", "size": 111, "size_vram": 111,
                "details": {"family": "ok", "parameter_size": "1B", "quantization_level": "Q4"},
                "OLLAMA_BASE_URL": "http://10.0.0.5:11434",
                "headers": {"Authorization": "Bearer sk-secret"},
                "error": "boom", "raw": "<html>internal</html>",
            }
        ]})
        rt.clear_runtime_cache()
        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        logger = logging.getLogger("clarity")
        logger.addHandler(handler)
        try:
            r = client.get("/v1/local/runtime")
        finally:
            logger.removeHandler(handler)
        blob = r.text + log_capture.getvalue()
        low = blob.lower()
        forbidden = [
            "clarity-windows", "_peer.internal", "11434", "127.0.0.1",
            "10.0.0.5", "api/ps", "api/tags", "sk-", "gw_", "bearer",
            "authorization", "secret", "traceback", "<html>",
        ]
        for f in forbidden:
            assert f not in low, f"leak: '{f}' present in runtime response/log"
        # explicit negative: hostile fields must not be retained
        det = r.json()["models"][0]
        assert "OLLAMA_BASE_URL" not in det
        assert "headers" not in det
        assert "error" not in det
        assert "raw" not in det
        print("PASS no host/url/key/path/header/error leaked in /v1/local/runtime")
    finally:
        _restore_state(s)


# ---------------------------------------------------------------------------
# refresh never invokes inference
# ---------------------------------------------------------------------------
def test_runtime_refresh_never_invokes_inference():
    s = _save_state()
    try:
        calls = {"n": 0, "urls": []}

        async def rec(url, timeout):
            calls["n"] += 1
            calls["urls"].append(url)
            # return a valid but empty ps result
            return FakeResp(200, {"models": []})

        rt._do_get = rec
        rt.clear_runtime_cache()
        client.get("/v1/local/runtime")
        assert calls["n"] == 1, calls
        assert calls["urls"][0].endswith("/api/ps"), calls["urls"]
        assert "chat" not in calls["urls"][0]
        # a second fresh-ish call path still hits only /api/ps
        client.get("/v1/local/runtime")
        assert all(u.endswith("/api/ps") for u in calls["urls"]), calls["urls"]
        print("PASS runtime refresh issues GET /api/ps only (zero inference)")
    finally:
        _restore_state(s)


# ---------------------------------------------------------------------------
# cache + lock behavior
# ---------------------------------------------------------------------------
def test_runtime_cache_prevents_burst():
    s = _save_state()
    try:
        rt.RUNTIME_CACHE_TTL = 60.0
        rt.FAILURE_TTL = 4.0
        calls = {"n": 0}

        async def counting(url, timeout):
            calls["n"] += 1
            return FakeResp(200, {"models": [{"name": "qwen3:1.7b", "size": 1, "size_vram": 1}]})

        rt._do_get = counting
        rt.clear_runtime_cache()
        for _ in range(5):
            client.get("/v1/local/runtime")
        assert calls["n"] == 1, f"expected 1 fetch, got {calls['n']}"
        print("PASS runtime cache: 5 calls => 1 upstream fetch (no burst)")
    finally:
        _restore_state(s)


def test_runtime_concurrent_refreshes_deduplicated():
    s = _save_state()
    try:
        rt.RUNTIME_CACHE_TTL = 60.0
        rt.FAILURE_TTL = 4.0
        calls = {"n": 0}

        async def slow(url, timeout):
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return FakeResp(200, {"models": [{"name": "qwen3:1.7b", "size": 1, "size_vram": 1}]})

        rt._do_get = slow
        rt.clear_runtime_cache()

        async def run():
            return await asyncio.gather(*[rt.get_runtime() for _ in range(10)])

        results = asyncio.run(run())
        assert all(r["status"] == "ok" for r in results)
        assert calls["n"] == 1, calls["n"]
        print("PASS concurrent runtime refreshes => 1 fetch (deduplicated)")
    finally:
        _restore_state(s)


def test_runtime_independent_of_discovery_cache():
    s = _save_state()
    try:
        # Runtime failure must NOT corrupt the /api/tags discovery cache and
        # vice-versa. Simulate a runtime probe failure while discovery is clean.
        import app.local as local_mod
        local_mod._fetch_tags = lambda: _async_list([{"name": "qwen3:1.7b", "id": "local:qwen3:1.7b"}])()
        local_mod.clear_local_cache()

        async def boom(url, timeout):
            raise rt.RuntimeProbeError("down")

        rt._do_get = boom
        rt.clear_runtime_cache()
        r = client.get("/v1/local/runtime").json()
        assert r["status"] == "unavailable", r
        # discovery (separate cache) must still be unaffected
        models = client.get("/v1/models").json()["data"]
        assert any(m["id"] == "local:qwen3:1.7b" for m in models), "discovery cache disturbed"
        print("PASS runtime cache independent from /api/tags discovery cache")
    finally:
        _restore_state(s)


def _async_list(items):
    async def _f():
        return list(items)
    return _f


# ---------------------------------------------------------------------------
# last local request telemetry
# ---------------------------------------------------------------------------
def test_telemetry_recorded_on_local_non_streaming_success():
    s = _save_state()
    try:
        rt.clear_runtime_cache()
        rt.record_local_success("local:qwen3:1.7b", 123.45, 10, 20)
        last = asyncio.run(rt.get_runtime())["last_local_request"]
        assert last is not None, "telemetry not recorded"
        assert last["model"] == "local:qwen3:1.7b"
        assert last["gateway_upstream_round_trip_ms"] == 123.5
        assert last["prompt_tokens"] == 10
        assert last["completion_tokens"] == 20
        assert last["total_tokens"] == 30
        assert isinstance(last["measured_at"], str) and last["measured_at"].endswith("Z")
        print("PASS telemetry recorded for local non-streaming success")
    finally:
        _restore_state(s)


def test_telemetry_contains_only_safe_fields():
    s = _save_state()
    try:
        rt.clear_runtime_cache()
        rt.record_local_success("local:qwen3:1.7b", 1.0, 1, 1)
        last = rt.get_last_local_request()
        allowed = {"model", "measured_at", "gateway_upstream_round_trip_ms",
                   "prompt_tokens", "completion_tokens", "total_tokens"}
        assert set(last.keys()) == allowed, last.keys()
        for forbidden in ("prompt", "content", "response", "key", "skey",
                          "authorization", "bearer", "host", "url", "token", "messages"):
            assert forbidden not in {k.lower() for k in last.keys()}, f"forbidden key {forbidden}"
        print("PASS telemetry holds only safe fields; no prompt/key/url")
    finally:
        _restore_state(s)


def test_telemetry_cleared_on_reset():
    s = _save_state()
    try:
        rt.clear_runtime_cache()
        rt.record_local_success("local:qwen3:1.7b", 1.0, 1, 1)
        assert rt.get_last_local_request() is not None
        rt.clear_runtime_cache()
        assert rt.get_last_local_request() is None
        print("PASS telemetry reset by clear_runtime_cache")
    finally:
        _restore_state(s)


def test_router_records_telemetry_only_for_local_non_streaming():
    s = _save_state()
    try:
        rt.clear_runtime_cache()
        recorded = {}

        def fake_record(model, rt_ms, pt, ct):
            recorded["hit"] = (model, rt_ms, pt, ct)

        orig_record = rt.record_local_success
        rt.record_local_success = fake_record

        # fake the actual upstream call (no network, no inference)
        orig_chat = router_mod._chat_openai

        async def fake_openai(client, provider, payload, stream):
            data = {"id": "x", "object": "chat.completion", "model": payload["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}}
            return data, 5, 7

        router_mod._chat_openai = fake_openai
        try:
            # 1) local non-streaming -> recorded
            asyncio.run(router_mod.run_completion(
                {"model": "local:qwen3:1.7b", "messages": [{"role": "user", "content": "hi"}]}, "k1"))
            assert recorded.get("hit"), "local non-streaming telemetry not recorded"
            assert recorded["hit"][0] == "local:qwen3:1.7b"
            recorded.clear()

            # 2) local streaming -> NOT recorded
            async def fake_openai_stream(client, provider, payload, stream):
                async def gen():
                    yield "data: {}\n\n"
                return gen(), provider, payload["model"]

            router_mod._chat_openai = fake_openai_stream
            asyncio.run(router_mod.run_completion(
                {"model": "local:qwen3:1.7b", "stream": True,
                 "messages": [{"role": "user", "content": "hi"}]}, "k2"))
            assert "hit" not in recorded, "streaming must not populate telemetry"

            # 3) cloud non-streaming -> NOT recorded
            recorded.clear()
            router_mod._chat_openai = fake_openai
            asyncio.run(router_mod.run_completion(
                {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}, "k3"))
            assert "hit" not in recorded, "cloud request must not populate local telemetry"

            # 4) local non-streaming FAILURE -> NOT recorded
            async def fake_openai_fail(client, provider, payload, stream):
                raise UpstreamError(502, provider=provider, reason="network_error")

            router_mod._chat_openai = fake_openai_fail
            recorded.clear()
            try:
                asyncio.run(router_mod.run_completion(
                    {"model": "local:qwen3:1.7b", "messages": [{"role": "user", "content": "hi"}]}, "k4"))
            except UpstreamError:
                pass
            assert "hit" not in recorded, "failed local request must not populate telemetry"
            print("PASS telemetry only on local non-streaming success (not cloud/stream/failure)")
        finally:
            router_mod._chat_openai = orig_chat
            rt.record_local_success = orig_record
    finally:
        _restore_state(s)


def test_refresh_runtime_preserves_last_local_request():
    s = _save_state()
    try:
        rt.clear_runtime_cache()
        # Record a successful local request.
        rt.record_local_success("local:qwen3:1.7b", 99.9, 7, 13)
        # First read of the runtime surface (served from probe cache).
        first = client.get("/v1/local/runtime").json()
        assert first["last_local_request"] is not None
        assert first["last_local_request"]["model"] == "local:qwen3:1.7b"
        # Force a fresh probe (simulating what a Refresh Runtime re-probe does)
        # WITHOUT touching telemetry: only the probe cache/backoff is invalidated.
        rt._runtime_cache["models"] = None
        rt._runtime_cache["time"] = 0.0
        rt._failure_time = 0.0
        rt._last_status = None
        rt._do_get = _fake_do_get(200, {"models": [
            {"name": "qwen3:1.7b", "size": 1324347080, "size_vram": 1324347080,
             "details": {"family": "qwen3", "parameter_size": "1.7B",
                         "quantization_level": "Q4_K_M", "context_length": 40960}}
        ]})
        # A Refresh Runtime (GET /v1/local/runtime) must NOT erase telemetry.
        second = client.get("/v1/local/runtime").json()
        assert second["last_local_request"] is not None, "telemetry erased by refresh"
        assert second["last_local_request"]["model"] == "local:qwen3:1.7b"
        assert second["last_local_request"]["prompt_tokens"] == 7
        assert second["last_local_request"]["completion_tokens"] == 13
        assert second["status"] == "ok"
        # Explicit test-only telemetry reset helper wipes only telemetry.
        rt.reset_local_telemetry()
        assert rt.get_last_local_request() is None
        assert client.get("/v1/local/runtime").json()["status"] == "ok"
        print("PASS Refresh Runtime preserves Last Local Request; telemetry reset is explicit-only")
    finally:
        _restore_state(s)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
