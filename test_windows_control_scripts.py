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


# --------------------------------------------------------------------------
# C2.1 regression coverage
# --------------------------------------------------------------------------

def test_stop_does_not_clobber_automatic_pid(scripts):
    t = scripts["Stop-Clarity.ps1"]
    # must not assign to the automatic $PID / $pid variable
    assert not re.search(r"(?i)\$pid\s*=", t), "Stop-Clarity.ps1 assigns to $pid"
    # safe local name is used instead
    assert "$clarityPid" in t
    # Get-ClarityPid still returns the value
    assert "function Get-ClarityPid" in t


def test_start_gates_perform_fresh_reads(scripts):
    t = scripts["Start-Clarity.ps1"]
    # every readiness gate must call Get-Json inside its scriptblock
    gates = re.findall(r"Wait-For\s+'[^']+'\s*\{([^}]*)\}", t, re.S)
    assert len(gates) >= 5, f"expected >=5 readiness gates, found {len(gates)}"
    for g in gates:
        assert "Get-Json" in g, f"gate does not perform a fresh GET: {g!r}"


def test_start_build_gate_reads_build_each_retry(scripts):
    t = scripts["Start-Clarity.ps1"]
    assert re.search(r"Wait-For\s+'build\.current_commit == git HEAD'\s*\{[^}]*Get-Json[^}]*\$expected", t, re.S)


def test_start_port_safety(scripts):
    t = scripts["Start-Clarity.ps1"]
    assert "Test-PortListening" in t
    assert re.search(r"LISTENING", t)
    assert "PORT 7860 OCCUPIED OR BUILD MISMATCH" in t
    # when reusing, uvicorn must NOT be started again
    assert re.search(r"if\s*\(-not \$reuse\)\s*\{[\s\S]*Start-Process[\s\S]*uvicorn", t)
    # safe abort (Write-Error) when identity cannot be proven
    assert re.search(r"PORT 7860 OCCUPIED OR BUILD MISMATCH[\s\S]*Write-Error", t)


def test_start_no_duplicate_against_unknown_listener(scripts):
    t = scripts["Start-Clarity.ps1"]
    # the occupied-but-unproven branch must not start uvicorn
    assert not re.search(r"PORT 7860 OCCUPIED OR BUILD MISMATCH[\s\S]*Start-Process.*uvicorn", t)


def test_start_uses_safe_short_sha(scripts):
    t = scripts["Start-Clarity.ps1"]
    assert "function ShortSha" in t
    # final attestation never calls .Substring() directly on runtime commit
    assert re.search(r"ShortSha\s+\$finalBuild\.current_commit", t)


def test_update_uses_correct_ancestor_proof(scripts):
    t = scripts["Update-Clarity.ps1"]
    assert "merge-base --is-ancestor" in t
    assert re.search(r"merge-base --is-ancestor HEAD origin/main", t)
    assert re.search(r"if\s*\(\$LASTEXITCODE -ne 0\)", t)
    # the old incorrect equality test must be gone
    assert "merge-base HEAD origin/main" not in t


def test_update_behind_main_accepted_structurally(scripts):
    t = scripts["Update-Clarity.ps1"]
    # --is-ancestor by nature allows HEAD behind origin/main; ensure no
    # equality-to-origin requirement blocks a fast-forward
    assert "merge-base --is-ancestor" in t
    assert "--ff-only" in t


def test_update_already_current_preserves_rollback_target(scripts):
    t = scripts["Update-Clarity.ps1"]
    assert "$alreadyCurrent = ($oldSha -eq $originMain)" in t
    # Previous-Clarity-Commit.txt is only written when NOT already current
    assert re.search(
        r"if\s*\(-not \$alreadyCurrent\)\s*\{[\s\S]*?Previous-Clarity-Commit\.txt", t
    )
    assert "CLARITY IS UP TO DATE" in t


def test_update_null_runtime_evidence_safe(scripts):
    t = scripts["Update-Clarity.ps1"]
    # guard before dereferencing runtime_commit
    assert re.search(r"if\s*\(\$startEv -and \$startEv\.runtime_commit\)", t)
    # failure path uses a safe 'unknown' value
    assert re.search(r"rtShown.*'unknown'", t) or "'unknown'" in t
    # no bare "$startEv.runtime_commit.Substring" on a possibly-null object
    assert "if ($startEv -and $startEv.runtime_commit)" in t


def test_update_installs_requirements_into_venv(scripts):
    t = scripts["Update-Clarity.ps1"]
    # The venv must live under the repo dir (C:\Users\fyou1\clarity\.venv on the
    # host), so the install never touches a global/system interpreter.
    assert re.search(r"Join-Path\s+\$RepoDir\s+'.venv", t), "venv must live under repo dir"
    # Install must use the venv python, not a global pip/python.
    assert re.search(r"\$VenvPython\s+-m\s+pip\s+install\s+-r", t), \
        "updater must install requirements via the venv python"
    assert "requirements.txt" in t
    # The install must run before (re)starting or reusing Clarity.
    pip_idx = t.find("-m pip install -r")
    start_idx = t.find("Start-Clarity.ps1")
    assert pip_idx != -1 and start_idx != -1 and pip_idx < start_idx, \
        "requirements install must run before starting/reusing Clarity"
    # Must not perform a global or --user install.
    assert not re.search(r"(?m)^\s*pip\s+install\b", t), "must not call global pip"
    assert not re.search(r"pip\s+install\b[^\n]*--user", t), "must not use --user install"
    # Skips re-install when requirements are byte-for-byte unchanged.
    assert "Get-FileHash" in t and "requirements.sha256.txt" in t
    # Does not handle or expose secrets.
    low = t.lower()
    for b in ("sk-", "gw_", "password", "bearer ", "api_key =", "secret"):
        assert b not in low, f"updater may contain credential token '{b}'"


def test_rollback_null_failure_path_safe(scripts):
    t = scripts["Rollback-Clarity.ps1"]
    # null guard before dereferencing runtime_commit
    assert re.search(r"\$null -ne \$startEv", t)
    # evidence is written even when Start failed
    assert "Last-Rollback.json" in t
    assert re.search(r"runtime_commit\s*=\s*if\s*\(\$startEv\)", t)


def test_recovery_readme_inside_staging(scripts):
    t = scripts["Create-Clarity-Recovery-Bundle.ps1"]
    # README is written into $Staging (so it lands in the ZIP)
    assert re.search(r"Join-Path \$Staging 'RECOVERY_README\.txt'", t)


def test_recovery_manifest_inside_staging(scripts):
    t = scripts["Create-Clarity-Recovery-Bundle.ps1"]
    # manifest is written into $Staging (so it lands in the ZIP)
    assert re.search(r"Join-Path \$Staging 'SHA256MANIFEST\.txt'", t)
    # manifest excludes itself
    assert re.search(r"Where-Object \{\s*\$_\.Name -ne 'SHA256MANIFEST\.txt'", t)


def test_recovery_bundle_checks_last_exit_code(scripts):
    t = scripts["Create-Clarity-Recovery-Bundle.ps1"]
    assert re.search(r"bundle create", t)
    assert re.search(r"\$LASTEXITCODE -eq 0", t)
    assert re.search(r"\$bundleOk = \$true", t)


def test_recovery_zip_includes_readme_and_manifest(scripts):
    t = scripts["Create-Clarity-Recovery-Bundle.ps1"]
    # both README and manifest are staged before Compress-Archive
    staging_writes = t.split("Compress-Archive")[0]
    assert "RECOVERY_README.txt" in staging_writes
    assert "SHA256MANIFEST.txt" in staging_writes


# --------------------------------------------------------------------------
# C2.2 clean-tree hotfix regression coverage
# --------------------------------------------------------------------------

def test_clean_tree_assertion_array_based(scripts):
    for name in ("Rollback-Clarity.ps1", "Update-Clarity.ps1"):
        t = scripts[name]
        # status output is collected into an array (never a bare $null scalar)
        assert re.search(r"\$statusLines = @\(git -C \$RepoDir status --porcelain\)", t), \
            f"{name} must collect porcelain output into an array"
        # git-status failure is rejected
        assert re.search(r"if \(\$LASTEXITCODE -ne 0\)", t), \
            f"{name} must check \$LASTEXITCODE"
        # non-empty porcelain output is rejected (Count, not .Trim())
        assert re.search(r"if \(\$statusLines\.Count -gt 0\)", t), \
            f"{name} must reject non-empty porcelain via Count"


def test_clean_tree_no_trim_on_status(scripts):
    for name in ("Rollback-Clarity.ps1", "Update-Clarity.ps1"):
        t = scripts[name]
        # the porcelain result must NOT be .Trim()'d (the null-valued crash)
        assert "status --porcelain).Trim(" not in t, \
            f"{name} still calls .Trim() on the status result"


def test_clean_tree_rejects_dirty(scripts):
    for name, verb in (("Rollback-Clarity.ps1", "roll back"),
                       ("Update-Clarity.ps1", "update")):
        t = scripts[name]
        assert re.search(r'Write-Error "Working tree is not clean; refusing to ' + re.escape(verb) + r'\."', t), \
            f"{name} must reject a dirty working tree"


def test_clean_tree_git_failure_rejected(scripts):
    for name in ("Rollback-Clarity.ps1", "Update-Clarity.ps1"):
        t = scripts[name]
        assert 'Write-Error "Unable to determine Git working-tree status."' in t, \
            f"{name} must reject git-status failure"


def test_clean_tree_accepts_zero_output(scripts):
    # Structural guarantee: a clean tree produces an empty array (Count == 0),
    # so the dirty-tree branch is skipped and no method is called on $null.
    for name in ("Rollback-Clarity.ps1", "Update-Clarity.ps1"):
        t = scripts[name]
        assert re.search(r"\$statusLines = @\(git -C \$RepoDir status --porcelain\)", t)
        assert re.search(r"if \(\$statusLines\.Count -gt 0\)", t)
        assert "status --porcelain).Trim(" not in t


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
