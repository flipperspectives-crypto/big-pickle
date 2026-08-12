"""Local Ollama runtime surface (zero-inference) + last-request telemetry.

This module is intentionally SEPARATE from :mod:`app.local` (which performs the
zero-cost ``GET /api/tags`` discovery used to populate ``/v1/models``). It
probes ``GET /api/ps`` (the models currently *loaded* into VRAM) from the
configured Ollama endpoint.

Everything here is ZERO-INFERENCE: it never runs a model, never sends a
completion, and never touches the ``/api/tags`` discovery cache in
:mod:`app.local`. Results are cached behind an INDEPENDENT async lock with a
short failure backoff, so a dead endpoint can never stall the runtime panel and
a transient outage cannot poison the success cache.

Only sanitized, public fields are returned. No URLs, hosts, IPs, filesystem
paths, headers, credentials, raw payloads, or upstream error text are exposed.

The last-request telemetry is an ephemeral, in-memory record of the most recent
SUCCESSFUL NON-STREAMING local inference. It deliberately stores only safe,
non-sensitive facts (model, round-trip timing, token counts); it never holds
prompt text, response content, API keys, hostnames, or URLs, and it is never
populated by failed requests or by cloud/streaming requests.
"""
import asyncio
import time
from datetime import datetime, timezone

import httpx

from . import providers
from .config import settings


class RuntimeProbeError(Exception):
    """Internal signal that the Ollama runtime probe failed (never surfaced)."""


# Zero-inference: only a /api/ps GET, no inference. Short timeout so a dead
# endpoint can never stall the runtime panel.
RUNTIME_TIMEOUT = 2.0
RUNTIME_CACHE_TTL = 5.0   # running models change frequently; short TTL
FAILURE_TTL = 4.0         # short backoff after a probe failure

# Module-level caches + refresh lock (re-bound to the running loop on first use).
# These are deliberately independent of app.local's discovery caches.
_runtime_cache = {"models": None, "time": 0.0}   # None => no successful result yet
_failure_time = 0.0                                 # monotonic() of last failure
_last_status = None                                # "ok" | "unavailable" | None(unprobed)

# Ephemeral, in-memory telemetry for the last successful NON-STREAMING local
# request. Never persisted; never holds prompt/response/key/host/url.
_last_local_request = None

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


def clear_runtime_cache() -> None:
    """Reset the in-memory runtime caches + telemetry.

    Test-only / on-demand helper. Resets BOTH the probe cache/backoff and the
    last-local-request telemetry.
    """
    global _failure_time, _last_status, _last_local_request
    _runtime_cache["models"] = None
    _runtime_cache["time"] = 0.0
    _failure_time = 0.0
    _last_status = None
    _last_local_request = None


def reset_local_telemetry() -> None:
    """Reset ONLY the last-local-request telemetry (explicit test-only helper).

    The probe cache/backoff are intentionally left untouched, so a runtime probe
    refresh never wipes telemetry. Telemetry otherwise only disappears on process
    restart or via this helper or :func:`clear_runtime_cache`.
    """
    global _last_local_request
    _last_local_request = None


def _as_str(v) -> str | None:
    return v if isinstance(v, str) and v else None


def _as_int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        i = int(v)
        return i if i >= 0 else None
    return None


def _sanitize_model(raw) -> dict | None:
    """Return a sanitized public runtime dict for one loaded Ollama model.

    Returns ``None`` (and never raises) for a malformed entry so a single bad
    model cannot crash the probe of the rest. Only the explicit, public fields
    below are retained; everything else (hosts, paths, headers, raw payload)
    is dropped.
    """
    if not isinstance(raw, dict):
        return None
    name = _as_str(raw.get("name")) or _as_str(raw.get("model"))
    if not name:
        return None
    details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
    try:
        entry = {
            "name": name,
            "id": "local:" + name,
            "size_bytes": _as_int(raw.get("size")),
            "size_vram_bytes": _as_int(raw.get("size_vram")),
            "context_length": _as_int(details.get("context_length")),
            "expires_at": _as_str(raw.get("expires_at")),
            "family": _as_str(details.get("family")),
            "parameter_size": _as_str(details.get("parameter_size")),
            "quantization_level": _as_str(details.get("quantization_level")),
        }
    except Exception:
        return None
    return entry


async def _do_get(url: str, timeout: float) -> httpx.Response:
    """Perform the actual zero-inference GET. Isolated for test monkeypatching."""
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.get(url, headers={"User-Agent": "clarity-local-runtime"})


async def _fetch_ps() -> list[dict]:
    """Return sanitized per-model runtime metadata from a successful GET /api/ps.

    Raises :class:`RuntimeProbeError` on any failure mode (unreachable, timeout,
    non-200, malformed JSON, or malformed payload). A valid 200 with an empty
    ``{"models": []}`` is a SUCCESS and returns ``[]`` (not an error).
    """
    url = providers.ollama_api_url("api/ps")
    try:
        r = await _do_get(url, RUNTIME_TIMEOUT)
    except httpx.HTTPError:
        # connection refused / timeout / TLS error -> fail closed, no leak
        raise RuntimeProbeError("unreachable")
    if r.status_code != 200:
        raise RuntimeProbeError(f"status {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeProbeError("invalid json")
    try:
        models = data.get("models") or []
        return [
            m for m in (_sanitize_model(x) for x in models)
            if m is not None
        ]
    except Exception:
        raise RuntimeProbeError("malformed payload")


async def _discover_runtime() -> list[dict]:
    """Cached, fail-closed, lock-deduplicated runtime probe -> sanitized dicts.

    - Successful probe (including a valid empty list) is cached for
      ``RUNTIME_CACHE_TTL``.
    - A failure fails closed (returns ``[]``) and only sets a short
      ``FAILURE_TTL`` backoff; it is never stored as a successful result, so a
      transient outage cannot poison the success cache.
    """
    global _runtime_cache, _failure_time, _last_status
    now = time.monotonic()
    if _runtime_cache["models"] is not None and (now - _runtime_cache["time"]) < RUNTIME_CACHE_TTL:
        return _runtime_cache["models"]
    if (now - _failure_time) < FAILURE_TTL:
        return []

    async with _lock():
        # Double-check after acquiring: a concurrent request may have refreshed.
        now = time.monotonic()
        if _runtime_cache["models"] is not None and (now - _runtime_cache["time"]) < RUNTIME_CACHE_TTL:
            return _runtime_cache["models"]
        if (now - _failure_time) < FAILURE_TTL:
            return []

        try:
            models = await _fetch_ps()
        except RuntimeProbeError:
            # Fail closed + short backoff. Do NOT store as a successful result.
            _failure_time = time.monotonic()
            _runtime_cache["models"] = None
            _last_status = "unavailable"
            return []

        _runtime_cache["models"] = models
        _runtime_cache["time"] = now
        _last_status = "ok"
        return models


async def get_runtime() -> dict:
    """Return ``{"status", "models", "last_local_request"}``.

    ``status`` reflects the ACTUAL result of the runtime probe this call makes
    (or the cached result of the last attempt): ``"ok"`` when Ollama answered
    ``/api/ps`` (including a valid empty ``{"models": []}``), and
    ``"unavailable"`` when the attempt failed or we are inside the failure
    backoff caused by a known failure. An unprobed state is never labelled
    ``"ok"`` — it defaults to ``"unavailable"`` until evidence exists.
    """
    models = await _discover_runtime()
    status = _last_status if _last_status is not None else "unavailable"
    return {
        "status": status,
        "models": models,
        "last_local_request": _last_local_request,
    }


def get_last_local_request() -> dict | None:
    """Return the current in-memory last-local-request telemetry (or None)."""
    return _last_local_request


def record_local_success(
    model: str,
    round_trip_ms: float,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """Record ephemeral telemetry for the last successful NON-STREAMING local
    inference.

    Only safe, non-sensitive facts are stored: the model id, the Clarity-side
    round-trip time measured around the actual upstream request, and the token
    counts. It NEVER stores prompt text, response content, API keys, hostnames,
    or URLs.

    This must only ever be called on a successful, non-streaming local request
    (see :func:`app.router.run_completion`).
    """
    global _last_local_request
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    _last_local_request = {
        "model": model,
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gateway_upstream_round_trip_ms": round(float(round_trip_ms), 1),
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": total,
    }
