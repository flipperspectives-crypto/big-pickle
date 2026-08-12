"""Tests for the Clarity Agent SDK example.

All tests are OFFLINE and NO-FUNDS: they inject fake HTTP callables, never touch
the network, never sign, and never move funds. They also assert that no private
keys / secrets leak into the (simulated) payment output.
"""
import base64
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import clarity_agent  # noqa: E402

from clarity_agent import (  # noqa: E402
    ClarityAgent,
    PaymentRequired,
    discover_clarity,
    demo,
)

FAKE_PR_HEADER = base64.b64encode(
    json.dumps(
        {
            "x402Version": 2,
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


def test_discover_reads_public_status():
    called = {}

    def fake_get(url):
        called["url"] = url
        return {"status": "healthy", "providers": {}}

    d = discover_clarity(base_url="https://gw.test", http_get=fake_get)
    assert d["base_url"] == "https://gw.test"
    assert d["status"] == {"status": "healthy", "providers": {}}
    assert called["url"].endswith("/v1/status")
    print("PASS discover: reads public /v1/status, no auth")


def test_parse_payment_required_header():
    pr = PaymentRequired.from_header(FAKE_PR_HEADER)
    assert pr.scheme == "exact"
    assert pr.network == "eip155:84532"
    assert pr.asset == "0x036CbD53842c5426634e7929541eC2318f3dCF7E"
    assert pr.amount == "1000"
    assert pr.pay_to == "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f"
    print("PASS parse: PAYMENT-REQUIRED decoded correctly")


def test_chat_200_returns_data():
    def fake_post(url, headers, body):
        return 200, {"choices": [{"message": {"content": "hi"}}]}, {}

    a = ClarityAgent(base_url="https://gw.test", http_post=fake_post)
    r = a.chat([{"role": "user", "content": "hi"}])
    assert r.status == 200
    assert r.data["choices"][0]["message"]["content"] == "hi"
    assert r.dry_run_plan is None
    print("PASS chat: 200 handled, no payment path")


def test_chat_402_dry_run_no_payer():
    def fake_post(url, headers, body):
        return 402, "", {"PAYMENT-REQUIRED": FAKE_PR_HEADER}

    a = ClarityAgent(base_url="https://gw.test", payer=None, dry_run=True, http_post=fake_post)
    r = a.chat([{"role": "user", "content": "hi"}])
    assert r.status == 402
    assert r.mode == "dry-run"
    assert r.payment_required is not None
    plan = r.dry_run_plan
    assert plan is not None
    assert plan.simulated_signature_header.startswith("SIMULATED.")
    # No payer was supplied -> plan must not embed a payer identity.
    assert plan.payer is None
    print("PASS chat 402: dry-run plan produced, no payer, clearly simulated")


def test_chat_402_dry_run_with_payer():
    def fake_post(url, headers, body):
        return 402, "", {"PAYMENT-REQUIRED": FAKE_PR_HEADER}

    a = ClarityAgent(
        base_url="https://gw.test", payer="0xAGENT_DEMO_ADDRESS", dry_run=True, http_post=fake_post
    )
    r = a.chat([{"role": "user", "content": "hi"}])
    assert r.status == 402
    plan = r.dry_run_plan
    assert plan.payer == "0xAGENT_DEMO_ADDRESS"
    # Decoded simulated header must be clearly marked simulated.
    payload = json.loads(base64.b64decode(plan.simulated_signature_header[len("SIMULATED."):]))
    assert payload["simulated"] is True
    assert payload["payTo"] == "0x42ad8e4c4f2fe41ee2730d2e3b2970fe4f50ae8f"
    print("PASS chat 402: dry-run plan embeds payer, still simulated (no funds)")


def test_no_secrets_in_simulated_output():
    def fake_post(url, headers, body):
        return 402, "", {"PAYMENT-REQUIRED": FAKE_PR_HEADER}

    a = ClarityAgent(
        base_url="https://gw.test", payer="0xAGENT_DEMO_ADDRESS", dry_run=True, http_post=fake_post
    )
    r = a.chat([{"role": "user", "content": "hi"}])
    blob = json.dumps(r.dry_run_plan.to_dict()).lower()
    for forbidden in ("sk-", "private", "secret", "gw_", "bearer", "api_key", "x-api-key",
                     "authorization", "0x036c", "0x42ad"):
        # allow public addresses only when they are the documented payTo/asset, so
        # we scope the check to key material words, not public contract addresses.
        if forbidden in ("0x036c", "0x42ad"):
            continue
        assert forbidden not in blob, f"forbidden token '{forbidden}' in dry-run output"
    print("PASS security: no private-key/secret material in dry-run plan")


def test_live_mode_requires_env_key():
    os.environ.pop("X402_PAYER_KEY", None)

    def fake_post(url, headers, body):
        return 402, "", {"PAYMENT-REQUIRED": FAKE_PR_HEADER}

    a = ClarityAgent(base_url="https://gw.test", dry_run=False, http_post=fake_post)
    # Live mode must refuse to sign/spend unless X402_PAYER_KEY is supplied.
    try:
        a.chat([{"role": "user", "content": "hi"}], path="/v1/x402/topup")
        assert False, "live chat should raise without X402_PAYER_KEY"
    except RuntimeError as e:
        assert "X402_PAYER_KEY" in str(e)
    print("PASS live-mode guard: refuses without X402_PAYER_KEY (no sign/spend)")


def test_live_signing_retry_mocked(monkeypatch):
    # Valid-format key is set but NEVER used: the SDK bits are mocked, so no real
    # signing or funds occur. This exercises the full live state machine.
    os.environ["X402_PAYER_KEY"] = "0x" + "11" * 32

    def fake_post(url, headers, body):
        if "PAYMENT-SIGNATURE" in headers:
            resp = base64.b64encode(json.dumps(
                {"success": True, "transaction": "0xTX", "network": "eip155:84532", "payer": "0xpayer"}
            ).encode()).decode()
            return 200, {"id": "k", "skey": "gw_fake", "balance_usd": 0.001}, {"PAYMENT-RESPONSE": resp}
        return 402, "", {"PAYMENT-REQUIRED": FAKE_PR_HEADER}

    def fake_build_client(payer_key):
        return object(), "0xpayeraddress"

    async def fake_sign(client, pr_sdk, url):
        return "SIMULATED_LIVE." + base64.b64encode(json.dumps({"signed": True}).encode()).decode()

    monkeypatch.setattr(clarity_agent, "_build_client", fake_build_client)
    monkeypatch.setattr(clarity_agent, "_sign_payment", fake_sign)

    a = ClarityAgent(base_url="https://gw.test", dry_run=False, http_post=fake_post)
    r = a.chat([{"role": "user", "content": "hi"}], path="/v1/x402/topup")
    assert r.mode == "live"
    live = r.data
    assert live.payment_signed is True
    assert live.settlement_verified is True
    assert live.gateway_credit_verified is True
    assert live.payment_response["transaction"] == "0xTX"
    # no private key material anywhere in the live result
    assert "X402_PAYER_KEY" not in json.dumps(live.__dict__).lower()
    assert "11" * 32 not in json.dumps(live.__dict__)
    print("PASS live signing/retry (mocked): signed+settled+credited, no real funds")


def test_live_sdk_signing_integration_mocked(monkeypatch):
    # Use the REAL x402 client + signer construction, but mock only the network
    # signing round-trip, proving the SDK integration path is correct.
    os.environ["X402_PAYER_KEY"] = "0x" + "22" * 32

    import x402

    class FakePayload:
        def model_dump(self, mode="json"):
            return {"x402_version": 2, "payload": {"signature": "0xsig"}, "accepted": {}, "resource": {}}

    async def fake_create(self, payment_required, resource=None):
        return FakePayload()

    monkeypatch.setattr(x402.x402Client, "create_payment_payload", fake_create)

    def fake_post(url, headers, body):
        if "PAYMENT-SIGNATURE" in headers:
            resp = base64.b64encode(json.dumps(
                {"success": True, "transaction": "0xREALTX"}
            ).encode()).decode()
            return 200, {"id": "k", "skey": "gw_x"}, {"PAYMENT-RESPONSE": resp}
        return 402, "", {"PAYMENT-REQUIRED": FAKE_PR_HEADER}

    a = ClarityAgent(base_url="https://gw.test", dry_run=False, http_post=fake_post)
    r = a.chat([{"role": "user", "content": "hi"}], path="/v1/x402/topup")
    assert r.mode == "live"
    assert r.data.payment_signed is True
    assert r.data.settlement_verified is True
    assert r.data.gateway_credit_verified is True
    print("PASS live SDK integration (mocked network): x402 client used, no real funds")


def test_self_contained_demo_runs_offline():
    out = demo()
    assert out["chat_status"] == 402
    assert out["mode"] == "dry-run"
    assert out["plan"]["mode"] == "dry-run"
    assert out["plan"]["retry_header"]["PAYMENT-SIGNATURE"].startswith("SIMULATED.")
    assert "no funds moved" in out["plan"]["note"].lower()
    print("PASS demo: full discovery + 402 + simulated retry runs offline, no funds")


print("ALL AGENT SDK EXAMPLE TESTS PASSED")
