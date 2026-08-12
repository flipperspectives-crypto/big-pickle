"""Frontend UX verification for the Clarity gateway.

Covers: HTTP smoke (page + static assets serve), structural/static checks that
the new UX hooks exist and that no secrets are persisted/exposed, and a
behavioral Node test (test_frontend_ux.mjs) that runs app.js in a DOM stub to
verify the pure UX helpers (state mapping + hostname sanitization).
"""
import os
import re
import shutil
import subprocess
import sys

os.environ["GATEWAY_DB"] = "/tmp/gateway_frontend_test.db"
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read(p):
    with open(os.path.join(ROOT, p), "r", encoding="utf-8") as f:
        return f.read()


def test_page_and_assets_serve():
    c = TestClient(app)
    assert c.get("/").status_code == 200
    assert c.get("/static/app.js").status_code == 200
    assert c.get("/static/app.css").status_code == 200
    # favicon must be declared in the served page
    assert 'rel="icon"' in c.get("/").text


def test_no_secret_persistence_in_frontend():
    js = _read("static/app.js")
    # Never persist keys in browser storage.
    assert "localStorage" not in js, "localStorage must not be used"
    assert "sessionStorage" not in js, "sessionStorage must not be used"
    # The created key is held only in an in-memory variable.
    assert "sessionKey" in js
    # Key is never written to logs.
    assert "console.log" not in js, "no console logging of anything (avoids key leakage)"


def test_key_masking_controls_present():
    html = _read("static/index.html")
    assert 'id="signup-skey"' in html
    # masked by default via password type
    assert 'type="password" id="signup-skey"' in html
    assert 'id="key-show-toggle"' in html
    assert 'id="copy-skey"' in html
    # key inputs are password fields (no plaintext exposure by default)
    assert 'type="password" id="topup-key"' in html
    assert 'id="pg-key"' in html


def test_status_table_responsive_and_sanitized():
    html = _read("static/index.html")
    assert 'class="table-scroll"' in html
    assert 'id="status-providers"' in html
    assert 'id="status-retry"' in html
    css = _read("static/app.css")
    assert ".table-scroll" in css
    assert ".out-badge.insufficient" in css
    # mobile stacked-table fallback (data-label driven)
    assert "status-table thead" in css
    assert 'content: attr(data-label)' in css
    js = _read("static/app.js")
    # rows carry data-label so the stacked mobile layout works
    assert 'data-label="Provider"' in js
    # hostname sanitization helper exists
    assert "safeReason" in js


def test_playground_states_and_first_run_present():
    html = _read("static/index.html")
    assert 'id="pg-topup-cta"' in html          # top-up CTA on insufficient balance
    assert 'id="pg-firstrun"' in html           # honest first-run hint for $0 keys
    assert 'id="local"' in html                 # dedicated Local $0 section
    low = html.lower()
    assert "bring your own ollama" in low
    assert "not on clarity" in low              # "not on Clarity's cloud" phrasing
    js = _read("static/app.js")
    assert "insufficient" in js and "unavailable" in js
    assert "PG_STATE_LABELS" in js


def test_status_endpoint_wired_to_real_backend():
    html = _read("static/index.html")
    for ep in ["/v1/status", "/v1/models", "/v1/signup", "/v1/checkout", "/v1/usage"]:
        assert ep in html, f"page must reference {ep}"


def test_no_real_secret_values_in_static():
    blob = _read("static/index.html") + _read("static/app.js") + _read("static/app.css")
    # placeholders like gw_… are fine; real-looking full keys are not.
    assert not re.search(r"gw_[A-Za-z0-9]{20,}", blob), "no real gateway key committed"
    # "sk-" also appears in "skeleton" (a CSS class) but is never followed by
    # 20+ alnum chars, so a length-qualified pattern avoids false positives.
    assert not re.search(r"sk-[A-Za-z0-9]{20,}", blob), "no provider/secret key in static assets"
    assert not re.search(r"0x[a-fA-F0-9]{64}", blob), "no raw private key in static assets"


def test_node_ux_behavioral():
    """Run the DOM-stub behavioral test for the pure UX helpers."""
    if not shutil.which("node"):
        import pytest
        pytest.skip("node not available; skipping behavioral JS test")
    r = subprocess.run(
        ["node", "test_frontend_ux.mjs"],
        cwd=ROOT, capture_output=True, text=True,
    )
    print(r.stdout, r.stderr, file=sys.stderr)
    assert r.returncode == 0, "frontend UX behavioral test failed"
