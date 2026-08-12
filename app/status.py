"""Public, read-only /v1/status reliability surface.

Reports clearly separated, evidence-backed provider facts:
  - configured:              provider is part of the gateway configuration
  - credentials_configured:  the gateway holds the credential needed to use it
  - reachable:               live network reachability (bool | null);
                             null means it could NOT be safely probed
  - probe_latency_ms:        measured latency of the reachability probe (number | null)
  - models_in_routes:        count of canonical models routed to this provider

Probes are ZERO-COST: they issue an unauthenticated HTTP request to the
provider's endpoint and treat ANY HTTP/TLS response (including a 401/403 auth
challenge) as reachable. No inference is performed and no paid tokens are
consumed. Raw upstream response bodies are never captured or returned; only a
short, non-sensitive status class name or fixed message is recorded as `reason`.
"""
import asyncio
import time
from datetime import datetime, timezone

import httpx

from . import providers
from .config import settings
from .db import gateway_status

PROBE_TIMEOUT = 3.0


async def _probe(url: str):
    """Return ``(reachable, latency_ms, reason)``.

    reachable is True on any HTTP/TLS response, False on connection/DNS/timeout
    failure, and None when probing is unsafe or errors unexpectedly. ``reason``
    is a short, non-sensitive class name or fixed message (never a response body
    or URL).
    """
    if not url:
        return None, None, "no endpoint configured"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
            try:
                r = await client.head(url, headers={"User-Agent": "clarity-status-probe"})
                if r.status_code >= 405:
                    r = await client.get(url, headers={"User-Agent": "clarity-status-probe"})
            except (httpx.UnsupportedProtocol, httpx.ProtocolError):
                r = await client.get(url, headers={"User-Agent": "clarity-status-probe"})
        latency = (time.monotonic() - start) * 1000.0
        # Any HTTP response means the host is reachable at the TLS/HTTP layer.
        return True, round(latency, 1), None
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
            httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError,
            httpx.HTTPError) as e:
        latency = (time.monotonic() - start) * 1000.0
        return False, round(latency, 1), f"unreachable: {type(e).__name__}"
    except Exception as e:
        return None, None, f"probe error: {type(e).__name__}"


async def _provider_probe(provider: str):
    """Per-provider probe. Local Ollama is never guessed: it is explicitly
    false when its local health endpoint is not reachable."""
    if provider == "local":
        ok, latency, reason = await _probe("http://127.0.0.1:11434/api/version")
        if ok is True:
            return True, latency, None
        return False, latency, (reason or "ollama not running")
    url = providers.base_url(provider)
    if not url:
        return None, None, "no endpoint configured"
    return await _probe(url)


async def build_status() -> dict:
    gw = gateway_status()
    all_providers = (
        set(providers.OPENAI_COMPATIBLE)
        | set(providers.ANTHROPIC)
        | {p for provs in providers.ROUTES.values() for p in provs}
    )
    tasks = {p: _provider_probe(p) for p in sorted(all_providers)}
    probe_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    providers_summary = {}
    for p, res in zip(tasks.keys(), probe_results):
        if isinstance(res, Exception):
            reachable, latency, reason = None, None, f"probe error: {type(res).__name__}"
        else:
            reachable, latency, reason = res
        entry = {
            "configured": True,
            "credentials_configured": providers.has_credentials(p),
            "reachable": reachable,
            "probe_latency_ms": latency,
            "models_in_routes": sum(1 for provs in providers.ROUTES.values() if p in provs),
        }
        if reason:
            entry["reason"] = reason
        providers_summary[p] = entry
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gateway": gw,
        "providers": providers_summary,
        "recent_activity": {
            "note": "probe_latency_ms is a live per-request reachability probe; no historical uptime statistics are recorded"
        },
        "failover": {
            "note": "failover events are tested via mocked providers (test_failover.py); no real-time events recorded without inference"
        },
    }
