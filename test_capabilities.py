import os
import re

# Isolated DB so these security tests do not perturb shared test state.
os.environ.setdefault("GATEWAY_DB", "/tmp/gateway_capabilities_test.db")

from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)


def test_capabilities_default_signup_disabled(monkeypatch):
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", False)
    monkeypatch.setattr(config.settings, "X402_PAYTO", "")
    r = client.get("/v1/capabilities")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["public_signup_enabled"] is False
    assert body["x402_enabled"] is False


def test_capabilities_explicit_true(monkeypatch):
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", True)
    r = client.get("/v1/capabilities")
    assert r.json()["public_signup_enabled"] is True


def test_capabilities_x402_reflects_state(monkeypatch):
    monkeypatch.setattr(config.settings, "X402_PAYTO", "")
    assert client.get("/v1/capabilities").json()["x402_enabled"] is False
    monkeypatch.setattr(config.settings, "X402_PAYTO", "0xPublicWalletAddress")
    assert client.get("/v1/capabilities").json()["x402_enabled"] is True


def test_capabilities_no_secrets(monkeypatch):
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", True)
    monkeypatch.setattr(config.settings, "X402_PAYTO", "0xabc")
    body = client.get("/v1/capabilities").json()
    assert set(body.keys()) == {"public_signup_enabled", "x402_enabled"}
    lowered = {k.lower() for k in body.keys()}
    for bad in ("admin", "key", "secret", "payto", "wallet", "db", "env",
                "url", "path", "stripe", "hf", "ollama", "password"):
        assert bad not in lowered


def test_capabilities_no_store_headers():
    r = client.get("/v1/capabilities")
    assert "no-store" in r.headers.get("Cache-Control", "").lower()


def test_ui_fails_closed_when_capabilities_missing():
    # Frontend must route a failed/missing capability fetch to applyCapabilities(null).
    import pathlib
    src = pathlib.Path(__file__).parent.joinpath("static", "app.js").read_text()
    assert ".catch(" in src and "applyCapabilities(null)" in src
    assert "signup-name" in src and "signup-btn" in src


def test_ui_signup_controls_unavailable_when_disabled():
    import pathlib
    src = pathlib.Path(__file__).parent.joinpath("static", "app.js").read_text()
    # applyCapabilities must disable the controls when not enabled.
    assert 'el("signup-name").disabled = !enabled' in src
    assert "signupBtn.disabled = !enabled" in src


def test_ui_signup_controls_available_when_enabled():
    import pathlib
    src = pathlib.Path(__file__).parent.joinpath("static", "app.js").read_text()
    # When enabled, the status is cleared/hidden and controls are usable.
    assert 'statusEl.hidden = true' in src


def test_signup_backend_still_fail_closed(monkeypatch):
    # Frontend is presentation only; the backend remains independently fail-closed.
    monkeypatch.setattr(config.settings, "PUBLIC_SIGNUP_ENABLED", False)
    r = client.post("/v1/signup", json={"name": "x"})
    assert r.status_code == 403, (r.status_code, r.text)


def test_x402_behavior_unchanged_by_capabilities():
    r = client.post("/v1/x402/topup").status_code
    assert r in (501, 402, 200)


def _html():
    import pathlib
    return pathlib.Path(__file__).parent.joinpath("static", "index.html").read_text()


def test_html_ships_signup_name_disabled():
    import re
    html = _html()
    m = re.search(r'<input[^>]*id="signup-name"[^>]*>', html)
    assert m, "signup-name input not found"
    assert "disabled" in m.group(0), "signup-name must ship disabled at parse time"


def test_html_ships_signup_btn_disabled():
    import re
    html = _html()
    m = re.search(r'<button[^>]*id="signup-btn"[^>]*>', html)
    assert m, "signup-btn not found"
    assert "disabled" in m.group(0), "signup-btn must ship disabled at parse time"


def test_html_ships_status_visible_with_checking_text():
    import re
    html = _html()
    m = re.search(r'<div[^>]*id="signup-status"[^>]*>(.*?)</div>', html, re.S)
    assert m, "signup-status div not found"
    assert "hidden" not in m.group(0), "signup-status must be visible by default"
    assert "Checking signup availability" in m.group(1)


def test_only_explicit_positive_capability_enables():
    import pathlib
    src = pathlib.Path(__file__).parent.joinpath("static", "app.js").read_text()
    # Controls are disabled in the raw HTML; applyCapabilities only re-enables
    # them when public_signup_enabled is explicitly true.
    assert 'el("signup-name").disabled = !enabled' in src
    assert "signupBtn.disabled = !enabled" in src
    html = _html()
    assert 'id="signup-name"' in html and "disabled" in re.search(r'<input[^>]*id="signup-name"[^>]*>', html).group(0)
    assert "disabled" in re.search(r'<button[^>]*id="signup-btn"[^>]*>', html).group(0)


def test_null_or_error_leaves_controls_disabled():
    # A missing/hanging/failed capability fetch routes to applyCapabilities(null)
    # which keeps controls disabled (covered by app.js source + HTML default).
    import pathlib
    src = pathlib.Path(__file__).parent.joinpath("static", "app.js").read_text()
    assert ".catch(" in src and "applyCapabilities(null)" in src
    html = _html()
    assert "disabled" in re.search(r'<input[^>]*id="signup-name"[^>]*>', html).group(0)
    assert "disabled" in re.search(r'<button[^>]*id="signup-btn"[^>]*>', html).group(0)
