"""Local Ollama model discovery (optional private provider).

Discovers installed Ollama models from the configured ``OLLAMA_BASE_URL`` and
exposes them to Clarity as ``local:<exact-ollama-tag>`` (e.g. ``local:qwen3:1.7b``,
``local:llama3.2:3b``).

Everything here is ZERO-COST: it only issues ``GET /api/tags`` and performs no
inference. Successful discoveries are cached for a normal TTL behind an
``asyncio.Lock`` so concurrent requests share ONE upstream call instead of
launching a probe storm.

Failures (timeout, connection failure, non-200, malformed JSON, or malformed
payload) fail CLOSED and cleanly: callers see no local models, and raw errors,
URLs, hostnames, headers, or response bodies are never surfaced. A failure is
NOT cached as a successful (possibly empty) result -- it only triggers a short
failure backoff so a transient Ollama outage cannot poison the 60-second
success cache. Cloud routing and pricing are untouched.
"""
import asyncio
import time

import httpx

from . import providers
from .config import settings


class DiscoveryError(Exception):
    """Internal signal that Ollama discovery failed (never surfaced to clients)."""


# Zero-cost: only a tags GET, no inference. Short timeout so a dead endpoint can
# never stall model listing.
DISCOVERY_TIMEOUT = 2.0
DISCOVERY_CACHE_TTL = 60.0   # normal TTL for *successful* discovery (incl. empty)
FAILURE_TTL = 4.0           # short backoff after a discovery failure

# Module-level caches + refresh lock (re-bound to the running loop on first use).
_success_cache = {"models": None, "time": 0.0}  # None => no successful result yet
_failure_time = 0.0                                # monotonic() of last failure
_refresh_lock = None
_lock_loop = None


def _lock() -> asyncio.Lock:
    """Return the refresh lock, (re)bound to the currently running event loop."""
    global _refresh_lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _refresh_lock is None or _lock_loop is not loop:
        _refresh_lock = asyncio.Lock()
        _lock_loop = loop
    return _refresh_lock


def clear_local_cache() -> None:
    """Reset the in-memory discovery caches (used by tests and on demand)."""
    _success_cache["models"] = None
    _success_cache["time"] = 0.0
    _failure_time = 0.0


async def _fetch_tags() -> list[str]:
    """Return raw Ollama tag names from a successful GET /api/tags.

    Raises :class:`DiscoveryError` on any failure mode (unreachable, timeout,
    non-200, malformed JSON, or malformed payload). A valid 200 with an empty
    ``{"models": []}`` is a SUCCESS and returns ``[]`` (not an error).
    """
    url = providers.ollama_api_url("api/tags")
    try:
        async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT, follow_redirects=False) as client:
            r = await client.get(url, headers={"User-Agent": "clarity-local-discovery"})
    except httpx.HTTPError:
        # connection refused / timeout / TLS error -> fail closed, no leak
        raise DiscoveryError("unreachable")
    if r.status_code != 200:
        raise DiscoveryError(f"status {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise DiscoveryError("invalid json")
    try:
        models = data.get("models") or []
        return [
            m["name"]
            for m in models
            if isinstance(m, dict) and isinstance(m.get("name"), str) and m["name"]
        ]
    except Exception:
        raise DiscoveryError("malformed payload")


async def local_model_ids() -> list[str]:
    """Discovered local models as ``local:<exact-ollama-tag>`` (cached, zero-cost).

    - Successful discovery (including a valid empty list) is cached for
      ``DISCOVERY_CACHE_TTL``.
    - A failure fails closed (returns ``[]``) and only sets a short
      ``FAILURE_TTL`` backoff; it is never stored as a successful result, so a
      transient outage cannot poison the 60-second success cache.
    """
    global _success_cache, _failure_time
    now = time.monotonic()
    if _success_cache["models"] is not None and (now - _success_cache["time"]) < DISCOVERY_CACHE_TTL:
        return _success_cache["models"]
    if (now - _failure_time) < FAILURE_TTL:
        return []

    async with _lock():
        # Double-check after acquiring: a concurrent request may have refreshed.
        now = time.monotonic()
        if _success_cache["models"] is not None and (now - _success_cache["time"]) < DISCOVERY_CACHE_TTL:
            return _success_cache["models"]
        if (now - _failure_time) < FAILURE_TTL:
            return []

        try:
            tags = await _fetch_tags()
        except DiscoveryError:
            # Fail closed + short backoff. Do NOT store as a successful result.
            _failure_time = time.monotonic()
            _success_cache["models"] = None
            return []

        ids = ["local:" + t for t in tags]
        _success_cache["models"] = ids
        _success_cache["time"] = now
        return ids
