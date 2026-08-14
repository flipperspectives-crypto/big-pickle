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
    DEFAULT_BASE_URL,
    discover_clarity,
    demo,
    demo_direct,
    fetch_openapi,
    discover_paid_routes,
    select_direct_chat_route,
    DIRECT_CHAT_ROUTE,
)

# A realistic x402 v2 PAYMENT-REQUIRED header (mirrors what /v1/x402/topup sends).
# resource.url is the ABSOLUTE https URL the fixed gateway now advertises.
FAKE_PR_HEADER = base64.b64encode(
    json.dumps(
        {
            "x402Version": 2,
            "resource": {"url": f"{DEFAULT_BASE_URL}/v1/x402/topup"},
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
# resource identifies the direct route (absolute https URL) and distinguishes it
# from top-up.
FAKE_CHAT_PR_HEADER = base64.b64encode(
    json.dumps(
        {
            "x402Version": 2,
            "resource": {"url": f"{DEFAULT_BASE_URL}/v1/x402/chat/completions"},
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

# Mirrors what /v1/x402/solana/chat/completions (the SEPARATE Solana-devnet
# direct-inference route) sends. Solana devnet CAIP-2, devnet USDC asset, and a
# configured public Solana payTo -- all TEST funds, never Solana mainnet.
SOLANA_DEVNET_CAIP2 = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
SOLANA_DEVNET_USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
SOLANA_TEST_PAYTO = "So11111111111111111111111111111111111111112"

FAKE_SOLANA_PR_HEADER = base64.b64encode(
    json.dumps(
        {
            "x402Version": 2,
            "resource": {"url": f"{DEFAULT_BASE_URL}/v1/x402/solana/chat/completions"},
            "accepts": [
                {
                    "scheme": "exact",
                    "network": SOLANA_DEVNET_CAIP2,
                    "asset": SOLANA_DEVNET_USDC,
                    "amount": "1000",
                    "payTo": SOLANA_TEST_PAYTO,
                    "maxTimeoutSeconds": 60,
                    "extra": {"feePayer": "FeePayer1111111111111111111111111111111111"},
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


# Default curated OpenAPI the gateway serves at /openapi.json (two x402-paid
# routes). Tests may pass a custom `openapi_doc` to exercise selection edges.
_DEFAULT_OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Clarity Agent API", "version": "0.1.0"},
    "paths": {
        "/v1/x402/topup": {
            "post": {
                "operationId": "purchaseClarityInferenceCredit",
                "summary": "Purchase Clarity AI inference credit",
                "description": (
                    "Machine-payable top-up. After successful settlement the caller "
                    "receives gateway credit plus a secret skey for /v1/chat/completions."
                ),
                "x-payment-info": {
                    "price": {"mode": "fixed", "currency": "USD", "amount": "0.001000"},
                    "protocols": [{"x402": {}}],
                },
                "responses": {"402": {"description": "Payment Required"}},
            }
        },
        "/v1/x402/chat/completions": {
            "post": {
                "operationId": "purchaseClarityChatCompletion",
                "summary": "Buy a Clarity AI chat completion",
                "description": (
                    "Direct pay-per-request AI inference through Clarity using "
                    "local:qwen3:1.7b. Receive an OpenAI-compatible chat completion "
                    "directly. No Clarity API key is required."
                ),
                "x-payment-info": {
                    "price": {"mode": "fixed", "currency": "USD", "amount": "0.001000"},
                    "protocols": [{"x402": {}}],
                    "network": "eip155:84532",
                },
                "responses": {"402": {"description": "Payment Required"}},
            }
        },
    },
}


def _make_transport(calls=None, openapi_doc=None):
    doc = openapi_doc if openapi_doc is not None else _DEFAULT_OPENAPI

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
        if request.method == "GET" and path == "/openapi.json":
            return httpx.Response(200, json=doc)
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
        if request.method == "POST" and path == "/v1/x402/solana/chat/completions":
            # SEPARATE Solana-devnet direct-inference challenge: unpaid -> 402 +
            # PAYMENT-REQUIRED (resource /v1/x402/solana/chat/completions).
            # Paid retry -> completion, no skey.
            if "PAYMENT-SIGNATURE" not in request.headers:
                return httpx.Response(
                    402, headers={"PAYMENT-REQUIRED": FAKE_SOLANA_PR_HEADER}, json={}
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-solana",
                    "object": "chat.completion",
                    "choices": [
                        {"message": {"role": "assistant", "content": "hi solana"}}
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
    assert pr.resource == f"{DEFAULT_BASE_URL}/v1/x402/chat/completions"
    assert pr.network == "eip155:84532"
    assert pr.asset == "0x036CbD53842c5426634e7929541eC2318f3dCF7E"
    assert pr.amount == "1000"
    assert pr.pay_to == "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f"


def test_agent_direct_flow_dry_run_stops_before_signing(fake_server):
    agent = ClarityAgent(dry_run=True)
    result = asyncio.run(agent.run_direct())
    assert result["mode"] == "dry-run"
    assert result["flow"] == "direct"
    assert result["payment_required"]["resource"] == f"{DEFAULT_BASE_URL}/v1/x402/chat/completions"
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
    assert decoded["resource"] == f"{DEFAULT_BASE_URL}/v1/x402/chat/completions"


# ---------------------------------------------------------------------------
# Origin-only discovery (no route path supplied manually)
# ---------------------------------------------------------------------------
def test_discover_from_origin_only_begins_with_base_url():
    calls = []
    transport = _make_transport(calls)
    agent = ClarityAgent(dry_run=True, transport=transport)
    # run_direct takes NO path argument — it starts from the origin only.
    result = asyncio.run(agent.run_direct())
    assert result["mode"] == "dry-run"
    assert result["discovery"]["title"] == "Clarity Agent API"
    assert result["discovery"]["selected_route"] == "/v1/x402/chat/completions"
    assert result["discovery"]["selected_method"] == "POST"
    assert "selection_reason" in result["discovery"]
    assert result["payment_required"]["resource"] == f"{DEFAULT_BASE_URL}/v1/x402/chat/completions"
    # dry-run STOP before signing.
    assert result["payment_signed"] is False


def test_openapi_discovery_is_fetched():
    calls = []
    transport = _make_transport(calls)
    agent = ClarityAgent(dry_run=True, transport=transport)
    asyncio.run(agent.run_direct())
    disc = [c for c in calls if c[1] == "/openapi.json"]
    assert disc, "agent must fetch /openapi.json for discovery"
    assert disc[0][0] == "GET"


def test_direct_chat_route_found_from_metadata():
    paid = discover_paid_routes(_DEFAULT_OPENAPI)
    paths = [r["path"] for r in paid]
    assert "/v1/x402/chat/completions" in paths
    assert "/v1/x402/topup" in paths


def test_route_selected_over_topup_for_chat():
    paid = discover_paid_routes(_DEFAULT_OPENAPI)
    chosen = select_direct_chat_route(paid)
    assert chosen["path"] == "/v1/x402/chat/completions"
    assert chosen["path"] != "/v1/x402/topup"
    assert "chat" in (chosen["summary"]).lower()


def test_route_method_path_derived_from_discovery_not_supplied():
    # Prove the agent follows the discovered path (not a hard-coded constant):
    # serve the chat route under a custom relative path in the OpenAPI doc and
    # a matching transport handler; the agent must POST to that discovered path.
    custom_doc = json.loads(json.dumps(_DEFAULT_OPENAPI))
    custom_doc["paths"]["/v1/x402/inference-now"] = custom_doc["paths"].pop(
        "/v1/x402/chat/completions"
    )

    def handler(request):
        if request.method == "GET" and request.url.path == "/openapi.json":
            return httpx.Response(200, json=custom_doc)
        if request.method == "POST" and request.url.path == "/v1/x402/inference-now":
            return httpx.Response(
                402, headers={"PAYMENT-REQUIRED": FAKE_CHAT_PR_HEADER}, json={}
            )
        return httpx.Response(404)

    calls = []
    transport = httpx.MockTransport(
        lambda req: _record_and_dispatch(req, calls, handler)
    )
    agent = ClarityAgent(dry_run=True, transport=transport)
    result = asyncio.run(agent.run_direct())
    assert result["discovery"]["selected_route"] == "/v1/x402/inference-now"
    chat_calls = [c for c in calls if c[1] == "/v1/x402/inference-now"]
    assert chat_calls, "agent must POST to the discovered path"
    assert chat_calls[0][1] != DIRECT_CHAT_ROUTE  # proves it followed metadata


def _record_and_dispatch(request, calls, handler):
    calls.append((request.method, request.url.path, dict(request.headers)))
    return handler(request)


def test_malformed_discovery_fails_safely():
    agent = ClarityAgent(dry_run=True)
    with pytest.raises(RuntimeError):
        select_direct_chat_route(discover_paid_routes({"paths": {}}))


def test_no_matching_paid_inference_route_fails_safely():
    # OpenAPI advertises ONLY the top-up route -> no direct inference candidate.
    only_topup = {"paths": {k: v for k, v in _DEFAULT_OPENAPI["paths"].items() if k == "/v1/x402/topup"}}
    calls = []
    transport = _make_transport(calls, openapi_doc=only_topup)
    agent = ClarityAgent(dry_run=True, transport=transport)
    result = asyncio.run(agent.run_direct())
    assert result["error"] is not None
    assert "No suitable direct paid inference route" in result["error"]


def test_unrelated_paid_route_not_selected():
    weather = {
        "openapi": "3.1.0",
        "info": {"title": "Clarity Agent API"},
        "paths": {
            "/v1/x402/weather": {
                "post": {
                    "operationId": "purchaseWeather",
                    "summary": "Buy weather data",
                    "description": "Machine-payable weather forecast.",
                    "x-payment-info": {
                        "price": {"mode": "fixed", "currency": "USD", "amount": "0.001000"},
                        "protocols": [{"x402": {}}],
                    },
                    "responses": {"402": {"description": "Payment Required"}},
                }
            }
        },
    }
    calls = []
    transport = _make_transport(calls, openapi_doc=weather)
    agent = ClarityAgent(dry_run=True, transport=transport)
    result = asyncio.run(agent.run_direct())
    assert result["error"] is not None
    assert "No suitable direct paid inference route" in result["error"]


def test_cross_origin_route_metadata_rejected():
    cross = json.loads(json.dumps(_DEFAULT_OPENAPI))
    cross["paths"]["https://evil.example/x402/chat/completions"] = cross["paths"].pop(
        "/v1/x402/chat/completions"
    )
    paid = discover_paid_routes(cross)
    with pytest.raises(RuntimeError):
        select_direct_chat_route(paid)


def test_direct_flow_sends_no_payment_signature_discovery():
    calls = []
    transport = _make_transport(calls)
    agent = ClarityAgent(dry_run=True, transport=transport)
    asyncio.run(agent.run_direct())
    # No top-up, and no PAYMENT-SIGNATURE is ever sent.
    assert not [c for c in calls if c[1] == "/v1/x402/topup"]
    assert not [c for c in calls if "PAYMENT-SIGNATURE" in (c[2] or {})]
    chat_calls = [c for c in calls if c[1] == "/v1/x402/chat/completions"]
    assert len(chat_calls) == 1
    assert "PAYMENT-SIGNATURE" not in (chat_calls[0][2] or {})


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


# ---------------------------------------------------------------------------
# CLARITY SOLANA: a SEPARATE Solana-devnet direct-inference x402 route.
# The agent must be able to (a) discover it, (b) obtain its 402, (c) parse the
# network/asset/amount/payTo, (d) STOP before signing (dry-run), and (e) never
# transmit a PAYMENT-SIGNATURE. Existing Base discovery/flow tests are
# unaffected.
# ---------------------------------------------------------------------------

# A discovery doc advertising ALL three routes so the network filter can be
# proven to pick Base vs Solana independently.
_SOLANA_OPENAPI = json.loads(json.dumps(_DEFAULT_OPENAPI))
_SOLANA_OPENAPI["paths"]["/v1/x402/solana/chat/completions"] = {
    "post": {
        "operationId": "purchaseClaritySolanaDevnetChatCompletion",
        "summary": "Buy a Clarity AI chat completion (Solana devnet x402)",
        "description": (
            "Direct pay-per-request AI inference through Clarity using "
            "local:qwen3:1.7b, paid via Solana DEVNET x402. No Clarity API key."
        ),
        "x-payment-info": {
            "price": {"mode": "fixed", "currency": "USD", "amount": "0.001000"},
            "protocols": [{"x402": {}}],
            "network": SOLANA_DEVNET_CAIP2,
        },
        "responses": {"402": {"description": "Payment Required"}},
    }
}


def test_select_direct_chat_route_network_filter():
    paid = discover_paid_routes(_SOLANA_OPENAPI)
    sol = select_direct_chat_route(paid, network=SOLANA_DEVNET_CAIP2)
    assert sol["path"] == "/v1/x402/solana/chat/completions"
    base = select_direct_chat_route(paid, network="eip155:84532")
    assert base["path"] == "/v1/x402/chat/completions"
    # Without a network hint, the Base direct chat route is still preferred.
    default = select_direct_chat_route(paid)
    assert default["path"] == "/v1/x402/chat/completions"


def test_agent_solana_dry_run_discovers_and_stops():
    calls = []
    transport = _make_transport(calls, openapi_doc=_SOLANA_OPENAPI)
    agent = ClarityAgent(dry_run=True, transport=transport)
    result = asyncio.run(agent.run_direct(network=SOLANA_DEVNET_CAIP2))
    assert result["mode"] == "dry-run"
    assert result["discovery"]["selected_route"] == "/v1/x402/solana/chat/completions"
    pr = result["payment_required"]
    assert pr["network"] == SOLANA_DEVNET_CAIP2
    assert pr["asset"] == SOLANA_DEVNET_USDC
    assert pr["amount"] == "1000"
    assert pr["pay_to"] == SOLANA_TEST_PAYTO
    assert pr["resource"] == f"{DEFAULT_BASE_URL}/v1/x402/solana/chat/completions"
    # Dry-run must STOP before signing/settling.
    assert result["payment_signed"] is False
    assert result["settlement_verified"] is False
    assert result["gateway_credit_verified"] is False
    assert result["inference_verified"] is False
    # No PAYMENT-SIGNATURE is ever transmitted.
    assert not [c for c in calls if "PAYMENT-SIGNATURE" in (c[2] or {})]
    sol_calls = [c for c in calls if c[1] == "/v1/x402/solana/chat/completions"]
    assert len(sol_calls) == 1
    assert "PAYMENT-SIGNATURE" not in (sol_calls[0][2] or {})
