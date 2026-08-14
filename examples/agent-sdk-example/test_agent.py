"""Offline tests for the Clarity Agent SDK example.

These run with a mocked HTTP transport and, where noted, a mocked or offline
x402 SDK client. No real network calls, no funds, no real inference.
"""

import asyncio
import base64
import json
import os

import httpx
import pytest

sys_path = os.path.dirname(os.path.abspath(__file__))
import sys  # noqa: E402

sys.path.insert(0, sys_path)

import clarity_agent  # noqa: E402

from clarity_agent import (  # noqa: E402
    ClarityAgent,
    PaymentRequired,
    discover_clarity,
    demo,
    demo_direct,
)

# A realistic x402 v2 PAYMENT-REQUIRED header (mirrors what /v1/x402/topup sends).
FAKE_PR_HEADER = base64.b64encode(
    json.dumps(
        {
            "x402Version": 2,
            "resource": {"url": "/v1/x402/topup"},
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7E",
                    "amount": "1000",
                    "payTo": "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f",
                    "maxTimeoutSeconds": 60,
                    "extra": {"assetTransferMethod": "eip3009"},
                }
            ],
        }
    ).encode()
).decode()


# Mirrors what /v1/x402/chat/completions (DIRECT paid inference) sends. The
# resource identifies the direct route and distinguishes it from top-up.
FAKE_CHAT_PR_HEADER = base64.b64encode(
    json.dumps(
        {
            "x402Version": 2,
            "resource": {"url": "/v1/x402/chat/completions"},
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7E",
                    "amount": "1000",
                    "payTo": "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f",
                    "maxTimeoutSeconds": 60,
                    "extra": {"assetTransferMethod": "eip3009"},
                }
            ],
        }
    ).encode()
).decode()

# Value-like markers that should NEVER appear in any output. Descriptive words
# ("authorization", "bearer", "private") are intentionally excluded so the
# plan's explanatory text does not trigger false positives.
SECRET_SUBSTRINGS = [
    "sk-",
    "gw_",
    "x-api-key",
    "api_key",
]


def _make_transport(calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append((request.method, request.url.path, dict(request.headers)))
        path = request.url.path
        if request.method == "GET" and path == "/v1/status":
            return httpx.Response(
                200,
                json={
                    "status": "healthy",
                    "gateway": {"active_keys": 1, "total_balance_usd": 0.5},
                },
            )
        if request.method == "POST" and path == "/v1/x402/topup":
            # Unpaid request -> 402 + PAYMENT-REQUIRED (the only x402 challenge).
            if "PAYMENT-SIGNATURE" not in request.headers:
                return httpx.Response(
                    402, headers={"PAYMENT-REQUIRED": FAKE_PR_HEADER}, json={}
                )
            # Paid retry -> gateway credit with an skey.
            return httpx.Response(
                200, json={"id": "k1", "skey": "gw_test_skey", "balance_usd": 1.0}
            )
        if request.method == "POST" and path == "/v1/chat/completions":
            auth = request.headers.get("Authorization", "")
            if auth == "Bearer gw_test_skey":
                return httpx.Response(
                    200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]}
                )
            # Insufficient-balance 402: NO PAYMENT-REQUIRED header (not an x402 challenge).
            return httpx.Response(402, json={"error": "insufficient balance"})
        if request.method == "POST" and path == "/v1/x402/chat/completions":
            # DIRECT paid-inference challenge: unpaid -> 402 + PAYMENT-REQUIRED
            # (resource /v1/x402/chat/completions). Paid retry -> completion, no skey.
            if "PAYMENT-SIGNATURE" not in request.headers:
                return httpx.Response(
                    402, headers={"PAYMENT-REQUIRED": FAKE_CHAT_PR_HEADER}, json={}
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [
                        {"message": {"role": "assistant", "content": "hi direct"}}
                    ],
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def fake_server(monkeypatch):
    transport = _make_transport()
    real_client = httpx.Client

    def _client_factory(*a, **k):
        return real_client(transport=transport)

    monkeypatch.setattr(clarity_agent.httpx, "Client", _client_factory)
    return transport


# ---------------------------------------------------------------------------
# Discovery + parsing
# ---------------------------------------------------------------------------
def test_discover_reads_public_status(fake_server):
    agent = ClarityAgent()
    status, data, _ = agent.discover()
    assert status == 200
    assert data["status"] == "healthy"


def test_discover_module_function(fake_server):
    data = discover_clarity()
    assert data["status"] == "healthy"


def test_parse_payment_required_header():
    pr = PaymentRequired.from_header(FAKE_PR_HEADER)
    assert pr.scheme == "exact"
    assert pr.network == "eip155:84532"
    assert pr.asset == "0x036CbD53842c5426634e7929541eC2318f3dCF7E"
    assert pr.amount == "1000"
    assert pr.pay_to == "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f"
    assert pr.max_timeout_seconds == 60


def test_topup_challenge_returns_402_with_payment_required(fake_server):
    agent = ClarityAgent()
    pr, raw, body = agent.topup_challenge()
    assert pr.scheme == "exact"
    assert pr.network == "eip155:84532"
    assert raw == FAKE_PR_HEADER


def test_chat_insufficient_balance_returns_plain_402_no_challenge(fake_server):
    agent = ClarityAgent()
    status, data, headers = agent._post("/v1/chat/completions", {}, {})
    assert status == 402
    assert "PAYMENT-REQUIRED" not in headers  # chat 402 is NOT an x402 challenge


# ---------------------------------------------------------------------------
# DIRECT paid-inference route (/v1/x402/chat/completions)
# ---------------------------------------------------------------------------
def test_chat_direct_challenge_returns_402_with_payment_required(fake_server):
    agent = ClarityAgent()
    pr, raw, body = agent.chat_challenge()
    assert pr.scheme == "exact"
    assert pr.network == "eip155:84532"
    assert raw == FAKE_CHAT_PR_HEADER


def test_direct_chat_402_parses_resource_and_fields(fake_server):
    agent = ClarityAgent()
    pr, _raw, _body = agent.chat_challenge()
    # The agent can identify network, asset, amount, payTo AND resource.
    assert pr.resource == "/v1/x402/chat/completions"
    assert pr.network == "eip155:84532"
    assert pr.asset == "0x036CbD53842c5426634e7929541eC2318f3dCF7E"
    assert pr.amount == "1000"
    assert pr.pay_to == "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f"


def test_agent_direct_flow_dry_run_stops_before_signing(fake_server):
    agent = ClarityAgent(dry_run=True)
    result = asyncio.run(agent.run_direct())
    assert result["mode"] == "dry-run"
    assert result["flow"] == "direct"
    assert result["payment_required"]["resource"] == "/v1/x402/chat/completions"
    assert "dry_run_plan" in result
    # DRY-RUN must never sign or settle.
    assert result["payment_signed"] is False
    assert result["settlement_verified"] is False
    assert result["inference_verified"] is False
    assert result["gateway_credit_verified"] is False
    sig = result["dry_run_plan"]["simulated_signature"]
    assert sig.startswith("SIMULATED.")
    decoded = json.loads(base64.b64decode(sig.split(".", 1)[1]))
    assert decoded["simulated"] is True
    assert decoded["resource"] == "/v1/x402/chat/completions"


def test_direct_flow_sends_no_payment_signature_and_no_topup():
    calls = []
    transport = _make_transport(calls)

    agent = ClarityAgent(dry_run=True, transport=transport)
    result = asyncio.run(agent.run_direct())

    # No top-up was required before discovering/using the direct route.
    topup_calls = [c for c in calls if c[1] == "/v1/x402/topup"]
    assert not topup_calls, "direct flow must not call /v1/x402/topup"
    # No PAYMENT-SIGNATURE was ever sent (no paid retry happened).
    sig_calls = [c for c in calls if "PAYMENT-SIGNATURE" in (c[2] or {})]
    assert not sig_calls, "dry-run must not send a PAYMENT-SIGNATURE header"
    # Exactly one unpaid direct request was made.
    chat_calls = [c for c in calls if c[1] == "/v1/x402/chat/completions"]
    assert len(chat_calls) == 1
    assert chat_calls[0][0] == "POST"
    assert "PAYMENT-SIGNATURE" not in (chat_calls[0][2] or {})


def test_demo_direct_runs_offline(fake_server, capsys):
    demo_direct()
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "/v1/x402/chat/completions" in out
    assert "SIMULATED" in out


# ---------------------------------------------------------------------------
# DRY-RUN
# ---------------------------------------------------------------------------
def test_dry_run_shows_simulated_plan_and_no_skey(fake_server):
    agent = ClarityAgent(dry_run=True)
    result = asyncio.run(agent.run())
    assert result["mode"] == "dry-run"
    assert "dry_run_plan" in result
    assert result["payment_signed"] is False
    assert result["skey_present"] is False
    assert result["inference_verified"] is False
    sig = result["dry_run_plan"]["simulated_signature"]
    assert sig.startswith("SIMULATED.")
    decoded = json.loads(base64.b64decode(sig.split(".", 1)[1]))
    assert decoded["simulated"] is True


def test_no_secrets_in_dry_run_output(fake_server):
    agent = ClarityAgent(dry_run=True)
    result = asyncio.run(agent.run())
    blob = json.dumps(result).lower()
    for s in SECRET_SUBSTRINGS:
        assert s not in blob


def test_self_contained_demo_runs_offline(fake_server, capsys):
    demo()
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "PAYMENT-REQUIRED" not in out or "SIMULATED" in out


# ---------------------------------------------------------------------------
# LIVE (guarded; never spends)
# ---------------------------------------------------------------------------
def test_live_requires_env_key(fake_server, monkeypatch):
    monkeypatch.delenv("X402_PAYER_KEY", raising=False)
    agent = ClarityAgent(dry_run=False)
    with pytest.raises(RuntimeError):
        asyncio.run(agent.run())


def test_live_two_endpoint_flow_mocked(monkeypatch):
    monkeypatch.setenv("X402_PAYER_KEY", "0xdummy_private_key_for_test_only")
    calls = []
    transport = _make_transport(calls)
    real_client = httpx.Client
    monkeypatch.setattr(
        clarity_agent.httpx, "Client", lambda *a, **k: real_client(transport=transport)
    )

    class FakeHTTPClient:
        async def handle_402_response(self, headers, body):
            return (
                {
                    "PAYMENT-SIGNATURE": "FAKE_SIG",
                    "Access-Control-Expose-Headers": "PAYMENT-RESPONSE",
                },
                {"x402Version": 2},
            )

        async def process_payment_result(self, payload, get_header, status):
            from types import SimpleNamespace

            return SimpleNamespace(recovered=True)

    monkeypatch.setattr(
        clarity_agent.ClarityAgent,
        "_build_client",
        lambda self, key, net: (FakeHTTPClient(), "0xpayer"),
    )

    agent = ClarityAgent(dry_run=False)
    result = asyncio.run(agent.run(model="clarity/local-demo"))

    assert result["mode"] == "live"
    assert result["payment_signed"] is True
    assert result["settlement_verified"] is True
    assert result["gateway_credit_verified"] is True
    assert result["inference_verified"] is True
    assert result["chat_status"] == 200

    # chat must have been called with the skey returned by the top-up
    chat_calls = [c for c in calls if c[1] == "/v1/chat/completions"]
    assert chat_calls, "chat/completions was never called"
    sent_auth = chat_calls[0][2].get("Authorization") or chat_calls[0][2].get("authorization")
    assert sent_auth == "Bearer gw_test_skey"

    # the private key value must never appear in the result
    assert "0xdummy_private_key_for_test_only" not in json.dumps(result)


def test_live_sdk_integration_offline():
    """Exercise the REAL x402 client SDK offline (no network, no funds).

    The exact/eip3009 scheme signs locally, so a valid PAYMENT-SIGNATURE is
    produced without any facilitator or on-chain interaction.
    """
    from eth_account import Account
    from x402 import x402Client
    from x402.http.x402_http_client import x402HTTPClient
    from x402.mechanisms.evm.exact import ExactEvmScheme

    acct = Account.create()
    scheme = ExactEvmScheme(acct)
    client = x402Client()
    client.register("eip155:84532", scheme)
    hc = x402HTTPClient(client)

    headers, payload = asyncio.run(
        hc.handle_402_response({"PAYMENT-REQUIRED": FAKE_PR_HEADER}, b"")
    )
    assert "PAYMENT-SIGNATURE" in headers
    decoded = json.loads(base64.b64decode(headers["PAYMENT-SIGNATURE"]))
    assert decoded["x402Version"] == 2
    assert decoded["payload"]["authorization"]["from"].lower() == acct.address.lower()


# ---------------------------------------------------------------------------
# Documentation alignment (stale statements removed)
# ---------------------------------------------------------------------------
def test_agent_quickstart_no_longer_claims_single_challenge():
    quickstart = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "docs",
        "AGENT_QUICKSTART.md",
    )
    with open(quickstart, "r", encoding="utf-8") as fh:
        text = fh.read()
    # The stale claim that top-up is the ONLY x402-challenge endpoint is gone.
    assert "only endpoint that issues an x402 challenge" not in text
    # Both machine-payable resources are now documented.
    assert "/v1/x402/topup" in text
    assert "/v1/x402/chat/completions" in text
