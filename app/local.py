"""Local Ollama model discovery (optional private provider).

Discovers installed Ollama models from the configured ``OLLAMA_BASE_URL`` and
exposes them to Clarity as ``local:<exact-ollama-tag>`` (e.g. ``local:qwen3:1.7b``,
``local:llama3.2:3b``).

Everything here is ZERO-COST: it only issues ``GET /api/tags`` and performs no
inference. Results are cached with a short TTL behind an ``asyncio.Lock`` so
concurrent requests share ONE upstream call instead of launching a probe storm.

Failures (Ollama offline, unreachable, non-200, or malformed body) fail CLOSED
and cleanly: no local models are advertised, and raw errors, URLs, hostnames,
headers, or response bodies are never surfaced. Cloud routing and pricing are
untouched.
"""
import asyncio
import time

import httpx

from . import providers
from .config import settings

# Zero-cost: only a version/tags GET, no inference. Short timeout so a dead
# endpoint can never stall model listing.
DISCOVERY_TIMEOUT = 2.0
DISCOVERY_CACHE_TTL = 60.0

# Module-level cache + refresh lock (re-bound to the running loop on first use).
_cache = {"models": None, "time": 0.0}
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
    """Reset the in-memory discovery cache (used by tests and on demand)."""
    _cache["models"] = None
    _cache["time"] = 0.0


async def _fetch_tags() -> list[str]:
    """Return raw Ollama tag names from GET /api/tags. Fail closed on any issue."""
    url = providers.ollama_api_url("api/tags")
    try:
        async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT, follow_redirects=False) as client:
            r = await client.get(url, headers={"User-Agent": "clarity-local-discovery"})
        if r.status_code != 200:
            return []
        data = r.json()
    except (httpx.HTTPError, ValueError):
        # connection refused / timeout / invalid JSON -> fail closed, no leak
        return []
    except Exception:
        return []
    try:
        models = data.get("models") or []
        return [
            m["name"]
            for m in models
            if isinstance(m, dict) and isinstance(m.get("name"), str) and m["name"]
        ]
    except Exception:
        return []


async def local_model_ids() -> list[str]:
    """Discovered local models as ``local:<exact-ollama-tag>`` (cached, zero-cost)."""
    now = time.monotonic()
    cached = _cache["models"]
    if cached is not None and (now - _cache["time"]) < DISCOVERY_CACHE_TTL:
        return cached
    async with _lock():
        # Double-check after acquiring: a concurrent request may have refreshed.
        now = time.monotonic()
        cached = _cache["models"]
        if cached is not None and (now - _cache["time"]) < DISCOVERY_CACHE_TTL:
            return cached
        tags = await _fetch_tags()
        ids = ["local:" + t for t in tags]
        _cache["models"] = ids
        _cache["time"] = now
        return ids
