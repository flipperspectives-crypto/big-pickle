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

- `local:qwen3:1.7b` — the designated model for this pilot (runs on the
  gateway's single local Ollama slot at $0 token cost).

## Payment network

- **Base Sepolia** testnet — chain id `eip155:84532`.
- **LIVE mode spends Base Sepolia TEST USDC only.** No mainnet funds are involved.

## Payment flow (x402)

Clarity exposes **two** machine-payable x402 v2 resources. Both answer an
unpaid request with `HTTP 402` + a `PAYMENT-REQUIRED` header (base64 JSON x402 v2
requirement) whose `resource` field names the route you called.

### Option A — DIRECT paid inference (preferred, one-shot)

No signup, no `skey`, no second request. An autonomous agent starts from **only
the public origin** and discovers this route itself.

1. `GET /openapi.json` — fetch Clarity's machine-readable API metadata (public,
   no auth). It advertises the x402-paid resources with `x-payment-info` and
   descriptions. The agent selects the direct chat/completion route (POST, same
   origin, x402-paid, described as direct inference — **not** gateway credit /
   top-up / `skey`); it does not require the route path to be supplied manually.
2. `POST /v1/x402/chat/completions` **without payment** — an OpenAI-style body
   (`model`, `messages`, optional `max_tokens` ≤ 128, `stream` must be false).
   The endpoint answers `HTTP 402` with a `PAYMENT-REQUIRED` header whose
   `resource` is `/v1/x402/chat/completions`.
3. Read the `PAYMENT-REQUIRED` challenge. Sign it with an **x402-compatible Base
   Sepolia wallet** (client-side signing — see security rules below).
4. Retry `POST /v1/x402/chat/completions` with the `PAYMENT-SIGNATURE` header.
   On a successful settlement the `200` response carries the OpenAI-compatible
   completion **directly**. No gateway balance is credited and no `skey` is issued.

### Option B — persistent gateway credit (top-up + `skey`)

Use this when you want reusable credit / a gateway key.

1. `POST /v1/x402/topup` **without payment** — answers `HTTP 402` with a
   `PAYMENT-REQUIRED` header whose `resource` is `/v1/x402/topup`.
2. Read the challenge, sign it, and retry `POST /v1/x402/topup` with the
   `PAYMENT-SIGNATURE` header. On a successful settlement the response carries a
   gateway **`skey`**.
3. Call `POST /v1/chat/completions` with:
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

## CLI usage

The SDK example lives at `examples/agent-sdk-example/clarity_agent.py`.

**DRY RUN (no funds, no signature, no secrets):**

```bash
python examples/agent-sdk-example/clarity_agent.py
```

On Windows (one line):

```bat
python examples/agent-sdk-example/clarity_agent.py
```

**LIVE (spends Base Sepolia TEST USDC):** export a funded Base Sepolia wallet
client-side only, then:

```bash
python examples/agent-sdk-example/clarity_agent.py \
  --live \
  --base-url https://desktop-o99r0sf.tail935fba.ts.net \
  --model local:qwen3:1.7b
```

On Windows (one line):

```bat
python examples/agent-sdk-example/clarity_agent.py --live --base-url https://desktop-o99r0sf.tail935fba.ts.net --model local:qwen3:1.7b
```

The example uses a **120-second default HTTP timeout** because local
inference can take longer than typical cloud API requests; pass
`--timeout-seconds N` to override. This is not a guaranteed response time.

The private key is read from the `X402_PAYER_KEY` environment variable, used only
to build the signer, then immediately discarded — it is never printed, logged,
persisted, or sent to Clarity.

## What this pilot is NOT

This is a testnet pilot. It is not production-ready, not battle-tested, not a
claim of secure autonomous operation, and generates no real (mainnet) revenue.

## Mainnet mode (not enabled by default)

The gateway ships in `X402_NETWORK_MODE=testnet` (Base Sepolia). Mainnet is
opt-in and **fail-closed**: the gateway only advertises a mainnet
`PAYMENT-REQUIRED` challenge when `X402_NETWORK_MODE=mainnet` **and** both
`CDP_API_KEY_ID` and `CDP_API_KEY_SECRET` are configured. With missing
credentials it silently disables x402 rather than falling back to testnet.

> **WARNING:** mainnet x402 spends **real Base USDC**. Do not enable mainnet
> mode unless you intend to move real funds. The default testnet pilot uses
> Base Sepolia TEST USDC only.
