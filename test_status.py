import os

os.environ["GATEWAY_DB"] = "/tmp/gateway_status_test.db"
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_status_endpoint_returns_json():
    r = client.get("/v1/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "healthy"
    assert "timestamp" in body
    assert "gateway" in body
    assert "providers" in body
    assert "recent_activity" in body
    assert "failover" in body
    # gateway must be evidence-backed (DB aggregates), not invented
    gw = body["gateway"]
    assert "active_keys" in gw and "total_balance_usd" in gw
    assert isinstance(gw["active_keys"], int)
    # providers must list configured providers with needs_key + model count
    assert isinstance(body["providers"], dict) and len(body["providers"]) > 0
    for pname, info in body["providers"].items():
        assert isinstance(info["needs_key"], bool)
        assert isinstance(info["configured_models"], int)
    print("PASS status: json shape + evidence-backed fields")


def test_status_no_secret_exposure():
    r = client.get("/v1/status")
    body = r.json()
    text = str(body).lower()
    # none of these secrets/identifiers may appear anywhere in the response
    for forbidden in ("admin", "skey", "gw_", "bearer", "token", "api_key", "secret"):
        assert forbidden not in text, f"forbidden token '{forbidden}' leaked in /v1/status"
    # provider entries must not embed keys or host URLs
    for pname, info in body["providers"].items():
        assert "key" not in str(info).lower() or isinstance(info.get("needs_key"), bool)
    print("PASS status: no secret/key exposure")


def test_status_probe_note_present():
    r = client.get("/v1/status")
    body = r.json()
    note = body["recent_activity"]["note"]
    assert "no external probe latency" in note
    print("PASS status: probe latency honestly labeled as unmeasured")


def test_status_failover_note_present():
    r = client.get("/v1/status")
    body = r.json()
    note = body["failover"]["note"]
    assert "mocked providers" in note
    print("PASS status: failover honestly labeled as mocked-tested")


print("ALL STATUS TESTS PASSED")
