import hashlib
import hmac
import json

import os

from fastapi import FastAPI, Header, HTTPException, Request
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
    list_keys,
    set_stripe_customer,
    usage_all,
    usage_for,
)
from .router import UpstreamError, available_models, run_completion

app = FastAPI(title="Big Pickle", version="0.1.0")

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
    _customer_key(authorization, x_api_key)
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in available_models()]}


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
    if x_admin_key != settings.ADMIN_KEY:
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
        "message": "Key created. Top up credits to enable cloud models; local models are free.",
    }


@app.get("/v1/usage")
async def customer_usage(
    authorization: str | None = Header(None),
    x_api_key: str | None = Header(None),
):
    key = _customer_key(authorization, x_api_key)
    return {**usage_for(key["id"]), "balance_usd": balance_for(key["id"])}


@app.post("/v1/credits")
async def add_credits_endpoint(
    req: CreditRequest,
    x_admin_key: str | None = Header(None),
):
    if x_admin_key != settings.ADMIN_KEY:
        raise HTTPException(401, "admin key required")
    if req.amount <= 0:
        raise HTTPException(400, "amount must be positive")
    balance = add_credits(req.key_id, req.amount)
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
        customer = session.get("customer")
        if cid and customer:
            set_stripe_customer(cid, customer)
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
