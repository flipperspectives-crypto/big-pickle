"""Prove that raw upstream data is exposed in NEITHER the HTTP response NOR the
application logs.

This guards correction B: the upstream-error path must not place provider
response bodies, provider keys, Authorization tokens, internal URLs/hosts, or
unsanitized exception text into logs or client responses. Only sanitized
metadata (provider name, status code, exception class, safe reason code) may be
logged, and the client receives a safe, status-keyed message.
"""
import os
import logging

os.environ["GATEWAY_DB"] = "/tmp/gateway_sanitize_test.db"
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.main import app
from app.router import UpstreamError

client = TestClient(app)

# A stand-in for everything we must NEVER leak: a raw provider body, an internal
# host/port, and a secret provider key.
RAW = "RAW_BODY_SECRET apikey=sk-supersecret 127.0.0.1:11434 Authorization=Bearer leak"


def _funded_key():
    k = client.post("/v1/keys", json={"name": "sanitize"}, headers={"x-admin-key": "testadmin"}).json()
    client.post("/v1/credits", json={"key_id": k["id"], "amount": 5.0}, headers={"x-admin-key": "testadmin"})
    return k["skey"]


def test_upstreamerror_never_carries_raw_body():
    err = UpstreamError(500, provider="groq", reason="provider_http_500")
    assert RAW not in err.detail
    assert "sk-" not in err.detail
    assert "127.0.0.1" not in err.detail
    # the exception's repr must not embed a raw body either
    assert RAW not in str(err)


def test_upstream_raw_absent_from_response_and_logs(caplog):
    async def boom(body, key_id):
        # Even if a (buggy) caller stuffed a raw body into `detail`, the client
        # must get the safe message and logs must stay sanitized.
        raise UpstreamError(500, detail=RAW, provider="groq", reason="provider_http_500")

    original = main_mod.run_completion
    main_mod.run_completion = boom
    try:
        key = _funded_key()
        with caplog.at_level(logging.WARNING, logger="clarity"):
            r = client.post(
                "/v1/chat/completions",
                json={"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {key}"},
            )
        # HTTP response is sanitized
        assert r.status_code == 500
        body = r.text
        assert RAW not in body
        assert "sk-supersecret" not in body
        assert "127.0.0.1" not in body
        assert "Authorization" not in body
        assert "Bearer" not in body

        # application logs are sanitized
        assert RAW not in caplog.text
        assert "sk-supersecret" not in caplog.text
        assert "127.0.0.1" not in caplog.text
        assert "Authorization=Bearer leak" not in caplog.text
        # but sanitized metadata IS present (proves we log what's allowed)
        assert "provider=groq" in caplog.text
        assert "status=500" in caplog.text
        assert "reason=provider_http_500" in caplog.text
        assert "UpstreamError" in caplog.text
    finally:
        main_mod.run_completion = original


def test_generic_exception_logs_only_class(caplog):
    async def boom(body, key_id):
        raise RuntimeError("RAW trace secret 127.0.0.1 token=abc")

    original = main_mod.run_completion
    main_mod.run_completion = boom
    try:
        key = _funded_key()
        with caplog.at_level(logging.WARNING, logger="clarity"):
            r = client.post(
                "/v1/chat/completions",
                json={"model": "llama-3.1-8b", "messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": f"Bearer {key}"},
            )
        assert r.status_code == 502
        assert "RAW trace" not in r.text
        assert "RAW trace" not in caplog.text
        assert "127.0.0.1" not in caplog.text
        # only the exception class is logged
        assert "RuntimeError" in caplog.text
    finally:
        main_mod.run_completion = original


print("UPSTREAM SANITIZE TESTS PASSED")
