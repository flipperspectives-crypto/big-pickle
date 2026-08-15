"""Tests for the x402scan agent-discovery OpenAPI contract.

These prove that GET /openapi.json advertises ONLY POST /v1/x402/topup as a
machine-payable resource, with correct discovery metadata, while the real
runtime router (and its x402 402 behavior) is untouched. No payment is ever
signed or settled and no live inference is performed (FakeFacilitator only).
"""

import base64
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from x402 import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.mechanisms.svm.exact import ExactSvmServerScheme
from x402.schemas import PaymentRequirements
from x402.extensions.bazaar import BAZAAR

import app.config as config_mod
import app.db as db_mod
import app.main as main_mod
import app.x402 as x402_mod
from app.x402 import (
    build_topup_route,
    build_x402_middleware,
    build_solana_chat_route,
    build_solana_x402_middleware,
    x402_topup,
)

TESTNET_CHAIN = "eip155:84532"
MAINNET_CHAIN = "eip155:8453"
TESTNET_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
MAINNET_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EXPECTED_PAYTO = "0x1111111111111111111111111111111111111111"


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

    return app


@pytest.fixture
def clarity_x402_bazaar(tmp_path):
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
        "X402_PUBLIC_ORIGIN": s.X402_PUBLIC_ORIGIN,
    }
    s.DB_PATH = str(tmp_path / "openapi_x402.db")
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
    s.X402_PUBLIC_ORIGIN = "https://clarity-test.example"
    db_mod.init_db()
    yield
    for k, v in saved.items():
        setattr(s, k, v)


def _openapi():
    client = TestClient(main_mod.app)
    r = client.get("/openapi.json")
    return r, r.json()


# 1. GET /openapi.json returns 200.
def test_openapi_returns_200():
    r, _ = _openapi()
    assert r.status_code == 200


# 2. info.title == "Clarity Agent API".
def test_openapi_title():
    _, o = _openapi()
    assert o["info"]["title"] == "Clarity Agent API"


# 3. info.contact.email == onelovefuck@gmail.com
def test_openapi_contact_email():
    _, o = _openapi()
    assert o["info"]["contact"]["email"] == "onelovefuck@gmail.com"


# 4. info.x-guidance present and non-empty.
def test_openapi_guidance_present():
    _, o = _openapi()
    g = o["info"].get("x-guidance")
    assert g and isinstance(g, str) and g.strip()


# 5. paths contains exactly the payable resources. When Solana x402 is ENABLED
#    the SEPARATE Solana-devnet direct chat is advertised alongside the two Base
#    routes; when DISABLED (the production default) only the two Base routes are
#    advertised. Base routes are unchanged in either case.
def test_openapi_paths_only_three():
    s = config_mod.settings
    previous = s.X402_SOLANA_ENABLED
    try:
        s.X402_SOLANA_ENABLED = False
        _, o = _openapi()
        assert list(o["paths"].keys()) == [
            "/v1/x402/topup",
            "/v1/x402/chat/completions",
        ]
        s.X402_SOLANA_ENABLED = True
        _, o = _openapi()
        assert list(o["paths"].keys()) == [
            "/v1/x402/topup",
            "/v1/x402/chat/completions",
            "/v1/x402/solana/chat/completions",
        ]
    finally:
        s.X402_SOLANA_ENABLED = previous


# 6. that path contains exactly the intended POST discovery operation.
def test_topup_post_operation_present():
    _, o = _openapi()
    post = o["paths"]["/v1/x402/topup"].get("post")
    assert post is not None


# 6b. chat direct-pay path contains the intended POST discovery operation.
def test_chat_post_operation_present():
    _, o = _openapi()
    post = o["paths"]["/v1/x402/chat/completions"].get("post")
    assert post is not None
    assert post["operationId"] == "purchaseClarityChatCompletion"
    assert post["summary"] == "Buy a Clarity AI chat completion"


# 7. POST operation has operationId, summary, description.
def test_topup_operation_fields():
    _, o = _openapi()
    post = o["paths"]["/v1/x402/topup"]["post"]
    assert post["operationId"] == "purchaseClarityInferenceCredit"
    assert post["summary"]
    assert post["description"]


# 8. requestBody required/JSON/object/additionalProperties:false/example {}.
def test_topup_request_body():
    _, o = _openapi()
    rb = o["paths"]["/v1/x402/topup"]["post"]["requestBody"]
    assert rb["required"] is True
    js = rb["content"]["application/json"]
    schema = js["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert js["example"] == {}


# 9. x-payment-info correctness.
def test_topup_payment_info():
    _, o = _openapi()
    pi = o["paths"]["/v1/x402/topup"]["post"]["x-payment-info"]
    assert pi["price"]["mode"] == "fixed"
    assert pi["price"]["currency"] == "USD"
    assert pi["price"]["amount"] == "0.001000"
    assert pi["protocols"] == [{"x402": {}}]


# 10. responses include 200 and 402.
def test_topup_responses():
    _, o = _openapi()
    resp = o["paths"]["/v1/x402/topup"]["post"]["responses"]
    assert "200" in resp and "402" in resp


# 11. 200 schema covers the real x402_topup response fields.
def test_topup_200_schema():
    _, o = _openapi()
    schema = o["paths"]["/v1/x402/topup"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    required = set(schema["required"])
    assert required == {
        "id", "skey", "balance_usd", "credited_usd",
        "network", "scheme", "asset", "payer", "message",
    }


# 12. No real secret/token anywhere in the generated OpenAPI JSON.
def test_openapi_no_real_secrets():
    import re

    _, o = _openapi()
    blob = json.dumps(o)
    assert "sk_live" not in blob
    assert "PAYMENT-SIGNATURE" not in blob
    # The guidance references the *usage template* "Authorization: Bearer <skey>"
    # (no real token). Forbid actual bearer credentials only.
    assert "Bearer eyJ" not in blob
    assert "Bearer 0x" not in blob
    assert "Bearer sk_" not in blob
    # No 64-hex private key.
    assert not re.search(r"0x[0-9a-fA-F]{64}", blob)
    # Only the redacted example skey is allowed.
    assert "gw_example_redacted" in blob
    assert "gw_" not in blob.replace("gw_example_redacted", "")


# 12b. Direct chat OpenAPI contract.
def test_chat_openapi_contract():
    _, o = _openapi()
    post = o["paths"]["/v1/x402/chat/completions"]["post"]
    # request schema present and correctly restricted
    schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["model", "messages"]
    props = schema["properties"]
    assert props["model"]["enum"] == ["local:qwen3:1.7b"]
    assert props["max_tokens"]["maximum"] == 128
    assert props["max_tokens"]["minimum"] == 1
    assert props["stream"]["enum"] == [False]
    assert props["stream"]["default"] is False
    # responses
    assert set(post["responses"].keys()) >= {"200", "400", "402", "503"}
    # x-payment-info
    pi = post["x-payment-info"]
    assert pi["price"]["mode"] == "fixed"
    assert pi["price"]["currency"] == "USD"
    assert pi["price"]["amount"] == "0.001000"
    assert pi["protocols"] == [{"x402": {}}]
    # 200 schema covers OpenAI-compatible completion fields
    s200 = post["responses"]["200"]["content"]["application/json"]["schema"]
    assert set(s200["required"]) >= {"id", "object", "model", "choices", "usage"}


# 13. None of the unrelated routes appear in paths.
def test_unrelated_routes_hidden():
    _, o = _openapi()
    hidden = [
        "/", "/v1/models", "/v1/chat/completions", "/v1/keys", "/v1/signup",
        "/v1/capabilities", "/v1/usage", "/v1/credits", "/v1/admin/usage",
        "/health", "/v1/build", "/v1/diagnostics", "/v1/local/runtime",
        "/v1/status", "/v1/checkout", "/v1/webhooks/stripe",
    ]
    for h in hidden:
        assert h not in o["paths"]


# 14. Runtime routes still exist in the real router (not removed).
def test_runtime_routes_preserved():
    paths = {getattr(rt, "path", None) for rt in main_mod.app.routes}
    for p in ["/v1/chat/completions", "/health", "/v1/admin/usage", "/v1/webhooks/stripe", "/v1/x402/topup"]:
        assert p in paths


# 15. Existing POST /v1/x402/topup 402 behavior intact (posting {} reaches
#     the x402 middleware and yields 402, not 400/405/422).
def test_topup_402_intact(clarity_x402_bazaar):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r = client.post("/v1/x402/topup", json={})
    assert r.status_code == 402
    raw = r.headers.get("payment-required")
    assert raw
    obj = json.loads(base64.b64decode(raw))
    assert obj["accepts"][0]["payTo"] == EXPECTED_PAYTO


# 16. Testnet x402 behavior intact.
def test_testnet_behavior_intact(clarity_x402_bazaar):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r = client.post("/v1/x402/topup", json={})
    obj = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert obj["accepts"][0]["network"] == TESTNET_CHAIN


# 17. Mainnet exact/eip155:8453/1000/Base-USDC behavior intact.
def test_mainnet_behavior_intact(clarity_x402_bazaar, monkeypatch):
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
        r = client.post("/v1/x402/topup", json={})
        obj = json.loads(base64.b64decode(r.headers["payment-required"]))
        req = PaymentRequirements.model_validate(obj["accepts"][0])
        assert str(req.network) == MAINNET_CHAIN
        assert req.scheme == "exact"
        assert req.amount == "1000"
        assert req.asset == MAINNET_ASSET
        assert req.extra.get("assetTransferMethod") == "eip3009"
    finally:
        s.X402_NETWORK_MODE = "testnet"
        s.X402_CHAIN_ID = TESTNET_CHAIN
        s.X402_CHAIN_INT = 84532
        s.X402_ASSET = TESTNET_ASSET
        s.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        s.X402_RPC_URL = "https://sepolia.base.org"
        s.X402_CDP_API_KEY_ID = ""
        s.X402_CDP_API_KEY_SECRET = ""


# 18. Mainnet missing-CDP-credentials still fails closed.
def test_mainnet_fail_closed(clarity_x402_bazaar):
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


# 19. Bazaar extension present and not double-wrapped.
def test_bazaar_present_not_double(clarity_x402_bazaar):
    ext = build_topup_route().extensions
    assert set(ext.keys()) == {BAZAAR.key}
    assert BAZAAR.key not in ext[BAZAAR.key]


# 20. No test makes a real payment or live inference call.
def test_no_real_payment_or_inference(clarity_x402_bazaar):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r = client.post("/v1/x402/topup", json={})
    assert r.status_code == 402
    assert fac.settled == 0


# 21. The advertised direct-chat price must truthfully match the runtime
#     configured price and must never regress to the audit's "0.00" artifact.
#     The read-only audit observed 0.00 because AgentCash's discover tool renders
#     the price with 2-decimal precision (0.001 -> "0.00"); the source has
#     always advertised "0.001000" (decimal USD) == 1000 atomic USDC, matching
#     settings.X402_PRICE_USD and the live x402 challenge accepts[].amount="1000".
def test_chat_advertised_price_matches_runtime(clarity_x402_bazaar):
    s = config_mod.settings
    _, o = _openapi()
    chat = o["paths"]["/v1/x402/chat/completions"]["post"]
    pi = chat["x-payment-info"]

    # Forbid the 0.00 artifact and any zero/empty price.
    amount = pi["price"]["amount"]
    assert amount not in ("0.00", "0", "0.0", "")
    assert float(amount) != 0.0

    # Advertised decimal-USD amount equals the runtime configured price.
    assert float(amount) == float(s.X402_PRICE_USD)

    # 1 USDC == 1e6 atomic units; runtime challenge charges 1000 atomic for 0.001 USD.
    assert int(float(amount) * 1_000_000) == 1000

    # Network/asset/protocol metadata stays consistent with the real x402 challenge.
    assert pi["price"]["mode"] == "fixed"
    assert pi["price"]["currency"] == "USD"
    assert pi["protocols"] == [{"x402": {}}]

    # Route remains discoverable.
    assert chat["operationId"] == "purchaseClarityChatCompletion"
    assert chat["summary"] == "Buy a Clarity AI chat completion"

    # Top-up metadata is unaffected and likewise truthful.
    topup = o["paths"]["/v1/x402/topup"]["post"]["x-payment-info"]
    assert topup["price"]["amount"] == pi["price"]["amount"]
    assert topup["protocols"] == [{"x402": {}}]


# 22. The emitted PAYMENT-REQUIRED resource.url MUST be an absolute https:// URL
#     (Coinbase x402 Bazaar rejects relative resource urls). This applies to BOTH
#     paid routes, and the price/network/asset/payTo fields must be unchanged.
def test_resource_url_absolute_https_both_routes(clarity_x402_bazaar):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    expected_origin = config_mod.settings.X402_PUBLIC_ORIGIN.rstrip("/")
    for path in ("/v1/x402/chat/completions", "/v1/x402/topup"):
        r = client.post(path, json={})
        assert r.status_code == 402, (path, r.status_code, r.text)
        obj = json.loads(base64.b64decode(r.headers["payment-required"]))
        url = obj["resource"]["url"]
        assert url.startswith("https://"), url
        assert url == f"{expected_origin}{path}", url
        # Pricing/network/asset/payTo must be unchanged by the URL fix.
        a = obj["accepts"][0]
        assert a["scheme"] == "exact"
        assert a["amount"] == "1000"
        assert a["network"] == TESTNET_CHAIN
        assert a["asset"] == TESTNET_ASSET
        assert a["payTo"] == EXPECTED_PAYTO
        assert a["extra"]["assetTransferMethod"] == "eip3009"


# 23. after_settle must still credit top-up when the settled resource.url is the
#     absolute public URL (path-based matching), and must NOT credit a direct
#     chat settlement. Regresses the bug where an absolute resource.url would
#     stop top-up credit because the hook compared against the relative route.
def test_after_settle_absolute_topup_credits(monkeypatch, clarity_x402_bazaar):
    import types

    from app.x402 import _after_settle

    calls = []
    monkeypatch.setattr(
        x402_mod,
        "settle_x402_credit",
        lambda **kw: calls.append(kw) or {"id": "c1"},
    )

    class _Payload:
        resource = types.SimpleNamespace(url="https://clarity-test.example/v1/x402/topup")
        payload = {"authorization": {"from": "0xpayer"}}

    class _Req:
        network = TESTNET_CHAIN
        asset = TESTNET_ASSET
        amount = "1000"

    class _Res:
        payer = "0xpayer"
        transaction = "0xtx"

    class _Ctx:
        payment_payload = _Payload()
        requirements = _Req()
        result = _Res()

    _after_settle(_Ctx())
    assert calls, "top-up settlement with absolute resource.url must credit"
    assert calls[0]["payer"] == "0xpayer"


def test_after_settle_absolute_chat_does_not_credit(monkeypatch, clarity_x402_bazaar):
    import types

    from app.x402 import _after_settle

    calls = []
    monkeypatch.setattr(
        x402_mod,
        "settle_x402_credit",
        lambda **kw: calls.append(kw) or {"id": "c1"},
    )

    class _Payload:
        resource = types.SimpleNamespace(
            url="https://clarity-test.example/v1/x402/chat/completions"
        )
        payload = {"authorization": {"from": "0xpayer"}}

    class _Req:
        network = TESTNET_CHAIN
        asset = TESTNET_ASSET
        amount = "1000"

    class _Res:
        payer = "0xpayer"
        transaction = "0xtx"

    class _Ctx:
        payment_payload = _Payload()
        requirements = _Req()
        result = _Res()

    _after_settle(_Ctx())
    assert calls == [], "direct chat settlement must NEVER credit gateway balance"


# ---------------------------------------------------------------------------
# CLARITY SOLANA: a SEPARATE Solana-devnet x402 direct-inference path.
# These prove the new /v1/x402/solana/chat/completions route generates a
# correct Solana x402 v2 PAYMENT-REQUIRED (devnet USDC, configured payTo,
# absolute https resource.url), cannot be used for free inference, and its
# settlement can NEVER credit gateway balance, while the existing Base
# route/configuration stays unchanged. No payment is signed/settled and no
# live inference runs (FakeFacilitator only).
# ---------------------------------------------------------------------------

SOLANA_NETWORK = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
SOLANA_DEVNET_USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
SOLANA_TEST_PAYTO = "So11111111111111111111111111111111111111112"


class SolanaFakeFacilitator:
    def __init__(self):
        self.settled = 0

    async def verify(self, payload, requirements):
        from x402.schemas.hooks import ResourceVerifyResponse
        from x402.schemas.responses import VerifyResponse

        payer = (payload.payload.get("authorization") or {}).get("from", "solana-payer")
        rv = ResourceVerifyResponse(verify=VerifyResponse(is_valid=True, payer=payer))
        rv.payment_payload = payload
        rv.payment_requirements = requirements
        return rv

    async def settle(self, payload, requirements):
        from x402.schemas.responses import SettleResponse

        self.settled += 1
        return SettleResponse(
            success=True,
            transaction="solana-tx",
            network=config_mod.settings.X402_SOLANA_NETWORK,
            payer="solana-payer",
        )

    def get_supported(self):
        from x402.schemas import SupportedKind, SupportedResponse

        return SupportedResponse(
            kinds=[
                SupportedKind(
                    x402Version=2,
                    scheme="exact",
                    network=config_mod.settings.X402_SOLANA_NETWORK,
                    extra={"feePayer": "FeePayer1111111111111111111111111111111111"},
                )
            ]
        )


def make_solana_app(fac):
    server = x402ResourceServer(fac)
    server.register(config_mod.settings.X402_SOLANA_NETWORK, ExactSvmServerScheme())
    server.initialize()
    mw = build_solana_x402_middleware(
        facilitator_client=fac, server=server, sync_facilitator_on_start=False
    )
    assert mw is not None, "Solana middleware must build when enabled"
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(BaseHTTPMiddleware, dispatch=mw)

    @app.post("/v1/x402/solana/chat/completions")
    async def solana_chat(request: Request):
        if not getattr(request.state, "payment_payload", None):
            raise HTTPException(503, "solana x402 not enabled")
        raw = await request.body()
        try:
            body = json.loads(raw)
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        return await x402_mod.handle_x402_chat(body)

    return app


@pytest.fixture
def clarity_x402_solana(tmp_path):
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
        "X402_PUBLIC_ORIGIN": s.X402_PUBLIC_ORIGIN,
        "X402_SOLANA_ENABLED": s.X402_SOLANA_ENABLED,
        "X402_SOLANA_PAYTO": s.X402_SOLANA_PAYTO,
        "X402_SOLANA_NETWORK": s.X402_SOLANA_NETWORK,
        "X402_SOLANA_FACILITATOR_URL": s.X402_SOLANA_FACILITATOR_URL,
    }
    s.DB_PATH = str(tmp_path / "openapi_x402_solana.db")
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
    s.X402_PUBLIC_ORIGIN = "https://clarity-test.example"
    s.X402_SOLANA_ENABLED = True
    s.X402_SOLANA_PAYTO = SOLANA_TEST_PAYTO
    s.X402_SOLANA_NETWORK = SOLANA_NETWORK
    s.X402_SOLANA_FACILITATOR_URL = "https://x402.org/facilitator"
    db_mod.init_db()
    yield
    for k, v in saved.items():
        setattr(s, k, v)


# 24. Live discovery exposes the third (Solana devnet) payable route; Base
#     entries are unchanged.
def test_openapi_solana_discovery_present():
    s = config_mod.settings
    previous = s.X402_SOLANA_ENABLED
    try:
        # DISABLED: Solana route must NOT be advertised; Base discovery intact.
        s.X402_SOLANA_ENABLED = False
        _, o = _openapi()
        assert "/v1/x402/solana/chat/completions" not in o["paths"]
        base_post = o["paths"]["/v1/x402/chat/completions"]["post"]
        assert base_post["operationId"] == "purchaseClarityChatCompletion"

        # ENABLED: Solana route advertised with correct metadata.
        s.X402_SOLANA_ENABLED = True
        _, o = _openapi()
        sol_post = o["paths"]["/v1/x402/solana/chat/completions"]["post"]
        assert sol_post["operationId"] == "purchaseClaritySolanaDevnetChatCompletion"
        assert sol_post["summary"] == "Buy a Clarity AI chat completion (Solana devnet x402)"
        assert sol_post["x-payment-info"]["network"] == SOLANA_NETWORK
        # Base discovery is untouched.
        base_post = o["paths"]["/v1/x402/chat/completions"]["post"]
        assert base_post["operationId"] == "purchaseClarityChatCompletion"
    finally:
        s.X402_SOLANA_ENABLED = previous


# 25. Solana route generates HTTP 402 with correct x402 v2 / Solana devnet
#     payment requirements.
def test_solana_402_intact(clarity_x402_solana):
    fac = SolanaFakeFacilitator()
    client = TestClient(make_solana_app(fac))
    r = client.post("/v1/x402/solana/chat/completions", json={})
    assert r.status_code == 402, (r.status_code, r.text)
    obj = json.loads(base64.b64decode(r.headers["payment-required"]))
    assert obj["x402Version"] == 2
    a = obj["accepts"][0]
    assert a["scheme"] == "exact"
    assert a["network"] == SOLANA_NETWORK
    assert a["asset"] == SOLANA_DEVNET_USDC
    assert a["amount"] == "1000"
    assert a["payTo"] == SOLANA_TEST_PAYTO
    assert a["maxTimeoutSeconds"] == 60
    # SVM scheme injects a feePayer from the facilitator supported kind.
    assert a["extra"]["feePayer"]


# 26. Solana resource.url MUST be an absolute https:// URL (same fix as Base).
def test_solana_resource_url_absolute_https(clarity_x402_solana):
    fac = SolanaFakeFacilitator()
    client = TestClient(make_solana_app(fac))
    r = client.post("/v1/x402/solana/chat/completions", json={})
    obj = json.loads(base64.b64decode(r.headers["payment-required"]))
    url = obj["resource"]["url"]
    assert url.startswith("https://"), url
    assert url == f"https://clarity-test.example/v1/x402/solana/chat/completions", url


# 27. Bazaar metadata present on the Solana route (real SDK-generated shape:
#     info.output is {"type": "json", "example": {...}}, not "contentType").
def test_solana_bazaar_present(clarity_x402_solana):
    route = x402_mod.build_solana_chat_route()
    assert BAZAAR.key in route.extensions
    bazaar_info = route.extensions[BAZAAR.key]["info"]
    assert bazaar_info["output"]["type"] == "json"
    assert bazaar_info["input"]["method"] == "POST"


# 28. Solana route cannot be used for free inference (no payment -> 402).
def test_solana_cannot_execute_free(clarity_x402_solana):
    fac = SolanaFakeFacilitator()
    client = TestClient(make_solana_app(fac))
    r = client.post(
        "/v1/x402/solana/chat/completions",
        json={"model": "local:qwen3:1.7b", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 402
    assert fac.settled == 0


# 29. Settlement of the Solana resource can NEVER credit gateway balance.
def test_solana_after_settle_does_not_credit(monkeypatch, clarity_x402_solana):
    import types

    calls = []
    monkeypatch.setattr(
        x402_mod,
        "settle_x402_credit",
        lambda **kw: calls.append(kw) or {"id": "c1"},
    )

    class _Payload:
        resource = types.SimpleNamespace(
            url="https://clarity-test.example/v1/x402/solana/chat/completions"
        )
        payload = {"authorization": {"from": "solana-payer"}}

    class _Req:
        network = SOLANA_NETWORK
        asset = SOLANA_DEVNET_USDC
        amount = "1000"

    class _Res:
        payer = "solana-payer"
        transaction = "solana-tx"

    class _Ctx:
        payment_payload = _Payload()
        requirements = _Req()
        result = _Res()

    x402_mod._after_settle(_Ctx())
    assert calls == [], "Solana direct settlement must NEVER credit gateway balance"


# 30. Missing Solana payTo fails closed (middleware is None even when enabled).
def test_solana_missing_payto_fails_closed(clarity_x402_solana):
    s = config_mod.settings
    s.X402_SOLANA_ENABLED = True
    s.X402_SOLANA_PAYTO = ""
    try:
        assert build_solana_x402_middleware(facilitator_client=SolanaFakeFacilitator()) is None
    finally:
        s.X402_SOLANA_ENABLED = False
        s.X402_SOLANA_PAYTO = SOLANA_TEST_PAYTO


# 31. Solana mainnet is rejected (fail-closed guardrail): only the devnet CAIP-2
#     is accepted for this devnet build. The guard runs on every Settings
#     construction, so we pass the values explicitly (env is read at import).
def test_solana_mainnet_rejected():
    from app.config import Settings

    with pytest.raises(RuntimeError):
        Settings(
            X402_SOLANA_ENABLED=True,
            X402_SOLANA_PAYTO=SOLANA_TEST_PAYTO,
            X402_SOLANA_NETWORK="solana:5eykt4UsFv8P8NJdTREpY1vzqKvdp",
        )


# 31b. The Solana DEVNET CAIP-2 is explicitly accepted (proves the guard only
#      blocks mainnet, not the intended devnet build).
def test_solana_devnet_accepted():
    from app.config import Settings

    s = Settings(
        X402_SOLANA_ENABLED=True,
        X402_SOLANA_PAYTO=SOLANA_TEST_PAYTO,
        X402_SOLANA_NETWORK="solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
    )
    assert s.X402_SOLANA_NETWORK == "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"