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
from x402.schemas import PaymentRequirements
from x402.extensions.bazaar import BAZAAR

import app.config as config_mod
import app.db as db_mod
import app.main as main_mod
import app.x402 as x402_mod
from app.x402 import build_topup_route, build_x402_middleware, x402_topup

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


# 5. paths contains exactly /v1/x402/topup.
def test_openapi_paths_only_topup():
    _, o = _openapi()
    assert list(o["paths"].keys()) == ["/v1/x402/topup"]


# 6. that path contains exactly the intended POST discovery operation.
def test_topup_post_operation_present():
    _, o = _openapi()
    post = o["paths"]["/v1/x402/topup"].get("post")
    assert post is not None


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
