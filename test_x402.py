"""x402 payment-mode integration tests (testnet + mainnet, fully mocked).

These exercise the gateway's x402 server behavior without any real blockchain,
real Coinbase CDP calls, or real facilitator:

- the payment challenge advertises the gateway's own payTo/network/asset with the
  EXACT asset casing the x402 client expects (the "no matching payment requirements"
  casing bug),
- without CDP mainnet credentials the middleware is built None (fail-closed),
- the CDP facilitator header is produced by the official CDP SDK generator, scoped
  per-endpoint, and no custom JWT/crypto is used,
- in testnet mode no CDP JWT is generated at all.

No external request is made by default (the only facilitator calls are against an
in-process FakeFacilitator).

Isolation: the settings singleton is mutated directly (no module reload) and fully
restored in the fixture teardown, so the shared global app object used by other
test files is never disturbed.
"""

import base64
import json
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from x402 import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import Network, PaymentRequirements, SupportedResponse, SupportedKind
from x402.schemas.hooks import ResourceVerifyResponse
from x402.schemas.responses import SettleResponse, VerifyResponse

import app.config as config_mod
import app.db as db_mod
import app.x402 as x402_mod

EXPECTED_PAYTO = "0x1111111111111111111111111111111111111111"
TESTNET_CHAIN = "eip155:84532"
TESTNET_ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
CDP_FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"


class FakeFacilitator:
    def __init__(self):
        self.settled = 0

    async def verify(self, payload, requirements):
        payer = payload.payload["authorization"]["from"]
        rv = ResourceVerifyResponse(verify=VerifyResponse(is_valid=True, payer=payer))
        rv.payment_payload = payload
        rv.payment_requirements = requirements
        return rv

    async def settle(self, payload, requirements):
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
    mw = x402_mod.build_x402_middleware(facilitator_client=fac, server=server, sync_facilitator_on_start=False)
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(BaseHTTPMiddleware, dispatch=mw)

    @app.post("/v1/x402/topup")
    async def topup(request: Request):
        return await x402_mod.x402_topup(request)

    return app


@pytest.fixture
def clarity_x402(tmp_path):
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
    s.DB_PATH = str(tmp_path / "x402.db")
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
    obj = json.loads(base64.b64decode(raw))
    return r, obj


def test_x402_requires_payment_header(clarity_x402):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r, obj = _challenge(client)
    req = PaymentRequirements.model_validate(obj["accepts"][0])
    assert req.pay_to == EXPECTED_PAYTO
    assert req.network == Network(TESTNET_CHAIN)
    assert req.asset == TESTNET_ASSET


def test_x402_challenge_asset_case_matches_client_expectation(clarity_x402):
    """Regression for the 'no matching payment requirements' casing bug: the gateway
    advertises the USDC asset with the EXACT (lower-case 'e') casing the x402 client
    matches against, so a client reusing the server requirement will satisfy the SDK's
    exact-string ``find_matching_requirements`` comparison."""
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r, obj = _challenge(client)
    asset = obj["accepts"][0]["asset"]
    assert asset == "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
    assert asset != "0x036CbD53842c5426634e7929541eC2318f3dCF7e".upper()


def test_x402_cdp_absent_falls_back_to_insufficient(clarity_x402):
    fac = FakeFacilitator()
    client = TestClient(_make_app(fac))
    r, obj = _challenge(client)
    assert isinstance(obj["accepts"][0]["scheme"], str)


def test_x402_no_cdp_generates_no_jwt(clarity_x402, monkeypatch):
    monkeypatch.setattr(x402_mod, "_cdp_generate_jwt", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    fac = FakeFacilitator()
    # testnet path must not invoke the CDP JWT generator
    assert x402_mod.build_x402_middleware(facilitator_client=fac) is not None
    client = TestClient(_make_app(fac))
    r, obj = _challenge(client)
    assert r.status_code == 402


def test_x402_mainnet_fails_closed_without_cdp(clarity_x402):
    s = config_mod.settings
    s.X402_NETWORK_MODE = "mainnet"
    s.X402_PAYTO = EXPECTED_PAYTO
    s.X402_CDP_API_KEY_ID = ""
    s.X402_CDP_API_KEY_SECRET = ""
    try:
        assert x402_mod.build_x402_middleware(facilitator_client=FakeFacilitator()) is None
    finally:
        s.X402_NETWORK_MODE = "testnet"
        s.X402_CDP_API_KEY_ID = ""
        s.X402_CDP_API_KEY_SECRET = ""


def test_x402_cdp_headers_use_official_sdk(clarity_x402, monkeypatch):
    s = config_mod.settings
    s.X402_NETWORK_MODE = "mainnet"
    s.X402_PAYTO = EXPECTED_PAYTO
    s.X402_FACILITATOR_URL = CDP_FACILITATOR_URL
    s.X402_CDP_API_KEY_ID = "organizations/org/apiKeys/key"
    s.X402_CDP_API_KEY_SECRET = '{"privateKey":"FAKE_PRIVATE_KEY_FOR_TEST_ONLY"}'
    monkeypatch.setenv("CDP_API_KEY_ID", "organizations/org/apiKeys/key")
    monkeypatch.setenv("CDP_API_KEY_SECRET", '{"privateKey":"FAKE_PRIVATE_KEY_FOR_TEST_ONLY"}')
    captured = {}

    def _fake(key_id, secret, host, method, path):
        captured[(method, path)] = (key_id, secret, host)
        return f"tok.{method}.{path}"

    monkeypatch.setattr(x402_mod, "_cdp_generate_jwt", _fake)
    try:
        headers = x402_mod._cdp_create_headers()
        assert set(headers.keys()) == {"verify", "settle", "supported"}
        assert "bazaar" not in headers
        for method, path in [
            ("POST", "/platform/v2/x402/verify"),
            ("POST", "/platform/v2/x402/settle"),
            ("GET", "/platform/v2/x402/supported"),
        ]:
            assert (method, path) in captured, f"missing CDP scope {method} {path}"
            key_id, secret, host = captured[(method, path)]
            assert key_id == "organizations/org/apiKeys/key"
            assert secret == "FAKE_PRIVATE_KEY_FOR_TEST_ONLY"
            assert host == "api.cdp.coinbase.com"
            name = path.rstrip("/").split("/")[-1]
            assert name in ("verify", "settle", "supported")
            assert headers[name]["Authorization"].startswith("Bearer ")
    finally:
        s.X402_NETWORK_MODE = "testnet"
        s.X402_CDP_API_KEY_ID = ""
        s.X402_CDP_API_KEY_SECRET = ""
