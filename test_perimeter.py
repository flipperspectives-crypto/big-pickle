"""Perimeter hardening tests for POST /v1/chat/completions.

Covers the three protections added for the single-process Windows laptop:

  A. Request body size limit (413 on oversized / spoofed Content-Length).
  D. Per authenticated key-id rate limiting (429 + Retry-After, pruned,
     independent per key, no state on failed auth).
  F. Local-inference concurrency (fail-fast 503 when busy, released on every
     exit path; cloud requests and rejected-early requests never take a slot).

No real inference is performed: ``app.main.run_completion`` is stubbed. The DB
is isolated per run.
"""
import asyncio
import os
import time as _time

os.environ.setdefault("GATEWAY_DB", "/tmp/gateway_perimeter_test.db")
os.environ.setdefault("GATEWAY_ADMIN_KEY", "testadmin")

from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.guard import ChatRateLimiter, LocalConcurrencyLimit  # noqa: E402
from app import main as main_mod  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

ADMIN = "x-admin-key"
FAKE = {
    "id": "chatcmpl-fake",
    "object": "chat.completion",
    "model": "local:test",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


def _make_key(name="cust"):
    r = client.post("/v1/keys", json={"name": name}, headers={ADMIN: "testadmin"})
    assert r.status_code == 200, r.text
    return r.json()["skey"], r.json()["id"]


def _stub_run(monkeypatch, calls):
    async def fake_run(body, key_id):
        calls.append((body, key_id))
        return FAKE, 0.0, "local"

    monkeypatch.setattr("app.main.run_completion", fake_run)


def _unlimited_local(monkeypatch):
    # No local slot cap so rate-limit tests isolate only the rate limiter.
    monkeypatch.setattr(main_mod, "_local_concurrency", LocalConcurrencyLimit(0))


def _unlimited_ratelimit(monkeypatch):
    # Effectively unlimited rate limit so local-concurrency tests isolate only
    # the concurrency cap.
    monkeypatch.setattr(main_mod, "_chat_rate_limiter", ChatRateLimiter(100000, 60))


# --- A. Request body size limit ---------------------------------------------


def test_A1_small_body_allowed(monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_CHAT_REQUEST_BYTES", 1000)
    _unlimited_local(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    payload = {"model": "local:test", "messages": [{"role": "user", "content": "hi"}]}
    import json as _json

    r = client.post(
        "/v1/chat/completions",
        content=_json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 200, r.text
    assert calls, "run_completion should have been called for an in-limit body"


def test_A2_oversized_body_rejected_413(monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_CHAT_REQUEST_BYTES", 100)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    big = "x" * 10000
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": big}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 413, r.text
    assert not calls, "oversized body must NOT trigger inference"


def test_A3_spoofed_small_content_length_cannot_bypass(monkeypatch):
    # Declare a tiny Content-Length but send a huge body; the actual body bytes
    # are what is enforced, so this must still be rejected.
    monkeypatch.setattr(config.settings, "MAX_CHAT_REQUEST_BYTES", 100)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    import json as _json

    body = _json.dumps(
        {"model": "local:test", "messages": [{"role": "user", "content": "y" * 5000}]}
    ).encode()
    r = client.post(
        "/v1/chat/completions",
        content=body,
        headers={"Authorization": f"Bearer {skey}", "Content-Length": "10"},
    )
    assert r.status_code == 413, r.text
    assert not calls, "spoofed Content-Length must not bypass the body-size gate"


def test_A4_huge_content_length_rejected_early(monkeypatch):
    monkeypatch.setattr(config.settings, "MAX_CHAT_REQUEST_BYTES", 100)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    r = client.post(
        "/v1/chat/completions",
        content=b'{"model":"local:test"}',
        headers={"Authorization": f"Bearer {skey}", "Content-Length": "999999999"},
    )
    assert r.status_code == 413, r.text
    assert not calls, "oversized Content-Length must short-circuit before inference"


# --- D. Per-key rate limiting -----------------------------------------------


def test_D1_within_limit_ok(monkeypatch):
    monkeypatch.setattr(main_mod, "_chat_rate_limiter", ChatRateLimiter(3, 60))
    _unlimited_local(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    for _ in range(3):
        r = client.post(
            "/v1/chat/completions",
            json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {skey}"},
        )
        assert r.status_code == 200, r.text
    assert len(calls) == 3


def test_D2_over_limit_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(main_mod, "_chat_rate_limiter", ChatRateLimiter(3, 60))
    _unlimited_local(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    for i in range(3):
        r = client.post(
            "/v1/chat/completions",
            json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {skey}"},
        )
        assert r.status_code == 200, (i, r.text)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 429, r.text
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) >= 1
    assert len(calls) == 3, "the 429 request must not reach inference"


def test_D3_independent_per_key(monkeypatch):
    monkeypatch.setattr(main_mod, "_chat_rate_limiter", ChatRateLimiter(2, 60))
    _unlimited_local(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    skey_a, _ = _make_key("a")
    skey_b, _ = _make_key("b")
    for _ in range(2):
        r = client.post(
            "/v1/chat/completions",
            json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {skey_a}"},
        )
        assert r.status_code == 200, r.text
    # Key A is now rate-limited...
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey_a}"},
    )
    assert r.status_code == 429, r.text
    # ...but key B is untouched.
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey_b}"},
    )
    assert r.status_code == 200, r.text


def test_D4_failed_auth_records_no_state(monkeypatch):
    # Hammering with bad keys must not consume the good key's budget.
    monkeypatch.setattr(main_mod, "_chat_rate_limiter", ChatRateLimiter(2, 60))
    _unlimited_local(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    for _ in range(20):
        r = client.post(
            "/v1/chat/completions",
            json={"model": "local:test"},
            headers={"Authorization": "Bearer gw_bogus"},
        )
        assert r.status_code == 401, r.text
    # The good key still has its full budget.
    for _ in range(2):
        r = client.post(
            "/v1/chat/completions",
            json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {skey}"},
        )
        assert r.status_code == 200, r.text


def test_D5_window_expiry_restores(monkeypatch):
    import time as _time

    monkeypatch.setattr(main_mod, "_chat_rate_limiter", ChatRateLimiter(2, 0.3))
    _unlimited_local(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    for _ in range(2):
        r = client.post(
            "/v1/chat/completions",
            json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {skey}"},
        )
        assert r.status_code == 200, r.text
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 429, r.text
    _time.sleep(0.4)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 200, r.text


def test_D6_pruned_bounded_state(monkeypatch):
    rl = ChatRateLimiter(100000, 0.05)
    monkeypatch.setattr(main_mod, "_chat_rate_limiter", rl)
    _unlimited_local(monkeypatch)
    _stub_run(monkeypatch, [])
    # Many distinct keys; total buckets must stay bounded (no unbounded growth).
    for i in range(50):
        k, _ = _make_key(f"rl-{i}")
        client.post(
            "/v1/chat/completions",
            json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {k}"},
        )
    assert len(rl._buckets) <= 4096
    # After the short window passes, a previously-seen key's expired timestamp is
    # pruned on next access and the request is allowed again.
    _time.sleep(0.1)
    assert rl.check("rl-0")[0] is True
    assert len(rl._buckets["rl-0"]) == 1


# --- F. Local-inference concurrency -----------------------------------------


def test_F1_unit_fail_fast_and_release():
    async def go():
        lim = LocalConcurrencyLimit(1)
        assert await lim.acquire() is True
        # Second acquire must fail fast (no slot).
        assert await lim.acquire() is False
        # Release and re-acquire.
        lim.release()
        assert await lim.acquire() is True
        lim.release()

    asyncio.run(go())


def test_F2_unit_unlimited_when_zero():
    async def go():
        lim = LocalConcurrencyLimit(0)
        assert await lim.acquire() is True
        assert await lim.acquire() is True  # no cap

    asyncio.run(go())


class FakeLocal:
    """Deterministic stand-in for LocalConcurrencyLimit in integration tests.

    Avoids cross-event-loop semaphore manipulation. Can be told to report as
    busy (acquire -> False) and records acquire/release calls so tests can prove
    the endpoint engages and releases the slot exactly once per execution.
    """

    def __init__(self, busy=False, max_concurrency=1):
        self.busy = busy
        self.max_concurrency = max_concurrency
        self.acquired = 0
        self.released = 0

    async def acquire(self):
        if self.busy:
            return False
        self.acquired += 1
        return True

    def release(self):
        self.released += 1

    @property
    def available(self):
        held = self.acquired - self.released
        return max(0, self.max_concurrency - held)


def test_F3_busy_local_model_fails_fast_503(monkeypatch):
    fake = FakeLocal(busy=True)
    monkeypatch.setattr(main_mod, "_local_concurrency", fake)
    _unlimited_ratelimit(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 503, r.text
    assert not calls, "busy local model must not run inference"
    assert fake.acquired == 0, "a busy slot must never be acquired"


def test_F4_slot_released_on_success(monkeypatch):
    fake = FakeLocal(busy=False)
    monkeypatch.setattr(main_mod, "_local_concurrency", fake)
    _unlimited_ratelimit(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 200, r.text
    # Exactly one acquire and one release on the success path.
    assert fake.acquired == 1 and fake.released == 1
    assert fake.available == 1


def test_F5_slot_released_on_exception(monkeypatch):
    fake = FakeLocal(busy=False)
    monkeypatch.setattr(main_mod, "_local_concurrency", fake)
    _unlimited_ratelimit(monkeypatch)

    async def boom(body, key_id):
        raise RuntimeError("inference exploded")

    monkeypatch.setattr("app.main.run_completion", boom)
    skey, _ = _make_key()
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 502, r.text
    # Slot must still be released despite the exception.
    assert fake.acquired == 1 and fake.released == 1
    assert fake.available == 1


def test_F6_cloud_requests_ignore_local_cap(monkeypatch):
    # A cloud (non-local) model must never consume a local slot, so a saturated
    # local cap does not block cloud traffic.
    fake = FakeLocal(busy=True)  # would fail-fast IF engaged
    monkeypatch.setattr(main_mod, "_local_concurrency", fake)
    _unlimited_ratelimit(monkeypatch)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    r = client.post(
        "/v1/chat/completions",
        json={"model": "some-cloud-model", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    # Cloud model with no funds -> 402, but crucially the local slot is never
    # touched (not 503), proving cloud traffic bypasses the local cap.
    assert r.status_code in (402, 200), r.text
    assert fake.acquired == 0, "cloud requests must never acquire a local slot"


def test_F7_rejected_early_never_takes_local_slot(monkeypatch):
    fake = FakeLocal(busy=False)
    monkeypatch.setattr(main_mod, "_local_concurrency", fake)
    monkeypatch.setattr(config.settings, "MAX_CHAT_REQUEST_BYTES", 50)
    _unlimited_ratelimit(monkeypatch)

    skey, _ = _make_key()
    # Oversized body -> 413. Must not consume the local slot.
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "z" * 5000}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 413, r.text
    assert fake.acquired == 0
    # Bad auth -> 401. Must not consume the local slot either.
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test"},
        headers={"Authorization": "Bearer gw_bogus"},
    )
    assert r.status_code == 401, r.text
    assert fake.acquired == 0


# --- F (unit): BoundedSemaphore over-release protection ----------------------

def test_F8_bounded_starts_with_one_slot():
    # A. max_concurrency=1 starts with exactly one available slot.
    lim = LocalConcurrencyLimit(1)
    assert lim.available == 1


def test_F9_acquire_succeeds_once():
    # B. acquire succeeds once when capacity exists.
    async def go():
        lim = LocalConcurrencyLimit(1)
        assert await lim.acquire() is True

    asyncio.run(go())


def test_F10_second_acquire_while_occupied_false():
    # C. second acquire while occupied returns False immediately (fail-fast).
    async def go():
        lim = LocalConcurrencyLimit(1)
        assert await lim.acquire() is True
        assert await lim.acquire() is False

    asyncio.run(go())


def test_F11_one_release_restores_one_slot():
    # D. one release restores exactly one slot.
    async def go():
        lim = LocalConcurrencyLimit(1)
        await lim.acquire()
        lim.release()
        assert lim.available == 1

    asyncio.run(go())


def test_F12_overrelease_does_not_increase_capacity():
    # E. an accidental second release must NOT raise capacity above 1.
    async def go():
        lim = LocalConcurrencyLimit(1)
        await lim.acquire()
        lim.release()
        # BoundedSemaphore raises on over-release; release() swallows it, so the
        # accidental second release is a safe no-op and capacity stays at 1.
        lim.release()
        assert lim.available == 1

    asyncio.run(go())


def test_F13_overrelease_at_most_one_subsequent_acquire():
    # F. after an accidental second release, at most one subsequent acquire
    #    succeeds (capacity was NOT increased above 1).
    async def go():
        lim = LocalConcurrencyLimit(1)
        await lim.acquire()
        lim.release()
        lim.release()  # accidental over-release (swallowed)
        assert await lim.acquire() is True
        assert await lim.acquire() is False

    asyncio.run(go())


def test_F14_rejected_acquire_creates_no_backlog():
    # H. a rejected second acquire registers no waiting task / backlog.
    async def go():
        lim = LocalConcurrencyLimit(1)
        await lim.acquire()  # occupy the only slot
        # Fail-fast returns False without ever calling semaphore.acquire().
        assert await lim.acquire() is False
        # Behavioral proof of no backlog: a release makes the slot available to a
        # NEW acquire immediately. If the rejected acquire had queued a phantom
        # waiter, that release would have been consumed by the waiter and this
        # acquire would fail fast again (value still 0). It succeeds instead.
        lim.release()
        assert await lim.acquire() is True
        # Occupied again -> a further acquire still fails fast (no backlog).
        assert await lim.acquire() is False

    asyncio.run(go())


# --- Regression: other surfaces unchanged -----------------------------------


def test_G1_non_chat_endpoints_untouched():
    assert client.get("/health").status_code == 200
    assert client.get("/v1/capabilities").status_code == 200
    assert client.get("/v1/build").status_code == 200
    assert client.get("/v1/status").status_code == 200
    # Signup remains fail-closed by default.
    r = client.post("/v1/signup", json={"name": "x"})
    assert r.status_code == 403, r.text
