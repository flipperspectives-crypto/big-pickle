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

> **Note:** `x402[fastapi]` alone is NOT sufficient for the *resource server*.
> Registering the `exact` scheme for `eip155:84532` requires
> `ExactEvmServerScheme`, which lives in the `[evm]` extra:
> `SchemeNotFoundError: No scheme 'exact' registered for network 'eip155:84532'`
> without it. Hence `requirements.txt` uses `x402[fastapi,evm]`.

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

> **Not executed in this session** — requires a funded Base Sepolia wallet
> holding USDC test tokens (https://faucet.circle.com for USDC, plus ETH for gas).

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
2. `fly deploy` (build installs `x402[fastapi,evm]`).
3. A client wallet funded with Base Sepolia USDC + ETH.
4. Run the step-3 client flow against the live URL; confirm:
   - `402` + `PAYMENT-REQUIRED` on first call,
   - on-chain USDC transfer settled by the facilitator (capture `transaction` tx hash),
   - `200` with a working `skey`,
   - `skey` accepted by `/v1/chat/completions` (needs a provider key, e.g.
     `GATEWAY_GROQ_KEY`, set separately via `fly secrets` stdin).

> Not executed here: no funded wallet/USDC available in this environment,
> and the Groq provider key is intentionally not handled in this session.
