"""Bazaar v2 discovery tests for POST /v1/x402/topup.

These verify the OFFICIAL x402 Bazaar discovery extension is attached to the
paid top-up route exactly once (``extensions.bazaar``) and that the wire
declaration accurately describes the real POST endpoint and its response shape.

No real payment is signed or settled and no external inference is performed:
the facilitator is an in-process FakeFacilitator. Mainnet challenges are produced
with FAKE CDP credentials (the facilitator client is never actually contacted).
"""

import base64
import json

import jsonschema
import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from x402 import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import Network, PaymentRequirements, SupportedResponse, SupportedKind
from x402.extensions.bazaar import BAZAAR

import app.config as config_mod
import app.db as db_mod
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
    s.DB_PATH = str(tmp_path / "x402_bazaar.db")
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


def _challenge(client):
    r = client.post("/v1/x402/topup", json={})
    assert r.status_code == 402, r.text
    raw = r.headers.get("payment-required")
    assert raw, "missing PAYMENT-REQUIRED header"
    return r, json.loads(base64.b64decode(raw))


# 1. Bazaar extension exists on the x402 route config.
def test_bazaar_extension_present_on_route_config(clarity_x402_bazaar):
    route = build_topup_route()
    assert route.extensions is not None
    assert BAZAAR.key in route.extensions


# 2. Present exactly once: extensions.bazaar, not extensions.bazaar.bazaar.
def test_bazaar_not_double_wrapped(clarity_x402_bazaar):
    ext = build_topup_route().extensions
    assert isinstance(ext, dict)
    assert set(ext.keys()) == {BAZAAR.key}
    assert BAZAAR.key not in ext[BAZAAR.key]


# 3. Bazaar info identifies method = POST (enriched from request context).
def test_bazaar_method_post(clarity_x402_bazaar):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r, obj = _challenge(client)
    bz = obj["extensions"][BAZAAR.key]
    assert bz["info"]["input"]["method"] == "POST"


# 4. Input example valid against its declared JSON schema.
def test_bazaar_input_example_valid_against_schema(clarity_x402_bazaar):
    ext = build_topup_route().extensions[BAZAAR.key]
    body_schema = ext["schema"]["properties"]["input"]["properties"]["body"]
    jsonschema.validate(instance={}, schema=body_schema)


# 5. POST body discovery metadata represents JSON body semantics.
def test_bazaar_json_body_semantics(clarity_x402_bazaar):
    ext = build_topup_route().extensions[BAZAAR.key]
    inp = ext["info"]["input"]
    assert inp["type"] == "http"
    assert inp["bodyType"] == "json"
    assert inp["body"] == {}


# 6. Output example/schema accurately represents the top-up response shape (no secrets).
def test_bazaar_output_represents_response_shape(clarity_x402_bazaar):
    ext = build_topup_route().extensions[BAZAAR.key]
    out = ext["info"]["output"]["example"]
    assert set(out.keys()) >= {"id", "skey", "balance_usd", "credited_usd", "network", "scheme"}
    assert out["skey"] == "gw_example_redacted"
    assert out["network"] == TESTNET_CHAIN
    assert out["scheme"] == "exact"
    blob = json.dumps(out)
    assert "Bearer " not in blob
    assert "0x" not in out["skey"]


# 7. Existing 402 behavior remains intact.
def test_existing_402_behavior_intact(clarity_x402_bazaar):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r, obj = _challenge(client)
    req = PaymentRequirements.model_validate(obj["accepts"][0])
    assert req.pay_to == EXPECTED_PAYTO
    assert req.network == Network(TESTNET_CHAIN)
    assert req.asset == TESTNET_ASSET


# 8. Mainnet route still advertises eip155:8453 / exact / 1000 / Base USDC / EIP-3009.
def test_mainnet_advertises_eip155_8453(clarity_x402_bazaar, monkeypatch):
    s = config_mod.settings
    s.X402_NETWORK_MODE = "mainnet"
    s.X402_PAYTO = EXPECTED_PAYTO
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
        r, obj = _challenge(client)
        req = PaymentRequirements.model_validate(obj["accepts"][0])
        assert req.network == Network(MAINNET_CHAIN)
        assert req.scheme == "exact"
        assert req.amount == "1000"
        assert req.asset == MAINNET_ASSET
        assert req.extra.get("assetTransferMethod") == "eip3009"
        # Bazaar output network tracks the live challenge.
        assert obj["extensions"][BAZAAR.key]["info"]["output"]["example"]["network"] == MAINNET_CHAIN
    finally:
        s.X402_NETWORK_MODE = "testnet"
        s.X402_CHAIN_ID = TESTNET_CHAIN
        s.X402_CHAIN_INT = 84532
        s.X402_ASSET = TESTNET_ASSET
        s.X402_FACILITATOR_URL = "https://x402.org/facilitator"
        s.X402_RPC_URL = "https://sepolia.base.org"
        s.X402_CDP_API_KEY_ID = ""
        s.X402_CDP_API_KEY_SECRET = ""


# 9. Testnet route remains unchanged.
def test_testnet_route_unchanged(clarity_x402_bazaar):
    route = build_topup_route()
    assert route.accepts.network == TESTNET_CHAIN
    assert route.accepts.scheme == "exact"


# 10. Missing CDP credentials in mainnet still fails closed.
def test_mainnet_fails_closed_without_cdp(clarity_x402_bazaar):
    s = config_mod.settings
    s.X402_NETWORK_MODE = "mainnet"
    s.X402_PAYTO = EXPECTED_PAYTO
    s.X402_CDP_API_KEY_ID = ""
    s.X402_CDP_API_KEY_SECRET = ""
    try:
        assert build_x402_middleware(facilitator_client=FakeFacilitator()) is None
    finally:
        s.X402_NETWORK_MODE = "testnet"
        s.X402_CDP_API_KEY_ID = ""
        s.X402_CDP_API_KEY_SECRET = ""


# 11. No real payment signed/settled.
def test_no_real_payment_signed_or_settled(clarity_x402_bazaar):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r, obj = _challenge(client)
    assert r.status_code == 402
    # challenge returned before any verify/settle runs
    assert fac.settled == 0


# 12. No default test performs live external inference (FakeFacilitator only).
def test_no_live_inference_by_default(clarity_x402_bazaar):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r, obj = _challenge(client)
    assert r.status_code == 402
    # facilitator is in-process; no network call was made to settle
    assert fac.settled == 0
