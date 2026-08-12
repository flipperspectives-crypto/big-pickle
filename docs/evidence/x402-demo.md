# x402 Demo — Evidence

Machine-payable gateway top-up for **Clarity** using the official
[`x402`](https://github.com/coinbase/x402) Python SDK (x402 **v2.19.0**).

Network: **Base Sepolia** (`eip155:84532`), asset **USDC**
`0x036CbD53842c5426634e7929541eC2318f3dCF7e`, settled via the official
**x402.org test facilitator** (`https://x402.org/facilitator`).

## 1. Install / version

```bash
pip install "x402[fastapi,evm]"   # x402 2.19.0
```

> **Note:** `x402[fastapi,evm]` is **intentional** for the current Python SDK.
> The resource server registers `ExactEvmServerScheme` for `eip155:84532`, which
> lives in the `[evm]` extra. `x402[fastapi]` alone raises
> `SchemeNotFoundError: No scheme 'exact' registered for network 'eip155:84532'`,
> so the `[evm]` extra is required on the server.

## 2. Verified `402 Payment Required` + `PAYMENT-REQUIRED` header

`POST /v1/x402/topup` with no payment returns `402` and a base64-JSON
`PAYMENT-REQUIRED` header (captured from the unit test, `test_x402.py`):

```json
{
  "x402Version": 2,
  "error": "Payment required",
  "resource": {
    "url": "/v1/x402/topup",
    "description": "Clarity gateway credit top-up (machine-payable via x402)",
    "mimeType": "",
    "serviceName": "Clarity"
  },
  "accepts": [
    {
      "scheme": "exact",
      "network": "eip155:84532",
      "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
      "amount": "1000",
      "payTo": "<X402_PAYTO receiving wallet>",
      "maxTimeoutSeconds": 60,
      "extra": {
        "name": "USDC",
        "version": "2",
        "assetTransferMethod": "eip3009"
      }
    }
  ]
}
```

`amount: "1000"` = 0.001 USDC (6 decimals) = `X402_PRICE_USD=0.001`.

## 3. Client-side payment (EIP-3009 `transferWithAuthorization`)

The payer signs an EIP-3009 USDC `transferWithAuthorization` over
`eip155:84532` and retries with the `PAYMENT-SIGNATURE` header. Using the
official SDK client:

```python
from x402 import x402Client
from x402.mechanisms.evm.exact import ExactEvmScheme
# ExactEvmScheme needs a signer funded with USDC on Base Sepolia.
client = x402Client()
client.register("eip155:*", ExactEvmScheme(signer=my_signer))
payment = client.create_payment_payload(payment_required)   # from step 2
r = requests.post(url, headers={"PAYMENT-SIGNATURE": b64(payment)}, json={})
```

> **Not executed in this session** — requires a Base Sepolia wallet holding USDC
> test tokens (https://faucet.circle.com). The payer is **gasless**: the x402.org
> test facilitator sponsors the EIP-3009 settlement, so the payer needs **no ETH**
> for this exact test path.

## 4. Protected endpoint

`POST /v1/x402/topup` — gated by the x402 FastAPI middleware
(`app/x402.py`). On a verified+settled payment the handler credits a
**deterministic gateway key per payer address** and returns:

```json
{
  "id": "<key id>",
  "skey": "gw_...",
  "balance_usd": 0.001,
  "credited_usd": 0.001,
  "network": "eip155:84532",
  "scheme": "exact",
  "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
  "payer": "0x...",
  "message": "Gateway key funded. Use `skey` as Bearer token for /v1/chat/completions."
}
```

## 5. Settlement

Verification + settlement are performed by the **x402.org test facilitator**
(`X402_FACILITATOR_URL`). The middleware settles after the handler returns a
non-error response and attaches a `PAYMENT-RESPONSE` header
(`{success, payer, transaction, network}`).

## 6. Key crediting (verified by unit test)

`test_x402.py` mocks the facilitator and asserts:
- 402 + correct `PAYMENT-REQUIRED` (exact / eip155:84532 / USDC / eip3009).
- Valid signature → `200` with a funded `skey`; `balance_usd == 0.001`.
- **Idempotency:** same payer reuses the same key; balance accrues
  (`0.001` → `0.002`).
- Facilitator `verify` and `settle` each invoked.

```
ALL X402 TESTS PASSED
```

## 6b. Atomicity & settlement ledger (verified by `test_x402.py`)

The gateway balance is credited **only inside the x402 `after_settle` post-settlement
success hook**, never in the request handler. The official FastAPI middleware runs
the handler after verification but *before* `process_settlement()`; the `after_settle`
hook fires **only when settlement succeeds** (settlement failure runs
`on_settle_failure`, not `after_settle`). Therefore a verified payment whose
settlement later fails leaves **zero** gateway credit.

Every successful settlement is recorded in a persistent ledger
(`x402_settlements` table) keyed by a deterministic `payment_id`
(`sha256(payer | authorization.nonce | network | amount)`). `settle_x402_credit()`
does `INSERT OR IGNORE` + balance update in one DB transaction, so replaying the
exact same `PAYMENT-SIGNATURE` reuses the same `payment_id` and is skipped — the
balance can never be credited twice for one on-chain settlement.

`test_x402.py` proves:
- verify-ok + **settle-fail => zero credit** (ledger empty, balance 0).
- **successful settlement => exactly one credit** (balance 0.001, single ledger row).
- **replay of identical `PAYMENT-SIGNATURE` => no extra credit** (balance stays 0.001).
- same payer (distinct nonce) => **same gateway key**, balance accrues (0.002).
- secret `skey` is `gw_<random>` and **not derived from the payer address**.

## 7. 402 schema

`x402Version: 2`, `accepts[]` with `scheme:"exact"`, `network:"eip155:84532"`,
`asset` (USDC), `amount` (atomic), `payTo` (gateway wallet),
`maxTimeoutSeconds`, and `extra:{name,version,assetTransferMethod:"eip3009"}`.
Response headers: `PAYMENT-REQUIRED` (request) and `PAYMENT-RESPONSE` (settle).

## 8. Go client

Out of scope for this change. The Clarity gateway is Python/FastAPI; a Go
client would call `/v1/x402/topup` using the `github.com/coinbase/x402/go`
`x402Client` with the same scheme/network/asset.

## 9. Live Base Sepolia lifecycle (PENDING — manual)

Requires:
1. `fly secrets set X402_PAYTO=<your Base Sepolia wallet> --app big-pickle`
   (plus `X402_ENABLED=true`). This is a **public receiving address**, not a secret.
2. `fly deploy` (build installs `x402[fastapi,evm]` — the `[evm]` extra is required).
3. A client wallet funded with Base Sepolia **USDC only**. The payer is **gasless**
   via the x402.org test facilitator — **no ETH is required** for this exact test path.
4. Run the step-3 client flow against the live URL; confirm:
   - `402` + `PAYMENT-REQUIRED` on first call,
   - on-chain USDC transfer settled by the facilitator (capture `transaction` tx hash),
   - `200` with a working `skey`,
   - `skey` accepted by `/v1/chat/completions` (needs a provider key, e.g.
     `GATEWAY_GROQ_KEY`, set separately via `fly secrets` stdin).

> Not executed here: no funded wallet/USDC available in this environment,
> and the Groq provider key is intentionally not handled in this session.
