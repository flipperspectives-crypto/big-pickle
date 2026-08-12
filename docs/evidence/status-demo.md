# Status Demo — Evidence

Phase 3 reliability/status proof for **Clarity** (`big-pickle`), corrected in a
second pass to **separate configuration from live provider health**. Adds a
public, read-only `GET /v1/status` endpoint and a website Status section backed
**only** by that endpoint. No x402 architecture was changed.

## 1. What was audited

- `app/providers.py` — `ROUTES` (model → ordered provider list), `needs_key()`,
  `has_credentials()`, `upstream_model()`, `price_for()`, `base_url()`.
- `app/router.py` — `run_completion()` iterates `providers_for(model)`, catches
  `UpstreamError`, and falls through to the next provider; all providers
  exhausted → `UpstreamError(502, "all providers failed: …")`. No persistent
  failover-event log (in-memory `last_err` only).
- `app/main.py` — `/v1/chat/completions` calls `run_completion` and maps
  `UpstreamError` → `HTTPException`. No status endpoint existed.

**Conclusion:** failover logic is functional and reused as-is; it only lacked a
public, evidence-backed status surface. This change adds that surface without
altering routing.

## 2. The `/v1/status` contract (CORRECTED)

The first pass reported pure configuration (`needs_key`, `configured_models`) and
labeled it provider availability — that was wrong. The corrected contract
**separates configuration from live health**:

| Field | Meaning | Source |
|-------|---------|--------|
| `configured` | provider is part of the gateway configuration | code (`ROUTES` / endpoint table) |
| `credentials_configured` | gateway holds the credential needed to use it | `providers.has_credentials()` (env-var check, not a probe) |
| `reachable` | **live** network reachability: `true` / `false` / `null` | zero-cost HTTP/TLS probe |
| `probe_latency_ms` | measured latency of that probe (`number` / `null`) | probe timer |
| `models_in_routes` | count of canonical models routed to this provider | `ROUTES` |
| `reason` | short, non-sensitive note why `reachable` is `false`/`null` | probe result class name / fixed message |

### Probe rules (zero-cost, no inference)
- An unauthenticated `HEAD` (fallback `GET`) is sent to the provider endpoint.
- **Any HTTP/TLS response counts as reachable** — including a 401/403 auth
  challenge. No inference is performed and no paid tokens are consumed.
- Connection / DNS / timeout failures → `reachable: false` with reason
  `unreachable: <ErrorClass>`.
- If a provider has no endpoint to probe → `reachable: null` with reason
  `no endpoint configured` (never guessed).
- **Local Ollama** is probed at `http://127.0.0.1:11434/api/version`; if that is
  not reachable it is reported `reachable: false` (never `null`) — it is an
  explicit local fact, not a guess.
- Raw upstream response bodies / URLs are **never** captured or returned. Only a
  short status class name or fixed message is stored as `reason`.

### Ambiguous field removed
The old `needs_key` field is **removed**. It conflated "requires a key" with
"provider health". It is replaced by the explicit `configured` +
`credentials_configured` pair.

### Honesty guarantees
- No uptime percentage, no historical success rate, no latency histogram is
  invented. `probe_latency_ms` is a **live per-request** value.
- No API keys, `skey`, bearer tokens, admin key, private hostnames, or upstream
  error bodies appear in the response.

## 3. Live sample output (representative; probes injected for the demo)

```json
{
  "status": "healthy",
  "timestamp": "2026-08-12T16:38:44Z",
  "gateway": { "active_keys": 0, "total_balance_usd": 0 },
  "providers": {
    "anthropic":   { "configured": true, "credentials_configured": false, "reachable": true,  "probe_latency_ms": 61.5, "models_in_routes": 2 },
    "cerebras":    { "configured": true, "credentials_configured": false, "reachable": false, "probe_latency_ms": 1200.0, "models_in_routes": 6, "reason": "unreachable: ConnectTimeout" },
    "deepinfra":   { "configured": true, "credentials_configured": false, "reachable": true,  "probe_latency_ms": 95.1, "models_in_routes": 4 },
    "fireworks":   { "configured": true, "credentials_configured": false, "reachable": null,  "probe_latency_ms": null, "models_in_routes": 0, "reason": "no endpoint configured" },
    "groq":        { "configured": true, "credentials_configured": false, "reachable": true,  "probe_latency_ms": 48.2, "models_in_routes": 6 },
    "huggingface": { "configured": true, "credentials_configured": true,  "reachable": true,  "probe_latency_ms": 30.4, "models_in_routes": 4 },
    "local":       { "configured": true, "credentials_configured": true,  "reachable": false, "probe_latency_ms": 1.2,  "models_in_routes": 5, "reason": "ollama not running" },
    "openai":      { "configured": true, "credentials_configured": false, "reachable": true,  "probe_latency_ms": 70.9, "models_in_routes": 2 },
    "openrouter":  { "configured": true, "credentials_configured": false, "reachable": null,  "probe_latency_ms": null, "models_in_routes": 0, "reason": "no endpoint configured" },
    "together":    { "configured": true, "credentials_configured": false, "reachable": null,  "probe_latency_ms": null, "models_in_routes": 2, "reason": "no endpoint configured" }
  },
  "recent_activity": {
    "note": "probe_latency_ms is a live per-request reachability probe; no historical uptime statistics are recorded"
  },
  "failover": {
    "note": "failover events are tested via mocked providers (test_failover.py); no real-time events recorded without inference"
  }
}
```

In this sample, `credentials_configured` is `false` for keyed providers because no
provider keys are set in the environment; `huggingface` and `local` are `true`
(no key required). `cerebras` is explicitly `false` (ConnectTimeout);
`openrouter`/`together`/`fireworks` are `null` (nothing to probe, honestly
labeled); `local` is `false` with `ollama not running`.

## 4. Test results

### `test_status.py` (new) — 4 passed
- `test_status_contract_fields` — all required fields present; `needs_key`
  removed.
- `test_status_honest_unreachable_and_unknown` — unreachable=`false` w/ reason,
  unknown=`null` w/ reason, local=`false` (never `null`).
- `test_status_no_secret_or_raw_error_leak` — asserts none of
  `sk-|gw_|bearer|api_key|secret|token|admin|password|x-api-key|authorization|
  127.0.0.1|localhost|0.0.0.0|/data/|.db|sqlite|traceback|...` appear; provider
  entries never embed credentials or host URLs.
- `test_status_probe_and_failover_notes_honest` — probe + failover notes are
  honest ("live per-request reachability probe", "mocked providers").

### `test_failover.py` (existing, re-run) — `FAILOVER TESTS PASSED`
Mocked-provider failover proof (no paid inference):
- deepinfra down → fallback to `together` (status 200, "failover works").
- zero balance → `402`.
- all providers down → `502` (`all providers failed: down`).

> **Labeled accurately:** this failover evidence is **simulated/mocked** via
> `test_failover.py`. No real provider rollover was observed, because doing so
> requires live provider keys / paid inference which was intentionally **not**
> performed. No live failover is claimed.

## 5. Website Status section

`static/index.html` gained a `#status` nav link and a `Live gateway status`
section that fetches `/v1/status` at load and renders:
- Gateway (status · active keys · balance)
- Providers reachable (live count `X / Y`)
- Last checked (UTC)
- A table of every provider with `configured`, `credentials_configured`,
  `reachable` (yes/no/unknown + reason), `probe_latency_ms`, `models_in_routes`
- The honest failover note

The section is driven **solely** by the real `/v1/status` JSON — no hard-coded
values.

## 6. Files changed (this correction)
| File | Change |
|------|--------|
| `app/status.py` | **New** — `build_status()` + zero-cost probe (`_probe`, `_provider_probe`); separated contract. |
| `app/providers.py` | Added `has_credentials()`. |
| `app/main.py` | `/v1/status` now delegates to `build_status()`; removed inline config-only logic. |
| `static/index.html` | Status section + script updated to new fields. |
| `test_status.py` | **New** — 4 tests for new contract, honest unreachable/unknown, no-leak. |
| `docs/evidence/status-demo.md` | **Updated** — corrected contract + representative output. |

No changes to `app/router.py`, `app/config.py`, `app/x402.py`, or
`requirements.txt`. x402 architecture untouched.

## 7. Probe cache (production hardening)

Live probes are one HTTP round-trip per provider and must not run on every
public `/v1/status` request. `app/status.py` adds an in-memory cache guarded by
an `asyncio.Lock`:

- Results are cached for `PROBE_CACHE_TTL = 45.0s` (within the 30–60s window).
- `get_status()` returns cached results while fresh. On expiry (or when empty)
  it acquires the lock and refreshes the zero-cost probes **once**.
- Concurrent requests that arrive during a refresh wait on the lock, then reuse
  the single refreshed result — no duplicate provider probe storms. The lock is
  re-bound to the running event loop, so it is safe under FastAPI's loop.
- `probe_latency_ms` is always the latency from the **most recent actual probe**
  (cached verbatim; not re-measured on a cache hit).
- `probed_at` (ISO UTC) and `probe_age_seconds` are added at read time so users
  know when the health measurement was taken.
- Zero-cost / no-inference behavior and all secret/raw-error protections are
  unchanged.

### Cache tests (in `test_status.py`)
- `test_cache_reuse_within_ttl` — two requests within TTL → exactly one probe
  batch (`N` calls, `N` = provider count).
- `test_ttl_refresh_after_expiry` — after TTL expires a new batch runs; a
  third request within the new TTL is served from cache.
- `test_concurrent_refresh_single_probe_batch` — 12 simultaneous `get_status()`
  calls → exactly one probe batch (`N` calls), proving no storm.

## 8. Verification summary
- ✅ `/v1/status` separates `configured` / `credentials_configured` /
  `reachable` / `probe_latency_ms` / `models_in_routes`.
- ✅ `reachable` is from a real zero-cost probe (any HTTP/TLS response counts,
  incl. 401/403); no inference, no paid tokens.
- ✅ Unreachable → `false`+reason; unprobeable → `null`+reason; local Ollama →
  explicit `false`.
- ✅ Ambiguous `needs_key` removed.
- ✅ No secrets / host info / raw upstream errors leaked.
- ✅ Probe results cached (45s TTL) behind an async lock; concurrent requests
  share one refresh — verified by tests.
- ✅ `probed_at` / `probe_age_seconds` expose measurement freshness.
- ✅ Tests pass: `test_status.py` (7) + `test_failover.py` (mocked, labeled).
- ✅ Website Status section reads exclusively from `/v1/status`.
- ⏸️ Not deployed; not pushed (see commit). Live failover not claimed (mocked only).
