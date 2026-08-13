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
    $status = (git -C $RepoDir status --porcelain)
    if ($status.Trim().Length -ne 0) {
        Write-Error "Working tree is not clean; refusing to update."
    }
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
