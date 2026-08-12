# Agent SDK Example — Evidence

Minimal developer example for the Clarity gateway that exercises the **real**
production lifecycle:

**discovery → `POST /v1/x402/topup` 402/PAYMENT-REQUIRED → (dry-run plan OR
live x402 sign+retry) → extract `skey` → `POST /v1/chat/completions` with
`Authorization: Bearer <skey>`**.

Scope guardrails (per request):
- **x402 architecture unchanged** — the example only talks to the gateway over
  its public HTTP API (`/v1/status`, `/v1/x402/topup`, `/v1/chat/completions`).
  No production code was modified.
- **status architecture unchanged** — `/v1/status` is consumed read-only.
- **Dry-run / no-funds by default** — no signing, no settlement, no private
  keys/secrets unless you explicitly opt into LIVE with a real key.

## 1. Tests (offline, no network, no funds)

```
python -m pytest test_agent.py -v
```

```
test_agent.py::test_discover_reads_public_status PASSED
test_agent.py::test_discover_module_function PASSED
test_agent.py::test_parse_payment_required_header PASSED
test_agent.py::test_topup_challenge_returns_402_with_payment_required PASSED
test_agent.py::test_chat_insufficient_balance_returns_plain_402_no_challenge PASSED
test_agent.py::test_dry_run_shows_simulated_plan_and_no_skey PASSED
test_agent.py::test_no_secrets_in_dry_run_output PASSED
test_agent.py::test_self_contained_demo_runs_offline PASSED
test_agent.py::test_live_requires_env_key PASSED
test_agent.py::test_live_two_endpoint_flow_mocked PASSED
test_agent.py::test_live_sdk_integration_offline PASSED

11 passed
```

### What each test proves
- `test_discover_reads_public_status` — discovery hits `GET /v1/status`, no auth.
- `test_discover_module_function` — the `discover_clarity()` helper works.
- `test_parse_payment_required_header` — a real-shaped `PAYMENT-REQUIRED` header
  is decoded to scheme/network/asset/amount/payTo/maxTimeoutSeconds.
- `test_topup_challenge_returns_402_with_payment_required` — `POST /v1/x402/topup`
  (no payment) yields the `402` + `PAYMENT-REQUIRED` challenge (the ONLY x402
  challenge endpoint).
- `test_chat_insufficient_balance_returns_plain_402_no_challenge` — a
  `/v1/chat/completions` `402` carries **no** `PAYMENT-REQUIRED` (it is not an
  x402 challenge); the agent must not mistake it for one.
- `test_dry_run_shows_simulated_plan_and_no_skey` — dry-run returns a simulated
  plan, `payment_signed`/`skey_present`/`inference_verified` all `False`.
- `test_no_secrets_in_dry_run_output` — no `sk-`/`gw_`/`x-api-key`/`api_key`
  material in the dry-run plan.
- `test_self_contained_demo_runs_offline` — the dry-run `demo()` runs end-to-end
  with an embedded offline server.
- `test_live_requires_env_key` — LIVE raises `RuntimeError` unless `X402_PAYER_KEY`
  is set (no signing, no spend without the key).
- `test_live_two_endpoint_flow_mocked` — full two-endpoint LIVE flow
  (top-up sign+retry → `skey` → chat with `Bearer <skey>`) exercised with the SDK
  bits mocked: `payment_signed`, `settlement_verified`, `gateway_credit_verified`,
  and `inference_verified` all become `True`; the chat request is proven to carry
  `Authorization: Bearer gw_test_skey`; the private key value never appears in the
  result.
- `test_live_sdk_integration_offline` — uses the REAL `x402` client SDK +
  `ExactEvmScheme` signer (EIP-3009 signs **locally**, no network/funds) to prove
  the SDK integration path produces a valid `PAYMENT-SIGNATURE`.

**LIVE never executes during tests**: the env-key test asserts `RuntimeError`, and
the two live tests mock the SDK/transport so no real signature or on-chain
settlement occurs.

## 2. Demo output (captured, offline dry-run)

```
=== Clarity Agent SDK example (DRY RUN / NO FUNDS) ===
{
  "mode": "dry-run",
  "base_url": "https://example.invalid",
  "discovery": {"status": 200, "gateway": "healthy"},
  "payment_required": {
    "scheme": "exact",
    "network": "eip155:84532",
    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7E",
    "amount": "1000",
    "pay_to": "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f",
    "resource": null
  },
  "payment_signed": false,
  "settlement_verified": false,
  "gateway_credit_verified": false,
  "inference_verified": false,
  "skey_present": false,
  "chat_status": null,
  "error": null,
  "dry_run_plan": {
    "note": "DRY RUN - no funds moved and no valid signature produced. ...",
    "payer": "0xAGENT_PUBLIC_ADDRESS_DEMO",
    "would_sign_with": "official x402 SDK ExactEvmScheme (EIP-3009 transferWithAuthorization)",
    "retry": "POST /v1/x402/topup with PAYMENT-SIGNATURE header",
    "on_success": "extract 'skey' from the top-up response, then POST /v1/chat/completions with Authorization: Bearer <skey>",
    "simulated_signature": "SIMULATED.eyJzaW11bGF0ZWQi..."
  }
}
```

The `PAYMENT-SIGNATURE` value is prefixed `SIMULATED.` and decodes to
`{"simulated": true, "network": "eip155:84532", "asset": "0x036C…", "amount":
"1000", "payTo": "0x42ad…", "payer": "0xAGENT_PUBLIC_ADDRESS_DEMO", "note": "no
real signing occurred"}` — explicitly **not** a settleable signature.

## 3. Honesty labels

- The example output and README always mark dry-run vs live settlement.
- LIVE is fully implemented but **gated** behind `dry_run=False` **and**
  `X402_PAYER_KEY`; it refuses to run without a real funded key, and the key is
  scrubbed (`key = None`) immediately after building the signer. No real spend
  occurs in any test.

## 4. Files (no production behavior changed)

```
examples/agent-sdk-example/clarity_agent.py
examples/agent-sdk-example/test_agent.py
examples/agent-sdk-example/README.md
examples/agent-sdk-example/EVIDENCE.md
```
