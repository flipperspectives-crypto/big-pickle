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
import re
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from x402 import x402ResourceServer
from x402.http import HTTPFacilitatorClient, FacilitatorConfig
from x402.http.facilitator_client_base import CreateHeadersAuthProvider
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.extensions.bazaar import (
    declare_discovery_extension,
    bazaar_resource_server_extension,
    OutputConfig,
    BAZAAR,
)
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from .config import settings
from .db import (
    balance_for,
    create_key,
    get_key_by_name,
    settle_x402_credit,
)
from .router import UpstreamError, run_completion

log = logging.getLogger("x402")

# Route resource identifiers (must match the FastAPI route paths exactly). The
# settlement hook uses payment_payload.resource.url to tell them apart.
TOPUP_ROUTE = "/v1/x402/topup"
CHAT_ROUTE = "/v1/x402/chat/completions"
SOLANA_CHAT_ROUTE = "/v1/x402/solana/chat/completions"

# Direct-paid inference is restricted to this single local model for the MVP.
DIRECT_MODEL = "local:qwen3:1.7b"
DIRECT_MAX_TOKENS = 128
DIRECT_MAX_MESSAGES = 16
DIRECT_MAX_TEXT_CHARS = 12000


def _absolute_resource(path: str) -> str:
    """Build the absolute public HTTPS resource URL for an x402 route.

    The emitted ``PAYMENT-REQUIRED`` challenge must carry an absolute
    ``resource.url`` (``https://...``) so external validators such as the
    Coinbase x402 Bazaar accept it. We derive it from the configured canonical
    public origin (``settings.X402_PUBLIC_ORIGIN``) rather than the request
    ``Host``/``X-Forwarded-Host`` header, which a proxy or client can spoof.
    The relative ``path`` argument is still used as the internal route key and
    for matching, so only the advertised URL changes.
    """
    origin = settings.X402_PUBLIC_ORIGIN.rstrip("/")
    return f"{origin}{path}"


def _cdp_auth_available() -> bool:
    """True only when both CDP mainnet credentials are present.

    Mainnet must fail closed: no credentials => no mainnet challenge.
    """
    return bool(settings.X402_CDP_API_KEY_ID and settings.X402_CDP_API_KEY_SECRET)


def _cdp_create_headers() -> dict:
    """Build CDP Authorization headers for the x402 facilitator via the official SDK.

    Returns a dict with ``verify``, ``settle``, ``supported`` header maps, each
    ``{"Authorization": "Bearer <jwt>"}``. Each JWT is generated per-endpoint by the
    official CDP SDK generator (``cdp.auth.utils.jwt.generate_jwt``), scoped to the
    exact facilitator endpoint (method, host, path) and short-lived (120s). No custom
    JWT/crypto is implemented here; the SDK determines the algorithm, header, claims,
    and signature. Secrets are read from the environment and never logged, printed, or
    returned. Mainnet only; fail-closed otherwise (see ``build_x402_middleware``).
    """
    from urllib.parse import urlparse

    key_id = os.environ["CDP_API_KEY_ID"]
    secret = os.environ["CDP_API_KEY_SECRET"]
    # The CDP SDK expects the raw key (PEM/EC or base64/Ed25519). Tolerate the JSON
    # key-file form by extracting the privateKey field when present.
    try:
        parsed = json.loads(secret)
        if isinstance(parsed, dict) and parsed.get("privateKey"):
            secret = parsed["privateKey"]
    except Exception:
        pass

    url = urlparse(settings.X402_FACILITATOR_URL)
    host = url.netloc
    base_path = url.path.rstrip("/")

    endpoints = [
        ("verify", "POST", base_path + "/verify"),
        ("settle", "POST", base_path + "/settle"),
        ("supported", "GET", base_path + "/supported"),
    ]
    headers: dict[str, dict[str, str]] = {}
    for name, method, path in endpoints:
        token = _cdp_generate_jwt(key_id, secret, host, method, path)
        headers[name] = {"Authorization": f"Bearer {token}"}
    return headers


def _cdp_generate_jwt(key_id: str, secret: str, host: str, method: str, path: str) -> str:
    """Generate a CDP facilitator JWT via the official CDP SDK (no custom crypto)."""
    from cdp.auth.utils.jwt import JwtOptions, generate_jwt

    return generate_jwt(
        JwtOptions(
            api_key_id=key_id,
            api_key_secret=secret,
            request_method=method,
            request_host=host,
            request_path=path,
            expires_in=120,
        )
    )


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
    # Bazaar v2 discovery metadata so autonomous buyers can understand and
    # catalog this paid endpoint after a successful external CDP settlement.
    # The HTTP method is injected by bazaar_resource_server_extension at request
    # time from the live request context (see build_x402_middleware); we also
    # pre-populate it so the static declaration is schema-valid (the discovery
    # body schema requires `method`) and the build-time warning is silenced.
    bazaar_extension = declare_discovery_extension(
        input={},
        input_schema={"type": "object", "properties": {}},
        body_type="json",
        output=OutputConfig(
            example={
                "id": "example-key-id",
                "skey": "gw_example_redacted",
                "balance_usd": 0.001,
                "credited_usd": 0.001,
                "network": settings.X402_CHAIN_ID,
                "scheme": "exact",
            }
        ),
    )
    bazaar_extension[BAZAAR.key]["info"]["input"]["method"] = "POST"
    return RouteConfig(
        accepts=option,
        resource=_absolute_resource(TOPUP_ROUTE),
        description="Clarity gateway credit top-up (machine-payable via x402)",
        mime_type="application/json",
        service_name="Clarity",
        extensions=bazaar_extension,
    )


# ---------------------------------------------------------------------------
# Direct pay-per-request inference route: POST /v1/x402/chat/completions
#
# No signup, no API key, no skey handoff, no second request. The payer pays the
# live x402 challenge and the SAME request is retried with PAYMENT-SIGNATURE to
# receive the local qwen3:1.7b completion directly. This route must NEVER
# credit gateway balance, create a gateway key, or record customer usage -- it
# is purely pay-per-request (see handle_x402_chat and the _after_settle guard).
# ---------------------------------------------------------------------------

_CHAT_INPUT_SCHEMA = {
    "type": "object",
    "required": ["model", "messages"],
    "properties": {
        "model": {"type": "string", "enum": [DIRECT_MODEL]},
        "messages": {
            "type": "array",
            "minItems": 1,
            "maxItems": DIRECT_MAX_MESSAGES,
            "items": {
                "type": "object",
                "required": ["role", "content"],
                "properties": {
                    "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                    "content": {"type": "string"},
                },
            },
        },
        "max_tokens": {
            "type": "integer",
            "minimum": 1,
            "maximum": DIRECT_MAX_TOKENS,
            "default": DIRECT_MAX_TOKENS,
        },
        "temperature": {"type": "number", "minimum": 0, "maximum": 2},
        "stream": {"type": "boolean", "enum": [False], "default": False},
    },
}


def build_chat_route() -> RouteConfig:
    option = PaymentOption(
        scheme="exact",
        pay_to=settings.X402_PAYTO,
        price=settings.X402_PRICE_USD,
        network=settings.X402_CHAIN_ID,
        max_timeout_seconds=60,
        extra={"assetTransferMethod": "eip3009"},
    )
    bazaar_extension = declare_discovery_extension(
        input={
            "model": DIRECT_MODEL,
            "messages": [
                {"role": "user", "content": "Say hello in one sentence."}
            ],
        },
        input_schema=_CHAT_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(
            example={
                "id": "chatcmpl-example",
                "object": "chat.completion",
                "model": "qwen3:1.7b",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hello from Clarity."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        ),
    )
    bazaar_extension[BAZAAR.key]["info"]["input"]["method"] = "POST"
    return RouteConfig(
        accepts=option,
        resource=_absolute_resource(CHAT_ROUTE),
        description=(
            "Direct pay-per-request AI inference through Clarity using "
            f"{DIRECT_MODEL}. Pay the live x402 challenge and receive an "
            "OpenAI-compatible chat completion directly."
        ),
        mime_type="application/json",
        service_name="Clarity",
        extensions=bazaar_extension,
    )


def build_solana_chat_route() -> RouteConfig:
    """Build the Solana-devnet x402 direct-inference route.

    Uses the SVM (Solana) exact scheme. The asset (devnet USDC mint) and the
    facilitator fee payer are derived automatically by the x402 SVM scheme from
    ``X402_SOLANA_NETWORK``; ``PaymentOption`` has no explicit asset field, so we
    only supply scheme/network/payTo/price. Reuses the same hardened direct
    inference input/output schema as the Base EVM direct route.
    """
    option = PaymentOption(
        scheme="exact",
        pay_to=settings.X402_SOLANA_PAYTO,
        price=settings.X402_PRICE_USD,
        network=settings.X402_SOLANA_NETWORK,
        max_timeout_seconds=60,
        extra={},
    )
    bazaar_extension = declare_discovery_extension(
        input={
            "model": DIRECT_MODEL,
            "messages": [
                {"role": "user", "content": "Say hello in one sentence."}
            ],
        },
        input_schema=_CHAT_INPUT_SCHEMA,
        body_type="json",
        output=OutputConfig(
            example={
                "id": "chatcmpl-solana-example",
                "object": "chat.completion",
                "model": "qwen3:1.7b",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Hello from Clarity (Solana devnet).",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        ),
    )
    bazaar_extension[BAZAAR.key]["info"]["input"]["method"] = "POST"
    return RouteConfig(
        accepts=option,
        resource=_absolute_resource(SOLANA_CHAT_ROUTE),
        description=(
            "Direct pay-per-request AI inference through Clarity using "
            f"{DIRECT_MODEL}, paid via Solana DEVNET x402 (TEST funds only -- NOT "
            "Solana mainnet). Pay the live x402 v2 challenge and receive an "
            "OpenAI-compatible chat completion directly. No Clarity API key required."
        ),
        mime_type="application/json",
        service_name="Clarity",
        extensions=bazaar_extension,
    )


def build_solana_x402_middleware(
    facilitator_client=None,
    server=None,
    sync_facilitator_on_start: bool = True,
):
    """Build the Solana-devnet x402 payment middleware, or None if disabled.

    Fail-closed: returns None unless ``X402_SOLANA_ENABLED`` and a valid public
    Solana payTo are configured. The SVM server scheme is imported lazily so
    Base/EVM hosts without Solana packages keep importing and running. Uses its
    own x402.org facilitator (separate from the Base CDP/mainnet facilitator)
    because a single resource server binds to a single facilitator.
    """
    if not settings.X402_SOLANA_ENABLED:
        log.debug("X402_SOLANA_ENABLED not set; Solana route disabled")
        return None
    if not settings.X402_SOLANA_PAYTO:
        log.warning("X402_SOLANA_PAYTO not set; Solana route disabled (fail closed)")
        return None
    try:
        from x402.mechanisms.svm.exact import ExactSvmServerScheme
    except ImportError as e:
        log.warning("Solana SVM packages missing; Solana route disabled: %s", e)
        return None
    if facilitator_client is None:
        facilitator_client = HTTPFacilitatorClient(
            FacilitatorConfig(url=settings.X402_SOLANA_FACILITATOR_URL)
        )
    if server is None:
        server = x402ResourceServer(facilitator_client)
        server.register(settings.X402_SOLANA_NETWORK, ExactSvmServerScheme())
    # Register the Bazaar resource-server extension and reuse the SAME after_settle
    # guard (never credits anything but the top-up resource) for defense-in-depth.
    server.register_extension(bazaar_resource_server_extension)
    server.on_after_settle(_after_settle)
    routes = {SOLANA_CHAT_ROUTE: build_solana_chat_route()}
    return payment_middleware(
        routes, server, sync_facilitator_on_start=sync_facilitator_on_start
    )


def _safe_chat_error(status: int) -> str:
    if status == 404:
        return "That model is not available from the local provider."
    if status == 429:
        return "The model is rate-limiting requests right now. Please retry shortly."
    if 500 <= status < 600:
        return "The model backend is temporarily unavailable. Please retry."
    return "The model backend returned an error. Please retry."


async def handle_x402_chat(body: object) -> dict:
    """Validate a direct-paid chat request and run LOCAL inference only.

    Returns an OpenAI-compatible completion dict. Raises fastapi.HTTPException
    for client/backend errors. Never creates a gateway key, never credits
    balance, and never records customer usage (run_completion(record_usage=False)).
    """
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be a JSON object")
    model = body.get("model")
    if model != DIRECT_MODEL:
        raise HTTPException(400, f"only {DIRECT_MODEL} is supported on this route")
    messages = body.get("messages")
    if not isinstance(messages, list) or not (1 <= len(messages) <= DIRECT_MAX_MESSAGES):
        raise HTTPException(400, "messages must be an array of 1..16 items")
    total_text = 0
    allowed_roles = ("system", "user", "assistant")
    for m in messages:
        if not isinstance(m, dict):
            raise HTTPException(400, "each message must be an object")
        if m.get("role") not in allowed_roles:
            raise HTTPException(400, "message role must be one of system/user/assistant")
        content = m.get("content")
        if not isinstance(content, str) or content == "":
            raise HTTPException(400, "message content must be a non-empty string")
        total_text += len(content)
    if total_text > DIRECT_MAX_TEXT_CHARS:
        raise HTTPException(400, "total message text exceeds 12000 characters")

    stream = body.get("stream", False)
    if stream is True:
        raise HTTPException(400, "streaming is not supported on this route")

    max_tokens = body.get("max_tokens", DIRECT_MAX_TOKENS)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not (
        1 <= max_tokens <= DIRECT_MAX_TOKENS
    ):
        raise HTTPException(400, f"max_tokens must be an integer between 1 and {DIRECT_MAX_TOKENS}")

    temperature = body.get("temperature")
    if temperature is not None:
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not (
            0 <= temperature <= 2
        ):
            raise HTTPException(400, "temperature must be a number between 0 and 2")

    # Build the execution body. qwen3 reasoning is disabled to minimize hidden
    # thinking latency for this direct route. The local OpenAI-compatible path
    # forwards `think` straight through to Ollama.
    exec_body = {
        "model": DIRECT_MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "think": False,
    }
    if temperature is not None:
        exec_body["temperature"] = temperature

    try:
        data, _cost, _provider = await run_completion(exec_body, key_id=None, record_usage_flag=False)
    except UpstreamError as e:
        raise HTTPException(e.status, _safe_chat_error(e.status))
    return data


def _payment_id(payer: str, inner: dict, network: str, amount: str) -> str:
    auth = inner.get("authorization") or {}
    nonce = auth.get("nonce") or json.dumps(inner, sort_keys=True)
    return hashlib.sha256(
        f"{payer}|{nonce}|{network}|{amount}".encode()
    ).hexdigest()


def _after_settle(context) -> None:
    """Credit the gateway key ONLY after a successful TOP-UP settlement.

    Runs exclusively on settlement success (the x402 middleware never invokes
    after_settle on settlement failure). The ledger makes the credit idempotent:
    replaying the exact same payment reuses the same payment_id and is skipped.

    Route isolation: only the top-up resource (POST /v1/x402/topup) credits
    gateway balance and creates a payer key. The direct inference resource
    (POST /v1/x402/chat/completions) is pay-per-request and must NEVER credit
    balance, create a key, or record usage. The settled resource identity is the
    SDK-supported ``payment_payload.resource.url`` (the route the 402 challenge
    was issued for), not a payer/amount/timing heuristic.
    """
    payload = getattr(context, "payment_payload", None)
    requirements = getattr(context, "requirements", None)
    result = getattr(context, "result", None)
    if payload is None or requirements is None or result is None:
        return

    resource = getattr(getattr(payload, "resource", None), "url", None)
    # Match on the URL path so this holds whether the advertised resource.url is
    # the relative route or the configured absolute public origin URL.
    resource_path = (urlparse(resource or "").path or "").rstrip("/")
    if resource_path != TOPUP_ROUTE:
        # Direct inference settlement (or any non-topup resource): no credit.
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
    # Register the Bazaar resource-server extension so the discovery declaration
    # is enriched with the live HTTP method (POST) for facilitator cataloging.
    server.register_extension(bazaar_resource_server_extension)
    server.on_after_settle(_after_settle)
    routes = {
        TOPUP_ROUTE: build_topup_route(),
        CHAT_ROUTE: build_chat_route(),
    }
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
