# Status Demo — Evidence

Phase 3 reliability/status proof for **Clarity** (`big-pickle`). Adds a public,
read-only `GET /v1/status` endpoint and a website Status section backed **only**
by that endpoint. No x402 architecture was changed.

## 1. What was audited

- `app/providers.py` — `ROUTES` (model → ordered provider list), `needs_key()`,
  `upstream_model()`, `price_for()`. Failover targets are declared here.
- `app/router.py` — `run_completion()` iterates `providers_for(model)`, catches
  `UpstreamError`, and falls through to the next provider; all providers
  exhausted → `UpstreamError(502, "all providers failed: …")`. No persistent
  failover-event log (in-memory `last_err` only).
- `app/main.py` — `/v1/chat/completions` calls `run_completion` and maps
  `UpstreamError` → `HTTPException`. No status endpoint existed.

**Conclusion:** failover logic is functional and reused as-is; it only lacked a
public, evidence-backed status surface. This change adds that surface without
altering routing.

## 2. The `/v1/status` endpoint

Added in `app/main.py` (`gateway_status()` helper added to `app/db.py`). It
returns **only** evidence-backed data:

- `status` — `"healthy"` (gateway process is serving).
- `timestamp` — UTC time the response was generated.
- `gateway` — `active_keys` (DB `COUNT`) and `total_balance_usd`
  (DB `SUM(balance)`). Real aggregates, no secrets.
- `providers` — every configured provider with `needs_key` (a **config** fact
  from `providers.needs_key()`, not an inference probe) and `configured_models`
  (count of `ROUTES` entries that include it).
- `recent_activity.note` — explicitly states **no probe latency is recorded**.
- `failover.note` — explicitly states failover is **tested via mocked
  providers (`test_failover.py`)**, not observed live.

### Fields deliberately NOT invented

- No uptime percentage, no historical success rate, no latency histogram — none
  are measured, so none are reported.
- No API keys, `skey`, bearer tokens, admin key, private hostnames, or upstream
  error bodies appear in the response.

## 3. Live sample output (captured from `GET /v1/status`)

```json
{
  "status": "healthy",
  "timestamp": "2026-08-12T16:31:10Z",
  "gateway": {
    "active_keys": 0,
    "total_balance_usd": 0
  },
  "providers": {
    "anthropic":     { "needs_key": true,  "configured_models": 2 },
    "cerebras":      { "needs_key": true,  "configured_models": 6 },
    "deepinfra":     { "needs_key": true,  "configured_models": 4 },
    "fireworks":      { "needs_key": true, "configured_models": 0 },
    "groq":          { "needs_key": true,  "configured_models": 6 },
    "huggingface":   { "needs_key": false, "configured_models": 4 },
    "local":         { "needs_key": true,  "configured_models": 5 },
    "openai":        { "needs_key": true,  "configured_models": 2 },
    "openrouter":    { "needs_key": true,  "configured_models": 0 },
    "together":      { "needs_key": true,  "configured_models": 2 }
  },
  "recent_activity": {
    "note": "no external probe latency recorded in this session; measurement infrastructure not wired"
  },
  "failover": {
    "note": "failover events are tested via mocked providers (test_failover.py); no real-time events recorded without inference"
  }
}
```

`huggingface` shows `needs_key: false` here because no `HF_TOKEN`/`GATEWAY_HF_KEY`
is set — which is accurate: the HF router can serve some models without a key.

## 4. Test results

### `test_status.py` (new) — 4 passed

- `test_status_endpoint_returns_json` — shape + evidence-backed fields present.
- `test_status_no_secret_exposure` — asserts none of
  `admin|skey|gw_|bearer|token|api_key|secret` appear anywhere in the response.
- `test_status_probe_note_present` — probe latency honestly labeled unmeasured.
- `test_status_failover_note_present` — failover honestly labeled mocked-tested.

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
- Providers (count)
- Last checked (UTC)
- A table of every provider with `needs_key` and `configured_models`
- The honest failover note

The section is driven **solely** by the real `/v1/status` JSON — no hard-coded
values.

## 6. Files changed

| File | Change |
|------|--------|
| `app/main.py` | Added `GET /v1/status` (imports `datetime/timezone`, `gateway_status`). |
| `app/db.py` | Added `gateway_status()` (active key count + total balance). |
| `test_status.py` | **New** — 4 status tests (shape, no-secret, honest notes). |
| `static/index.html` | Added `#status` nav link + Status section + fetch script. |
| `docs/evidence/status-demo.md` | **New** — this evidence record. |

No changes to `app/providers.py`, `app/router.py`, `app/config.py`,
`app/x402.py`, or `requirements.txt`. x402 architecture untouched.

## 7. Verification summary

- ✅ `GET /v1/status` returns only evidence-backed fields; no secrets leaked.
- ✅ No invented uptime/latency/statistics.
- ✅ Existing failover mechanism reused (no parallel routing system).
- ✅ Tests pass: `test_status.py` (4) + `test_failover.py` (mocked, labeled).
- ✅ Website Status section reads exclusively from `/v1/status`.
- ⏸️ Not deployed; not pushed. Live failover not claimed (mocked only).
