"""Suite-wide pytest isolation harness.

Establishes baseline environment variables BEFORE any test module imports
``app``, so the shared global ``app`` object and the ``settings`` singleton are
configured deterministically regardless of test-file import order.

Without this, the first file to import ``app`` (e.g. ``test_admin_auth.py``)
freezes ``settings`` using whatever environment was present at that moment.
Later files that set ``GATEWAY_ADMIN_KEY`` via ``os.environ.setdefault`` at
module import have no effect on the already-imported ``settings`` singleton,
which made every admin-dependent test order-dependent (failing whenever an
earlier file imported ``app`` first).

This fixes that class of contamination without touching runtime code: it only
sets the test environment. Per-test isolation (unique DBs, settings restore,
router/provider monkeypatch restore) is handled by the individual test fixtures.
"""

import os

# Test-owned baseline. FORCE these (not setdefault) so a production parent shell
# -- e.g. the Windows Clarity instance -- cannot leak real values into the suite
# and make results order/environment dependent.
os.environ["GATEWAY_ADMIN_KEY"] = "testadmin"
# Ensure a writable DB path is always configured before app import so the
# default "/data/gateway.db" (which does not exist in the test sandbox) is
# never used regardless of import order.
_SHARED_DB = "/tmp/gateway_pytest_shared.db"
if os.path.exists(_SHARED_DB):
    try:
        os.remove(_SHARED_DB)
    except OSError:
        pass
os.environ["GATEWAY_DB"] = _SHARED_DB
# Default the network mode to testnet so mainnet fail-closed behavior is the
# deterministic baseline (individual tests monkeypatch settings for mainnet).
os.environ["X402_NETWORK_MODE"] = "testnet"

# Strip any inherited production payment credentials so mainnet fail-closed
# tests and testnet fixtures are deterministic (tests that need CDP creds set
# them explicitly via monkeypatch).
for _v in (
    "CDP_API_KEY_ID",
    "CDP_API_KEY_SECRET",
    "X402_CDP_API_KEY_ID",
    "X402_CDP_API_KEY_SECRET",
):
    os.environ.pop(_v, None)
