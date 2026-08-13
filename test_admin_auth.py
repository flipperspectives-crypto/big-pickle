import os

# Use an isolated DB so these security tests do not perturb shared test state.
os.environ.setdefault("GATEWAY_DB", "/tmp/gateway_admin_auth_test.db")

from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)

ADMIN = "X-Admin-Key"

# Endpoints that must be admin-protected (path, http method, sample json body).
ADMIN_ENDPOINTS = [
    ("/v1/keys", "post", {"name": "sample"}),
    ("/v1/credits", "post", {"key_id": "sample", "amount": 1.0}),
    ("/v1/admin/usage", "get", None),
]


def _call(path, method, body, headers):
    if method == "get":
        return client.get(path, headers=headers)
    return client.post(path, json=body, headers=headers)


def test_A_unconfigured_no_header_rejected(monkeypatch):
    # No GATEWAY_ADMIN_KEY and no header => every admin endpoint fails closed.
    monkeypatch.setattr(config.settings, "ADMIN_KEY", "")
    for path, method, body in ADMIN_ENDPOINTS:
        r = _call(path, method, body, {})
        assert r.status_code == 503, (path, r.status_code, r.text)


def test_B_unconfigured_legacy_literal_rejected(monkeypatch):
    # Even if a caller supplies the legacy literal, an unconfigured admin API
    # must never grant access.
    monkeypatch.setattr(config.settings, "ADMIN_KEY", "")
    for path, method, body in ADMIN_ENDPOINTS:
        r = _call(path, method, body, {ADMIN: "admin-change-me"})
        assert r.status_code == 503, (path, r.status_code, r.text)


def test_B2_legacy_literal_explicitly_rejected(monkeypatch):
    # Supplying "admin-change-me" via the environment must NOT enable admin.
    monkeypatch.setattr(config.settings, "ADMIN_KEY", "admin-change-me")
    r = client.post("/v1/keys", json={"name": "x"}, headers={ADMIN: "admin-change-me"})
    assert r.status_code == 503


def test_C_configured_wrong_or_missing_header_rejected(monkeypatch):
    monkeypatch.setattr(config.settings, "ADMIN_KEY", "strong-secret-xyz")
    # wrong header
    r1 = client.post("/v1/keys", json={"name": "x"}, headers={ADMIN: "wrong"})
    assert r1.status_code == 401, r1.text
    # missing header
    r2 = client.post("/v1/keys", json={"name": "x"})
    assert r2.status_code == 401, r2.text
    # wrong header on admin usage
    r3 = client.get("/v1/admin/usage", headers={ADMIN: "nope"})
    assert r3.status_code == 401, r3.text


def test_D_configured_correct_header_accepted(monkeypatch):
    monkeypatch.setattr(config.settings, "ADMIN_KEY", "strong-secret-xyz")
    r = client.post("/v1/keys", json={"name": "acme"}, headers={ADMIN: "strong-secret-xyz"})
    assert r.status_code == 200, r.text
    kid = r.json()["id"]
    r2 = client.post(
        "/v1/credits", json={"key_id": kid, "amount": 5.0},
        headers={ADMIN: "strong-secret-xyz"},
    )
    assert r2.status_code == 200, r2.text
    r3 = client.get("/v1/admin/usage", headers={ADMIN: "strong-secret-xyz"})
    assert r3.status_code == 200, r3.text


def test_E_single_helper_fail_closed_everywhere():
    import pathlib
    src = pathlib.Path(__file__).parent.joinpath("app", "main.py").read_text()
    # All three admin endpoints delegate to the single helper.
    assert src.count("require_admin(x_admin_key)") == 3, src
    assert src.count("require_admin(") >= 4
    # The insecure direct comparison was removed.
    assert "x_admin_key != settings.ADMIN_KEY" not in src


def test_F_non_admin_endpoints_unchanged(monkeypatch):
    # Public/customer paths are unaffected by the admin hardening.
    assert client.get("/health").status_code == 200
    m = client.get("/v1/models")
    assert m.status_code == 200
    assert "data" in m.json()
    # Customer skey auth still works end to end (signup explicitly enabled here
    # so the customer-auth path can be exercised; default is now fail-closed).
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", True)
    r = client.post("/v1/signup", json={"name": "cust-f"})
    assert r.status_code == 200, r.text
    skey = r.json()["skey"]
    u = client.get("/v1/usage", headers={"Authorization": f"Bearer {skey}"})
    assert u.status_code == 200, u.text
    # x402 top-up endpoint still reports its own configuration state.
    assert client.post("/v1/x402/topup").status_code in (501, 402, 200)
