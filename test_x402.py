import base64
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
from app.db import balance_for, get_key_by_name, init_db

init_db()

USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
NET = "eip155:84532"
PAYER = "0xabc1230000000000000000000000000000000001"


class FakeFacilitator:
    def __init__(self):
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
            success=True,
            transaction="0xTXHASH",
            network=NET,
            payer=payload.payload["authorization"]["from"],
        )

    def get_supported(self):
        return SupportedResponse(
            kinds=[SupportedKind(x402Version=2, scheme="exact", network=NET, extra={})]
        )


fac = FakeFacilitator()
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


client = TestClient(app)

# 1) No payment -> 402 + PAYMENT-REQUIRED with exact/EVM requirements
r = client.post("/v1/x402/topup", json={})
assert r.status_code == 402, r.text
hdr = r.headers.get("payment-required")
assert hdr, "missing PAYMENT-REQUIRED header"
preq = json.loads(base64.b64decode(hdr))
acc = preq["accepts"][0]
assert acc["scheme"] == "exact", acc
assert acc["network"] == NET, acc
assert acc["asset"] == USDC, acc
assert acc["amount"] == "1000", acc
assert acc["extra"]["assetTransferMethod"] == "eip3009", acc
assert acc["payTo"] == os.environ["X402_PAYTO"], acc
print("OK 402 + PAYMENT-REQUIRED:", json.dumps(acc))

# 2) With valid payment signature -> 200 + funded key
inner = {
    "scheme": "exact", "network": NET, "asset": USDC, "amount": "1000",
    "payTo": os.environ["X402_PAYTO"],
    "authorization": {"from": PAYER}, "signature": "0xdeadbeef",
}
wrapper = {
    "x402Version": 2, "payload": inner, "accepted": acc,
    "resource": preq["resource"], "extensions": {},
}
sig = base64.b64encode(json.dumps(wrapper).encode()).decode()
r2 = client.post("/v1/x402/topup", headers={"PAYMENT-SIGNATURE": sig}, json={})
assert r2.status_code == 200, r2.text
body = r2.json()
assert body["payer"] == PAYER, body
assert body["balance_usd"] == 0.001, body
assert body["skey"].startswith("gw_"), body
key_name = f"x402:{PAYER}"
assert get_key_by_name(key_name)["id"] == body["id"]
print("OK funded key:", body["id"], "balance", body["balance_usd"])

# 3) Idempotency: same payer gets the SAME key, balance accumulates
r3 = client.post("/v1/x402/topup", headers={"PAYMENT-SIGNATURE": sig}, json={})
assert r3.status_code == 200, r3.text
assert r3.json()["id"] == body["id"], r3.json()
assert r3.json()["balance_usd"] == 0.002, r3.json()
print("OK idempotent reuse, balance", r3.json()["balance_usd"])

assert fac.verified >= 2 and fac.settled >= 2
print("ALL X402 TESTS PASSED")
