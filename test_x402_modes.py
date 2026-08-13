"""Fail-closed Base mainnet x402 mode: configuration, fail-closed, and constants.

These tests never contact a real facilitator, never sign a payment, and never
use real credentials. They exercise the mode resolution + fail-closed logic with
an in-process fake facilitator (mirrors test_x402.py).
"""

import base64
import importlib
import json
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

import app.config as config_mod
import app.x402 as x402_mod
from x402 import x402ResourceServer
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import SupportedResponse, SupportedKind
from x402.schemas.hooks import ResourceVerifyResponse
from x402.schemas.responses import SettleResponse, VerifyResponse

USDC_SEPOLIA = "0x036CbD53842c5426634e7929541eC2318f3dCF7E"
USDC_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
CDP_FAC = "https://api.cdp.coinbase.com/platform/v2/x402"
X402_ORG = "https://x402.org/facilitator"

_ENV_KEYS = [
    "X402_NETWORK_MODE", "X402_PAYTO", "X402_PRICE_USD",
    "CDP_API_KEY_ID", "CDP_API_KEY_SECRET", "X402_ENABLED",
]


def _reload(env: dict):
    saved = {k: os.environ.pop(k, None) for k in _ENV_KEYS}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    importlib.reload(config_mod)
    importlib.reload(x402_mod)
    return saved


def _restore(saved):
    for k, v in saved.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v
    importlib.reload(config_mod)
    importlib.reload(x402_mod)


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
    mw = x402_mod.build_x402_middleware(
        facilitator_client=fac, server=server, sync_facilitator_on_start=False
    )
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(BaseHTTPMiddleware, dispatch=mw)

    @app.post("/v1/x402/topup")
    async def topup(request: Request):
        return await x402_mod.x402_topup(request)

    return app


def _decode_req(client):
    r = client.post("/v1/x402/topup", json={})
    assert r.status_code == 402, r.status_code
    raw = r.headers.get("payment-required")
    assert raw, "missing PAYMENT-REQUIRED header"
    obj = json.loads(base64.b64decode(raw))
    return r, obj


# --- default + testnet ------------------------------------------------------

def test_default_mode_is_testnet():
    saved = _reload({"X402_NETWORK_MODE": "testnet", "X402_PAYTO": "0xPAY", "X402_PRICE_USD": "0.001"})
    try:
        assert config_mod.settings.X402_NETWORK_MODE == "testnet"
        assert config_mod.settings.X402_CHAIN_ID == "eip155:84532"
        assert config_mod.settings.X402_ASSET == USDC_SEPOLIA
        assert config_mod.settings.X402_FACILITATOR_URL == X402_ORG
        assert config_mod.settings.X402_RPC_URL == "https://sepolia.base.org"
    finally:
        _restore(saved)


def test_testnet_constants_unchanged_and_work_without_cdp():
    saved = _reload({"X402_NETWORK_MODE": "testnet", "X402_PAYTO": "0xPAY", "X402_PRICE_USD": "0.001"})
    try:
        fac = FakeFacilitator()
        client = TestClient(_make_app(fac))
        r, obj = _decode_req(client)
        acc = obj["accepts"][0]
        assert acc["network"] == "eip155:84532"
        assert acc["asset"].lower() == USDC_SEPOLIA.lower()
        assert str(acc["amount"]) == "1000"
        assert config_mod.settings.X402_FACILITATOR_URL == X402_ORG
    finally:
        _restore(saved)


def test_invalid_network_mode_rejected():
    os.environ["X402_NETWORK_MODE"] = "bogus"
    with pytest.raises(RuntimeError):
        importlib.reload(config_mod)
    os.environ.pop("X402_NETWORK_MODE", None)
    importlib.reload(config_mod)
    importlib.reload(x402_mod)


# --- mainnet constants ------------------------------------------------------

def test_mainnet_constants():
    saved = _reload({"X402_NETWORK_MODE": "mainnet", "X402_PAYTO": "0xPAY", "X402_PRICE_USD": "0.001"})
    try:
        assert config_mod.settings.X402_NETWORK_MODE == "mainnet"
        assert config_mod.settings.X402_CHAIN_ID == "eip155:8453"
        assert config_mod.settings.X402_CHAIN_INT == 8453
        assert config_mod.settings.X402_ASSET == USDC_MAINNET
        assert config_mod.settings.X402_FACILITATOR_URL == CDP_FAC
        assert config_mod.settings.X402_RPC_URL == "https://mainnet.base.org"
    finally:
        _restore(saved)


def test_mainnet_challenge_uses_8453_and_mainnet_usdc_and_1000():
    saved = _reload({"X402_NETWORK_MODE": "mainnet", "X402_PAYTO": "0xPAY", "X402_PRICE_USD": "0.001"})
    os.environ["CDP_API_KEY_ID"] = "org/test/key"
    os.environ["CDP_API_KEY_SECRET"] = '{"privateKey":"dummy"}'
    importlib.reload(config_mod)
    importlib.reload(x402_mod)
    try:
        fac = FakeFacilitator()
        client = TestClient(_make_app(fac))
        r, obj = _decode_req(client)
        acc = obj["accepts"][0]
        assert acc["network"] == "eip155:8453"
        assert acc["asset"].lower() == USDC_MAINNET.lower()
        assert str(acc["amount"]) == "1000"
        assert acc["payTo"] == "0xPAY"
    finally:
        _restore(saved)


def test_mainnet_facilitator_url_exact_cdp():
    saved = _reload({"X402_NETWORK_MODE": "mainnet", "X402_PAYTO": "0xPAY"})
    try:
        assert config_mod.settings.X402_FACILITATOR_URL == CDP_FAC
    finally:
        _restore(saved)


# --- fail closed ------------------------------------------------------------

def test_mainnet_fails_closed_without_cdp_key_id():
    saved = _reload({"X402_NETWORK_MODE": "mainnet", "X402_PAYTO": "0xPAY"})
    os.environ.pop("CDP_API_KEY_ID", None)
    os.environ.pop("CDP_API_KEY_SECRET", None)
    importlib.reload(config_mod)
    importlib.reload(x402_mod)
    try:
        assert config_mod.settings.X402_FACILITATOR_URL == CDP_FAC  # no fallback to x402.org
        fac = FakeFacilitator()
        assert x402_mod.build_x402_middleware(facilitator_client=fac) is None
    finally:
        _restore(saved)


def test_mainnet_fails_closed_without_cdp_secret():
    saved = _reload({"X402_NETWORK_MODE": "mainnet", "X402_PAYTO": "0xPAY"})
    os.environ["CDP_API_KEY_ID"] = "org/test/key"
    os.environ.pop("CDP_API_KEY_SECRET", None)
    importlib.reload(config_mod)
    importlib.reload(x402_mod)
    try:
        fac = FakeFacilitator()
        assert x402_mod.build_x402_middleware(facilitator_client=fac) is None
    finally:
        _restore(saved)


def test_no_mainnet_challenge_generated_when_auth_unavailable():
    saved = _reload({"X402_NETWORK_MODE": "mainnet", "X402_PAYTO": "0xPAY"})
    os.environ.pop("CDP_API_KEY_ID", None)
    os.environ.pop("CDP_API_KEY_SECRET", None)
    importlib.reload(config_mod)
    importlib.reload(x402_mod)
    try:
        assert x402_mod.build_x402_middleware(facilitator_client=FakeFacilitator()) is None
    finally:
        _restore(saved)


def test_mainnet_enabled_with_creds_builds_middleware():
    saved = _reload({"X402_NETWORK_MODE": "mainnet", "X402_PAYTO": "0xPAY"})
    os.environ["CDP_API_KEY_ID"] = "org/test/key"
    os.environ["CDP_API_KEY_SECRET"] = '{"privateKey":"dummy"}'
    importlib.reload(config_mod)
    importlib.reload(x402_mod)
    try:
        # facilitator_client=None -> real FacilitatorConfig with CDP auth provider.
        mw = x402_mod.build_x402_middleware(facilitator_client=None)
        assert mw is not None
        assert config_mod.settings.X402_CHAIN_ID == "eip155:8453"
    finally:
        _restore(saved)


# --- secrets never leak -----------------------------------------------------

def test_secrets_not_in_challenge_response():
    saved = _reload({"X402_NETWORK_MODE": "mainnet", "X402_PAYTO": "0xPAY"})
    os.environ["CDP_API_KEY_ID"] = "org/test/secretkeyid"
    os.environ["CDP_API_KEY_SECRET"] = '{"privateKey":"dummy"}'
    importlib.reload(config_mod)
    importlib.reload(x402_mod)
    try:
        fac = FakeFacilitator()
        client = TestClient(_make_app(fac))
        r, obj = _decode_req(client)
        blob = json.dumps(obj).lower()
        assert "secretkeyid" not in blob
        assert "bearer" not in blob
        assert "cdp_api_key_secret" not in blob
        # The challenge itself never carries an Authorization header.
        assert "authorization" not in {k.lower() for k in r.headers}
    finally:
        _restore(saved)
