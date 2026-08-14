# Clarity Windows Control Scripts
# Source-controlled under scripts/windows/
#
# Updates the local Clarity checkout to origin/main using FAST-FORWARD ONLY
# semantics. Never hard-resets, never stashes, never deletes local work. Update
# is reported successful ONLY when runtime attestation confirms the running
# build's current_commit equals the new Git HEAD.
#
# Fast-forward is proven with `git merge-base --is-ancestor HEAD origin/main`
# (exit code 0), which correctly accepts:
#   - main behind origin/main
#   - a detached rollback commit that is an ancestor of origin/main
#   - an already-current main
# and rejects diverged AND unrelated history.

$ErrorActionPreference = 'Stop'

$RepoDir      = Join-Path $HOME 'clarity'
$LauncherDir  = Join-Path $HOME 'Clarity-Launcher'

function Get-Sha($ref) { return (git -C $RepoDir rev-parse $ref).Trim() }

function ShortSha($s) {
    if ($s -is [string] -and $s -match '^[0-9a-f]{40}$') { return $s.Substring(0, 12) }
    if ($s -is [string] -and $s -match '^[0-9a-f]{7,}$') { return $s.Substring(0, [Math]::Min(12, $s.Length)) }
    return 'unknown'
}

function Assert-CleanTree {
    # Windows PowerShell returns $null (not an empty string) for a clean tree,
    # so never call .Trim() on the raw status. Collect into an array and rely
    # on Count, which is 0 for a clean tree.
    $statusLines = @(git -C $RepoDir status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Unable to determine Git working-tree status."
    }
    if ($statusLines.Count -gt 0) {
        Write-Error "Working tree is not clean; refusing to update."
    }
}

function Write-Step($msg) { Write-Host "[update] $msg" }

function Install-RequiresIntoVenv {
    # Ensure the repo's COMMITTED requirements are installed into the project
    # .venv (never globally) before (re)starting Clarity. This prevents a
    # dependency-changing fast-forward from leaving a stale venv missing
    # packages (e.g. the x402 SVM/solana set after moving from
    # x402[fastapi,evm] to x402[fastapi,evm,svm]). Installation is skipped when
    # requirements.txt is byte-for-byte unchanged since the last successful
    # update, so an up-to-date venv is never reinstalled needlessly.
    $VenvPython  = Join-Path $RepoDir '.venv\Scripts\python.exe'
    $reqFile     = Join-Path $RepoDir 'requirements.txt'
    $reqHashFile = Join-Path $LauncherDir 'requirements.sha256.txt'

    if (-not (Test-Path $VenvPython)) {
        Write-Error "Virtualenv python not found at $VenvPython; cannot install dependencies"
    }
    if (-not (Test-Path $reqFile)) {
        Write-Error "requirements.txt not found at $reqFile"
    }

    $newHash = (Get-FileHash -Algorithm SHA256 $reqFile).Hash
    $oldHash = $null
    if (Test-Path $reqHashFile) { $oldHash = (Get-Content $reqHashFile -Raw).Trim() }

    if ($newHash -eq $oldHash) {
        Write-Step "requirements unchanged since last update; skipping venv install"
        return
    }

    Write-Step "requirements changed; installing into project .venv (not global)"
    & $VenvPython -m pip install -r $reqFile
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pip install into .venv failed (exit $LASTEXITCODE)"
    }
    Set-Content -Path $reqHashFile -Value $newHash -Encoding utf8
}

Assert-CleanTree
git -C $RepoDir fetch --prune origin

$oldSha     = Get-Sha HEAD
$originMain = Get-Sha origin/main

# Correct fast-forward proof: HEAD must be an ancestor of origin/main.
git -C $RepoDir merge-base --is-ancestor HEAD origin/main
if ($LASTEXITCODE -ne 0) {
    Write-Error "History has diverged from or is unrelated to origin/main; refusing update."
}

$alreadyCurrent = ($oldSha -eq $originMain)

# Save the old SHA as the candidate rollback target ONLY when actually changing
# commits (never overwrite it with the same SHA on an already-current machine).
if (-not $alreadyCurrent) {
    # Stop the running Clarity process before changing files.
    & (Join-Path $LauncherDir 'Stop-Clarity.ps1')
    Set-Content -Path (Join-Path $LauncherDir 'Previous-Clarity-Commit.txt') -Value $oldSha -Encoding utf8
}

# Fast-forward to the exact origin/main (main or detached HEAD from rollback).
if (-not $alreadyCurrent) {
    $branch = (git -C $RepoDir rev-parse --abbrev-ref HEAD).Trim()
    if ($branch -eq 'main') {
        git -C $RepoDir merge --ff-only origin/main
    } else {
        git -C $RepoDir checkout main
        git -C $RepoDir merge --ff-only origin/main
    }
}

# Hard git verification.
$newHead = Get-Sha HEAD
$branch  = (git -C $RepoDir rev-parse --abbrev-ref HEAD).Trim()
    if ($newHead -ne $originMain -or $branch -ne 'main') {
        Write-Error "Git verification failed after update."
    }

# Ensure committed requirements are installed into the project .venv (never
# globally) before (re)starting Clarity, so a dependency-changing fast-forward
# can never leave a stale venv missing packages.
Install-RequiresIntoVenv

# Run Start attestation; a failure must NOT be reported as success.
$runtimeOk = $false
$startEv = $null
try {
    & (Join-Path $LauncherDir 'Start-Clarity.ps1')
    if (Test-Path (Join-Path $LauncherDir 'Last-Start.json')) {
        $startEv = Get-Content (Join-Path $LauncherDir 'Last-Start.json') -Raw | ConvertFrom-Json
    }
} catch {
    $startEv = $null
}

if ($startEv -and $startEv.runtime_commit) {
    $runtimeOk = ($startEv.runtime_commit -eq $newHead)
}

$gitVerified     = ($newHead -eq $originMain)
$runtimeVerified = $runtimeOk

if ($gitVerified -and $runtimeVerified) {
    $ev = [ordered]@{
        timestamp_utc      = (Get-Date).ToUniversalTime().ToString('o')
        previous_commit    = $oldSha
        current_commit     = $newHead
        origin_main_commit = $originMain
        runtime_commit     = if ($startEv) { $startEv.runtime_commit } else { $null }
        git_verified       = $true
        runtime_verified   = $true
    }
    $ev | ConvertTo-Json | Set-Content -Path (Join-Path $LauncherDir 'Last-Update.json') -Encoding utf8
    if ($alreadyCurrent) {
        Write-Host "CLARITY IS UP TO DATE"
    } else {
        Write-Host "UPDATE COMPLETE"
    }
} else {
    # Previous SHA is preserved in Previous-Clarity-Commit.txt for manual rollback.
    # History is intentionally NOT rewritten.
    Write-Host "UPDATE INSTALLED BUT RUNTIME VERIFICATION FAILED"
    $rtShown = if ($startEv -and $startEv.runtime_commit) { (ShortSha $startEv.runtime_commit) } else { 'unknown' }
    Write-Host ("git_head=" + (ShortSha $newHead) + " runtime=" + $rtShown)
}
