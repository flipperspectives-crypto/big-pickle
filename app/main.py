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

# x402 machine-payable top-up (disabled unless X402_PAYTO is configured)
_x402_mw = x402.build_x402_middleware()
if _x402_mw is not None:
    app.add_middleware(BaseHTTPMiddleware, dispatch=_x402_mw)

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


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
):
    key = _customer_key(authorization, x_api_key)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")
    if not body.get("model"):
        raise HTTPException(400, "model is required")
    if balance_for(key["id"]) <= 0 and not providers.is_free_model(body.get("model", "")):
        raise HTTPException(
            402,
            "insufficient balance. Top up credits via your dashboard before continuing.",
        )
    try:
        data, _cost, _provider = await run_completion(body, key["id"])
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
    if hasattr(data, "__aiter__"):
        return StreamingResponse(data, media_type="text/event-stream")
    return data


@app.post("/v1/keys")
async def create_customer_key(
    req: KeyRequest,
    x_admin_key: str | None = Header(None),
):
    if not hmac.compare_digest(x_admin_key or "", settings.ADMIN_KEY):
        raise HTTPException(401, "admin key required")
    if not req.name.strip():
        raise HTTPException(400, "name required")
    return create_key(req.name.strip())


@app.post("/v1/signup")
async def signup(req: KeyRequest):
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
    if not hmac.compare_digest(x_admin_key or "", settings.ADMIN_KEY):
        raise HTTPException(401, "admin key required")
    if req.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    balance = add_credits(req.key_id, req.amount)
    if balance is None:
        raise HTTPException(404, "key not found")
    return {"key_id": req.key_id, "added_usd": req.amount, "balance_usd": balance}


@app.get("/v1/admin/usage")
async def admin_usage(x_admin_key: str | None = Header(None)):
    if x_admin_key != settings.ADMIN_KEY:
        raise HTTPException(401, "admin key required")
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
