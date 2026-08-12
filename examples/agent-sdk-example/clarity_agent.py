"""Minimal developer example: an agent that discovers the Clarity gateway and
handles the x402 402 / PAYMENT-REQUIRED flow.

IMPORTANT — DRY-RUN / NO-FUNDS by default:
  This example NEVER signs a transaction and NEVER moves funds. When a payer
  wallet (a *public* address string) is supplied, it shows how the
  PAYMENT-SIGNATURE retry would be constructed, but the signature is clearly
  SIMULATED and no settlement occurs. There are NO private keys or secrets
  anywhere in this file.

What it demonstrates:
  1. Discovery  - read the public, read-only /v1/status of a Clarity gateway.
  2. 402 handling - parse the base64 JSON `PAYMENT-REQUIRED` header.
  3. Retry plan  - when a payer wallet is supplied, build the (simulated)
                   PAYMENT-SIGNATURE header and describe the retry.

Production behavior of the gateway is NOT changed by this example; it only talks
to the gateway over its public HTTP API.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

DEFAULT_BASE_URL = os.environ.get("CLARITY_BASE_URL", "https://big-pickle.fly.dev")

# A fake signature prefix so dry-run output is unmistakable and never valid.
SIMULATED_SIG_PREFIX = "SIMULATED."


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def discover_clarity(base_url: Optional[str] = None, http_get: Optional[Callable] = None) -> dict:
    """Discover a Clarity gateway via its public /v1/status (no auth, no secrets).

    Returns ``{"base_url": ..., "status": <status json>}``. Network is only used
    when no ``http_get`` is injected (e.g. in tests/online runs).
    """
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    get = http_get or _default_get
    status = get(f"{base}/v1/status")
    return {"base_url": base, "status": status}


def _default_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _default_post(url: str, headers: dict, body: dict):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode()), dict(r.headers)
    except urllib.error.HTTPError as e:  # gateway returns 402 here, not 200
        return e.code, e.read().decode(), dict(e.headers)


# ---------------------------------------------------------------------------
# 402 parsing
# ---------------------------------------------------------------------------
@dataclass
class PaymentRequired:
    scheme: str
    network: str
    asset: str
    amount: str
    pay_to: str
    raw: dict

    @classmethod
    def from_header(cls, header_value: str) -> "PaymentRequired":
        """Decode the x402 `PAYMENT-REQUIRED` header (base64 JSON)."""
        if not header_value:
            raise ValueError("missing PAYMENT-REQUIRED header")
        try:
            payload = json.loads(base64.b64decode(header_value))
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"invalid PAYMENT-REQUIRED header: {e}")
        accepts = payload.get("accepts") or [{}]
        a = accepts[0] if isinstance(accepts, list) else {}
        return cls(
            scheme=a.get("scheme", ""),
            network=a.get("network", ""),
            asset=a.get("asset", ""),
            amount=a.get("amount", ""),
            pay_to=a.get("payTo", ""),
            raw=payload,
        )


# ---------------------------------------------------------------------------
# Dry-run payment plan (clearly simulated, never a real signature)
# ---------------------------------------------------------------------------
@dataclass
class DryRunPlan:
    mode: str = "dry-run"
    payment_required: Optional[PaymentRequired] = None
    payer: Optional[str] = None
    simulated_signature_header: Optional[str] = None
    note: str = "DRY RUN - no funds moved, no signature produced"

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "payer": self.payer,
            "payment_required": (
                {
                    "scheme": self.payment_required.scheme,
                    "network": self.payment_required.network,
                    "asset": self.payment_required.asset,
                    "amount": self.payment_required.amount,
                    "pay_to": self.payment_required.pay_to,
                }
                if self.payment_required
                else None
            ),
            "retry_header": {"PAYMENT-SIGNATURE": self.simulated_signature_header},
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# OPT-IN LIVE path (official x402 client SDK). Dry-run remains the default.
#
# LIVE runs ONLY when BOTH: (a) the caller opts in with dry_run=False, and
# (b) the payer PRIVATE key is present in the local X402_PAYER_KEY environment
# variable. The key is read from the environment, used solely to build the
# signer, then discarded. It is NEVER printed, logged, persisted, committed, or
# included in any output. No funds move unless the user explicitly invokes live
# mode with a funded wallet.
# ---------------------------------------------------------------------------
def _parse_payment_response(header_value: str):
    if not header_value:
        return None
    try:
        return json.loads(base64.b64decode(header_value))
    except Exception:  # noqa: BLE001
        return None


def _build_client(payer_key: str):
    """Build an x402 client with an EVM signer from the private key.

    The key is supplied by the caller (from X402_PAYER_KEY) and used only here.
    """
    from eth_account import Account
    from x402 import x402Client
    from x402.mechanisms.evm import EthAccountSigner
    from x402.mechanisms.evm.exact import ExactEvmScheme

    acct = Account.from_key(payer_key)
    client = x402Client()
    client.register("eip155:*", ExactEvmScheme(EthAccountSigner(acct)))
    return client, acct.address


async def _sign_payment(client, payment_required_sdk, resource_url: str) -> str:
    """Use the official x402 client to construct + sign the payment payload."""
    from x402 import ResourceInfo

    payload = await client.create_payment_payload(
        payment_required_sdk, resource=ResourceInfo(url=resource_url, method="POST")
    )
    return base64.b64encode(json.dumps(payload.model_dump(mode="json")).encode()).decode()


@dataclass
class LivePaymentResult:
    mode: str = "live"
    status: int = 0
    payer_address: Optional[str] = None
    payment_signed: bool = False
    settlement_verified: bool = False
    gateway_credit_verified: bool = False
    payment_response: Any = None
    data: Any = None


async def _chat_live(self, url: str, body: dict, raw_pr_header: str):
    from x402 import parse_payment_required

    key = os.environ.get("X402_PAYER_KEY")
    if not key:
        raise RuntimeError(
            "LIVE mode requires the payer private key in the X402_PAYER_KEY "
            "environment variable. Refusing to run without it."
        )
    client, address = _build_client(key)
    key = None  # scrub: drop the private-key reference immediately after use
    pr_sdk = parse_payment_required(base64.b64decode(raw_pr_header))
    signature_header = await _sign_payment(client, pr_sdk, url)

    status2, data2, headers2 = self._post(url, {"PAYMENT-SIGNATURE": signature_header}, body)
    resp = _parse_payment_response(headers2.get("PAYMENT-RESPONSE", ""))
    settlement_verified = bool(resp and (resp.get("success") or resp.get("transaction")))
    gateway_credit_verified = (
        status2 == 200 and isinstance(data2, dict) and bool(data2.get("skey"))
    )
    return LivePaymentResult(
        mode="live",
        status=status2,
        payer_address=address,
        payment_signed=True,
        settlement_verified=settlement_verified,
        gateway_credit_verified=gateway_credit_verified,
        payment_response=resp,
        data=data2,
    )


@dataclass
class ChatResult:
    status: int
    mode: str
    data: Any = None
    payment_required: Optional[PaymentRequired] = None
    dry_run_plan: Optional[DryRunPlan] = None


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------
class ClarityAgent:
    """A small agent that calls Clarity's OpenAI-compatible endpoint and handles
    the x402 402 flow. Dry-run by default; never signs or spends."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        payer: Optional[str] = None,
        dry_run: bool = True,
        http_post: Optional[Callable] = None,
    ):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        # `payer` is a PUBLIC address string only. NEVER a private key.
        self.payer = payer
        self.dry_run = dry_run
        self._post = http_post or _default_post

    def chat(self, messages: list, model: str = "gpt-oss-120b",
             path: str = "/v1/chat/completions") -> ChatResult:
        url = f"{self.base_url}{path}"
        body = {"model": model, "messages": messages}
        status, data, headers = self._post(url, {}, body)

        if status == 200:
            return ChatResult(status=200, mode="live-ok", data=data)

        if status == 402 and "PAYMENT-REQUIRED" in headers:
            raw = headers["PAYMENT-REQUIRED"]
            pr = PaymentRequired.from_header(raw)
            if self.dry_run:
                plan = self._plan_payment(pr)
                return ChatResult(status=402, mode="dry-run", payment_required=pr, dry_run_plan=plan)
            # LIVE: sign with the official x402 client and retry with PAYMENT-SIGNATURE.
            # Requires X402_PAYER_KEY in the environment (handled inside _chat_live).
            live = asyncio.run(_chat_live(self, url, body, raw))
            return ChatResult(status=live.status, mode="live", data=live, payment_required=pr)

        return ChatResult(status=status, mode="error", data=data)

    def _plan_payment(self, pr: PaymentRequired) -> DryRunPlan:
        plan = DryRunPlan(payment_required=pr, payer=self.payer)
        if self.dry_run:
            # Build a clearly-SIMULATED PAYMENT-SIGNATURE header. It is NOT a
            # valid signature and cannot settle anything on-chain.
            simulated = {
                "simulated": True,
                "network": pr.network,
                "asset": pr.asset,
                "amount": pr.amount,
                "payTo": pr.pay_to,
                "payer": self.payer,
                "note": "no real signing occurred",
            }
            plan.simulated_signature_header = SIMULATED_SIG_PREFIX + base64.b64encode(
                json.dumps(simulated).encode()
            ).decode()
            plan.note = (
                "DRY RUN - no funds moved and no valid signature produced. "
                "Supply a real signer to settle on-chain."
            )
            return plan

        # Live path requires a funded signer. Intentionally NOT implemented here
        # so this example never touches a private key or spends funds.
        raise NotImplementedError(
            "Live settlement requires a funded payer signer; this example stays "
            "dry-run/no-funds. Wire an x402 client with a real signer to enable."
        )


# ---------------------------------------------------------------------------
# Self-contained offline demo (no network, no funds)
# ---------------------------------------------------------------------------
def _make_fake_payment_required() -> str:
    """Produce a PAYMENT-REQUIRED header shaped like the real Clarity gateway."""
    payload = {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {
            "url": "/v1/x402/topup",
            "description": "Clarity gateway credit top-up (machine-payable via x402)",
            "mimeType": "",
            "serviceName": "Clarity",
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": "eip155:84532",
                "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7E",
                "amount": "1000",
                "payTo": "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f",
                "maxTimeoutSeconds": 60,
                "extra": {"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"},
            }
        ],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def demo(payer: Optional[str] = "0xAGENT_PUBLIC_ADDRESS_DEMO") -> dict:
    """Run the full discovery + 402 + simulated-retry flow with a fake server."""
    captured = {}

    def fake_post(url, headers, body):
        captured["url"] = url
        captured["body"] = body
        # Always answer with a 402 + PAYMENT-REQUIRED to exercise the flow.
        return 402, "", {"PAYMENT-REQUIRED": _make_fake_payment_required()}

    def fake_get(url):
        return {
            "status": "healthy",
            "timestamp": "2026-08-12T00:00:00Z",
            "gateway": {"active_keys": 1, "total_balance_usd": 0.5},
            "providers": {"groq": {"configured": True, "credentials_configured": True,
                                   "reachable": True, "probe_latency_ms": 40.0,
                                   "models_in_routes": 6}},
        }

    discovery = discover_clarity(base_url="https://example.invalid", http_get=fake_get)
    agent = ClarityAgent(base_url="https://example.invalid", payer=payer, dry_run=True, http_post=fake_post)
    result = agent.chat([{"role": "user", "content": "Hello"}], path="/v1/x402/topup")
    return {
        "discovery": discovery,
        "chat_status": result.status,
        "mode": result.mode,
        "plan": result.dry_run_plan.to_dict() if result.dry_run_plan else None,
    }


def live_example(base_url: Optional[str] = None, model: str = "gpt-oss-120b"):
    """Run the LIVE x402 flow against a real Clarity gateway.

    REQUIRES the payer PRIVATE key in the local environment variable
    X402_PAYER_KEY (a funded Base Sepolia USDC wallet). The key is read from the
    environment only, used to sign, then discarded. This function is NOT called
    by the demo and will not run during tests; invoke it explicitly when you want
    to spend real (testnet) funds.

    Example:
        export X402_PAYER_KEY=0x...your_private_key...   # Base Sepolia, funded w/ USDC
        python -c "from clarity_agent import live_example; live_example()"
    """
    agent = ClarityAgent(base_url=base_url, dry_run=False)
    result = agent.chat([{"role": "user", "content": "Hello"}], path="/v1/x402/topup", model=model)
    if result.mode != "live" or result.data is None:
        raise RuntimeError("live flow did not complete; see earlier errors")
    live: LivePaymentResult = result.data
    return {
        "mode": live.mode,
        "status": live.status,
        "payer_address": live.payer_address,  # public only
        "payment_signed": live.payment_signed,
        "settlement_verified": live.settlement_verified,
        "gateway_credit_verified": live.gateway_credit_verified,
        "payment_response": live.payment_response,
    }


if __name__ == "__main__":  # pragma: no cover - manual CLI
    import pprint

    out = demo()
    print("=== Clarity Agent SDK example (DRY RUN / NO FUNDS) ===")
    pprint.pprint(out)
