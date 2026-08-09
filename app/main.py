import hashlib
import hmac
import json

import os

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import providers
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
from .router import UpstreamError, available_models, run_completion

app = FastAPI(title="Big Pickle", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()

_STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
if os.path.isdir(_STATIC):
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")
async def index():
    index = os.path.join(_STATIC, "index.html")
    return FileResponse(index) if os.path.exists(index) else {"name": "Big Pickle Gateway", "docs": "/docs"}


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
async def models(authorization: str | None = Header(None), x_api_key: str | None = Header(None)):
    if authorization or x_api_key:
        _customer_key(authorization, x_api_key)
    data = []
    for m in available_models():
        ps = providers.providers_for(m)
        data.append({
            "id": m,
            "object": "model",
            "local": providers.is_free_model(m),
            "providers": ps,
            "created": 0,
        })
    return {"object": "list", "data": data}


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
        raise HTTPException(e.status, e.detail)
    except Exception as e:
        raise HTTPException(502, f"gateway error: {e}")
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
        raise HTTPException(502, f"stripe checkout failed: {e}")
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
