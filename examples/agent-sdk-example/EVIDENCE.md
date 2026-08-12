# Agent SDK Example — Evidence

Minimal, dependency-free developer example for the Clarity gateway that exercises
**discovery → 402 / PAYMENT-REQUIRED → simulated PAYMENT-SIGNATURE retry**.

Scope guardrails (per request):
- **x402 architecture unchanged** — the example only talks to the gateway over its
  public HTTP API (`/v1/status`, `/v1/chat/completions`). No production code was
  modified.
- **status architecture unchanged** — `/v1/status` is consumed read-only.
- **Dry-run / no-funds** — no signing, no settlement, no private keys/secrets.

## 1. Tests (offline, no network, no funds)

```
python -m pytest test_agent.py -v
```

```
test_agent.py::test_discover_reads_public_status PASSED
test_agent.py::test_parse_payment_required_header PASSED
test_agent.py::test_chat_200_returns_data PASSED
test_agent.py::test_chat_402_dry_run_no_payer PASSED
test_agent.py::test_chat_402_dry_run_with_payer PASSED
test_agent.py::test_no_secrets_in_simulated_output PASSED
test_agent.py::test_live_mode_requires_env_key PASSED
test_agent.py::test_live_signing_retry_mocked PASSED
test_agent.py::test_live_sdk_signing_integration_mocked PASSED
test_agent.py::test_self_contained_demo_runs_offline PASSED

10 passed
```

### What each test proves
- `test_discover_reads_public_status` — discovery hits `/v1/status`, no auth.
- `test_parse_payment_required_header` — a real-shaped `PAYMENT-REQUIRED` header
  is decoded to scheme/network/asset/amount/payTo.
- `test_chat_200_returns_data` — non-402 path returns the completion, no payment.
- `test_chat_402_dry_run_no_payer` — 402 yields a dry-run plan, no payer embedded.
- `test_chat_402_dry_run_with_payer` — with a public payer address, the simulated
  `PAYMENT-SIGNATURE` is built and decodes to `{"simulated": true, …}`.
- `test_no_secrets_in_simulated_output` — no `sk-`/`private`/`secret`/`gw_`/`bearer`/
  `api_key`/`x-api-key`/`authorization` material in the dry-run plan.
- `test_live_mode_requires_env_key` — LIVE refuses to run unless `X402_PAYER_KEY` is
  set (no signing, no spend without the key).
- `test_live_signing_retry_mocked` — full live state machine exercised with the SDK
  bits mocked: `payment_signed`, `settlement_verified`, and
  `gateway_credit_verified` all become `True`; the private key is never present in
  the result.
- `test_live_sdk_signing_integration_mocked` — uses the REAL `x402` client +
  `ExactEvmScheme` signer construction, with only the network signing round-trip
  mocked, proving the SDK integration path is correct (no real funds).
- `test_self_contained_demo_runs_offline` — full dry-run flow runs with a fake server.

**LIVE never executes during tests**: every live test either (a) lacks
`X402_PAYER_KEY` and asserts a `RuntimeError`, or (b) mocks the SDK/transport so
no real signature or on-chain settlement occurs.

## 2. Demo output (captured, offline)

```
=== Clarity Agent SDK example (DRY RUN / NO FUNDS) ===
{
  'chat_status': 402,
  'discovery': {'base_url': 'https://example.invalid',
                'status': {'status': 'healthy', 'gateway': {...}, 'providers': {...}}},
  'mode': 'dry-run',
  'plan': {
     'mode': 'dry-run',
     'note': 'DRY RUN - no funds moved and no valid signature produced. '
             'Supply a real signer to settle on-chain.',
     'payer': '0xAGENT_PUBLIC_ADDRESS_DEMO',
     'payment_required': {'amount': '1000',
                          'asset': '0x036CbD53842c5426634e7929541eC2318f3dCF7E',
                          'network': 'eip155:84532',
                          'pay_to': '0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f',
                          'scheme': 'exact'},
     'retry_header': {'PAYMENT-SIGNATURE': 'SIMULATED.eyJzaW11bGF0ZWQi...'}
  }
}
```

The `PAYMENT-SIGNATURE` value is prefixed `SIMULATED.` and decodes to
`{"simulated": true, "network": "eip155:84532", "asset": "0x036C…", "amount":
"1000", "payTo": "0x42ad…", "payer": "0xAGENT_PUBLIC_ADDRESS_DEMO", "note": "no
real signing occurred"}` — explicitly **not** a settleable signature.

## 3. Honesty labels

- The example output and README always mark dry-run vs live settlement.
- Live settlement is deliberately unimplemented so the example can never spend
  funds or handle a private key.

## 4. Files added (no production behavior changed)

```
examples/agent-sdk-example/clarity_agent.py
examples/agent-sdk-example/test_agent.py
examples/agent-sdk-example/README.md
examples/agent-sdk-example/EVIDENCE.md
```
