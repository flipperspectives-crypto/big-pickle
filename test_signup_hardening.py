import os

# Isolated DB so these security tests do not perturb shared test state.
os.environ.setdefault("GATEWAY_DB", "/tmp/gateway_signup_hardening_test.db")

from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)


def test_A_default_disabled_returns_403(monkeypatch):
    # No GATEWAY_PUBLIC_SIGNUP_ENABLED configured -> fail closed.
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", False)
    r = client.post("/v1/signup", json={"name": "x"})
    assert r.status_code == 403, (r.status_code, r.text)
    assert "public signup disabled" in r.text


def test_B_explicit_false_returns_403(monkeypatch):
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", False)
    r = client.post("/v1/signup", json={"name": "x"})
    assert r.status_code == 403, (r.status_code, r.text)


def test_C_disabled_creates_no_key(monkeypatch):
    # Disabled signup must produce ZERO database mutation (no key row created).
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", False)
    monkeypatch.setattr(config.settings, "ADMIN_KEY", "strong-secret-xyz")
    before = len(
        client.get("/v1/admin/usage", headers={"X-Admin-Key": "strong-secret-xyz"}).json()["keys"]
    )
    r = client.post("/v1/signup", json={"name": "no-create-attempt"})
    assert r.status_code == 403
    after = len(
        client.get("/v1/admin/usage", headers={"X-Admin-Key": "strong-secret-xyz"}).json()["keys"]
    )
    assert after == before, "disabled signup must not create a key row"


def test_D_enabled_returns_200_and_skey(monkeypatch):
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", True)
    r = client.post("/v1/signup", json={"name": "enabled-user"})
    assert r.status_code == 200, (r.status_code, r.text)
    body = r.json()
    assert body["skey"]
    assert body["status"] == "pending_topup"
    assert "id" in body


def test_E_blank_name_enabled_returns_400(monkeypatch):
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", True)
    r = client.post("/v1/signup", json={"name": "   "})
    assert r.status_code == 400, (r.status_code, r.text)


def test_F_admin_keys_still_requires_hardened_auth(monkeypatch):
    monkeypatch.setattr(config.settings, "ADMIN_KEY", "strong-secret-xyz")
    # missing admin header -> 401 (hardened admin auth preserved)
    r1 = client.post("/v1/keys", json={"name": "x"})
    assert r1.status_code == 401, (r1.status_code, r1.text)
    # correct admin header -> 200
    r2 = client.post("/v1/keys", json={"name": "x"}, headers={"X-Admin-Key": "strong-secret-xyz"})
    assert r2.status_code == 200, (r2.status_code, r2.text)


def test_G_x402_topup_unchanged_by_signup_setting(monkeypatch):
    # /v1/x402/topup must behave identically regardless of the signup setting.
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", False)
    s1 = client.post("/v1/x402/topup").status_code
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", True)
    s2 = client.post("/v1/x402/topup").status_code
    assert s1 == s2
    assert s1 in (501, 402, 200)


def test_H_customer_auth_works_via_admin_provisioned_key(monkeypatch):
    # Existing customer skey auth still works when a key is provisioned through
    # the allowed admin path.
    monkeypatch.setattr(config.settings, "ADMIN_KEY", "strong-secret-xyz")
    k = client.post(
        "/v1/keys", json={"name": "cust-h"}, headers={"X-Admin-Key": "strong-secret-xyz"}
    ).json()
    skey = k["skey"]
    r = client.get("/v1/usage", headers={"Authorization": f"Bearer {skey}"})
    assert r.status_code == 200, (r.status_code, r.text)
