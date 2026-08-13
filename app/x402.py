"""x402 v2 machine-payable top-up for Clarity.

Implements a protected ``POST /v1/x402/topup`` endpoint using the official
``x402`` Python SDK (``x402[fastapi,evm]``). Flow (x402 v2):

1. Client calls ``/v1/x402/topup`` with no payment -> 402 + ``PAYMENT-REQUIRED``
   header (base64 JSON: x402Version, resource, accepts[{scheme:"exact",
   network:"eip155:84532", asset:USDC, amount, payTo, maxTimeoutSeconds,
   extra:{name:"USDC", version:"2", assetTransferMethod:"eip3009"}}]).
2. Payer signs an EIP-3009 ``transferWithAuthorization`` (USDC) and retries with
   the ``PAYMENT-SIGNATURE`` header.
3. The x402 FastAPI middleware verifies AND settles via the official x402.org
   test facilitator on Base Sepolia.
4. Only AFTER a successful settlement does the ``after_settle`` hook credit a
   gateway key (one deterministic key per payer address). The credit is recorded
   in a persistent settlement ledger keyed by a payment identifier, so a payment
   can never credit the balance twice (even on replay).

Atomicity: the gateway balance is credited ONLY inside the post-settlement
success hook, never in the request handler. If verification succeeds but
settlement fails, the handler still runs (creating an unfunded key) but the
response is discarded (402) and no credit is recorded.
"""

import hashlib
import json
import logging
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient, FacilitatorConfig
from x402.http.facilitator_client_base import CreateHeadersAuthProvider
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from .config import settings
from .db import (
    balance_for,
    create_key,
    get_key_by_name,
    settle_x402_credit,
)

log = logging.getLogger("x402")


def _cdp_auth_available() -> bool:
    """True only when both CDP mainnet credentials are present.

    Mainnet must fail closed: no credentials => no mainnet challenge.
    """
    return bool(settings.X402_CDP_API_KEY_ID and settings.X402_CDP_API_KEY_SECRET)


def _cdp_create_headers() -> dict:
    """Build per-endpoint CDP Authorization headers (short-lived JWT Bearer).

    Uses the official x402 ``CreateHeadersAuthProvider`` contract: a callable that
    returns ``{verify, settle, supported, bazaar: {Authorization: "Bearer <jwt>"}}``.
    The JWT is the documented CDP API-key auth (RS256/EC, 120s expiry). Secrets are
    read from the environment and never logged, printed, or returned.
    """
    import base64
    import time

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key_id = os.environ["CDP_API_KEY_ID"]
    secret_raw = os.environ["CDP_API_KEY_SECRET"]
    try:
        secret_json = json.loads(secret_raw)
        pem = secret_json["privateKey"]
    except Exception:
        pem = secret_raw  # tolerate a raw PEM secret
    priv = load_pem_private_key(pem.encode(), password=None)

    base = settings.X402_FACILITATOR_URL.rstrip("/")
    endpoints = {
        "verify": f"POST {base}/verify",
        "settle": f"POST {base}/settle",
        "supported": f"GET {base}/supported",
        "bazaar": f"POST {base}/bazaar",
    }

    def _b64(d: bytes) -> bytes:
        return base64.urlsafe_b64encode(d).rstrip(b"=")

    def _make_jwt(uri: str) -> str:
        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iss": "cdp",
            "sub": key_id,
            "iat": now,
            "nbf": now,
            "exp": now + 120,
            "uri": uri,
        }
        signing_input = (
            _b64(json.dumps(header, separators=(",", ":")).encode())
            + b"."
            + _b64(json.dumps(payload, separators=(",", ":")).encode())
        )
        if isinstance(priv, ec.EllipticCurvePrivateKey):
            sig = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        else:  # RS256
            sig = priv.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return (b64 := _b64(sig)).decode()

    return {ep: {"Authorization": f"Bearer {_make_jwt(uri)}"} for ep, uri in endpoints.items()}


def _payer_address(request: Request) -> str:
    pp = getattr(request.state, "payment_payload", None)
    if not pp:
        return ""
    inner = getattr(pp, "payload", None) or {}
    return (inner.get("authorization") or {}).get("from", "").lower()


def get_or_create_payer_key(payer: str) -> dict:
    """Return a stable gateway key for a payer (one key per payer address)."""
    name = f"x402:{payer}"
    existing = get_key_by_name(name)
    if existing:
        return existing
    return create_key(name)


def build_topup_route() -> RouteConfig:
    option = PaymentOption(
        scheme="exact",
        pay_to=settings.X402_PAYTO,
        price=settings.X402_PRICE_USD,
        network=settings.X402_CHAIN_ID,
        max_timeout_seconds=60,
        extra={"assetTransferMethod": "eip3009"},
    )
    return RouteConfig(
        accepts=option,
        resource="/v1/x402/topup",
        description="Clarity gateway credit top-up (machine-payable via x402)",
        mime_type="application/json",
        service_name="Clarity",
    )


def _payment_id(payer: str, inner: dict, network: str, amount: str) -> str:
    auth = inner.get("authorization") or {}
    nonce = auth.get("nonce") or json.dumps(inner, sort_keys=True)
    return hashlib.sha256(
        f"{payer}|{nonce}|{network}|{amount}".encode()
    ).hexdigest()


def _after_settle(context) -> None:
    """Credit the gateway key ONLY after a successful settlement.

    Runs exclusively on settlement success (the x402 middleware never invokes
    after_settle on settlement failure). The ledger makes the credit idempotent:
    replaying the exact same payment reuses the same payment_id and is skipped.
    """
    payload = getattr(context, "payment_payload", None)
    requirements = getattr(context, "requirements", None)
    result = getattr(context, "result", None)
    if payload is None or requirements is None or result is None:
        return

    inner = getattr(payload, "payload", None) or {}
    auth = inner.get("authorization") or {}
    payer = (auth.get("from") or getattr(result, "payer", None) or "").lower()
    if not payer:
        log.warning("x402 after_settle: missing payer; skipping credit")
        return

    network = getattr(requirements, "network", settings.X402_CHAIN_ID)
    asset = getattr(requirements, "asset", settings.X402_ASSET)
    amount_atomic = getattr(requirements, "amount", "0")
    try:
        amount_usd = int(amount_atomic) / 1_000_000
    except (ValueError, TypeError):
        amount_usd = float(settings.X402_PRICE_USD)

    payment_id = _payment_id(payer, inner, network, amount_atomic)
    key = get_or_create_payer_key(payer)
    credited = settle_x402_credit(
        payment_id=payment_id,
        payer=payer,
        transaction=getattr(result, "transaction", None),
        network=network,
        asset=asset,
        amount_usd=amount_usd,
        key_id=key["id"],
    )
    if credited:
        log.info(
            "x402 settled %s for payer %s -> key %s (+$%.4f)",
            getattr(result, "transaction", "?"),
            payer,
            key["id"],
            amount_usd,
        )
    else:
        log.info(
            "x402 settlement already recorded (replay/dup) payment_id=%s payer=%s",
            payment_id[:16],
            payer,
        )


def build_x402_middleware(
    facilitator_client=None,
    server=None,
    sync_facilitator_on_start: bool = True,
):
    """Build the x402 payment middleware, or None if x402 is not configured."""
    if not settings.X402_PAYTO:
        log.warning("X402_PAYTO not set; /v1/x402/topup is disabled")
        return None
    if settings.X402_NETWORK_MODE == "mainnet":
        # Fail closed: never advertise a mainnet challenge without credentials,
        # and never silently fall back to testnet / x402.org.
        if not _cdp_auth_available():
            log.warning("mainnet facilitator unavailable: credentials/auth not configured")
            return None
        auth_provider = CreateHeadersAuthProvider(create_headers=_cdp_create_headers)
        if facilitator_client is None:
            facilitator_client = HTTPFacilitatorClient(
                FacilitatorConfig(url=settings.X402_FACILITATOR_URL, auth_provider=auth_provider)
            )
    else:
        if facilitator_client is None:
            facilitator_client = HTTPFacilitatorClient(
                FacilitatorConfig(url=settings.X402_FACILITATOR_URL)
            )
    if server is None:
        server = x402ResourceServer(facilitator_client)
        server.register(settings.X402_CHAIN_ID, ExactEvmServerScheme())
    server.on_after_settle(_after_settle)
    routes = {"/v1/x402/topup": build_topup_route()}
    return payment_middleware(
        routes, server, sync_facilitator_on_start=sync_facilitator_on_start
    )


async def x402_topup(request: Request):
    payer = _payer_address(request)
    if not payer:
        return JSONResponse(status_code=400, content={"error": "missing payer in payment"})
    key = get_or_create_payer_key(payer)
    amount = float(settings.X402_PRICE_USD)
    return {
        "id": key["id"],
        "skey": key["skey"],
        "balance_usd": balance_for(key["id"]),
        "credited_usd": amount,
        "network": settings.X402_CHAIN_ID,
        "scheme": "exact",
        "asset": settings.X402_ASSET,
        "payer": payer,
        "message": "Key funded after successful settlement; verify balance via /v1/usage.",
    }
