import hashlib
import hmac
import json
import logging
import os

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import providers, x402, local, runtime
from .config import settings
from .guard import ChatRateLimiter, LocalConcurrencyLimit
from .db import (
    add_credits,
    balance_for,
    create_key,
    get_key,
    init_db,
    is_charged,
    list_keys,
    mark_charged,
    set_stripe_customer,
    usage_all,
    usage_for,
)
from .status import get_status
from .router import UpstreamError, available_models, run_completion
from .build import get_build_info, CHECKPOINT_TAG, CHECKPOINT_COMMIT

app = FastAPI(title="Clarity", version="0.1.0")

logger = logging.getLogger("clarity")


def _public_upstream_message(status: int, reason: str | None) -> str:
    # Safe, client-facing text derived ONLY from the status code and a short,
    # non-sensitive reason code. Never includes raw upstream bodies, provider
    # keys, internal hostnames, or exception text.
    if reason == "network_error":
        return "The provider could not be reached (network error). Please retry."
    if status == 401 or status == 403:
        return "The upstream provider rejected the request (auth)."
    if status == 404:
        return "That model is not available from any configured provider."
    if status == 429:
        return "The provider is rate-limiting requests right now. Please retry shortly."
    if 500 <= status < 600:
        return "The provider is temporarily unavailable. Please retry or try another model."
    return "The provider returned an error. Please retry or try another model."

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

# Perimeter guards for POST /v1/chat/completions. Instantiated once from
# settings; tests may monkeypatch these module-level instances (e.g. to shrink
# the window or disable the local slot cap) without touching production code.
_chat_rate_limiter = ChatRateLimiter(
    settings.CHAT_RATE_LIMIT_REQUESTS, settings.CHAT_RATE_LIMIT_WINDOW_SECONDS
)
_local_concurrency = LocalConcurrencyLimit(settings.LOCAL_MAX_CONCURRENCY)

# x402 machine-payable top-up (disabled unless X402_PAYTO is configured)
_x402_mw = x402.build_x402_middleware()
if _x402_mw is not None:
    app.add_middleware(BaseHTTPMiddleware, dispatch=_x402_mw)

# x402 Solana-devnet direct inference (disabled unless X402_SOLANA_ENABLED and a
# valid public Solana payTo are configured). Separate middleware + facilitator
# from the Base EVM path; disjoint routes so each passes through the other.
_x402_solana_mw = x402.build_solana_x402_middleware()
if _x402_solana_mw is not None:
    app.add_middleware(BaseHTTPMiddleware, dispatch=_x402_solana_mw)

_STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")
async def index():
    index = os.path.join(_STATIC, "index.html")
    if not os.path.exists(index):
        return {"name": "Clarity Gateway", "docs": "/docs"}
    with open(index, "r", encoding="utf-8") as f:
        html = f.read()
    # Automatic cache-busting: version the static assets by their content
    # fingerprint so a stale app.js/app.css is never served after an update.
    version = get_build_info()["asset_version"]
    html = html.replace('/static/app.css', f"/static/app.css?v={version}")
    html = html.replace('/static/app.js', f"/static/app.js?v={version}")
    # The HTML shell changes with every asset edit; never let a browser or proxy
    # cache it. Versioned static assets are cached by their URL instead.
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/agents")
async def agents_landing() -> Response:
    """Public agent-facing landing page (discovery + install). No auth, no payment."""
    page = os.path.join(_STATIC, "agents.html")
    if not os.path.exists(page):
        return Response(content="Agent guide not found", media_type="text/plain", status_code=404)
    with open(page, "r", encoding="utf-8") as f:
        html = f.read()
    version = get_build_info()["asset_version"]
    html = html.replace("/static/app.css", f"/static/app.css?v={version}")
    html = html.replace("/static/app.js", f"/static/app.js?v={version}")
    return Response(
        content=html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


@app.get("/skills.md")
async def skills_md() -> Response:
    """Machine-readable agent skill (x402 paid inference). No auth, no payment."""
    path = os.path.join(_STATIC, "skills.md")
    if not os.path.exists(path):
        return Response(content="Skill not found", media_type="text/plain", status_code=404)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


class KeyRequest(BaseModel):
    name: str


class CreditRequest(BaseModel):
    key_id: str
    amount: float


def _customer_key(authorization: str | None, x_api_key: str | None) -> dict:
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    elif x_api_key:
        token = x_api_key
    if not token:
        raise HTTPException(401, "missing API key")
    key = get_key(token)
    if not key:
        raise HTTPException(401, "invalid API key")
    return key


def _admin_key_configured() -> bool:
    # Fail closed. An empty key, or the legacy insecure literal
    # "admin-change-me", must NEVER grant admin access -- even if supplied via
    # the environment variable.
    key = settings.ADMIN_KEY
    return bool(key) and key != "admin-change-me"


def require_admin(x_admin_key: str | None) -> None:
    """Centralized, fail-closed admin authentication for every admin endpoint.

    - No usable admin key configured -> 503 (admin API intentionally disabled).
    - Configured key + exact match     -> access granted (constant-time compare).
    - Configured key + missing/wrong   -> 401.
    Never logs, prints, or returns the configured secret.
    """
    if not _admin_key_configured():
        raise HTTPException(503, "admin API unavailable: GATEWAY_ADMIN_KEY not configured")
    if not hmac.compare_digest(x_admin_key or "", settings.ADMIN_KEY):
        raise HTTPException(401, "invalid admin key")


@app.get("/v1/models")
async def models(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
    response: Response = None,
):
    # Always serve a fresh catalog: the model list changes when the local Ollama
    # install changes, and a stale browser/proxy cache would hide newly
    # discovered local models. Never expose private data here.
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if authorization or x_api_key:
        _customer_key(authorization, x_api_key)
    if request.query_params.get("refresh") == "1":
        # Zero-cost, read-only refresh: bypass ONLY the local discovery cache so
        # the next read re-queries Ollama. Uses the existing discovery lock and
        # failure backoff; never restarts/pulls Ollama or runs inference.
        local.clear_local_cache()
    local_state = await local.local_models_with_status()
    local_by_id = {m["id"]: m for m in local_state["models"]}
    cloud_ids = set(await available_models()) - set(local_by_id.keys())
    all_ids = sorted(cloud_ids | set(local_by_id.keys()))
    data = []
    for mid in all_ids:
        if mid.startswith("local:"):
            m = local_by_id.get(mid, {})
            data.append({
                "id": mid,
                "object": "model",
                "local": True,
                "providers": ["local"],
                "created": 0,
                "details": _public_details(m),
            })
        else:
            data.append({
                "id": mid,
                "object": "model",
                "local": providers.is_free_model(mid),
                "providers": providers.providers_for(mid),
                "created": 0,
            })
    return {"object": "list", "data": data, "local_status": local_state["status"]}


def _public_details(m: dict) -> dict | None:
    """Sanitized local-model metadata for the API. Only fields Ollama actually
    provided are included; absent fields are omitted (never invented)."""
    if not isinstance(m, dict):
        return None
    d: dict = {}
    if m.get("size_bytes") is not None:
        d["size_bytes"] = m["size_bytes"]
    if m.get("family"):
        d["family"] = m["family"]
    if m.get("parameter_size"):
        d["parameter_size"] = m["parameter_size"]
    if m.get("quantization_level"):
        d["quantization_level"] = m["quantization_level"]
    if m.get("context_length") is not None:
        d["context_length"] = m["context_length"]
    if m.get("capabilities"):
        d["capabilities"] = m["capabilities"]
    return d or None


async def _enforce_request_size(request: Request) -> bytes:
    """Reject oversized chat bodies (413) and return the raw body bytes.

    Reads the body INCREMENTALLY via ``request.stream()`` so an attacker cannot
    force the gateway to buffer an arbitrarily large payload when no trusted
    ``Content-Length`` is present. Reading stops the moment the accumulated size
    exceeds the limit (never retaining more than ``limit`` bytes), and the exact
    raw bytes are returned for valid requests so the caller can parse them once.
    The ``Content-Length`` header is only an EARLY-REJECT optimization, never a
    trust boundary -- the actual body bytes are always the source of truth.
    The body is never logged.
    """
    max_bytes = settings.MAX_CHAT_REQUEST_BYTES
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                raise HTTPException(413, "request payload too large")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(413, "request payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def _release_on_stream_end(stream, limiter):
    """Proxy a local-model async stream, releasing the concurrency slot on every
    exit path: normal completion, exception, cancellation, or client disconnect
    (which cancels the generator). Guarantees exactly one release per successful
    acquire; the endpoint deliberately does NOT release streaming slots itself.
    """
    try:
        async for chunk in stream:
            yield chunk
    finally:
        limiter.release()


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
):
    # Gate order: A body-size -> B auth -> C JSON/model -> D rate limit ->
    # E balance/free-model -> F local concurrency -> G execution.
    # Rejected-early requests (A/B/C) MUST NOT consume a rate-limit slot or a
    # local concurrency slot.
    raw = await _enforce_request_size(request)  # A

    key = _customer_key(authorization, x_api_key)  # B

    try:  # C
        body = json.loads(raw)
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not isinstance(body, dict) or not body.get("model"):
        raise HTTPException(400, "model is required")
    model = body["model"]

    # D. Per-key request rate limit (only reached after auth; failed auth never
    # records state). Retry-After hints when the window frees up.
    allowed, retry_after = _chat_rate_limiter.check(key["id"])
    if not allowed:
        raise HTTPException(
            429,
            "rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )

    if balance_for(key["id"]) <= 0 and not providers.is_free_model(model):  # E
        raise HTTPException(
            402,
            "insufficient balance. Top up credits via your dashboard before continuing.",
        )

    # F. Local-inference concurrency (only for local:* models). Fail fast; never
    # block and build an unbounded backlog on the laptop.
    local_slot = False
    is_stream = False
    if model.startswith("local:"):
        if not await _local_concurrency.acquire():
            raise HTTPException(503, "local model is busy; please retry shortly.")
        local_slot = True

    # G. Execute. For non-streaming responses release the slot on every exit
    # path (success, upstream error, generic error, cancellation). For streaming
    # responses the slot must stay held until the client has consumed the whole
    # stream, so it is NOT released here -- _release_on_stream_end does that.
    try:
        data, _cost, _provider = await run_completion(body, key["id"])
        is_stream = hasattr(data, "__aiter__")
    except UpstreamError as e:
        # Log ONLY sanitized metadata. Never log raw upstream bodies, provider
        # keys, Authorization headers/tokens, internal URLs/hosts, or stack traces.
        logger.warning(
            "upstream_error provider=%s status=%s exc=%s reason=%s",
            e.provider, e.status, type(e).__name__, e.reason,
        )
        raise HTTPException(e.status, _public_upstream_message(e.status, e.reason))
    except Exception as e:
        # Log the exception CLASS only (no message text, no stack trace, no secrets).
        logger.warning("completion_failed exc=%s", type(e).__name__)
        raise HTTPException(502, "Gateway error. Please retry or try another model.")
    finally:
        if local_slot and not is_stream:
            _local_concurrency.release()

    if is_stream:
        if local_slot:
            return StreamingResponse(
                _release_on_stream_end(data, _local_concurrency),
                media_type="text/event-stream",
            )
        return StreamingResponse(data, media_type="text/event-stream")
    return data


@app.post("/v1/keys")
async def create_customer_key(
    req: KeyRequest,
    x_admin_key: str | None = Header(None),
):
    require_admin(x_admin_key)
    if not req.name.strip():
        raise HTTPException(400, "name required")
    return create_key(req.name.strip())


@app.post("/v1/signup")
async def signup(req: KeyRequest):
    if not settings.PUBLIC_SIGNUP_ENABLED:
        # Fail closed: public signup is disabled until explicitly enabled.
        raise HTTPException(403, "public signup disabled")
    if not req.name.strip():
        raise HTTPException(400, "name required")
    key = create_key(req.name.strip())
    return {
        "id": key["id"],
        "skey": key["skey"],
        "balance_usd": 0.0,
        "status": "pending_topup",
        "stripe_enabled": bool(settings.STRIPE_API_KEY and settings.STRIPE_PRICE_ID),
        "message": "Key created. Top up credits to enable cloud models; local models are free.",
    }


@app.get("/v1/capabilities")
async def capabilities(response: Response = None):
    """Public, read-only, non-secret runtime capability flags for the UI.

    The frontend fetches this on load and treats a missing/unreachable
    response as signup disabled (fail closed in the UI). This endpoint only
    reports safe, intentional configuration facts -- never admin config, API
    keys, wallet details, env values, internal URLs, paths, or credentials.
    """
    if response is not None:
        # Runtime configuration facts: never let a browser/proxy cache them.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return {
        "public_signup_enabled": settings.PUBLIC_SIGNUP_ENABLED,
        "x402_enabled": bool(settings.X402_PAYTO),
    }


@app.get("/v1/usage")
async def customer_usage(
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
):
    key = _customer_key(authorization, x_api_key)
    return {
        **usage_for(key["id"]),
        "balance_usd": balance_for(key["id"]),
        "stripe_enabled": bool(settings.STRIPE_API_KEY and settings.STRIPE_PRICE_ID),
        "price_usd": settings.STRIPE_PRICE_USD,
    }


@app.post("/v1/credits")
async def add_credits_endpoint(
    req: CreditRequest,
    x_admin_key: str | None = Header(None),
):
    require_admin(x_admin_key)
    if req.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    balance = add_credits(req.key_id, req.amount)
    if balance is None:
        raise HTTPException(404, "key not found")
    return {"key_id": req.key_id, "added_usd": req.amount, "balance_usd": balance}


@app.get("/v1/admin/usage")
async def admin_usage(x_admin_key: str | None = Header(None)):
    require_admin(x_admin_key)
    keys = list_keys()
    return {"keys": keys, "usage": usage_all()}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/build")
async def build(response: Response = None):
    """Public, read-only build identity (no secrets, hosts, or git error text)."""
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return get_build_info()


@app.get("/v1/diagnostics")
async def diagnostics(response: Response = None):
    """Read-only, ZERO-INFERENCE diagnostics surface.

    Aggregates only existing evidence-backed, public surfaces: local model
    discovery (``/v1/models`` backing cache) and the local runtime (``/api/ps``)
    plus last-request telemetry. Never runs inference, never claims internet/LAN
    reachability, GPU/CPU utilization, temperatures, or production readiness, and
    never exposes prompts, responses, API keys, OLLAMA_BASE_URL, hosts, IPs,
    filesystem paths, raw Ollama payloads/errors, env vars, credentials, or
    internal URLs.
    """
    if response is not None:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    build = get_build_info()
    local_state = await local.local_models_with_status()
    rt = await runtime.get_runtime()
    models = rt.get("models") or []
    last = rt.get("last_local_request")
    return {
        "status": "ok",
        "build": {
            "current_commit": build["current_commit"],
            "checkpoint_tag": CHECKPOINT_TAG,
            "checkpoint_commit": CHECKPOINT_COMMIT,
        },
        "gateway": {"process_healthy": True},
        "local": {
            "discovery_status": local_state["status"],
            "models_discovered": len(local_state["models"]),
            "runtime_status": rt["status"],
            "models_loaded": len(models),
            "last_local_request_measured": bool(last),
        },
    }


@app.get("/v1/local/runtime")
async def local_runtime(response: Response = None):
    """Local Ollama runtime surface (zero-inference).

    Probes ``GET /api/ps`` behind an independent short-lived cache + async lock
    (separate from the ``/api/tags`` discovery cache that powers ``/v1/models``).
    Returns the running (loaded) local models with only sanitized, public fields
    plus the most recent successful non-streaming local request telemetry. Only
    public runtime facts are exposed — never OLLAMA_BASE_URL, hosts, IPs,
    filesystem paths, raw payloads, headers, credentials, or internal URLs.
    """
    if response is not None:
        # Live, per-request runtime/telemetry surface: never let a browser or
        # proxy cache it (the frontend also fetches with cache: "no-store").
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return await runtime.get_runtime()


@app.get("/v1/status")
async def status():
    """Public, read-only reliability/status surface.

    Delegates to :func:`app.status.get_status`, which serves cached zero-cost
    probe results within a short TTL (a single shared refresh under an async
    lock, so concurrent requests never launch duplicate probe storms) and reports
    clearly separated, evidence-backed facts per provider: ``configured``,
    ``credentials_configured``, ``reachable`` (live zero-cost probe),
    ``probe_latency_ms``, ``models_in_routes``, plus ``probed_at`` /
    ``probe_age_seconds``. No API keys, secrets, internal tokens, private host
    info, or raw upstream error bodies are returned; unreachable/unknown
    providers are represented honestly (``reachable: false`` or ``null`` with a
    reason). No uptime percentages or historical statistics are invented.
    """
    return await get_status()


@app.post("/v1/x402/topup")
async def x402_topup(request: Request):
    if not settings.X402_PAYTO:
        raise HTTPException(501, "x402 top-up not configured")
    return await x402.x402_topup(request)


@app.post("/v1/x402/chat/completions")
async def x402_chat_completions(request: Request):
    # Fail-closed: this route must NEVER serve free local inference. The x402
    # payment middleware verifies payment and, on success, attaches the verified
    # payment payload to request.state before calling this handler. If no
    # verified payment is present -- which is exactly what happens on mainnet
    # when build_x402_middleware() returns None (no CDP credentials) -- refuse
    # service. (The top-up route mirrors this via _payer_address as well.)
    if not x402._payer_address(request):
        raise HTTPException(503, "x402 payments are not enabled on this server")
    # Gate order mirrors chat_completions: A body-size -> B local concurrency ->
    # C handler validation + execution. The x402 middleware is the payment gate;
    # this route performs NO auth, NO gateway balance check, NO credit creation.
    # Direct local inference only (handled inside x402.handle_x402_chat).
    raw = await _enforce_request_size(request)  # A

    try:  # C (validation + execution)
        body = json.loads(raw)
    except Exception:
        raise HTTPException(400, "invalid JSON body")

    # B. Local-inference concurrency (local:* models only). Fail fast; never
    # block and build an unbounded backlog on the laptop.
    local_slot = False
    if not await _local_concurrency.acquire():
        raise HTTPException(503, "local model is busy; please retry shortly.")
    local_slot = True
    try:
        return await x402.handle_x402_chat(body)
    finally:
        if local_slot:
            _local_concurrency.release()


@app.post("/v1/x402/solana/chat/completions")
async def x402_solana_chat_completions(request: Request):
    # Fail-closed: this route must NEVER serve free local inference. The x402
    # Solana middleware verifies payment and, on success, attaches the verified
    # payment payload to request.state before calling this handler. If no
    # verified payment is present, refuse service.
    if not settings.X402_SOLANA_ENABLED:
        raise HTTPException(501, "solana x402 not configured")
    if not getattr(request.state, "payment_payload", None):
        raise HTTPException(503, "x402 payments are not enabled on this server")
    # Gate order mirrors the Base direct route: A body-size -> B local
    # concurrency -> C handler validation + execution. NO auth, NO gateway
    # balance check, NO credit creation. Reuses the same hardened direct local
    # inference logic. A Solana direct payment must NEVER credit top-up balance.
    raw = await _enforce_request_size(request)  # A
    try:  # C (validation + execution)
        body = json.loads(raw)
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    local_slot = False  # B (local inference concurrency)
    if not await _local_concurrency.acquire():
        raise HTTPException(503, "local model is busy; please retry shortly.")
    local_slot = True
    try:
        return await x402.handle_x402_chat(body)
    finally:
        if local_slot:
            _local_concurrency.release()


@app.post("/v1/checkout")
async def create_checkout(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
):
    key = _customer_key(authorization, x_api_key)
    if not settings.STRIPE_API_KEY or not settings.STRIPE_PRICE_ID:
        raise HTTPException(501, "stripe not configured")
    import stripe

    stripe.api_key = settings.STRIPE_API_KEY
    base = str(request.base_url).rstrip("/")
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": settings.STRIPE_PRICE_ID, "quantity": 1}],
            metadata={"gateway_key_id": key["id"]},
            success_url=f"{base}/?paid=1",
            cancel_url=f"{base}/",
        )
    except Exception as e:
        logger.warning("stripe_checkout_failed exc=%s", type(e).__name__)
        raise HTTPException(502, "Stripe checkout failed. Please try again later.")
    return {"url": session.url, "amount_usd": settings.STRIPE_PRICE_USD}


@app.post("/v1/webhooks/stripe")
async def stripe_webhook(request: Request):
    if not settings.STRIPE_SECRET:
        raise HTTPException(501, "stripe not configured")
    raw = await request.body()
    signature = request.headers.get("stripe-signature", "")
    if not signature:
        raise HTTPException(400, "missing stripe-signature")
    payload = raw.decode("utf-8")
    if not _verify_stripe_sig(settings.STRIPE_SECRET, payload, signature):
        raise HTTPException(400, "invalid signature")
    event = json.loads(payload)
    if event.get("type") == "checkout.session.completed":
        session = event.get("data", {}).get("object", {})
        cid = session.get("metadata", {}).get("gateway_key_id")
        sid = session.get("id")
        customer = session.get("customer")
        if cid and sid:
            if customer:
                set_stripe_customer(cid, customer)
            if not is_charged(sid):
                amount = (session.get("amount_total") or 0) / 100.0
                if amount > 0:
                    add_credits(cid, amount)
                    mark_charged(sid, cid, amount)
    return {"received": True}


def _verify_stripe_sig(secret: str, payload: str, signature: str) -> bool:
    try:
        ts_part, _, sigs = signature.partition(",")
        ts = ts_part.split("=", 1)[1]
        expected = None
        for s in sigs.split(","):
            if s.startswith("v1="):
                expected = s[3:]
        if expected is None:
            return False
        computed = hmac.new(
            secret.encode(),
            f"{ts}.{payload}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, expected)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Intentional x402scan agent-discovery contract.
#
# The runtime router above keeps every Clarity endpoint working normally. The
# public OpenAPI document below is a CURATED discovery surface: it advertises
# ONLY the machine-payable agent resource (POST /v1/x402/topup) so external
# x402 scanners catalog the real payable endpoint instead of probing unrelated
# routes and mis-flagging them as failed discovery resources. This changes
# public discovery METADATA only -- no runtime route, auth, or behavior is
# altered, and nothing is deployed or restarted here.
# ---------------------------------------------------------------------------

_X402SCAN_GUIDANCE = (
    "Clarity provides pay-per-use AI inference. The simplest one-shot machine "
    "purchase is POST /v1/x402/chat/completions: send an OpenAI-style chat body, "
    "pay the live x402 v2 402 challenge, and receive the completion directly. No "
    "Clarity API key is required for that route. Initially only the local model "
    "local:qwen3:1.7b is supported (stream=false, max_tokens <= 128). Alternatively, "
    "POST /v1/x402/topup with an empty JSON object to purchase persistent gateway "
    "credit (a secret skey); keep that skey secret and use it as Authorization: "
    "Bearer <skey> when calling POST /v1/chat/completions. Pay exactly the "
    "network, asset and amount advertised by the live challenge."
)

_TOPUP_OUTPUT_SCHEMA = {
    "type": "object",
    "required": [
        "id",
        "skey",
        "balance_usd",
        "credited_usd",
        "network",
        "scheme",
        "asset",
        "payer",
        "message",
    ],
    "properties": {
        "id": {"type": "string"},
        "skey": {"type": "string"},
        "balance_usd": {"type": "number"},
        "credited_usd": {"type": "number"},
        "network": {"type": "string"},
        "scheme": {"type": "string"},
        "asset": {"type": "string"},
        "payer": {"type": "string"},
        "message": {"type": "string"},
    },
    # Clearly fake/redacted example: no real skey, private key, CDP credential,
    # PAYMENT-SIGNATURE, or bearer token.
    "example": {
        "id": "example-key-id",
        "skey": "gw_example_redacted",
        "balance_usd": 0.001,
        "credited_usd": 0.001,
        "network": "eip155:8453",
        "scheme": "exact",
        "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "payer": "0x1111111111111111111111111111111111111111",
        "message": "Key funded after successful settlement.",
    },
}

# OpenAI-compatible completion returned by the direct paid route (safe example).
_CHAT_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["id", "object", "model", "choices", "usage"],
    "properties": {
        "id": {"type": "string"},
        "object": {"type": "string", "enum": ["chat.completion"]},
        "model": {"type": "string"},
        "choices": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "message", "finish_reason"],
                "properties": {
                    "index": {"type": "integer"},
                    "message": {
                        "type": "object",
                        "required": ["role", "content"],
                        "properties": {
                            "role": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                    "finish_reason": {"type": "string"},
                },
            },
        },
        "usage": {
            "type": "object",
            "required": ["prompt_tokens", "completion_tokens", "total_tokens"],
            "properties": {
                "prompt_tokens": {"type": "integer"},
                "completion_tokens": {"type": "integer"},
                "total_tokens": {"type": "integer"},
            },
        },
    },
    "example": {
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
    },
}

_CHAT_INPUT_SCHEMA = {
    "type": "object",
    "required": ["model", "messages"],
    "properties": {
        "model": {"type": "string", "enum": ["local:qwen3:1.7b"]},
        "messages": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
                "type": "object",
                "required": ["role", "content"],
                "properties": {
                    "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                    "content": {"type": "string"},
                },
            },
        },
        "max_tokens": {"type": "integer", "minimum": 1, "maximum": 128, "default": 128},
        "temperature": {"type": "number", "minimum": 0, "maximum": 2},
        "stream": {"type": "boolean", "enum": [False], "default": False},
    },
}


def discovery_openapi() -> dict:
    """Curated OpenAPI used for agent/x402scan discovery.

    Exposes exactly the two machine-payable resources: POST /v1/x402/topup
    (persistent gateway credit) and POST /v1/x402/chat/completions (direct
    pay-per-request inference). Decimal USD price metadata is discovery-only;
    the actual runtime charge remains the x402 atomic amount advertised by the
    live 402 challenge.
    """
    topup = {
        "operationId": "purchaseClarityInferenceCredit",
        "summary": "Purchase Clarity AI inference credit",
        "description": (
            "Machine-payable top-up. The caller makes an x402 v2 payment and, after "
            "successful settlement, receives gateway credit plus a secret skey used to "
            "authenticate inference at POST /v1/chat/completions. Send an empty JSON "
            "object; an unpaid request returns a 402 payment challenge describing the "
            "exact network, asset, and amount to pay."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "example": {},
                }
            },
        },
        "x-payment-info": {
            "price": {"mode": "fixed", "currency": "USD", "amount": "0.001000"},
            "protocols": [{"x402": {}}],
        },
        "responses": {
            "402": {"description": "Payment Required"},
            "200": {
                "description": "Successful x402 settlement and Clarity gateway credit",
                "content": {"application/json": {"schema": _TOPUP_OUTPUT_SCHEMA}},
            },
        },
    }
    chat = {
        "operationId": "purchaseClarityChatCompletion",
        "summary": "Buy a Clarity AI chat completion",
        "description": (
            "Direct pay-per-request AI inference through Clarity using "
            "local:qwen3:1.7b. Pay the live x402 challenge and receive an "
            "OpenAI-compatible chat completion directly. No Clarity API key is "
            "required. stream=false and max_tokens <= 128."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _CHAT_INPUT_SCHEMA,
                    "example": {
                        "model": "local:qwen3:1.7b",
                        "messages": [{"role": "user", "content": "Say hello in one sentence."}],
                    },
                }
            },
        },
        "x-payment-info": {
            "price": {"mode": "fixed", "currency": "USD", "amount": "0.001000"},
            "protocols": [{"x402": {}}],
        },
        "responses": {
            "400": {"description": "Invalid request (bad model, messages, tokens, or streaming)"},
            "402": {"description": "Payment Required"},
            "503": {"description": "Local inference slot unavailable"},
            "200": {
                "description": "OpenAI-compatible chat completion",
                "content": {"application/json": {"schema": _CHAT_OUTPUT_SCHEMA}},
            },
        },
    }
    solana_chat = {
        "operationId": "purchaseClaritySolanaDevnetChatCompletion",
        "summary": "Buy a Clarity AI chat completion (Solana devnet x402)",
        "description": (
            "Direct pay-per-request AI inference through Clarity using "
            "local:qwen3:1.7b, paid via Solana DEVNET x402 (TEST funds only -- "
            "NOT Solana mainnet). Pay the live x402 v2 challenge and receive an "
            "OpenAI-compatible chat completion directly. No Clarity API key is "
            "required. stream=false and max_tokens <= 128."
        ),
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": _CHAT_INPUT_SCHEMA,
                    "example": {
                        "model": "local:qwen3:1.7b",
                        "messages": [{"role": "user", "content": "Say hello in one sentence."}],
                    },
                }
            },
        },
        "x-payment-info": {
            "price": {"mode": "fixed", "currency": "USD", "amount": "0.001000"},
            "protocols": [{"x402": {}}],
            "network": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1",
        },
        "responses": {
            "400": {"description": "Invalid request (bad model, messages, tokens, or streaming)"},
            "402": {"description": "Payment Required (Solana devnet x402)"},
            "503": {"description": "Local inference slot unavailable"},
            "200": {
                "description": "OpenAI-compatible chat completion",
                "content": {"application/json": {"schema": _CHAT_OUTPUT_SCHEMA}},
            },
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Clarity Agent API",
            "version": app.version,
            "description": (
                "Clarity provides pay-per-call AI inference for agents. "
                "OpenAI-compatible. $0.001 USDC per call over x402 on Base. "
                "Agent guide: /agents. Machine-readable skill: /skills.md."
            ),
            "contact": {"email": "onelovefuck@gmail.com"},
            "x-guidance": _X402SCAN_GUIDANCE,
        },
        "paths": {
            "/v1/x402/topup": {"post": topup},
            "/v1/x402/chat/completions": {"post": chat},
            **(
                {"/v1/x402/solana/chat/completions": {"post": solana_chat}}
                if settings.X402_SOLANA_ENABLED
                else {}
            ),
        },
    }


app.openapi = discovery_openapi
