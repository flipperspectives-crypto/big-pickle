"""Offline regression tests for build identity + cache-busting + diagnostics.

Zero inference. No real Ollama. No network. Covers the /v1/build and /v1/diagnostics
endpoints, the safe git/env resolution, and the static asset fingerprint used for
cache-busting.
"""
import os
import re

os.environ["GATEWAY_DB"] = "/tmp/gateway_build_diag_test.db"
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"

import pytest  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import app.build as build_mod  # noqa: E402
import app.local as local_mod  # noqa: E402
import app.runtime as runtime_mod  # noqa: E402
from app import main  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX12 = re.compile(r"^[0-9a-f]{12}$")


# ---- checkpoint constants -------------------------------------------------
def test_checkpoint_tag_constant():
    assert build_mod.CHECKPOINT_TAG == "clarity-local-v1.0.0"


def test_checkpoint_sha_constant():
    assert build_mod.CHECKPOINT_COMMIT == "f7dd11c4b5b31f44dc0d4f938be8528b8aef8fa0"


# ---- current_commit resolution --------------------------------------------
def test_valid_build_sha_accepted(monkeypatch):
    sha = "a" * 40
    monkeypatch.setenv("CLARITY_BUILD_SHA", sha)
    build_mod._reset_build_cache()
    try:
        assert build_mod.get_build_info()["current_commit"] == sha
    finally:
        build_mod._reset_build_cache()


def test_malformed_build_sha_rejected_falls_back(monkeypatch):
    monkeypatch.setenv("CLARITY_BUILD_SHA", "not-a-real-sha")
    build_mod._reset_build_cache()
    try:
        cur = build_mod.get_build_info()["current_commit"]
        assert cur != "not-a-real-sha"
        assert HEX40.match(cur) or cur == "unknown"
    finally:
        build_mod._reset_build_cache()


def test_git_fallback_sanitized(monkeypatch):
    monkeypatch.delenv("CLARITY_BUILD_SHA", raising=False)
    build_mod._reset_build_cache()
    try:
        cur = build_mod.get_build_info()["current_commit"]
        assert HEX40.match(cur) or cur == "unknown"
    finally:
        build_mod._reset_build_cache()


def test_git_failure_returns_unknown_safely(monkeypatch):
    monkeypatch.delenv("CLARITY_BUILD_SHA", raising=False)
    build_mod._reset_build_cache()

    def boom(*a, **k):
        raise RuntimeError("fatal git leak attempt /Users/secret/big-pickle")

    monkeypatch.setattr(build_mod.subprocess, "run", boom)
    try:
        info = build_mod.get_build_info()
        assert info["current_commit"] == "unknown"
        # No path / error text leakage from the failed git call.
        text = str(info).lower()
        for bad in ("fatal", "big-pickle", "/users/secret", "traceback",
                    "line ", "error", "exception"):
            assert bad not in text, f"leak: {bad}"
    finally:
        build_mod._reset_build_cache()


# ---- asset fingerprint ----------------------------------------------------
def test_asset_fingerprint_deterministic():
    a = build_mod.asset_fingerprint()
    assert HEX12.match(a)
    assert a == build_mod.asset_fingerprint()


def test_asset_fingerprint_changes_with_content(tmp_path):
    f1 = tmp_path / "app.js"
    f2 = tmp_path / "app.css"
    f1.write_text("alpha")
    f2.write_text("beta")
    fp1 = build_mod.asset_fingerprint([str(f1), str(f2)])
    f1.write_text("alpha-CHANGED")
    fp2 = build_mod.asset_fingerprint([str(f1), str(f2)])
    assert fp1 != fp2
    assert HEX12.match(fp2)


# ---- root HTML cache-busting + headers ------------------------------------
def test_root_html_versioned_assets():
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    ver = build_mod.asset_fingerprint()
    assert f"/static/app.js?v={ver}" in body
    assert f"/static/app.css?v={ver}" in body


def test_root_html_no_unresolved_placeholder():
    r = client.get("/")
    body = r.text
    assert 'href="/static/app.css"' not in body   # replaced with ?v=
    assert 'src="/static/app.js"' not in body


def test_root_html_no_cache_headers():
    r = client.get("/")
    assert r.headers.get("Cache-Control") == "no-store, no-cache, must-revalidate, max-age=0"
    assert r.headers.get("Pragma") == "no-cache"


# ---- /v1/build schema + safety -------------------------------------------
def test_build_endpoint_schema():
    r = client.get("/v1/build")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Clarity"
    assert "current_commit" in body
    assert body["checkpoint_tag"] == "clarity-local-v1.0.0"
    assert body["checkpoint_commit"] == "f7dd11c4b5b31f44dc0d4f938be8528b8aef8fa0"
    assert HEX12.match(body["asset_version"])
    # The current build is NOT labelled as the checkpoint.
    assert body["current_commit"] != body["checkpoint_commit"] or body["current_commit"] == "unknown"


def test_build_endpoint_no_sensitive_leak():
    r = client.get("/v1/build")
    text = r.text.lower()
    for bad in ("ollama", "127.0.0.1", "localhost", "http://", "https://", "sk-",
                "gw_", "bearer", "api_key", "secret", "password", "traceback",
                "ollama_base_url", "token="):
        assert bad not in text, f"leak: {bad}"


# ---- /v1/diagnostics ------------------------------------------------------
def test_diagnostics_success_counts(monkeypatch):
    async def fake_local():
        return {"status": "ok", "models": [{"id": f"local:m{i}"} for i in range(7)]}

    async def fake_rt():
        return {"status": "ok", "models": [{"id": "local:qwen3:1.7b"}],
                "last_local_request": {"model": "local:qwen3:1.7b"}}

    monkeypatch.setattr(local_mod, "local_models_with_status", fake_local)
    monkeypatch.setattr(runtime_mod, "get_runtime", fake_rt)
    r = client.get("/v1/diagnostics")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["local"]["discovery_status"] == "ok"
    assert body["local"]["models_discovered"] == 7
    assert body["local"]["runtime_status"] == "ok"
    assert body["local"]["models_loaded"] == 1
    assert body["local"]["last_local_request_measured"] is True
    assert body["gateway"]["process_healthy"] is True
    assert body["build"]["checkpoint_tag"] == "clarity-local-v1.0.0"


def test_diagnostics_unavailable_states(monkeypatch):
    async def fake_local():
        return {"status": "unavailable", "models": []}

    async def fake_rt():
        return {"status": "unavailable", "models": [], "last_local_request": None}

    monkeypatch.setattr(local_mod, "local_models_with_status", fake_local)
    monkeypatch.setattr(runtime_mod, "get_runtime", fake_rt)
    r = client.get("/v1/diagnostics")
    body = r.json()
    assert body["local"]["discovery_status"] == "unavailable"
    assert body["local"]["models_discovered"] == 0
    assert body["local"]["runtime_status"] == "unavailable"
    assert body["local"]["models_loaded"] == 0
    assert body["local"]["last_local_request_measured"] is False


def test_diagnostics_empty_local_runtime_state(monkeypatch):
    async def fake_local():
        return {"status": "ok", "models": []}

    async def fake_rt():
        return {"status": "ok", "models": [], "last_local_request": None}

    monkeypatch.setattr(local_mod, "local_models_with_status", fake_local)
    monkeypatch.setattr(runtime_mod, "get_runtime", fake_rt)
    r = client.get("/v1/diagnostics")
    body = r.json()
    assert body["local"]["discovery_status"] == "ok"
    assert body["local"]["models_discovered"] == 0
    assert body["local"]["runtime_status"] == "ok"
    assert body["local"]["models_loaded"] == 0
    assert body["local"]["last_local_request_measured"] is False


def test_diagnostics_zero_inference(monkeypatch):
    async def fake_local():
        return {"status": "ok", "models": []}

    async def fake_rt():
        return {"status": "ok", "models": [], "last_local_request": None}

    monkeypatch.setattr(local_mod, "local_models_with_status", fake_local)
    monkeypatch.setattr(runtime_mod, "get_runtime", fake_rt)
    called = {"n": 0}

    async def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("inference was invoked")

    monkeypatch.setattr(main, "run_completion", boom)
    r = client.get("/v1/diagnostics")
    assert r.status_code == 200
    assert called["n"] == 0


def test_diagnostics_exposes_counts_not_raw_payload(monkeypatch):
    async def fake_local():
        return {"status": "ok", "models": [{"id": "local:x"}]}

    async def fake_rt():
        return {"status": "ok",
                "models": [{"id": "local:x"}],
                "last_local_request": {"model": "local:x", "prompt_tokens": 1,
                                       "completion_tokens": 2, "total_tokens": 3}}

    monkeypatch.setattr(local_mod, "local_models_with_status", fake_local)
    monkeypatch.setattr(runtime_mod, "get_runtime", fake_rt)
    r = client.get("/v1/diagnostics")
    text = r.text.lower()
    for bad in ("sk-", "gw_", "bearer", "secret", "password", "ollama_base_url",
                "127.0.0.1", "localhost", "http://", "https://", "traceback",
                "api key", "authorization", "x-api-key", "response"):
        assert bad not in text, f"leak: {bad}"


def test_diagnostics_no_cache_headers():
    r = client.get("/v1/diagnostics")
    assert r.headers.get("Cache-Control") == "no-store, no-cache, must-revalidate, max-age=0"
    assert r.headers.get("Pragma") == "no-cache"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
