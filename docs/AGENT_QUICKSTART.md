# Clarity — Agent Quickstart (Base Sepolia TESTNET pilot)

> **Status: controlled TESTNET pilot.**
> Clarity is **not** mainnet, **not** production-ready, and makes **no claim of real
> revenue**. This pilot exists to validate machine-payable inference with external
> agents on test funds only.

## Public pilot endpoint

```
https://desktop-o99r0sf.tail935fba.ts.net
```

## Pilot model

- `local:qwen3:1.7b` — the only model enabled for the pilot (runs on the
  gateway's single local Ollama slot at $0 token cost).

## Payment network

- **Base Sepolia** testnet — chain id `eip155:84532`.
- **LIVE mode spends Base Sepolia TEST USDC only.** No mainnet funds are involved.

## Payment flow (x402)

1. `GET /v1/status` — discover the gateway contract (public, no auth).
2. `POST /v1/x402/topup` **without payment** — the endpoint answers
   `HTTP 402` with a `PAYMENT-REQUIRED` header (base64 JSON x402 v2 payment
   requirement). This is the only endpoint that issues an x402 challenge.
3. Read the `PAYMENT-REQUIRED` challenge. Sign it with an **x402-compatible Base
   Sepolia wallet** (client-side signing — see security rules below).
4. Retry `POST /v1/x402/topup` with the `PAYMENT-SIGNATURE` header. On a
   successful settlement the response carries a gateway **`skey`**.
5. Call `POST /v1/chat/completions` with:
   ```
   Authorization: Bearer <skey>
   ```

## Security rules for agents

- **Never send a wallet private key to Clarity.** Clarity never asks for it.
- **Never put a private key in the request JSON.** Signing happens on the
  agent/client side, then only the signature header is sent.
- **Read the `PAYMENT-REQUIRED` challenge** from the `402` response and use those
  values — do **not** hard-code payment details (amount, asset, payTo, network).
- Respect `429` — you are **rate limited**; obey the `Retry-After` header and back
  off. Do not hammer the gateway.
- Respect `503` — the **single local inference slot is busy**; the agent should
  retry later, not queue an unbounded backlog.
- Respect `413` — **request body too large**; shrink the request and retry.

## Python usage example

Uses the existing SDK example
`examples/agent-sdk-example/clarity_agent.py`. Set the public endpoint, then run
the DRY-RUN (no funds, no secrets). To actually settle, export a funded Base
Sepolia wallet as `X402_PAYER_KEY` and run LIVE.

```python
import os
from examples.agent_sdk_example.clarity_agent import ClarityAgent

# Public TESTNET pilot endpoint (no payment details hard-coded here).
BASE_URL = "https://desktop-o99r0sf.tail935fba.ts.net"

# DRY RUN: no funds moved, no signature produced, no secrets used.
agent = ClarityAgent(base_url=BASE_URL, dry_run=True)
result = agent.run_sync()  # default pilot model: local:qwen3:1.7b

# LIVE (spends Base Sepolia TEST USDC): export YOUR wallet client-side only.
#   export X402_PAYER_KEY="0xYOUR_BASE_SEPOLIA_PRIVATE_KEY"
#   agent = ClarityAgent(base_url=BASE_URL, dry_run=False)
#   result = agent.run_sync(model="local:qwen3:1.7b")
```

The private key is read from the `X402_PAYER_KEY` environment variable, used only
to build the signer, then immediately discarded by the SDK example — it is never
printed, logged, persisted, or sent to Clarity.

## What this pilot is NOT

This is a testnet pilot. It is not production-ready, not battle-tested, not a
claim of secure autonomous operation, and generates no real (mainnet) revenue.
