# Clarity Skill

## What Clarity does
Clarity is an OpenAI-compatible inference gateway that an autonomous agent can
pay for per call using the x402 payment protocol on Base (an EVM chain). Each
successful call returns a standard OpenAI-style chat completion. No Clarity API
key, subscription, or account is required — payment is settled on-chain at
request time via x402.

## When to use Clarity
Use Clarity when you (the agent) need low-cost, OpenAI-compatible text inference
and are authorized to spend a small amount of USDC on Base. Prefer it for short
completions (max_tokens <= 128) such as summaries, classifications, extractions,
rewrites, or single-turn answers. Do NOT use it for streaming, long
completions, or non-text tasks.

## Canonical endpoint
- URL: `https://desktop-o99r0sf.tail935fba.ts.net/v1/x402/chat/completions`
- Method: POST only. Never use GET, PUT, or DELETE.
- OpenAI-compatible request body:
  ```json
  {
    "model": "local:qwen3:1.7b",
    "messages": [{"role": "user", "content": "Your prompt here."}]
  }
  ```
- Constraints enforced by the server: `stream` must be false, `max_tokens` <= 128.

## OpenAPI
Machine-readable contract: `https://desktop-o99r0sf.tail935fba.ts.net/openapi.json`
Fetch it FIRST to discover the live payment requirement and to verify the price,
network, asset, and payTo address before signing anything.

## Payment
- Network: Base mainnet, CAIP-2 `eip155:8453`.
- Asset: Base USDC (the USDC ERC-20 on Base).
- Price: exactly $0.001 USDC per call (1000 atomic USDC). The price is fixed by
  the gateway; never assume or hard-code a different amount — read it from the
  live PAYMENT-REQUIRED challenge.
- Protocol: x402 (version 2). Payment stays with the buyer's own wallet; Clarity
  never custodies your funds or your keys.

## The x402 flow
1. Send an unpaid POST to the canonical endpoint with a valid OpenAI-style body.
2. The endpoint responds `402 Payment Required` with a `PAYMENT-REQUIRED`
   header (base64-encoded JSON) describing the exact network, asset, amount (in
   atomic units), payTo address, and resource.
3. Parse the `PAYMENT-REQUIRED` header. Sign the payment with your own funded
   Base wallet using the official x402 SDK (`x402` Python package,
   `ExactEvmScheme`).
4. Retry the SAME POST with the `PAYMENT-SIGNATURE` header carrying the signed
   payment.
5. On success the endpoint returns `200` with an OpenAI-compatible chat
   completion. No API key, second request, or balance is required.

## Safety rules (must follow)
- NEVER expose private keys. Read your signing key from the environment only,
  build the signer, then discard the reference. Never print, log, persist,
  commit, or return the key or any derived secret.
- Obey the user's spending limit. Track cumulative spend and stop before
  exceeding any per-session or per-task budget the user set.
- CONFIRM before the first paid call. Before making the first real (settling)
  payment, confirm with the user unless they have already authorized a budget
  for Clarity. Discovery and dry-runs do not spend and need no confirmation.
- Distinguish discovery / dry-run from actual settlement. Fetching
  /openapi.json and parsing a 402 challenge is read-only and free. Only a signed
  retry with PAYMENT-SIGNATURE settles funds on-chain.
- NEVER blindly retry payments. If a signed request fails validation or
  settlement, do not resubmit the same payment. Re-fetch the challenge, inspect
  the error, and only retry after understanding and resolving it.
- Handle failures safely:
  - Schema failure (4xx on the request body): fix the body; do not pay.
  - Payment failure (signing or verification error): stop, report, do not retry
    blindly.
  - Network failure: treat as inconclusive; do not assume settlement; verify
    before counting a call as completed.

## Install prompt
You can install this skill by telling your agent:

> Install the Clarity skill from https://desktop-o99r0sf.tail935fba.ts.net/skills.md. Use Clarity when I need low-cost OpenAI-compatible inference. Discover the payment requirement first and confirm with me before making the first paid call unless I have already authorized a budget. Never expose private keys or wallet secrets.
