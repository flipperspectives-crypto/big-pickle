# Agent SDK Example — Evidence

Minimal developer example for the Clarity gateway that exercises the **real**
production lifecycle. Clarity exposes **two** machine-payable x402 v2 resources:

- `POST /v1/x402/chat/completions` (DIRECT, preferred, one-shot paid inference)
- `POST /v1/x402/topup` (persistent gateway credit → `skey`)

DIRECT flow (origin-only discovery):

**`GET /openapi.json` → identify x402-paid routes → select the direct
chat/completion route (no path supplied manually) → `POST <discovered-route>`
402/PAYMENT-REQUIRED (resource = that route) → (dry-run plan OR live x402
sign+retry) → 200 carries the completion directly (no `skey`, no gateway
balance).**

Top-up flow:

**discovery → `POST /v1/x402/topup` 402/PAYMENT-REQUIRED → (dry-run plan OR live
x402 sign+retry) → extract `skey` → `POST /v1/chat/completions` with
`Authorization: Bearer <skey>`.**

Scope guardrails (per request):
- **x402 architecture unchanged** — the example only talks to the gateway over
  its public HTTP API (`/v1/status`, `/v1/x402/chat/completions`,
  `/v1/x402/topup`, `/v1/chat/completions`). No gateway production code was
  modified in this alignment pass.
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
test_agent.py::test_chat_direct_challenge_returns_402_with_payment_required PASSED
test_agent.py::test_direct_chat_402_parses_resource_and_fields PASSED
test_agent.py::test_agent_direct_flow_dry_run_stops_before_signing PASSED
test_agent.py::test_direct_flow_sends_no_payment_signature_and_no_topup PASSED
test_agent.py::test_dry_run_shows_simulated_plan_and_no_skey PASSED
test_agent.py::test_no_secrets_in_dry_run_output PASSED
test_agent.py::test_self_contained_demo_runs_offline PASSED
test_agent.py::test_demo_direct_runs_offline PASSED
test_agent.py::test_discover_from_origin_only_begins_with_base_url PASSED
test_agent.py::test_openapi_discovery_is_fetched PASSED
test_agent.py::test_direct_chat_route_found_from_metadata PASSED
test_agent.py::test_route_selected_over_topup_for_chat PASSED
test_agent.py::test_route_method_path_derived_from_discovery_not_supplied PASSED
test_agent.py::test_malformed_discovery_fails_safely PASSED
test_agent.py::test_no_matching_paid_inference_route_fails_safely PASSED
test_agent.py::test_unrelated_paid_route_not_selected PASSED
test_agent.py::test_cross_origin_route_metadata_rejected PASSED
test_agent.py::test_direct_flow_sends_no_payment_signature_discovery PASSED
test_agent.py::test_live_requires_env_key PASSED
test_agent.py::test_live_two_endpoint_flow_mocked PASSED
test_agent.py::test_live_sdk_integration_offline PASSED
test_agent.py::test_agent_quickstart_no_longer_claims_single_challenge PASSED

27 passed
```

### What each test proves
- `test_discover_reads_public_status` — discovery hits `GET /v1/status`, no auth.
- `test_discover_module_function` — the `discover_clarity()` helper works.
- `test_parse_payment_required_header` — a real-shaped `PAYMENT-REQUIRED` header
  is decoded to scheme/network/asset/amount/payTo/maxTimeoutSeconds.
- `test_topup_challenge_returns_402_with_payment_required` — `POST /v1/x402/topup`
  (no payment) yields the `402` + `PAYMENT-REQUIRED` challenge (resource
  `/v1/x402/topup`).
- `test_chat_insufficient_balance_returns_plain_402_no_challenge` — a
  `/v1/chat/completions` `402` carries **no** `PAYMENT-REQUIRED` (it is not an
  x402 challenge); the agent must not mistake it for one.
- `test_chat_direct_challenge_returns_402_with_payment_required` — `POST
  /v1/x402/chat/completions` (no payment) yields the `402` + `PAYMENT-REQUIRED`
  challenge.
- `test_direct_chat_402_parses_resource_and_fields` — the direct challenge parses
  to network/asset/amount/payTo **and** `resource == /v1/x402/chat/completions`.
- `test_agent_direct_flow_dry_run_stops_before_signing` — `run_direct()` dry-run
  reports the requirement and `dry_run_plan`, with `payment_signed`,
  `settlement_verified`, `inference_verified`, `gateway_credit_verified` all
  `False` (STOP before signing).
- `test_direct_flow_sends_no_payment_signature_and_no_topup` — the direct dry-run
  flow makes exactly one unpaid `POST /v1/x402/chat/completions`, never calls
  `/v1/x402/topup`, and never sends a `PAYMENT-SIGNATURE`.
- `test_dry_run_shows_simulated_plan_and_no_skey` — top-up dry-run returns a
  simulated plan, `payment_signed`/`skey_present`/`inference_verified` all `False`.
- `test_no_secrets_in_dry_run_output` — no `sk-`/`gw_`/`x-api-key`/`api_key`
  material in the dry-run plan.
- `test_self_contained_demo_runs_offline` — the dry-run `demo()` runs end-to-end
  with an embedded offline server.
- `test_demo_direct_runs_offline` — the dry-run `demo_direct()` runs end-to-end
  with an embedded offline server and references `/v1/x402/chat/completions`.
- `test_discover_from_origin_only_begins_with_base_url` — `run_direct()` takes no
  route path; starting from the origin only it fetches OpenAPI, selects
  `/v1/x402/chat/completions`, and stops before signing.
- `test_openapi_discovery_is_fetched` — the agent issues `GET /openapi.json`
  during discovery.
- `test_direct_chat_route_found_from_metadata` — `discover_paid_routes` extracts
  both x402-paid routes from the OpenAPI doc.
- `test_route_selected_over_topup_for_chat` — `select_direct_chat_route` prefers
  the chat route over the credit/top-up route for a chat task.
- `test_route_method_path_derived_from_discovery_not_supplied` — when the
  OpenAPI advertises the direct route under a different relative path, the agent
  POSTs to that discovered path (not the hard-coded constant).
- `test_malformed_discovery_fails_safely` — an OpenAPI doc with no paid routes
  makes selection raise `RuntimeError`.
- `test_no_matching_paid_inference_route_fails_safely` — an OpenAPI advertising
  only the top-up route yields a safe failure (no direct inference route).
- `test_unrelated_paid_route_not_selected` — a paid non-inference route (weather)
  is not selected; the agent fails safely.
- `test_cross_origin_route_metadata_rejected` — a paid route whose path is an
  absolute URL is rejected as cross-origin.
- `test_direct_flow_sends_no_payment_signature_discovery` — the discovery-driven
  direct flow sends no `PAYMENT-SIGNATURE` and no top-up.
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
- `test_agent_quickstart_no_longer_claims_single_challenge` — `docs/AGENT_QUICKSTART.md`
  no longer claims top-up is the only x402-challenge endpoint and now documents
  both `/v1/x402/topup` and `/v1/x402/chat/completions`.

**LIVE never executes during tests**: the env-key test asserts `RuntimeError`, and
the two live tests mock the SDK/transport so no real signature or on-chain
settlement occurs.

## 2. Demo output (captured, offline dry-run — DIRECT)

```
=== Clarity Agent SDK example — DIRECT paid inference (DRY RUN / NO FUNDS) ===
{
  "mode": "dry-run",
  "flow": "direct",
  "base_url": "https://example.invalid",
  "discovery": {"status": 200, "gateway": "healthy"},
  "payment_required": {
    "scheme": "exact",
    "network": "eip155:84532",
    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7E",
    "amount": "1000",
    "pay_to": "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f",
    "resource": "/v1/x402/chat/completions"
  },
  "payment_signed": false,
  "settlement_verified": false,
  "gateway_credit_verified": false,
  "inference_verified": false,
  "chat_status": null,
  "error": null,
  "dry_run_plan": {
    "note": "DRY RUN (DIRECT) - no funds moved and no valid signature produced. ...",
    "payer": "0xAGENT_PUBLIC_ADDRESS_DEMO",
    "would_sign_with": "official x402 SDK ExactEvmScheme (EIP-3009 transferWithAuthorization)",
    "retry": "POST /v1/x402/chat/completions with PAYMENT-SIGNATURE header",
    "on_success": "the 200 response carries the OpenAI-compatible completion directly; no skey, no gateway balance, no second request",
    "simulated_signature": "SIMULATED.eyJzaW11bGF0ZWQiOiB0cnVlLCAicmVzb3VyY2UiOiAiL3YxL3g0MDIvY2hhdC9jb21wbGV0aW9ucyIs ...}"
  }
}
```

The `PAYMENT-SIGNATURE` value is prefixed `SIMULATED.` and decodes to
`{"simulated": true, "resource": "/v1/x402/chat/completions", "network":
"eip155:84532", "asset": "0x036C…", "amount": "1000", "payTo": "0x42ad…", "payer":
"0xAGENT_PUBLIC_ADDRESS_DEMO", "note": "no real signing occurred"}` — explicitly
**not** a settleable signature.

## 3. Discovery algorithm (origin-only)

`ClarityAgent.run_direct()` performs, in order:

1. `GET {origin}/openapi.json` — the gateway's curated, public OpenAPI.
2. `discover_paid_routes(spec)` — keep POST operations that carry an
   `x-payment-info` block (price + `x402` protocol). Each becomes a normalized
   route dict (path, method, operation_id, summary, description, payment).
3. `select_direct_chat_route(routes)` validates every candidate:
   - method must be `POST`;
   - path must be a same-origin relative path (starts with `/`) — absolute URLs
     are rejected as cross-origin;
   - payment metadata must be internally consistent (amount present, `x402` in
     protocols);
   - description/summary must indicate chat/completion (or bare inference) and
     must **not** read as gateway credit / top-up / `skey`.
   If no candidate qualifies it raises `RuntimeError` listing each rejection
   reason. If several remain, the one explicitly naming chat/completion is
   preferred. The chosen route's path/method are then used for the unpaid
   request — **never supplied manually**.

## 4. Honesty labels

- The example output and README always mark dry-run vs live settlement.
- LIVE is fully implemented but **gated** behind `dry_run=False` **and**
  `X402_PAYER_KEY`; it refuses to run without a real funded key, and the key is
  scrubbed (`key = None`) immediately after building the signer. No real spend
  occurs in any test.
- The DIRECT flow never creates a gateway key, credits balance, or requires a
  prior top-up — matching the gateway's settlement isolation.

## 5. Files (no gateway production behavior changed in this pass)

```
examples/agent-sdk-example/clarity_agent.py
examples/agent-sdk-example/test_agent.py
examples/agent-sdk-example/README.md
examples/agent-sdk-example/EVIDENCE.md
docs/AGENT_QUICKSTART.md
```
