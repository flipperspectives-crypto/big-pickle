# Clarity Agent SDK Example

A minimal developer-facing example showing how an **agent** drives the **real**
Clarity machine-payable lifecycle:

1. **Discover** the gateway contract via `GET /v1/status` (public, no auth).
2. **Request a gateway credit** with `POST /v1/x402/topup` and **no** payment.
   The protected endpoint answers `402` + a `PAYMENT-REQUIRED` header (a base64
   JSON x402 v2 requirement). This is the **only** endpoint that issues an x402
   challenge — `/v1/chat/completions` requires a gateway key and its
   insufficient-balance `402` does **not** include `PAYMENT-REQUIRED`.
3. **DRY-RUN** (default, no funds): display the requirements and a *simulated*
   signing plan. No signature is produced; nothing is settled.
4. **LIVE** (opt-in, requires `X402_PAYER_KEY`): use the official **`x402` client
   SDK** to sign the payment and retry `/v1/x402/topup` with the
   `PAYMENT-SIGNATURE` header. On a successful settlement the response carries a
   gateway `skey`.
5. **Only then** call `/v1/chat/completions` with `Authorization: Bearer <skey>`.

> **This example never signs a transaction or moves funds unless you opt into
> LIVE with a real funded key.** There are **no private keys or secrets** anywhere
> in this directory other than what you supply at runtime via the environment.

## What it demonstrates

| Stage | Endpoint | Meaning |
|-------|----------|---------|
| Discovery | `GET /v1/status` | Public reliability/contract surface. |
| Top-up challenge | `POST /v1/x402/topup` (no payment) | `402` + `PAYMENT-REQUIRED`. |
| Settlement | `POST /v1/x402/topup` (`PAYMENT-SIGNATURE`) | Real x402 payment; returns `skey`. |
| Inference | `POST /v1/chat/completions` (`Authorization: Bearer <skey>`) | Gateway-authorized completion. |

Each later stage is verified independently and is **never** marked `True` unless
the earlier one actually succeeded:

- `payment_signed` — the x402 SDK produced a `PAYMENT-SIGNATURE` header.
- `settlement_verified` — the gateway/facilitator confirmed settlement.
- `gateway_credit_verified` — the top-up returned a usable `skey`.
- `inference_verified` — `/v1/chat/completions` returned `200` with the `skey`.

## Files

| File | Purpose |
|------|---------|
| `clarity_agent.py` | `ClarityAgent`, `PaymentRequired`, `discover_clarity()`, `demo()` (offline dry-run), `live_example()`. |
| `test_agent.py` | 11 offline tests reproducing the two-endpoint production flow. |
| `README.md` | This file. |
| `EVIDENCE.md` | Test run + demo output. |

## Run it

```bash
# offline self-contained dry-run demo (embedded fake server, zero network, zero funds)
python clarity_agent.py

# or with pytest
python -m pytest test_agent.py -v
```

To target a real gateway, pass `base_url="https://your-gateway"` and the agent
uses real HTTP. The example stays in dry-run unless you explicitly opt into LIVE.

## LIVE mode (opt-in, uses the official x402 client SDK)

LIVE executes only when **both** hold:

1. the agent is created with `dry_run=False` (`live_example()` does this), **and**
2. the payer **private** key is present in the environment variable
   `X402_PAYER_KEY` (a funded **Base Sepolia** USDC wallet for a real run).

### History-safe key entry (no shell history leak)

Do **not** use `export X402_PAYER_KEY=0x...` — that writes the secret to shell
history. Instead read it interactively with `getpass`, which never echoes it:

```python
# live_run.py  (run with: python live_run.py)
import getpass
from clarity_agent import live_example

key = getpass.getpass("X402 payer private key (Base Sepolia, funded with USDC): ")
import os
os.environ["X402_PAYER_KEY"] = key          # kept only in this process
print(live_example("https://your-gateway"))  # scrubs the key immediately after use
```

The key is read from the environment, used only to build the signer, then
immediately discarded (`key = None` after use) and is **never** printed, logged,
persisted, committed, or returned.

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
