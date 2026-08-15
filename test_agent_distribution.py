"""Tests for the agent-distribution layer (Step 1).

Proves the new GET /agents and GET /skills.md surfaces exist and are correct,
the canonical paid endpoint remains POST, the price stays $0.001, the network
stays Base mainnet eip155:8453, no secrets are introduced, and the EXISTING x402
constants/behavior are untouched.
"""

import os
import re

import pytest
from fastapi.testclient import TestClient

import app.config as config_mod
import app.main as main_mod


SETUP_PROMPT = (
    "Install the Clarity skill from https://desktop-o99r0sf.tail935fba.ts.net/skills.md. "
    "Use Clarity when I need low-cost OpenAI-compatible inference. "
    "Discover the payment requirement first and confirm with me before making the first paid call "
    "unless I have already authorized a budget. Never expose private keys or wallet secrets."
)


@pytest.fixture
def client():
    return TestClient(main_mod.app)


# --- new surfaces ---------------------------------------------------------

def test_agents_page_200(client):
    r = client.get("/agents")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "Pay-per-call AI inference" in body
    assert "$0.001" in body
    assert "Base" in body
    assert "x402" in body
    assert SETUP_PROMPT in body


def test_skills_md_200(client):
    r = client.get("/skills.md")
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    body = r.text.lower()
    for token in (
        "local:qwen3:1.7b",
        "/v1/x402/chat/completions",
        "/openapi.json",
        "eip155:8453",
        "usdc",
        "$0.001",
        "post",
        "402",
        "payment-required",
        "payment-signature",
        "private keys",
        "spending limit",
        "confirm",
        "dry-run",
        "settlement",
        "schema",
        "network",
    ):
        assert token in body, f"skills.md missing token: {token}"


# --- unchanged canonical contract -----------------------------------------

def test_canonical_endpoint_is_post(client):
    o = client.get("/openapi.json").json()
    chat = o["paths"]["/v1/x402/chat/completions"]
    assert "post" in chat
    assert chat["post"]["operationId"] == "purchaseClarityChatCompletion"


def test_price_unchanged(client):
    # Discovery metadata price untouched.
    o = client.get("/openapi.json").json()
    amount = o["paths"]["/v1/x402/chat/completions"]["post"]["x-payment-info"]["price"]["amount"]
    assert amount == "0.001000"
    assert float(amount) == 0.001
    # Runtime config price untouched.
    assert config_mod.settings.X402_PRICE_USD == "0.001"


def test_network_unchanged(client):
    # Mainnet CAIP-2 mapping stays eip155:8453 and is not a new/extra chain.
    assert config_mod.Settings._X402_MAINNET["chain_id"] == "eip155:8453"
    # Testnet mapping stays the unchanged Base-Sepolia id (no regression).
    assert config_mod.Settings._X402_TESTNET["chain_id"] == "eip155:84532"
    # skills.md advertises only Base mainnet (no second blockchain introduced).
    body = client.get("/skills.md").text.lower()
    assert "eip155:8453" in body
    assert "solana" not in body


# --- no secrets introduced ------------------------------------------------

def test_no_secrets_in_new_routes(client):
    # crude secret patterns: 64-hex private key, sk- keys, PEM private keys,
    # inline api key assignments.
    bad = re.compile(
        r"(0x[a-fA-F0-9]{64})"
        r"|(sk-[A-Za-z0-9]{20,})"
        r"|(BEGIN [A-Z ]*PRIVATE KEY)"
        r"|(api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,})",
        re.I,
    )
    for path in ("/agents", "/skills.md"):
        body = client.get(path).text
        assert not bad.search(body), f"possible secret in {path}"


def test_no_secrets_in_new_files():
    base = os.path.join(os.path.dirname(main_mod.__file__), os.pardir, "static")
    files = ("skills.md", "agents.html")
    bad = re.compile(
        r"(0x[a-fA-F0-9]{64})"
        r"|(sk-[A-Za-z0-9]{20,})"
        r"|(BEGIN [A-Z ]*PRIVATE KEY)"
        r"|(api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,})",
        re.I,
    )
    for name in files:
        with open(os.path.join(base, name), "r", encoding="utf-8") as f:
            content = f.read()
        assert not bad.search(content), f"possible secret in static/{name}"


# --- Solana x402 route visibility in discovery (Step 1 fix) ----------------

SOLANA_ROUTE = "/v1/x402/solana/chat/completions"


def test_solana_route_absent_when_disabled(client):
    previous = main_mod.settings.X402_SOLANA_ENABLED
    main_mod.settings.X402_SOLANA_ENABLED = False
    try:
        paths = client.get("/openapi.json").json()["paths"]
    finally:
        main_mod.settings.X402_SOLANA_ENABLED = previous
    assert SOLANA_ROUTE not in paths


def test_solana_route_present_when_enabled(client):
    previous = main_mod.settings.X402_SOLANA_ENABLED
    main_mod.settings.X402_SOLANA_ENABLED = True
    try:
        paths = client.get("/openapi.json").json()["paths"]
    finally:
        main_mod.settings.X402_SOLANA_ENABLED = previous
    assert SOLANA_ROUTE in paths
    assert "post" in paths[SOLANA_ROUTE]


def test_base_chat_route_present_in_discovery(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/x402/chat/completions" in paths
    assert "post" in paths["/v1/x402/chat/completions"]
    # The canonical Base paid route must remain advertised regardless of Solana flag.
    previous = main_mod.settings.X402_SOLANA_ENABLED
    main_mod.settings.X402_SOLANA_ENABLED = False
    try:
        paths2 = client.get("/openapi.json").json()["paths"]
    finally:
        main_mod.settings.X402_SOLANA_ENABLED = previous
    assert "/v1/x402/chat/completions" in paths2
    assert "post" in paths2["/v1/x402/chat/completions"]


def test_price_unchanged_after_solana_fix(client):
    # Discovery price must remain exactly $0.001 on the Base chat route.
    chat = client.get("/openapi.json").json()["paths"]["/v1/x402/chat/completions"]["post"]
    amount = chat["x-payment-info"]["price"]["amount"]
    assert amount == "0.001000"
    assert float(amount) == 0.001
    assert config_mod.settings.X402_PRICE_USD == "0.001"
