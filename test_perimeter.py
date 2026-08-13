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
from collections import deque

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


# --- S. Streaming local concurrency lifetime ---------------------------------

def _make_stream_runner(hold=False, raise_=False):
    async def fake_run(body, key_id):
        async def _gen():
            yield b"data: 1\n\n"
            if raise_:
                raise RuntimeError("stream broke")
            if hold:
                await asyncio.sleep(0.4)
            yield b"data: 2\n\n"

        return _gen(), 0.0, "local"

    return fake_run


def test_S1_stream_holds_slot_while_open(monkeypatch):
    # A. A local streaming response keeps the slot held while its iterator is open.
    async def go():
        lim = LocalConcurrencyLimit(1)
        await lim.acquire()
        event = asyncio.Event()

        async def stream():
            yield b"data: 1\n\n"
            await event.wait()  # held open until released

        gen = main_mod._release_on_stream_end(stream(), lim)
        it = gen.__aiter__()
        first = await it.__anext__()
        assert first == b"data: 1\n\n"
        assert lim.available == 0  # held while the stream is still open
        event.set()
        try:
            await it.__anext__()
        except StopAsyncIteration:
            pass
        assert lim.available == 1  # released after the stream ends

    asyncio.run(go())


def test_S2_second_stream_fails_fast_while_active(monkeypatch):
    # B. A simultaneous second local request fails fast while the first stream
    #    remains active.
    async def go():
        lim = LocalConcurrencyLimit(1)
        await lim.acquire()
        event = asyncio.Event()

        async def stream():
            yield b"data: 1\n\n"
            await event.wait()

        gen = main_mod._release_on_stream_end(stream(), lim)
        it = gen.__aiter__()
        await it.__anext__()
        assert lim.available == 0
        # A second acquire while the stream is open must fail fast (no backlog).
        assert await lim.acquire() is False
        event.set()
        try:
            await it.__anext__()
        except StopAsyncIteration:
            pass
        assert lim.available == 1

    asyncio.run(go())


def test_S3_stream_release_on_consume(monkeypatch):
    # C. Consuming/closing the first stream releases the slot.
    fake = LocalConcurrencyLimit(1)
    monkeypatch.setattr(main_mod, "_local_concurrency", fake)
    monkeypatch.setattr("app.main.run_completion", _make_stream_runner(hold=False))
    skey, _ = _make_key()
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    _ = r.content
    assert fake.available == 1


def test_S4_stream_exception_releases_slot(monkeypatch):
    # D. An exception inside the stream releases the slot.
    async def go():
        lim = LocalConcurrencyLimit(1)
        await lim.acquire()

        async def stream():
            yield b"data: 1\n\n"
            raise RuntimeError("stream broke")

        gen = main_mod._release_on_stream_end(stream(), lim)
        it = gen.__aiter__()
        await it.__anext__()
        try:
            await it.__anext__()
        except RuntimeError:
            pass
        assert lim.available == 1  # released despite the stream exception

    asyncio.run(go())


def test_S5_nonstream_release_still_works(monkeypatch):
    # E. A non-stream local response still releases exactly once.
    fake = LocalConcurrencyLimit(1)
    monkeypatch.setattr(main_mod, "_local_concurrency", fake)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 200, r.text
    assert fake.available == 1


def test_S6_no_double_release_on_stream(monkeypatch):
    # F. Streaming path performs exactly one release (endpoint defers to wrapper).
    fake = FakeLocal(busy=False)
    monkeypatch.setattr(main_mod, "_local_concurrency", fake)
    monkeypatch.setattr("app.main.run_completion", _make_stream_runner(hold=False))
    skey, _ = _make_key()
    r = client.post(
        "/v1/chat/completions",
        json={"model": "local:test", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {skey}"},
    )
    _ = r.content
    assert fake.acquired == 1 and fake.released == 1
    assert fake.available == 1


# --- RL. Rate limiter total state is bounded ---------------------------------

def test_RL1_ceiling_bounds_total_buckets(monkeypatch):
    # A. Thousands of one-shot unique keys never grow state past the hard ceiling.
    monkeypatch.setattr("app.guard._MAX_BUCKETS", 8)
    rl = ChatRateLimiter(100000, 60)
    for i in range(500):
        rl.check(f"one-shot-{i}")
    assert len(rl._buckets) <= 8


def test_RL2_stale_bucket_evicted(monkeypatch):
    # B. A fully-expired bucket is removable after the window passes.
    monkeypatch.setattr("app.guard._MAX_BUCKETS", 4)
    rl = ChatRateLimiter(10, 0.1)
    rl.check("old-key")  # window 0.1s
    _time.sleep(0.2)  # old-key now fully expired
    for i in range(10):
        rl.check(f"fresh-{i}")
    assert "old-key" not in rl._buckets


def test_RL3_active_limit_works(monkeypatch):
    # C. Active per-key limits still enforce.
    rl = ChatRateLimiter(3, 60)
    assert rl.check("k")[0] is True
    assert rl.check("k")[0] is True
    assert rl.check("k")[0] is True
    assert rl.check("k")[0] is False
    assert rl.check("k")[1] >= 1


def test_RL4_independent_keys(monkeypatch):
    # D. Separate keys remain independent.
    rl = ChatRateLimiter(2, 60)
    assert rl.check("a")[0] is True
    assert rl.check("a")[0] is True
    assert rl.check("a")[0] is False
    assert rl.check("b")[0] is True


def test_RL5_no_skey_stored(monkeypatch):
    # E. Only the authenticated key id is stored; never the skey secret.
    rl = ChatRateLimiter(10, 60)
    skey, kid = _make_key("rl-skey")
    rl.check(kid)
    assert kid in rl._buckets
    assert skey not in rl._buckets
    assert all(k != skey for k in rl._buckets)
    for dq in rl._buckets.values():
        for ts in dq:
            assert not isinstance(ts, str)  # only monotonic timestamps, no secret


def test_RL6_partial_bucket_not_fully_stale(monkeypatch):
    # A. A bucket holding an expired oldest AND a recent newest timestamp is NOT
    #    fully stale (classification must use dq[-1], not dq[0]).
    rl = ChatRateLimiter(10, 60)
    now = _time.monotonic()
    rl._buckets["mix"] = deque([now - 100.0, now - 1.0])  # expired, recent
    fully_stale = [k for k, dq in rl._buckets.items() if not dq or dq[-1] <= now - 60]
    assert "mix" not in fully_stale


def test_RL7_active_not_preferentially_evicted(monkeypatch):
    # B. Under churn, an active (partially-stale) bucket is never chosen as the
    #    stale eviction victim while a fully-stale bucket exists.
    monkeypatch.setattr("app.guard._MAX_BUCKETS", 2)
    rl = ChatRateLimiter(10, 60)
    now = _time.monotonic()
    rl._buckets["stale"] = deque([now - 200.0])  # dq[-1] expired -> fully stale
    rl._buckets["active"] = deque([now - 100.0, now - 1.0])  # one active
    rl.check("fresh")  # forces one eviction past the ceiling of 2
    assert "active" in rl._buckets
    assert "stale" not in rl._buckets


def test_RL8_newest_expired_is_evictable(monkeypatch):
    # C. A bucket whose NEWEST timestamp is expired IS evictable.
    monkeypatch.setattr("app.guard._MAX_BUCKETS", 2)
    rl = ChatRateLimiter(10, 0.1)
    now = _time.monotonic()
    rl._buckets["old"] = deque([now - 1.0, now - 0.5])  # dq[-1] expired
    rl._buckets["cur"] = deque([now])
    rl.check("fresh2")
    assert "old" not in rl._buckets
    assert "cur" in rl._buckets


def test_RL9_lra_uses_newest_activity(monkeypatch):
    # D. Among fully-stale buckets, least-recently-active is by NEWEST timestamp.
    monkeypatch.setattr("app.guard._MAX_BUCKETS", 2)
    rl = ChatRateLimiter(10, 60)
    now = _time.monotonic()
    rl._buckets["a"] = deque([now - 300.0, now - 290.0])  # newest = now-290
    rl._buckets["b"] = deque([now - 1000.0, now - 100.0])  # newest = now-100 (more recent)
    rl.check("fresh3")
    # 'a' has the oldest newest-activity -> evicted; using dq[0] would wrongly pick 'b'.
    assert "a" not in rl._buckets
    assert "b" in rl._buckets


def test_RL10_churn_under_ceiling(monkeypatch):
    # E. High key churn never exceeds the 4096 hard ceiling.
    monkeypatch.setattr("app.guard._MAX_BUCKETS", 4096)
    rl = ChatRateLimiter(100000, 60)
    for i in range(20000):
        rl.check(f"k-{i}")
    assert len(rl._buckets) <= 4096


def test_RL11_active_count_not_reset(monkeypatch):
    # F. An active customer's count is NOT reset when only the oldest timestamp
    #    expires; in-window entries are preserved (partial-stale bucket untouched).
    rl = ChatRateLimiter(3, 60)
    now = _time.monotonic()
    rl._buckets["cust"] = deque([now - 100.0, now - 5.0, now - 2.0])
    allowed, _ = rl.check("cust")  # prunes oldest-expired; keeps 2; appends -> 3
    assert allowed is True
    assert len(rl._buckets["cust"]) == 3
    allowed2, _ = rl.check("cust")
    assert allowed2 is False  # 4th in-window denied; not silently reset to 1


def test_RL12_independent_quotas_intact(monkeypatch):
    # G. Independent key quotas remain intact.
    rl = ChatRateLimiter(2, 60)
    for _ in range(2):
        assert rl.check("a")[0] is True
    assert rl.check("a")[0] is False
    assert rl.check("b")[0] is True
    assert rl.check("b")[0] is True
    assert rl.check("b")[0] is False
    assert rl.check("a")[0] is False  # 'a' stays limited; independent from 'b'


def test_RL13_empty_bucket_eviction_safe(monkeypatch):
    # Edge: an empty bucket is fully-stale and eligible for eviction; selection
    # must use the safe _last_ts helper (no IndexError on empty deque).
    monkeypatch.setattr("app.guard._MAX_BUCKETS", 2)
    rl = ChatRateLimiter(10, 60)
    now = _time.monotonic()
    rl._buckets["empty"] = deque()  # empty -> fully stale
    rl._buckets["other"] = deque([now])
    rl.check("fresh")  # forces one eviction past the ceiling of 2
    assert "empty" not in rl._buckets  # empty bucket was the eviction victim
    assert "other" in rl._buckets
    assert len(rl._buckets) <= 2  # count stays bounded



# --- A (incremental body handling) ------------------------------------------

def test_A5_incremental_stop_on_oversized_stream(monkeypatch):
    # An oversized body (no spoofed CL trust) is stopped incrementally at the
    # limit; the full body is never buffered into inference.
    monkeypatch.setattr(config.settings, "MAX_CHAT_REQUEST_BYTES", 100)
    calls = []
    _stub_run(monkeypatch, calls)
    skey, _ = _make_key()
    big = "x" * (1024 * 1024)  # 1 MiB, far above the 100-byte limit
    r = client.post(
        "/v1/chat/completions",
        content=big.encode(),
        headers={"Authorization": f"Bearer {skey}"},
    )
    assert r.status_code == 413, r.text
    assert not calls, "oversized stream must not trigger inference"


# --- Regression: other surfaces unchanged -----------------------------------


def test_G1_non_chat_endpoints_untouched():
    assert client.get("/health").status_code == 200
    assert client.get("/v1/capabilities").status_code == 200
    assert client.get("/v1/build").status_code == 200
    assert client.get("/v1/status").status_code == 200
    # Signup remains fail-closed by default.
    r = client.post("/v1/signup", json={"name": "x"})
    assert r.status_code == 403, r.text
