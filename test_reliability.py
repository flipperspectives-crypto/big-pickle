"""Reliability regression tests for the Solana-devnet x402 dependency pin and
Base/Solana config invariants.

These prove:
  * requirements.txt pins solana to 0.39.0 (preventing incompatible 0.40.x that
    breaks the x402 2.19.0 SVM path), while Base EVM behavior is unchanged.
  * The Base mainnet x402 config constants are unchanged.
  * The Solana devnet config constants (network, facilitator, SVM USDC mint)
    are unchanged.
"""
import os
import re

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REQ_PATH = os.path.join(HERE, "requirements.txt")


def _read(p):
    with open(p, "r", encoding="utf-8") as fh:
        return fh.read()


def _parse_requirements(text):
    specs = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue        # strip inline comments / extras before parsing
        line = re.sub(r"\s*#.*$", "", line)
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*(==|>=|<=|~=|!=|==)\s*([^\s;]+)", line)
        if m:
            specs[m.group(1).lower()] = (m.group(2), m.group(3))
    return specs


def test_requirements_pins_solana_below_0_40():
    text = _read(REQ_PATH)
    # Base EVM + Solana SVM extras must both remain enabled.
    assert "x402[fastapi,evm,svm]" in text, "x402 svm extra must remain enabled"
    specs = _parse_requirements(text)
    # solana must be exactly pinned to the version verified working with x402 2.19.0.
    assert "solana" in specs, "solana must be pinned in requirements"
    op, ver = specs["solana"]
    assert op == "==", f"solana must be exactly pinned, got {op}{ver}"
    assert ver == "0.39.0", f"solana must pin 0.39.0 (compat with x402 2.19.0 SVM), got {ver}"
    # Any 0.40.x resolution would remove solana.rpc.api.Client and break x402 SVM.
    assert not re.search(r"solana\s*(==|>=)\s*0\.40", text), "solana 0.40.x must be excluded"
    # solana 0.39.0 resolves a compatible solders on its own; do not over-pin it.
    assert "solders" not in specs, "do not over-pin solders; let solana 0.39.0 resolve it"


def test_requirements_keep_base_evm():
    text = _read(REQ_PATH)
    assert re.search(r"x402\[fastapi,evm,svm\]", text), "Base EVM extra must remain present"


def test_base_mainnet_config_unchanged():
    import app.config as config_mod
    mainnet = config_mod.Settings._X402_MAINNET
    assert mainnet["chain_id"] == "eip155:8453"
    assert mainnet["chain_int"] == 8453
    assert mainnet["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert mainnet["facilitator"] == "https://api.cdp.coinbase.com/platform/v2/x402"
    assert mainnet["rpc"] == "https://mainnet.base.org"


def test_solana_devnet_config_unchanged():
    import app.config as config_mod
    s = config_mod.Settings()
    # Devnet-only CAIP-2 network id; mainnet CAIP-2 must stay rejected elsewhere.
    assert s.X402_SOLANA_NETWORK == "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
    assert s.X402_SOLANA_FACILITATOR_URL == "https://x402.org/facilitator"
    assert s._SOLANA_MAINNET_CAIP2 == "solana:5eykt4UsFv8P8NJdTREpY1vzqKvdp"
    # The SVM scheme must keep deriving the devnet USDC mint automatically.
    from x402.mechanisms.svm.constants import USDC_DEVNET_ADDRESS
    assert USDC_DEVNET_ADDRESS == "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"


def test_solana_pin_matches_runtime_when_installed():
    # If solana happens to be importable in this environment, it must be < 0.40 so
    # the x402 2.19.0 SVM import of solana.rpc.api.Client keeps working.
    solana = pytest.importorskip("solana")
    from importlib.metadata import version
    ver = tuple(int(p) for p in version("solana").split(".")[:2])
    assert ver < (0, 40), f"solana {ver} would break x402 2.19.0 SVM; pin to 0.39.0"
