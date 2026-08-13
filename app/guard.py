"""Request guards for ``POST /v1/chat/completions``.

These protect a single-process Windows laptop gateway from abusive or
accidental public inference load:

* :class:`ChatRateLimiter` - per authenticated key-id request rate limiting with
  a bounded, pruned sliding window keyed on monotonic time.
* :class:`LocalConcurrencyLimit` - fail-fast bounded concurrency for local
  Ollama models (default 1) using an :class:`asyncio.BoundedSemaphore`, released
  on every exit path (success, failure, exception, cancellation).

Neither depends on a third-party library. No provider keys, request bodies, or
secret material are stored here.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

# Hard ceiling on the number of distinct key buckets retained. A per-key deque
# is also pruned on every request, but this bounds total memory if clients
# rotate key ids.
_MAX_BUCKETS = 4096


class ChatRateLimiter:
    """Sliding-window rate limiter keyed by authenticated key id.

    - Counts ONLY successfully authenticated requests. Failed auth never
      records state, so an attacker cannot be rate-limited by probing with bad
      keys (and cannot exhaust another caller's budget).
    - Uses monotonic time; never wall-clock.
    - State is bounded: expired timestamps are pruned per request, and at a hard
      ceiling of ``_MAX_BUCKETS`` (4096) a single stale/old bucket is evicted so
      total retained buckets can never grow indefinitely. Quotas are never merged.
    """

    def __init__(self, limit: int, window_seconds: float):
        self.limit = max(1, int(limit))
        self.window = max(0.1, float(window_seconds))
        self._buckets: dict[str, deque[float]] = {}

    def check(self, key_id: str) -> tuple[bool, int]:
        """Record one request for ``key_id`` and return ``(allowed, retry_after)``.

        ``retry_after`` is the number of whole seconds until the oldest
        in-window request expires (``0`` when the request is allowed).
        """
        now = time.monotonic()
        dq = self._buckets.get(key_id)
        if dq is None:
            dq = deque()
            self._buckets[key_id] = dq
        # Drop timestamps that have fallen outside the window.
        while dq and dq[0] <= now - self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            retry_after = int(dq[0] + self.window - now) + 1
            return False, max(1, retry_after)
        dq.append(now)
        # Enforce the hard ceiling on total retained key buckets so memory can
        # never grow unbounded across many one-shot keys.
        if len(self._buckets) > _MAX_BUCKETS:
            self._evict_one_stale(now)
        return True, 0

    def _evict_one_stale(self, now: float) -> None:
        """Evict a single bucket when over the hard ceiling.

        A bucket is fully stale/expired ONLY when it is empty or its most-recent
        request timestamp is outside the window (``dq[-1] <= now - self.window``).
        A bucket that still holds an in-window timestamp is NEVER treated as
        stale, so an active customer's quota is never reset under key churn.

        Among fully-stale buckets, prefer the one whose most-recent activity is
        oldest. If none are fully stale, evict the globally least-recently-active
        bucket using ``dq[-1]`` (never merge quotas; only key ids are retained).
        """
        if not self._buckets:
            return

        def _last_ts(k):
            dq = self._buckets[k]
            return dq[-1] if dq else now

        fully_stale = [
            k for k, dq in self._buckets.items()
            if not dq or dq[-1] <= now - self.window
        ]
        if fully_stale:
            victim = min(fully_stale, key=_last_ts)
        else:
            victim = min(self._buckets, key=_last_ts)
        self._buckets.pop(victim, None)

    def reset(self) -> None:
        """Drop all recorded state (test isolation)."""
        self._buckets.clear()


class LocalConcurrencyLimit:
    """Fail-fast bounded concurrency for local Ollama models.

    A single GPU/CPU-bound laptop cannot safely run more than one local
    generation at once, so the default max concurrency is 1. Cloud requests are
    unaffected. When no slot is immediately available the caller fails fast
    (it must NOT block and build an unbounded backlog).
    """

    def __init__(self, max_concurrency: int):
        self.max_concurrency = max(0, int(max_concurrency))
        self._sem = (
            asyncio.BoundedSemaphore(self.max_concurrency)
            if self.max_concurrency > 0
            else None
        )

    async def acquire(self) -> bool:
        """Best-effort non-blocking acquire.

        Returns ``True`` only when a slot is held. With ``max_concurrency == 0``
        the limit is treated as unlimited (always ``True``). Returns ``False``
        when no slot is immediately available so the caller can fail fast.

        Uses ``Semaphore.locked()`` to detect an unavailable slot without ever
        blocking: when not locked, ``acquire()`` completes synchronously (no
        await) so there is no window for another coroutine to interleave.
        """
        if self._sem is None:
            return True
        if self._sem.locked():
            return False
        await self._sem.acquire()
        return True

    def release(self) -> None:
        """Release a held slot. Safe to call only after a successful acquire."""
        if self._sem is not None:
            try:
                self._sem.release()
            except ValueError:
                # Defensive against a double-release racing with re-acquire.
                pass

    @property
    def available(self) -> int:
        if self._sem is None:
            return self.max_concurrency
        return self._sem._value
