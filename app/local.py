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

Per-model metadata that Ollama reports is retained in a sanitized form so the
UI can show exactly what Ollama reports (size, family, parameter size,
quantization, context length, capabilities). Only public, non-sensitive fields
are kept; hostnames, IPs, filesystem paths, raw payloads, headers, and errors
are never retained. Malformed optional fields are dropped defensively and never
crash discovery.
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
_last_status = None                                # "ok" | "unavailable" | None(unprobed)
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
    """Reset the in-memory discovery caches (used by tests and on demand).

    Resets the success cache, the last-failure timestamp, AND the evidence-based
    status. Must declare the globals so the module-level values are cleared.
    """
    global _failure_time, _last_status
    _success_cache["models"] = None
    _success_cache["time"] = 0.0
    _failure_time = 0.0
    _last_status = None


def _as_str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _as_int(v) -> int | None:
    return v if isinstance(v, int) and v > 0 else None


def _sanitize_capabilities(raw) -> list[str] | None:
    """Preserve Ollama's per-model ``capabilities`` array, sanitized.

    Only non-empty string names are kept; duplicates are removed. A missing or
    malformed value yields ``None`` (omitted) — capabilities are NEVER invented
    (no "completion" filler, no family-based inference).
    """
    if not isinstance(raw, list):
        return None
    seen = set()
    out = []
    for c in raw:
        if isinstance(c, str) and c.strip():
            c = c.strip()
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out if out else None


def _sanitize_model(raw) -> dict | None:
    """Return a sanitized public metadata dict for one Ollama model entry.

    Returns ``None`` (and never raises) for a malformed entry so a single bad
    model cannot crash discovery of the rest.
    """
    if not isinstance(raw, dict):
        return None
    name = _as_str(raw.get("name")) or _as_str(raw.get("model"))
    if not name:
        return None
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    family = _as_str(details.get("family"))  # only Ollama-reported family; never invented
    try:
        entry = {
            "name": name,
            "id": "local:" + name,
            "size_bytes": _as_int(raw.get("size")),
            "family": family,
            "parameter_size": _as_str(details.get("parameter_size")),
            "quantization_level": _as_str(details.get("quantization_level")),
            "context_length": _as_int(details.get("context_length")),
            "capabilities": _sanitize_capabilities(details.get("capabilities")),
        }
    except Exception:
        return None
    return entry


async def _fetch_tags() -> list[dict]:
    """Return sanitized per-model metadata from a successful GET /api/tags.

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
            m for m in (_sanitize_model(x) for x in models)
            if m is not None
        ]
    except Exception:
        raise DiscoveryError("malformed payload")


async def _discover_models() -> list[dict]:
    """Cached, fail-closed, lock-deduplicated discovery -> enriched dicts.

    - Successful discovery (including a valid empty list) is cached for
      ``DISCOVERY_CACHE_TTL``.
    - A failure fails closed (returns ``[]``) and only sets a short
      ``FAILURE_TTL`` backoff; it is never stored as a successful result, so a
      transient outage cannot poison the 60-second success cache.
    """
    global _success_cache, _failure_time, _last_status
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
            models = await _fetch_tags()
        except DiscoveryError:
            # Fail closed + short backoff. Do NOT store as a successful result.
            # Record the failed attempt as evidence so status is honest.
            _failure_time = time.monotonic()
            _success_cache["models"] = None
            _last_status = "unavailable"
            return []

        _success_cache["models"] = models
        _success_cache["time"] = now
        _last_status = "ok"
        return models


async def local_model_ids() -> list[str]:
    """Discovered local model IDs as ``local:<exact-ollama-tag>`` (cached, zero-cost)."""
    return [m["id"] for m in await _discover_models()]


async def local_models_with_status() -> dict:
    """Return ``{"status": "ok"|"unavailable", "models": [...]}``.

    The status reflects the ACTUAL result of the discovery attempt this call
    makes (or the cached result of the last attempt): ``"ok"`` when Ollama
    answered ``/api/tags`` (including a valid empty ``{"models": []}``), and
    ``"unavailable"`` when the attempt failed or we are inside the failure
    backoff caused by a known failure. An unprobed state is never labelled
    ``"ok"`` — it defaults to ``"unavailable"`` until evidence exists.
    """
    models = await _discover_models()
    status = _last_status if _last_status is not None else "unavailable"
    return {"status": status, "models": models}
