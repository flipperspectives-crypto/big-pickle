"""Deterministic tests for the DIRECT x402-paid inference route.

POST /v1/x402/chat/completions lets an external agent pay once and receive a
local qwen3:1.7b completion directly -- no signup, no skey handoff, no second
request. These tests prove the contract, the strict request validation, the
local-only execution path, the top-up credit isolation in the settlement hook,
and that no real payment/inference/CDP secret is ever used (FakeFacilitator +
mocked run_completion only).

NOTE: this repo has no pytest-asyncio plugin, so async helpers are driven via
``asyncio.run`` from plain ``def`` tests (an ``async def`` test body never
executes under pytest without an async plugin).
"""

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from x402 import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import PaymentRequirements
from x402.extensions.bazaar import BAZAAR

import app.config as config_mod
import app.db as db_mod
import app.x402 as x402_mod
from app.x402 import build_x402_middleware, CHAT_ROUTE, TOPUP_ROUTE

TESTNET_CHAIN = "eip155:84532"
MAINNET_CHAIN = "eip155:8453"
TESTNET_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
MAINNET_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EXPECTED_PAYTO = "0x1111111111111111111111111111111111111111"


def _run(coro):
    return asyncio.run(coro)


class FakeFacilitator:
    def __init__(self):
        self.settled = 0

    async def verify(self, payload, requirements):
        from x402.schemas.hooks import ResourceVerifyResponse
        from x402.schemas.responses import VerifyResponse

        payer = payload.payload["authorization"]["from"]
        rv = ResourceVerifyResponse(verify=VerifyResponse(is_valid=True, payer=payer))
        rv.payment_payload = payload
        rv.payment_requirements = requirements
        return rv

    async def settle(self, payload, requirements):
        from x402.schemas.responses import SettleResponse

        self.settled += 1
        return SettleResponse(
            success=True,
            transaction="0xTXHASH",
            network=config_mod.settings.X402_CHAIN_ID,
            payer=payload.payload["authorization"]["from"],
        )

    def get_supported(self):
        from x402.schemas import SupportedKind, SupportedResponse

        return SupportedResponse(
            kinds=[
                SupportedKind(
                    x402Version=2,
                    scheme="exact",
                    network=config_mod.settings.X402_CHAIN_ID,
                    extra={},
                )
            ]
        )


def _make_app(fac):
    server = x402ResourceServer(fac)
    server.register(config_mod.settings.X402_CHAIN_ID, ExactEvmServerScheme())
    server.initialize()
    mw = build_x402_middleware(facilitator_client=fac, server=server, sync_facilitator_on_start=False)
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(BaseHTTPMiddleware, dispatch=mw)

    @app.post("/v1/x402/topup")
    async def topup(request: Request):
        return await x402_mod.x402_topup(request)

    @app.post("/v1/x402/chat/completions")
    async def chat(request: Request):
        raw = await request.body()
        try:
            body = json.loads(raw)
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        return await x402_mod.handle_x402_chat(body)

    return app


@pytest.fixture
def clarity_x402_direct(tmp_path):
    s = config_mod.settings
    saved = {
        "DB_PATH": s.DB_PATH,
        "X402_PAYTO": s.X402_PAYTO,
        "X402_NETWORK_MODE": s.X402_NETWORK_MODE,
        "X402_CHAIN_ID": s.X402_CHAIN_ID,
        "X402_CHAIN_INT": s.X402_CHAIN_INT,
        "X402_ASSET": s.X402_ASSET,
        "X402_FACILITATOR_URL": s.X402_FACILITATOR_URL,
        "X402_RPC_URL": s.X402_RPC_URL,
        "X402_PRICE_USD": s.X402_PRICE_USD,
        "X402_CDP_API_KEY_ID": s.X402_CDP_API_KEY_ID,
        "X402_CDP_API_KEY_SECRET": s.X402_CDP_API_KEY_SECRET,
        "X402_ENABLED": s.X402_ENABLED,
    }
    s.DB_PATH = str(tmp_path / "x402_direct.db")
    s.X402_PAYTO = EXPECTED_PAYTO
    s.X402_NETWORK_MODE = "testnet"
    s.X402_CHAIN_ID = TESTNET_CHAIN
    s.X402_CHAIN_INT = 84532
    s.X402_ASSET = TESTNET_ASSET
    s.X402_FACILITATOR_URL = "https://x402.org/facilitator"
    s.X402_RPC_URL = "https://sepolia.base.org"
    s.X402_PRICE_USD = "0.001"
    s.X402_CDP_API_KEY_ID = ""
    s.X402_CDP_API_KEY_SECRET = ""
    s.X402_ENABLED = False
    db_mod.init_db()
    yield
    for k, v in saved.items():
        setattr(s, k, v)


def _challenge(client, path):
    r = client.post(path, json={})
    assert r.status_code == 402, r.text
    raw = r.headers.get("payment-required")
    assert raw
    return r, json.loads(base64.b64decode(raw))


def _fake_req(body, paid=False):
    class FakeReq:
        headers = {}
        if paid:
            state = SimpleNamespace(
                payment_payload=SimpleNamespace(
                    payload={"authorization": {"from": "0xpayer"}}
                )
            )
        else:
            state = SimpleNamespace(payment_payload=None)

        async def stream(self):
            yield json.dumps(body).encode()

    return FakeReq()


VALID_BODY = {
    "model": "local:qwen3:1.7b",
    "messages": [{"role": "user", "content": "Say hello in one sentence."}],
}


# A. UNPAID DIRECT ROUTE -> 402, run_completion NOT called.
def test_direct_unpaid_returns_402(clarity_x402_direct, monkeypatch):
    called = {"n": 0}

    async def fake_run(*a, **k):
        called["n"] += 1
        raise AssertionError("run_completion must not run for an unpaid request")

    monkeypatch.setattr(x402_mod, "run_completion", fake_run)
    client = TestClient(_make_app(FakeFacilitator()))
    r = client.post(CHAT_ROUTE, json=VALID_BODY)
    assert r.status_code == 402
    assert called["n"] == 0


# B. INVALID MODEL -> 400, run_completion NOT called.
def test_direct_invalid_model(monkeypatch):
    called = {"n": 0}

    async def fake_run(*a, **k):
        called["n"] += 1
        raise AssertionError("should not run")

    monkeypatch.setattr(x402_mod, "run_completion", fake_run)
    with pytest.raises(HTTPException) as e:
        _run(x402_mod.handle_x402_chat({"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}))
    assert e.value.status_code == 400
    assert called["n"] == 0


# C. STREAM REJECTED.
def test_direct_stream_rejected():
    with pytest.raises(HTTPException) as e:
        _run(x402_mod.handle_x402_chat({**VALID_BODY, "stream": True}))
    assert e.value.status_code == 400


# D. MAX TOKENS.
def test_direct_max_tokens(clarity_x402_direct, monkeypatch):
    captured = {}

    async def fake_run(body, key_id=None, record_usage_flag=False):
        captured["body"] = body
        captured["key_id"] = key_id
        captured["record_usage"] = record_usage_flag
        return {"id": "x", "object": "chat.completion", "model": "qwen3:1.7b",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}, 0.0, "local"

    monkeypatch.setattr(x402_mod, "run_completion", fake_run)

    with pytest.raises(HTTPException) as e:
        _run(x402_mod.handle_x402_chat({**VALID_BODY, "max_tokens": 129}))
    assert e.value.status_code == 400

    _run(x402_mod.handle_x402_chat({**VALID_BODY, "max_tokens": 128}))
    assert captured["body"]["max_tokens"] == 128

    captured.clear()
    _run(x402_mod.handle_x402_chat(dict(VALID_BODY)))
    assert captured["body"]["max_tokens"] == 128


# E. MESSAGE LIMITS.
def test_direct_message_limits():
    with pytest.raises(HTTPException) as e:
        _run(x402_mod.handle_x402_chat({"model": "local:qwen3:1.7b", "messages": []}))
    assert e.value.status_code == 400

    too_many = [{"role": "user", "content": "x"}] * 17
    with pytest.raises(HTTPException) as e:
        _run(x402_mod.handle_x402_chat({"model": "local:qwen3:1.7b", "messages": too_many}))
    assert e.value.status_code == 400

    big = {"model": "local:qwen3:1.7b", "messages": [{"role": "user", "content": "a" * 12001}]}
    with pytest.raises(HTTPException) as e:
        _run(x402_mod.handle_x402_chat(big))
    assert e.value.status_code == 400


# F. LOCAL ONLY + G. THINKING CONTROL.
def test_direct_local_only_and_thinking(clarity_x402_direct, monkeypatch):
    captured = {}

    async def fake_run(body, key_id=None, record_usage_flag=False):
        captured["body"] = body
        captured["key_id"] = key_id
        captured["record_usage"] = record_usage_flag
        return {"id": "x", "object": "chat.completion", "model": "qwen3:1.7b",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}, 0.0, "local"

    monkeypatch.setattr(x402_mod, "run_completion", fake_run)

    _run(x402_mod.handle_x402_chat(dict(VALID_BODY)))
    body = captured["body"]
    assert body["model"] == "local:qwen3:1.7b"
    # No cloud key/balance is charged: run_completion is called with no key and
    # usage recording disabled.
    assert captured["key_id"] is None
    assert captured["record_usage"] is False
    # qwen3 thinking disabled for this direct route.
    assert body.get("think") is False


# H. CONCURRENCY: local slot unavailable -> 503, no inference starts.
def test_direct_concurrency_unavailable(monkeypatch):
    import app.main as main_mod

    called = {"n": 0}

    async def spy_handle(body):
        called["n"] += 1
        raise AssertionError("handle must not run when slot unavailable")

    monkeypatch.setattr(x402_mod, "handle_x402_chat", spy_handle)

    async def _acquire_false():
        return False

    monkeypatch.setattr(main_mod, "_local_concurrency", SimpleNamespace(acquire=_acquire_false))

    with pytest.raises(HTTPException) as e:
        _run(main_mod.x402_chat_completions(_fake_req(VALID_BODY, paid=True)))
    assert e.value.status_code == 503
    assert called["n"] == 0


# I. SLOT RELEASE: exactly once on success and on exception.
def test_direct_slot_release(monkeypatch):
    import app.main as main_mod

    class FakeConc:
        def __init__(self):
            self.acquire_calls = 0
            self.release_calls = 0

        async def acquire(self):
            self.acquire_calls += 1
            return True

        def release(self):
            self.release_calls += 1

    conc = FakeConc()
    monkeypatch.setattr(main_mod, "_local_concurrency", conc)

    async def ok_handle(body):
        return {"id": "x", "object": "chat.completion"}

    monkeypatch.setattr(x402_mod, "handle_x402_chat", ok_handle)
    _run(main_mod.x402_chat_completions(_fake_req(VALID_BODY, paid=True)))
    assert conc.release_calls == 1

    async def err_handle(body):
        raise HTTPException(400, "bad")

    monkeypatch.setattr(x402_mod, "handle_x402_chat", err_handle)
    with pytest.raises(HTTPException):
        _run(main_mod.x402_chat_completions(_fake_req(VALID_BODY, paid=True)))
    assert conc.release_calls == 2


# H2. ROUTE FAIL-CLOSED: a request reaching the route handler without a verified
# payment payload (exactly the mainnet-no-CDP situation, where build_x402_middleware
# returns None and the middleware is never installed) must be refused and must NOT
# perform any inference.
def test_direct_route_fail_closed_without_payment(monkeypatch):
    import app.main as main_mod

    called = {"n": 0}

    async def spy_handle(body):
        called["n"] += 1
        raise AssertionError("handle must not run without verified payment")

    monkeypatch.setattr(x402_mod, "handle_x402_chat", spy_handle)
    with pytest.raises(HTTPException) as e:
        _run(main_mod.x402_chat_completions(_fake_req(VALID_BODY, paid=False)))
    assert e.value.status_code == 503
    assert called["n"] == 0


# H3. ROUTE FAIL-CLOSED on mainnet without CDP credentials (integration).
def test_direct_route_mainnet_fail_closed(clarity_x402_direct, monkeypatch):
    import app.main as main_mod

    s = config_mod.settings
    s.X402_NETWORK_MODE = "mainnet"
    s.X402_CDP_API_KEY_ID = ""
    s.X402_CDP_API_KEY_SECRET = ""
    try:
        # build_x402_middleware must be None -> no middleware -> no payment
        # payload -> route is fail-closed regardless of how it is reached.
        assert build_x402_middleware(facilitator_client=FakeFacilitator()) is None
        called = {"n": 0}

        async def spy_handle(body):
            called["n"] += 1

        monkeypatch.setattr(x402_mod, "handle_x402_chat", spy_handle)
        with pytest.raises(HTTPException) as e:
            _run(main_mod.x402_chat_completions(_fake_req(VALID_BODY, paid=False)))
        assert e.value.status_code == 503
        assert called["n"] == 0
    finally:
        s.X402_NETWORK_MODE = "testnet"
        s.X402_CDP_API_KEY_ID = ""
        s.X402_CDP_API_KEY_SECRET = ""


# J. TOPUP CREDIT ISOLATION: direct settlement does not credit/create key.
def test_direct_settlement_isolation(monkeypatch):
    settle_calls = []
    key_calls = []
    monkeypatch.setattr(
        "app.db.settle_x402_credit",
        lambda **kw: settle_calls.append(kw) or True,
    )
    monkeypatch.setattr(
        x402_mod, "get_or_create_payer_key",
        lambda payer: key_calls.append(payer) or {"id": "k"},
    )

    ctx = SimpleNamespace(
        payment_payload=SimpleNamespace(
            resource=SimpleNamespace(url=CHAT_ROUTE),
            payload={"authorization": {"from": "0xpayer"}},
        ),
        requirements=SimpleNamespace(network=TESTNET_CHAIN, asset=TESTNET_ASSET, amount="1000"),
        result=SimpleNamespace(transaction="0xtx", payer="0xpayer"),
    )
    x402_mod._after_settle(ctx)  # sync hook
    assert settle_calls == [], "direct settlement must not credit balance"
    assert key_calls == [], "direct settlement must not create a payer key"


# K. TOPUP PRESERVED: topup settlement still credits + idempotency holds.
def test_topup_settlement_preserved(clarity_x402_direct):
    payer = "0xpayerdirect"
    ctx = SimpleNamespace(
        payment_payload=SimpleNamespace(
            resource=SimpleNamespace(url=TOPUP_ROUTE),
            payload={"authorization": {"from": payer}},
        ),
        requirements=SimpleNamespace(network=TESTNET_CHAIN, asset=TESTNET_ASSET, amount="1000"),
        result=SimpleNamespace(transaction="0xtx1", payer=payer),
    )
    x402_mod._after_settle(ctx)
    x402_mod._after_settle(ctx)  # replay -> no double credit
    key = x402_mod.get_or_create_payer_key(payer)
    assert db_mod.balance_for(key["id"]) == 0.001


# L. TESTNET 402 direct route.
def test_direct_testnet_402(clarity_x402_direct):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r, obj = _challenge(client, CHAT_ROUTE)
    req = PaymentRequirements.model_validate(obj["accepts"][0])
    assert str(req.network) == TESTNET_CHAIN
    assert obj["extensions"][BAZAAR.key]["info"]["input"]["method"] == "POST"


# M. MAINNET 402 direct route.
def test_direct_mainnet_402(clarity_x402_direct, monkeypatch):
    s = config_mod.settings
    s.X402_NETWORK_MODE = "mainnet"
    s.X402_CHAIN_ID = MAINNET_CHAIN
    s.X402_CHAIN_INT = 8453
    s.X402_ASSET = MAINNET_ASSET
    s.X402_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
    s.X402_RPC_URL = "https://mainnet.base.org"
    s.X402_CDP_API_KEY_ID = "organizations/org/apiKeys/key"
    s.X402_CDP_API_KEY_SECRET = '{"privateKey":"FAKE_PRIVATE_KEY_FOR_TEST_ONLY"}'
    monkeypatch.setenv("CDP_API_KEY_ID", "organizations/org/apiKeys/key")
    monkeypatch.setenv("CDP_API_KEY_SECRET", '{"privateKey":"FAKE_PRIVATE_KEY_FOR_TEST_ONLY"}')
    try:
        fac = FakeFacilitator()
        client = TestClient(_make_app(fac))
        r, obj = _challenge(client, CHAT_ROUTE)
        req = PaymentRequirements.model_validate(obj["accepts"][0])
        assert str(req.network) == MAINNET_CHAIN
        assert req.amount == "1000"
        assert req.asset == MAINNET_ASSET
        assert obj["extensions"][BAZAAR.key]["info"]["input"]["method"] == "POST"
    finally:
        s.X402_NETWORK_MODE = "testnet"
        s.X402_CHAIN_ID = TESTNET_CHAIN
        s.X402_CHAIN_INT = 84532
        s.X402_ASSET = TESTNET_ASSET
        s.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        s.X402_RPC_URL = "https://sepolia.base.org"
        s.X402_CDP_API_KEY_ID = ""
        s.X402_CDP_API_KEY_SECRET = ""


# N. MAINNET FAIL CLOSED without CDP credentials.
def test_direct_mainnet_fail_closed(clarity_x402_direct):
    s = config_mod.settings
    s.X402_NETWORK_MODE = "mainnet"
    s.X402_CDP_API_KEY_ID = ""
    s.X402_CDP_API_KEY_SECRET = ""
    try:
        assert build_x402_middleware(facilitator_client=FakeFacilitator()) is None
    finally:
        s.X402_NETWORK_MODE = "testnet"
        s.X402_CDP_API_KEY_ID = ""
        s.X402_CDP_API_KEY_SECRET = ""


# Q. SECRET SCAN on generated OpenAPI (no real secrets in direct examples).
def test_direct_openapi_no_real_secrets():
    import re
    import app.main as main_mod
    from fastapi.testclient import TestClient

    o = TestClient(main_mod.app).get("/openapi.json").json()
    blob = json.dumps(o)
    assert "sk_live" not in blob
    assert "PAYMENT-SIGNATURE" not in blob
    assert not re.search(r"0x[0-9a-fA-F]{64}", blob)
    assert "gw_example_redacted" in blob  # topup example only
    assert "Bearer eyJ" not in blob
