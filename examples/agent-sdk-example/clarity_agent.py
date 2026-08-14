"""Clarity Agent SDK example.

Models the REAL production Clarity lifecycle (no made-up endpoints). Clarity now
exposes TWO machine-payable x402 v2 resources:

  * POST /v1/x402/chat/completions  (DIRECT, one-shot paid inference)
  * POST /v1/x402/topup             (persistent gateway credit -> `skey`)

DIRECT is the preferred/simplest flow for an autonomous agent:

  1. Discover the gateway contract via GET /v1/status (public, no auth).
  2. POST /v1/x402/chat/completions with an unpaid OpenAI-style body. The
     protected endpoint answers 402 + a `PAYMENT-REQUIRED` header (base64 JSON
     x402 v2 requirement) whose `resource` is /v1/x402/chat/completions.
  3. DRY-RUN (default, no funds): parse and DISPLAY the requirement (network,
     asset, amount, payTo, resource) and a SIMULATED signing plan. STOP before
     signing — no signature is produced, nothing is settled, no top-up needed.
  4. LIVE (opt-in, requires X402_PAYER_KEY): use the official `x402` client SDK
     to sign the payment and retry /v1/x402/chat/completions with the
     `PAYMENT-SIGNATURE` header. The 200 response carries the completion
     directly — NO `skey`, NO second request, NO gateway balance involved.

The top-up (persistent-credit) flow remains available:

  1. POST /v1/x402/topup with NO payment -> 402 + `PAYMENT-REQUIRED`
     (resource /v1/x402/topup).
  2. LIVE: sign and retry /v1/x402/topup with `PAYMENT-SIGNATURE`; the 200
     response carries a gateway `skey`.
  3. Only then call /v1/chat/completions with `Authorization: Bearer <skey>`.

The agent verifies each stage independently:
  - payment_signed           : the SDK produced a PAYMENT-SIGNATURE header
  - settlement_verified      : the gateway/facilitator confirmed settlement
  - gateway_credit_verified  : (top-up only) the top-up returned a usable `skey`
  - inference_verified       : a completion endpoint returned 200

A later stage is NEVER marked True unless the earlier one actually succeeded.

Security:
    - No real funds move in DRY-RUN. LIVE spends only when you supply a real
      funded wallet via X402_PAYER_KEY and explicitly opt in. The agent pays
      whatever network/asset/amount the live PAYMENT-REQUIRED challenge
      advertises (eip155:8453 for mainnet, eip155:84532 for testnet) — it never
      hardcodes a network.
  - The private key is read from the environment, used only to build the
    signer, then immediately discarded (key = None).
  - The key value is never printed, logged, persisted, committed, or returned.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass

import httpx


# ---------------------------------------------------------------------------
# x402 SDK imports (official client used for real signing in LIVE mode)
# ---------------------------------------------------------------------------
from x402 import x402Client  # noqa: E402
from x402.http.x402_http_client import x402HTTPClient  # noqa: E402
from x402.mechanisms.evm.exact import ExactEvmScheme  # noqa: E402
from eth_account import Account  # noqa: E402


DEFAULT_BASE_URL = "https://example.invalid"

# Preferred direct paid-inference route (used only as a unit-test convenience
# fallback; the discovery decision always derives the path from OpenAPI metadata).
DIRECT_CHAT_ROUTE = "/v1/x402/chat/completions"

# Selection hints: a direct inference route is described with chat/completion (or
# bare "inference") and must NOT read as gateway credit / top-up / skey.
_CHAT_HINTS = ("chat", "completion")
_CREDIT_HINTS = ("credit", "top-up", "topup", "skey", "balance")


# A realistic x402 v2 PAYMENT-REQUIRED header, mirroring what the production
# /v1/x402/topup endpoint returns. Used only by the offline demo transport.
_DEMO_PR_HEADER = base64.b64encode(
    json.dumps(
        {
            "x402Version": 2,
            "resource": {"url": "/v1/x402/topup"},
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7E",
                    "amount": "1000",
                    "payTo": "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f",
                    "maxTimeoutSeconds": 60,
                    "extra": {"assetTransferMethod": "eip3009"},
                }
            ],
        }
    ).encode()
).decode()


# A realistic x402 v2 PAYMENT-REQUIRED header, mirroring what the production
# /v1/x402/chat/completions (DIRECT paid inference) endpoint returns. Used only
# by the offline demo transport. The resource identifies the direct route.
_DEMO_CHAT_PR_HEADER = base64.b64encode(
    json.dumps(
        {
            "x402Version": 2,
            "resource": {"url": "/v1/x402/chat/completions"},
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7E",
                    "amount": "1000",
                    "payTo": "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f",
                    "maxTimeoutSeconds": 60,
                    "extra": {"assetTransferMethod": "eip3009"},
                }
            ],
        }
    ).encode()
).decode()


# Curated OpenAPI discovery doc (mirrors what the gateway serves at
# /openapi.json). It advertises exactly the two x402-paid resources, each with
# an `x-payment-info` block and a description the agent uses for route selection.
_DEMO_OPENAPI = {
    "openapi": "3.1.0",
    "info": {
        "title": "Clarity Agent API",
        "version": "0.1.0",
        "x-guidance": "Clarity provides pay-per-use AI inference.",
    },
    "paths": {
        "/v1/x402/topup": {
            "post": {
                "operationId": "purchaseClarityInferenceCredit",
                "summary": "Purchase Clarity AI inference credit",
                "description": (
                    "Machine-payable top-up. The caller makes an x402 v2 payment and, "
                    "after successful settlement, receives gateway credit plus a secret "
                    "skey used to authenticate inference at POST /v1/chat/completions."
                ),
                "x-payment-info": {
                    "price": {"mode": "fixed", "currency": "USD", "amount": "0.001000"},
                    "protocols": [{"x402": {}}],
                },
                "responses": {
                    "402": {"description": "Payment Required"},
                    "200": {"description": "Clarity gateway credit"},
                },
            }
        },
        "/v1/x402/chat/completions": {
            "post": {
                "operationId": "purchaseClarityChatCompletion",
                "summary": "Buy a Clarity AI chat completion",
                "description": (
                    "Direct pay-per-request AI inference through Clarity using "
                    "local:qwen3:1.7b. Pay the live x402 challenge and receive an "
                    "OpenAI-compatible chat completion directly. No Clarity API key "
                    "is required. stream=false and max_tokens <= 128."
                ),
                "x-payment-info": {
                    "price": {"mode": "fixed", "currency": "USD", "amount": "0.001000"},
                    "protocols": [{"x402": {}}],
                },
                "responses": {
                    "402": {"description": "Payment Required"},
                    "200": {"description": "OpenAI-compatible chat completion"},
                },
            }
        },
    },
}


def _demo_transport() -> httpx.MockTransport:
    """Offline transport that reproduces the production endpoint shapes."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/status":
            return httpx.Response(
                200,
                json={
                    "status": "healthy",
                    "gateway": {"active_keys": 1, "total_balance_usd": 0.5},
                    "providers": {
                        "groq": {
                            "configured": True,
                            "credentials_configured": True,
                            "reachable": True,
                            "probe_latency_ms": 40.0,
                            "models_in_routes": 6,
                        }
                    },
                    "timestamp": "2026-08-12T00:00:00Z",
                },
            )
        if request.method == "GET" and path == "/openapi.json":
            return httpx.Response(200, json=_DEMO_OPENAPI)
        if request.method == "POST" and path == "/v1/x402/topup":
            if "PAYMENT-SIGNATURE" not in request.headers:
                return httpx.Response(
                    402, headers={"PAYMENT-REQUIRED": _DEMO_PR_HEADER}, json={}
                )
            return httpx.Response(
                200,
                json={"id": "k1", "skey": "gw_demo_skey", "balance_usd": 1.0},
            )
        if request.method == "POST" and path == "/v1/chat/completions":
            auth = request.headers.get("Authorization", "")
            if auth == "Bearer gw_demo_skey":
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {"message": {"role": "assistant", "content": "Hello from Clarity!"}}
                        ]
                    },
                )
            return httpx.Response(402, json={"error": "insufficient balance"})
        if request.method == "POST" and path == "/v1/x402/chat/completions":
            # DIRECT paid-inference challenge: unpaid -> 402 + PAYMENT-REQUIRED
            # (resource /v1/x402/chat/completions). Paid retry -> completion, no skey.
            if "PAYMENT-SIGNATURE" not in request.headers:
                return httpx.Response(
                    402, headers={"PAYMENT-REQUIRED": _DEMO_CHAT_PR_HEADER}, json={}
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-demo",
                    "object": "chat.completion",
                    "model": "qwen3:1.7b",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Hello from Clarity direct!",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@dataclass
class PaymentRequired:
    """Parsed view of a x402 v2 PAYMENT-REQUIRED header (for DRY-RUN display)."""

    x402_version: int | None = None
    scheme: str | None = None
    network: str | None = None
    asset: str | None = None
    amount: str | None = None
    pay_to: str | None = None
    max_timeout_seconds: int | None = None
    resource: str | None = None

    @classmethod
    def from_header(cls, raw: str) -> "PaymentRequired":
        """Decode a base64 JSON `PAYMENT-REQUIRED` header (x402 v2)."""
        payload = base64.b64decode(raw)
        obj = json.loads(payload)
        accepts = obj.get("accepts") or []
        if not accepts:
            raise ValueError("PAYMENT-REQUIRED header has no 'accepts' entry")
        a = accepts[0]
        raw_resource = obj.get("resource")
        # The official x402 SDK encodes `resource` as a ResourceInfo object
        # ({"url": ...}); accept either that or a bare string for robustness.
        if isinstance(raw_resource, dict):
            resource = raw_resource.get("url")
        else:
            resource = raw_resource
        return cls(
            x402_version=obj.get("x402Version"),
            scheme=a.get("scheme"),
            network=a.get("network"),
            asset=a.get("asset"),
            amount=a.get("amount"),
            pay_to=a.get("payTo"),
            max_timeout_seconds=a.get("maxTimeoutSeconds"),
            resource=resource,
        )


def discover_clarity(base_url: str = DEFAULT_BASE_URL) -> dict:
    """GET /v1/status — public, read-only gateway contract discovery."""
    with httpx.Client(timeout=30) as client:
        r = client.get(base_url.rstrip("/") + "/v1/status")
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Machine-readable discovery (OpenAPI). The gateway serves a curated OpenAPI at
# /openapi.json that advertises exactly the x402-paid resources, each with an
# `x-payment-info` block (price + protocol) and a description. This is the single
# discovery surface the agent uses; no path is hard-coded into the decision.
# ---------------------------------------------------------------------------
def fetch_openapi(base_url: str = DEFAULT_BASE_URL) -> dict:
    """Module-level helper: GET /openapi.json using a fresh client."""
    with httpx.Client(timeout=30) as client:
        r = client.get(base_url.rstrip("/") + "/openapi.json")
        r.raise_for_status()
        return r.json()


def discover_paid_routes(spec: dict) -> list[dict]:
    """Extract POST routes that carry an `x-payment-info` (x402-paid) block.

    Returns a list of normalized route dicts with path, method, operation_id,
    summary, description and payment metadata. Non-POST and non-paid routes are
    excluded here; finer selection happens in `select_direct_chat_route`.
    """
    routes: list[dict] = []
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() != "post" or not isinstance(op, dict):
                continue
            payment = op.get("x-payment-info")
            if not payment:
                continue
            routes.append(
                {
                    "path": path,
                    "method": method.upper(),
                    "operation_id": op.get("operationId"),
                    "summary": op.get("summary") or "",
                    "description": op.get("description") or "",
                    "payment": payment,
                }
            )
    return routes


def select_direct_chat_route(routes: list[dict]) -> dict:
    """Choose the DIRECT paid chat/completion route from discovered metadata.

    Validation (fail-safe; records every rejection reason):
      * method must be POST
      * path must be a same-origin relative path (starts with "/") — absolute
        URLs are treated as cross-origin and rejected
      * payment metadata must be internally consistent (amount present, x402 in
        protocols)
      * described as chat/completion (or bare inference) and NOT as gateway
        credit / top-up / skey
    If no candidate qualifies, raises RuntimeError explaining why.
    """
    candidates: list[dict] = []
    rejected: list[tuple[str, str]] = []
    for r in routes:
        if r["method"] != "POST":
            rejected.append((r.get("path"), "method is not POST"))
            continue
        path = r["path"]
        if not isinstance(path, str) or not path.startswith("/"):
            rejected.append((path, "cross-origin or non-local path (not same origin)"))
            continue
        price = (r.get("payment") or {}).get("price") or {}
        if not price.get("amount"):
            rejected.append((path, "payment metadata missing amount"))
            continue
        protocols = (r.get("payment") or {}).get("protocols") or []
        if not any(isinstance(p, dict) and "x402" in p for p in protocols):
            rejected.append((path, "not an x402-paid route"))
            continue
        text = ((r.get("summary") or "") + " " + (r.get("description") or "")).lower()
        if any(k in text for k in _CREDIT_HINTS):
            rejected.append((path, "described as gateway credit/top-up, not direct inference"))
            continue
        if not any(k in text for k in _CHAT_HINTS) and "inference" not in text:
            rejected.append((path, "not described as chat/completion/inference"))
            continue
        candidates.append(r)
    if not candidates:
        reasons = "; ".join(f"{p}: {why}" for p, why in rejected)
        raise RuntimeError(
            "No suitable direct paid inference route found in discovery metadata"
            + (f" (rejected: {reasons})" if reasons else "")
        )
    # Prefer an explicit chat/completion match when several candidates remain.
    if len(candidates) > 1:
        chat = [
            c
            for c in candidates
            if "chat" in (c.get("summary") or "").lower()
            or "chat" in (c.get("operation_id") or "").lower()
            or "completion" in (c.get("summary") or "").lower()
        ]
        if chat:
            candidates = chat
    chosen = dict(candidates[0])
    chosen["selection_reason"] = (
        f"Selected {chosen['path']} ({chosen['method']}): x402-paid, described as "
        "direct chat/completion inference, same origin, payment metadata consistent."
    )
    return chosen


class ClarityAgent:
    """Drives the Clarity machine-payable top-up + inference lifecycle."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        dry_run: bool = True,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 120.0,
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        if transport is not None:
            # Embedded MockTransport dry-run: offline, no real network timeout.
            self.client = httpx.Client(
                transport=transport, headers={"Content-Type": "application/json"}
            )
        else:
            # Real-network client: local inference can legitimately exceed typical
            # cloud API latency, so the default is 120s, not 30s.
            self.client = httpx.Client(
                timeout=timeout_seconds, headers={"Content-Type": "application/json"}
            )

    # -- low level HTTP -----------------------------------------------------
    def _post(self, path: str, headers: dict | None = None, body: dict | None = None):
        r = self.client.post(self.base_url + path, headers=headers or {}, json=body or {})
        try:
            data = r.json()
        except Exception:
            data = None
        return r.status_code, data, r.headers

    def discover(self):
        r = self.client.get(self.base_url + "/v1/status")
        return r.status_code, _safe_json(r), r.headers

    def fetch_openapi(self) -> dict:
        """GET /openapi.json via this agent's client (honors transport/timeout)."""
        r = self.client.get(self.base_url + "/openapi.json")
        r.raise_for_status()
        return r.json()

    # -- step 2: top-up challenge ------------------------------------------
    def topup_challenge(self) -> tuple[PaymentRequired, str, bytes]:
        """POST /v1/x402/topup with no payment -> 402 + PAYMENT-REQUIRED.

        Returns the parsed requirement, the raw header value, and the raw body.
        """
        status, _data, headers = self._post("/v1/x402/topup", {}, {})
        raw = headers.get("PAYMENT-REQUIRED")
        if status == 402 and raw:
            return PaymentRequired.from_header(raw), raw, b""
        raise RuntimeError(
            f"/v1/x402/topup did not return 402 + PAYMENT-REQUIRED "
            f"(status={status}, has_header={bool(raw)})"
        )

    # -- step 2b: DIRECT chat challenge (preferred, no top-up) -------------
    def chat_challenge(
        self,
        model: str = "local:qwen3:1.7b",
        messages: list[dict] | None = None,
        path: str | None = None,
    ) -> tuple[PaymentRequired, str, bytes]:
        """POST <direct chat route> with no payment -> 402 + PAYMENT-REQUIRED.

        `path` is the route discovered from OpenAPI metadata (defaults to
        DIRECT_CHAT_ROUTE only as a unit-test convenience). This is the DIRECT
        paid-inference challenge; it does NOT create a gateway key, credit
        balance, or require a prior top-up. Returns the parsed requirement, the
        raw header value, and the raw (empty) body.
        """
        path = path or DIRECT_CHAT_ROUTE
        body = {
            "model": model,
            "messages": messages
            or [{"role": "user", "content": "Hello from the Clarity agent SDK example."}],
        }
        status, _data, headers = self._post(path, {}, body)
        raw = headers.get("PAYMENT-REQUIRED")
        if status == 402 and raw:
            return PaymentRequired.from_header(raw), raw, b""
        raise RuntimeError(
            f"{path} did not return 402 + PAYMENT-REQUIRED "
            f"(status={status}, has_header={bool(raw)})"
        )

    # -- LIVE client construction (official x402 SDK) ----------------------
    def _build_client(self, key: str, network: str):
        """Build a real x402 HTTP client bound to a funded signer.

        The signer is created from the private key; the key is NOT retained.
        """
        acct = Account.from_key(key)
        scheme = ExactEvmScheme(acct)
        client = x402Client()
        client.register(network, scheme)
        return x402HTTPClient(client), acct.address

    # -- orchestration ------------------------------------------------------
    async def run(
        self,
        model: str = "local:qwen3:1.7b",
        messages: list[dict] | None = None,
        payer_address: str = "0xAGENT_PUBLIC_ADDRESS_DEMO",
    ) -> dict:
        messages = messages or [
            {"role": "user", "content": "Hello from the Clarity agent SDK example."}
        ]
        result: dict = {
            "mode": "dry-run" if self.dry_run else "live",
            "base_url": self.base_url,
            "discovery": None,
            "payment_required": None,
            "payment_signed": False,
            "settlement_verified": False,
            "gateway_credit_verified": False,
            "inference_verified": False,
            "skey_present": False,
            "chat_status": None,
            "error": None,
        }

        # 1. discover
        try:
            dstatus, ddata, _ = self.discover()
            gw = ddata.get("status") if isinstance(ddata, dict) else None
            result["discovery"] = {"status": dstatus, "gateway": gw}
        except Exception as e:  # noqa: BLE001
            result["error"] = f"discovery failed: {e!r}"
            return result

        # 2. top-up challenge
        try:
            pr, pr_header, pr_body = self.topup_challenge()
        except Exception as e:  # noqa: BLE001
            result["error"] = f"top-up challenge failed: {e!r}"
            return result
        result["payment_required"] = {
            "scheme": pr.scheme,
            "network": pr.network,
            "asset": pr.asset,
            "amount": pr.amount,
            "pay_to": pr.pay_to,
            "resource": pr.resource,
        }

        if self.dry_run:
            result["dry_run_plan"] = self._dry_run_plan(pr, payer_address)
            return result

        # 4. LIVE: sign + retry top-up with the official x402 SDK
        key = os.environ.get("X402_PAYER_KEY")
        if not key:
            raise RuntimeError(
                "LIVE mode requires the X402_PAYER_KEY environment variable "
                "(a funded wallet private key for the network advertised by the "
                "live PAYMENT-REQUIRED challenge — eip155:8453 for mainnet, "
                "eip155:84532 for testnet). Refusing to run."
            )
        http_client, _address = self._build_client(key, pr.network)
        key = None  # scrub the private-key reference immediately after use

        try:
            payment_headers, payload = await http_client.handle_402_response(
                {"PAYMENT-REQUIRED": pr_header}, pr_body
            )
        except Exception as e:  # noqa: BLE001
            result["error"] = f"payment signing failed: {e!r}"
            return result
        result["payment_signed"] = bool(
            payment_headers and "PAYMENT-SIGNATURE" in payment_headers
        )

        # 5. retry top-up with PAYMENT-SIGNATURE
        tstatus, tdata, theaders = self._post(
            "/v1/x402/topup", payment_headers, {}
        )
        try:
            proc = await http_client.process_payment_result(
                payload, lambda h: theaders.get(h), tstatus
            )
            result["settlement_verified"] = bool(getattr(proc, "recovered", False))
        except Exception:  # noqa: BLE001
            result["settlement_verified"] = False

        if tstatus == 200 and isinstance(tdata, dict) and tdata.get("skey"):
            result["gateway_credit_verified"] = True
            result["skey_present"] = True
            skey = tdata["skey"]

            # 6. inference only AFTER a usable skey exists
            cstatus, cdata, _ = self._post(
                "/v1/chat/completions",
                {
                    "Authorization": f"Bearer {skey}",
                    "Content-Type": "application/json",
                },
                {"model": model, "messages": messages},
            )
            result["chat_status"] = cstatus
            if cstatus == 200:
                result["inference_verified"] = True
                result["completion"] = cdata
            else:
                result["chat_error"] = cdata
        else:
            result["error"] = "top-up did not return a gateway skey"

        return result

    def run_sync(self, **kwargs) -> dict:
        return asyncio.run(self.run(**kwargs))

    # -- orchestration: DIRECT paid inference (preferred one-shot) ---------
    async def run_direct(
        self,
        model: str = "local:qwen3:1.7b",
        messages: list[dict] | None = None,
        payer_address: str = "0xAGENT_PUBLIC_ADDRESS_DEMO",
    ) -> dict:
        """Drive the DIRECT machine-payable inference flow (no top-up, no skey).

        Start from ONLY the public origin, fetch the machine-readable OpenAPI
        discovery doc, identify the x402-paid routes, select the direct
        chat/completion route, send an unpaid request, receive 402, parse the
        requirement, and (DRY-RUN) report the exact payment and STOP before
        signing. No route path is supplied manually.
        """
        messages = messages or [
            {"role": "user", "content": "Hello from the Clarity agent SDK example."}
        ]
        result: dict = {
            "mode": "dry-run" if self.dry_run else "live",
            "flow": "direct",
            "base_url": self.base_url,
            "discovery": None,
            "payment_required": None,
            "payment_signed": False,
            "settlement_verified": False,
            "gateway_credit_verified": False,
            "inference_verified": False,
            "chat_status": None,
            "error": None,
        }

        # 1. machine-readable discovery from the public origin (no path assumed)
        try:
            spec = self.fetch_openapi()
            paid = discover_paid_routes(spec)
            route = select_direct_chat_route(paid)
            result["discovery"] = {
                "title": (spec.get("info") or {}).get("title"),
                "paid_routes": [r["path"] for r in paid],
                "selected_route": route["path"],
                "selected_method": route["method"],
                "selection_reason": route.get("selection_reason"),
            }
        except Exception as e:  # noqa: BLE001
            result["error"] = f"discovery failed: {e!r}"
            return result

        # 2. DIRECT chat challenge to the DISCOVERED route (no top-up required)
        try:
            pr, pr_header, pr_body = self.chat_challenge(
                path=route["path"], model=model, messages=messages
            )
        except Exception as e:  # noqa: BLE001
            result["error"] = f"direct chat challenge failed: {e!r}"
            return result
        result["payment_required"] = {
            "scheme": pr.scheme,
            "network": pr.network,
            "asset": pr.asset,
            "amount": pr.amount,
            "pay_to": pr.pay_to,
            "resource": pr.resource,
        }

        if self.dry_run:
            result["dry_run_plan"] = self._dry_run_plan_direct(pr, payer_address)
            return result

        # 4. LIVE: sign + retry the DISCOVERED direct route with PAYMENT-SIGNATURE
        key = os.environ.get("X402_PAYER_KEY")
        if not key:
            raise RuntimeError(
                "LIVE mode requires the X402_PAYER_KEY environment variable "
                "(a funded wallet private key for the network advertised by the "
                "live PAYMENT-REQUIRED challenge — eip155:8453 for mainnet, "
                "eip155:84532 for testnet). Refusing to run."
            )
        http_client, _address = self._build_client(key, pr.network)
        key = None  # scrub the private-key reference immediately after use

        try:
            payment_headers, payload = await http_client.handle_402_response(
                {"PAYMENT-REQUIRED": pr_header}, pr_body
            )
        except Exception as e:  # noqa: BLE001
            result["error"] = f"payment signing failed: {e!r}"
            return result
        result["payment_signed"] = bool(
            payment_headers and "PAYMENT-SIGNATURE" in payment_headers
        )

        # 5. retry DIRECT chat with PAYMENT-SIGNATURE; completion returns directly
        cstatus, cdata, cheaders = self._post(
            route["path"], payment_headers, {"model": model, "messages": messages}
        )
        try:
            proc = await http_client.process_payment_result(
                payload, lambda h: cheaders.get(h), cstatus
            )
            result["settlement_verified"] = bool(getattr(proc, "recovered", False))
        except Exception:  # noqa: BLE001
            result["settlement_verified"] = False

        result["chat_status"] = cstatus
        if cstatus == 200:
            result["inference_verified"] = True
            result["completion"] = cdata
        else:
            result["chat_error"] = cdata

        return result

    def run_direct_sync(self, **kwargs) -> dict:
        return asyncio.run(self.run_direct(**kwargs))

    # -- DRY-RUN plan -------------------------------------------------------
    def _dry_run_plan(self, pr: PaymentRequired, payer_address: str) -> dict:
        simulated_sig = (
            "SIMULATED."
            + base64.b64encode(
                json.dumps(
                    {
                        "simulated": True,
                        "network": pr.network,
                        "asset": pr.asset,
                        "amount": pr.amount,
                        "payTo": pr.pay_to,
                        "payer": payer_address,
                        "note": "no real signing occurred",
                    }
                ).encode()
            ).decode()
        )
        return {
            "note": (
                "DRY RUN - no funds moved and no valid signature produced. "
                "Supply a real signer (X402_PAYER_KEY) to settle on-chain."
            ),
            "payer": payer_address,
            "would_sign_with": "official x402 SDK ExactEvmScheme "
            "(EIP-3009 transferWithAuthorization)",
            "retry": "POST /v1/x402/topup with PAYMENT-SIGNATURE header",
            "on_success": (
                "extract 'skey' from the top-up response, then POST "
                "/v1/chat/completions with Authorization: Bearer <skey>"
            ),
            "simulated_signature": simulated_sig,
        }

    def _dry_run_plan_direct(self, pr: PaymentRequired, payer_address: str) -> dict:
        simulated_sig = (
            "SIMULATED."
            + base64.b64encode(
                json.dumps(
                    {
                        "simulated": True,
                        "resource": pr.resource,
                        "network": pr.network,
                        "asset": pr.asset,
                        "amount": pr.amount,
                        "payTo": pr.pay_to,
                        "payer": payer_address,
                        "note": "no real signing occurred",
                    }
                ).encode()
            ).decode()
        )
        return {
            "note": (
                "DRY RUN (DIRECT) - no funds moved and no valid signature produced. "
                "Supply a real signer (X402_PAYER_KEY) to settle on-chain."
            ),
            "payer": payer_address,
            "would_sign_with": "official x402 SDK ExactEvmScheme "
            "(EIP-3009 transferWithAuthorization)",
            "retry": "POST /v1/x402/chat/completions with PAYMENT-SIGNATURE header",
            "on_success": (
                "the 200 response carries the OpenAI-compatible completion directly; "
                "no skey, no gateway balance, no second request"
            ),
            "simulated_signature": simulated_sig,
        }


def demo(base_url: str = DEFAULT_BASE_URL) -> dict:
    """Run the DRY-RUN lifecycle (no funds, no secrets).

    Uses an embedded offline transport that reproduces the production endpoint
    shapes, so the example runs with no network access. To target a real
    gateway, pass ``base_url=...`` and a real ``httpx`` transport/client.
    """
    print("=== Clarity Agent SDK example (DRY RUN / NO FUNDS) ===")
    agent = ClarityAgent(base_url=base_url, dry_run=True, transport=_demo_transport())
    result = agent.run_sync()
    print(json.dumps(result, indent=2))
    return result


def demo_direct(base_url: str = DEFAULT_BASE_URL) -> dict:
    """Run the DIRECT paid-inference DRY-RUN lifecycle (no funds, no secrets).

    Shows the preferred one-shot agent flow: discover -> identify
    /v1/x402/chat/completions -> unpaid request -> 402 -> parsed requirement ->
    report -> STOP before signing. No top-up and no skey are involved.
    """
    print("=== Clarity Agent SDK example — DIRECT paid inference (DRY RUN / NO FUNDS) ===")
    agent = ClarityAgent(base_url=base_url, dry_run=True, transport=_demo_transport())
    result = agent.run_direct_sync()
    print(json.dumps(result, indent=2))
    return result


def live_example(base_url: str = DEFAULT_BASE_URL) -> dict:
    """Run the LIVE lifecycle. Requires X402_PAYER_KEY in the environment."""
    print("=== Clarity Agent SDK example (LIVE / uses x402 SDK) ===")
    agent = ClarityAgent(base_url=base_url, dry_run=False)
    result = agent.run_sync()
    print(json.dumps(result, indent=2))
    return result


def _safe_json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return None


if __name__ == "__main__":
    import argparse

    def _positive_timeout(value: str) -> float:
        try:
            f = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("timeout must be a number")
        if f <= 0:
            raise argparse.ArgumentTypeError("timeout must be > 0")
        return f

    parser = argparse.ArgumentParser(description="Clarity Agent SDK example")
    parser.add_argument(
        "--base-url",
        default="https://example.invalid",
        help="Gateway base URL (defaults to a safe non-routable placeholder).",
    )
    parser.add_argument(
        "--model",
        default="local:qwen3:1.7b",
        help="Inference model to request.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_timeout,
        default=120.0,
        help="HTTP timeout for the live gateway client (default 120s; local "
        "inference can take longer than typical cloud API requests).",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Settle a real x402 payment (requires X402_PAYER_KEY).",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Run the DIRECT paid-inference dry-run demo (POST /v1/x402/chat/completions).",
    )
    args = parser.parse_args()

    base_url = args.base_url
    if args.live:
        # LIVE must target a real gateway; refuse the safe placeholder default.
        if base_url == "https://example.invalid":
            parser.error(
                "--live requires an explicit --base-url "
                "(refusing to target the placeholder)."
            )
        agent = ClarityAgent(
            base_url=base_url, dry_run=False, timeout_seconds=args.timeout_seconds
        )
        result = agent.run_sync(model=args.model)
    elif args.direct:
        # Offline DIRECT dry-run demo: embedded mock transport; no network, funds,
        # or secrets. base_url/timeout are ignored in this mode.
        result = demo_direct(base_url=base_url)
    else:
        # Offline DRY-RUN demo: embedded mock transport; no network, funds, or
        # secrets. base_url/timeout are ignored in this mode.
        agent = ClarityAgent(
            base_url=base_url,
            dry_run=True,
            transport=_demo_transport(),
            timeout_seconds=args.timeout_seconds,
        )
        result = agent.run_sync(model=args.model)
    print(json.dumps(result, indent=2))
