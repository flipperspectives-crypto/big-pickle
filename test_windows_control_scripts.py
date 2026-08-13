"""Offline structural/security tests for the Windows control scripts.

These scripts are NOT executed here (this is a Linux/OpenCode environment and the
scripts target Windows PowerShell). We verify them by parsing their source text
to prove the safety and attestation guarantees required by Phase C2.
"""
import os
import re
import shutil

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT_DIR = os.path.join(HERE, "scripts", "windows")

EXPECTED_FILES = [
    "Start-Clarity.ps1",
    "Stop-Clarity.ps1",
    "Update-Clarity.ps1",
    "Rollback-Clarity.ps1",
    "Install-Clarity-Launchers.ps1",
    "Create-Clarity-Recovery-Bundle.ps1",
    "README.md",
]

PS1 = [f for f in EXPECTED_FILES if f.endswith(".ps1")]


def _read(name):
    with open(os.path.join(SCRIPT_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def scripts():
    return {n: _read(n) for n in PS1}


def test_expected_files_exist():
    for n in EXPECTED_FILES:
        assert os.path.isfile(os.path.join(SCRIPT_DIR, n)), f"missing {n}"


def test_source_controlled_header(scripts):
    for n, t in scripts.items():
        assert "Clarity Windows Control Scripts" in t
        assert "scripts/windows/" in t


def test_no_hard_reset(scripts):
    for n, t in scripts.items():
        assert not re.search(r"git\s+reset\s+--hard", t), f"{n} uses git reset --hard"
        assert "reset --hard" not in t


def test_no_git_clean(scripts):
    for n, t in scripts.items():
        assert "git clean" not in t


def test_no_stash(scripts):
    for n, t in scripts.items():
        assert "git stash" not in t


def test_no_plaintext_credentials(scripts):
    bad = ("sk-", "gw_", "password", "bearer ", "api_key =", "secret")
    for n, t in scripts.items():
        low = t.lower()
        for b in bad:
            assert b not in low, f"{n} may contain credential token '{b}'"


def test_no_wireguard_or_private_keys(scripts):
    # Behavioral: no actual WireGuard / private-key handling (mentions in
    # documentation comments are allowed).
    for n, t in scripts.items():
        assert not re.search(r"\*\.key\b", t), f"{n} references *.key files"
        assert not re.search(r"\.pem\b", t), f"{n} references *.pem files"
        assert not re.search(r"wg[0-9]?\b.*\.conf", t), f"{n} references WireGuard conf"


def test_start_host_port_and_uvicorn(scripts):
    t = scripts["Start-Clarity.ps1"]
    assert "0.0.0.0" in t
    assert "7860" in t
    assert "uvicorn" in t and "app.main:app" in t


def test_start_readiness_endpoints(scripts):
    t = scripts["Start-Clarity.ps1"]
    assert "/health" in t
    assert "/v1/build" in t
    assert "/v1/diagnostics" in t
    assert "/v1/models" in t


def test_start_compares_runtime_to_git_head(scripts):
    t = scripts["Start-Clarity.ps1"]
    assert "rev-parse HEAD" in t
    assert "current_commit" in t
    assert "expected" in t
    assert re.search(r"current_commit\s*-ne\s*\$expected", t) or re.search(
        r"current_commit\s*-eq\s*\$expected", t
    )


def test_qwen3_readiness_preserved(scripts):
    t = scripts["Start-Clarity.ps1"]
    assert "local:qwen3:1.7b" in t


def test_start_emits_ready_and_mismatch(scripts):
    t = scripts["Start-Clarity.ps1"]
    assert "CLARITY READY" in t
    assert "RUNTIME BUILD MISMATCH" in t


def test_stop_checks_identity(scripts):
    t = scripts["Stop-Clarity.ps1"]
    assert "uvicorn" in t and "app.main:app" in t
    assert "netstat" in t or "Get-CimInstance" in t
    assert "Last-Stop.json" in t


def test_stop_leaves_evidence_safe(scripts):
    t = scripts["Stop-Clarity.ps1"]
    # The command line is read to verify identity but never persisted.
    assert "CommandLine" in t
    m = re.search(r"\$evidence\s*=\s*\[ordered\]@\{(.*?)\}", t, re.S)
    assert m, "stop evidence block not found"
    block = m.group(1).lower()
    for key in ("timestamp_utc", "process_found", "clarity_process_stopped"):
        assert key in block
    assert "commandline" not in block  # command line content is NOT stored


def test_update_fetch_ff(scripts):
    t = scripts["Update-Clarity.ps1"]
    assert "fetch" in t
    assert "--ff-only" in t or "ff-only" in t


def test_update_head_origin_main_check(scripts):
    t = scripts["Update-Clarity.ps1"]
    assert "origin/main" in t and "HEAD" in t
    assert re.search(r"newHead\s*-ne\s*\$originMain", t) or "originMain" in t


def test_update_requires_runtime_attestation(scripts):
    t = scripts["Update-Clarity.ps1"]
    assert "Last-Start.json" in t
    assert "runtime_verified" in t
    assert "UPDATE COMPLETE" in t
    assert "UPDATE INSTALLED BUT RUNTIME VERIFICATION FAILED" in t
    assert "Previous-Clarity-Commit.txt" in t


def test_rollback_detached(scripts):
    t = scripts["Rollback-Clarity.ps1"]
    assert "checkout --detach" in t


def test_rollback_never_moves_origin_main(scripts):
    t = scripts["Rollback-Clarity.ps1"]
    # Behavioral: rollback must never push or move the origin/main ref. Comments
    # that *describe* this guarantee are fine; only operations are checked.
    assert "git push" not in t
    assert not re.search(r"checkout\s+origin/main", t)
    assert not re.search(r"reset\b[^\n]*origin/main", t)


def test_rollback_requires_confirmation(scripts):
    t = scripts["Rollback-Clarity.ps1"]
    assert "YES" in t
    assert "Read-Host" in t
    assert "Redo-Clarity-Commit.txt" in t


def test_installer_shortcuts(scripts):
    t = scripts["Install-Clarity-Launchers.ps1"]
    for name in ("Start Clarity", "Stop Clarity", "Update Clarity",
                 "Rollback Clarity", "Create Clarity Recovery Bundle"):
        assert name in t, f"missing shortcut {name}"


def test_installer_backup(scripts):
    t = scripts["Install-Clarity-Launchers.ps1"]
    assert ".backup-" in t


def test_recovery_excludes_secrets(scripts):
    t = scripts["Create-Clarity-Recovery-Bundle.ps1"]
    # Behavioral: the bundle must never copy/embed these secret-bearing sources.
    # (Mentioning them in exclusion comments is allowed.)
    assert not re.search(r"Copy-Item[^\n]*gateway\.db", t), "recovery copies gateway.db"
    assert not re.search(r"Copy-Item[^\n]*\.env", t), "recovery copies .env"
    assert not re.search(r"Copy-Item[^\n]*\.venv", t), "recovery copies .venv"
    assert not re.search(r"\*\.key\b", t)


def test_recovery_manifest_sha256(scripts):
    t = scripts["Create-Clarity-Recovery-Bundle.ps1"]
    assert "SHA256" in t
    assert "SHA256MANIFEST" in t or "Get-FileHash" in t


def test_recovery_contains_safe_evidence(scripts):
    t = scripts["Create-Clarity-Recovery-Bundle.ps1"]
    for tok in ("clarity.bundle", "RECOVERY_README", "Last-Start.json",
                "Last-Update.json", "Last-Rollback.json"):
        assert tok in t


def test_evidence_schema_no_secrets(scripts):
    t = scripts["Start-Clarity.ps1"]
    m = re.search(r"\$evidence\s*=\s*\[ordered\]@\{(.*?)\}", t, re.S)
    assert m, "evidence block not found"
    block = m.group(1).lower()
    for allowed in ("expected_commit", "runtime_commit", "asset_version",
                   "health_ok", "diagnostics_process_healthy",
                   "local_models_ready", "qwen3_ready", "timestamp_utc"):
        assert allowed in block, f"evidence missing {allowed}"
    for bad in ("ollama_base_url", "api_key", "prompt", "response",
               "secret", "bearer", "password"):
        assert bad not in block, f"evidence may leak {bad}"


def test_powershell_availability_printed():
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    print("PowerShell execution available in this environment:", bool(pwsh))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
