# Clarity Agent SDK Example (dry-run / no-funds)

A minimal developer-facing example showing how an **agent** discovers a Clarity
gateway and handles the **x402 `402` / `PAYMENT-REQUIRED`** flow, then builds the
`PAYMENT-SIGNATURE` retry when a payer wallet is supplied.

> **This example never signs a transaction and never moves funds.** It runs in
> **dry-run / no-funds** mode by default. When a payer wallet (a *public* address
> string) is supplied, it shows how the `PAYMENT-SIGNATURE` retry would be
> constructed, but the signature is **clearly simulated** (`SIMULATED.…`) and no
> settlement occurs. There are **no private keys or secrets** anywhere in this
> directory.

## What it demonstrates

1. **Discovery** — read the gateway's public, read-only `GET /v1/status`
   (no auth, no secrets) to confirm the endpoint and see provider health.
2. **402 handling** — parse the base64-JSON `PAYMENT-REQUIRED` response header
   into scheme / network / asset / amount / payTo.
3. **Retry plan** — when a payer wallet is supplied, build the (simulated)
   `PAYMENT-SIGNATURE` header and describe the retry step.

## Files

| File | Purpose |
|------|---------|
| `clarity_agent.py` | `ClarityAgent` + `discover_clarity()` + `PaymentRequired` parsing + offline `demo()`. |
| `test_agent.py` | 8 offline tests (no network, no funds, no secrets). |
| `README.md` | This file. |
| `EVIDENCE.md` | Test run + demo output proving dry-run behavior. |

## Run it

```bash
# offline self-contained demo (fake server, zero network, zero funds)
python clarity_agent.py

# or with pytest
python -m pytest test_agent.py -v
```

To point at a real gateway, set `CLARITY_BASE_URL` (or pass `base_url=`) and the
agent will use real HTTP. The example still stays dry-run unless you explicitly
construct a live signer path (which is intentionally **not** implemented here, to
avoid ever touching a private key).

## Simulated vs live settlement

| Mode | Trigger | Behavior |
|------|---------|----------|
| **dry-run (default)** | `dry_run=True` (default) | 402 parsed; if a payer is supplied, a `PAYMENT-SIGNATURE` header is built but prefixed `SIMULATED.` — **not** a valid signature, **no** on-chain settlement. |
| **live (opt-in)** | `dry_run=False` **and** `X402_PAYER_KEY` set in the environment | Uses the official `x402` client SDK to construct + sign a real payment, retries with `PAYMENT-SIGNATURE`, parses `PAYMENT-RESPONSE`, and reports `payment_signed` / `settlement_verified` / `gateway_credit_verified`. Refuses to run without `X402_PAYER_KEY`. |

## LIVE mode (opt-in, uses the official x402 client SDK)

LIVE only executes when **both** conditions hold:

1. the agent is created with `dry_run=False`, **and**
2. the payer **private** key is present in the local environment variable
   `X402_PAYER_KEY` (a funded Base Sepolia USDC wallet for a real run).

```bash
export X402_PAYER_KEY=0x...your_private_key...   # Base Sepolia, funded with USDC
python -c "from clarity_agent import live_example; print(live_example())"
```

LIVE reports three independent states so you can see exactly how far the payment
got:

- `payment_signed` — the x402 client produced a signed `PAYMENT-SIGNATURE`.
- `settlement_verified` — the gateway/facilitator returned a successful
  `PAYMENT-RESPONSE` (on-chain settlement succeeded).
- `gateway_credit_verified` — the `/v1/x402/topup` call returned `200` with a
  usable `skey` (gateway balance was credited).

LIVE **never** runs during the test suite and **never** spends unless you invoke
it with a real key. The dry-run path is unchanged.

## Security notes

- No `sk-…`, `gw_…`, bearer tokens, API keys, or private keys are printed,
  logged, persisted, committed, or included in any output.
- The private key is read from `X402_PAYER_KEY`, used only to build the signer,
  then immediately discarded (`key = None` after use).
- The “payer” string in dry-run is a **public** address only.
- The simulated signature header decodes to `{"simulated": true, …}` and cannot
  settle anything.
