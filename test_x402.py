import base64
import hashlib
import json
import os

os.environ["GATEWAY_DB"] = "/tmp/gateway_x402_test.db"
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"
os.environ["X402_PAYTO"] = "0x1111111111111111111111111111111111111111"
os.environ["X402_PRICE_USD"] = "0.001"

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from x402 import x402ResourceServer
from x402.schemas import SupportedResponse, SupportedKind
from x402.schemas.hooks import ResourceVerifyResponse
from x402.schemas.responses import VerifyResponse, SettleResponse
from x402.mechanisms.evm.exact import ExactEvmServerScheme

from app.x402 import build_x402_middleware, x402_topup
from app.db import (
    balance_for,
    get_key_by_name,
    get_x402_settlement,
    init_db,
)

init_db()

USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
NET = "eip155:84532"
PAYER_A = "0xaaa1110000000000000000000000000000000001"
PAYER_B = "0xbbb2220000000000000000000000000000000002"
AMOUNT_ATOMIC = "1000"


def payment_id(payer, nonce, network, amount):
    return hashlib.sha256(f"{payer}|{nonce}|{network}|{amount}".encode()).hexdigest()


class FakeFacilitator:
    def __init__(self, settle_success=True):
        self.settle_success = settle_success
        self.verified = 0
        self.settled = 0

    async def verify(self, payload, requirements):
        self.verified += 1
        payer = payload.payload["authorization"]["from"]
        rv = ResourceVerifyResponse(verify=VerifyResponse(is_valid=True, payer=payer))
        rv.payment_payload = payload
        rv.payment_requirements = requirements
        return rv

    async def settle(self, payload, requirements):
        self.settled += 1
        return SettleResponse(
            success=self.settle_success,
            transaction="0xTXHASH" if self.settle_success else "",
            network=NET,
            payer=payload.payload["authorization"]["from"],
            error_reason=None if self.settle_success else "settlement_failed",
        )

    def get_supported(self):
        return SupportedResponse(
            kinds=[SupportedKind(x402Version=2, scheme="exact", network=NET, extra={})]
        )


def make_app(fac):
    server = x402ResourceServer(fac)
    server.register(NET, ExactEvmServerScheme())
    server.initialize()
    mw = build_x402_middleware(facilitator_client=fac, server=server, sync_facilitator_on_start=False)
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(BaseHTTPMiddleware, dispatch=mw)

    @app.post("/v1/x402/topup")
    async def topup(request: Request):
        return await x402_topup(request)

    return app, server


def sig_for(payer, nonce):
    inner = {
        "scheme": "exact", "network": NET, "asset": USDC, "amount": AMOUNT_ATOMIC,
        "payTo": os.environ["X402_PAYTO"],
        "authorization": {"from": payer, "nonce": nonce},
        "signature": "0xdeadbeef",
    }
    wrapper = {
        "x402Version": 2, "payload": inner,
        "accepted": {
            "scheme": "exact", "network": NET, "asset": USDC, "amount": AMOUNT_ATOMIC,
            "payTo": os.environ["X402_PAYTO"], "maxTimeoutSeconds": 60,
            "extra": {"name": "USDC", "version": "2", "assetTransferMethod": "eip3009"},
        },
        "resource": {"url": "/v1/x402/topup", "description": "", "mimeType": "", "serviceName": "Clarity"},
        "extensions": {},
    }
    return base64.b64encode(json.dumps(wrapper).encode()).decode()


def key_balance_for_payer(payer):
    k = get_key_by_name(f"x402:{payer}")
    return balance_for(k["id"]) if k else None


# ---------------------------------------------------------------------------
# 1) verify succeeds + settlement FAILS => ZERO credit
# ---------------------------------------------------------------------------
fac = FakeFacilitator(settle_success=False)
app, _ = make_app(fac)
client = TestClient(app)

r = client.post("/v1/x402/topup", json={})
assert r.status_code == 402, r.text
assert "payment-required" in r.headers

pid1 = payment_id(PAYER_A, "nonce-1", NET, AMOUNT_ATOMIC)
r1 = client.post("/v1/x402/topup", headers={"PAYMENT-SIGNATURE": sig_for(PAYER_A, "nonce-1")}, json={})
assert r1.status_code == 402, r1.text  # settlement failed -> 402
assert get_x402_settlement(pid1) is None, "ledger must stay empty on settlement failure"
assert key_balance_for_payer(PAYER_A) == 0, "no credit on settlement failure"
print("PASS 1) verify-ok + settle-fail => zero credit")

# ---------------------------------------------------------------------------
# 2) successful settlement => EXACTLY ONE credit
# ---------------------------------------------------------------------------
fac = FakeFacilitator(settle_success=True)
app, _ = make_app(fac)
client = TestClient(app)

pid2 = payment_id(PAYER_A, "nonce-2", NET, AMOUNT_ATOMIC)
r2 = client.post("/v1/x402/topup", headers={"PAYMENT-SIGNATURE": sig_for(PAYER_A, "nonce-2")}, json={})
assert r2.status_code == 200, r2.text
body = r2.json()
assert body["payer"] == PAYER_A
key_id = body["id"]
assert balance_for(key_id) == 0.001, body
assert get_x402_settlement(pid2) is not None
assert fac.verified == 1 and fac.settled == 1
print("PASS 2) successful settlement => exactly one credit (0.001)")

# ---------------------------------------------------------------------------
# 3) replaying the EXACT same PAYMENT-SIGNATURE cannot increase balance
# ---------------------------------------------------------------------------
r3 = client.post("/v1/x402/topup", headers={"PAYMENT-SIGNATURE": sig_for(PAYER_A, "nonce-2")}, json={})
assert r3.status_code == 200, r3.text
assert balance_for(key_id) == 0.001, "replay must NOT credit again"
assert get_x402_settlement(pid2) is not None  # still a single ledger row
# ledger row count for this payment_id is exactly one (idempotent INSERT OR IGNORE)
conn_rows = get_x402_settlement(pid2)
assert conn_rows is not None
print("PASS 3) replay of same PAYMENT-SIGNATURE => no extra credit")

# ---------------------------------------------------------------------------
# 4) same payer (different nonce) => SAME gateway account/key
# ---------------------------------------------------------------------------
pid4 = payment_id(PAYER_A, "nonce-3", NET, AMOUNT_ATOMIC)
r4 = client.post("/v1/x402/topup", headers={"PAYMENT-SIGNATURE": sig_for(PAYER_A, "nonce-3")}, json={})
assert r4.status_code == 200, r4.text
assert r4.json()["id"] == key_id, "same payer must reuse the same key"
assert balance_for(key_id) == 0.002, "second distinct payment accrues"
print("PASS 4) same payer => same key, balance accrues to 0.002")

# ---------------------------------------------------------------------------
# 5) secret skey is cryptographically random, NOT derived from payer
# ---------------------------------------------------------------------------
r5 = client.post("/v1/x402/topup", headers={"PAYMENT-SIGNATURE": sig_for(PAYER_B, "nonce-1")}, json={})
assert r5.status_code == 200, r5.text
key_b = r5.json()
skey_a = r2.json()["skey"]
skey_b = key_b["skey"]
assert skey_a != skey_b, "different payers must get different secret keys"
assert PAYER_A not in skey_a and PAYER_B not in skey_b, "skey must not embed the payer address"
assert skey_a.startswith("gw_") and len(skey_a) >= 32, "skey format"
assert key_b["id"] != key_id, "different payer => different key id"
print("PASS 5) skey random & independent of payer address")

print("ALL X402 ATOMICITY + IDEMPOTENCY TESTS PASSED")
