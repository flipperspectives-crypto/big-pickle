# Clarity Windows Control Scripts
# Source-controlled under scripts/windows/
#
# Updates the local Clarity checkout to origin/main using FAST-FORWARD ONLY
# semantics. Never hard-resets, never stashes, never deletes local work. Update
# is reported successful ONLY when runtime attestation confirms the running
# build's current_commit equals the new Git HEAD.

$ErrorActionPreference = 'Stop'

$RepoDir      = Join-Path $HOME 'clarity'
$LauncherDir  = Join-Path $HOME 'Clarity-Launcher'

function Get-Sha($ref) { return (git -C $RepoDir rev-parse $ref).Trim() }

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

# Refuse diverged/unrelated history: the merge-base must be origin/main.
$mergeBase = (git -C $RepoDir merge-base HEAD origin/main).Trim()
if ($mergeBase -ne $originMain) {
    Write-Error "History has diverged from origin/main; refusing update."
}

# Stop the running Clarity process before changing files.
& (Join-Path $LauncherDir 'Stop-Clarity.ps1')

# Save the old SHA as the candidate rollback target.
Set-Content -Path (Join-Path $LauncherDir 'Previous-Clarity-Commit.txt') -Value $oldSha -Encoding utf8

# Fast-forward to the exact origin/main (main or detached HEAD from rollback).
$branch = (git -C $RepoDir rev-parse --abbrev-ref HEAD).Trim()
if ($branch -eq 'main') {
    git -C $RepoDir merge --ff-only origin/main
} else {
    git -C $RepoDir checkout main
    git -C $RepoDir merge --ff-only origin/main
}

# Hard git verification.
$newHead = Get-Sha HEAD
$branch  = (git -C $RepoDir rev-parse --abbrev-ref HEAD).Trim()
if ($newHead -ne $originMain -or $branch -ne 'main') {
    Write-Error "Git verification failed after update."
}

# Run Start attestation; a failure must NOT be reported as success.
$runtimeOk = $false
try {
    & (Join-Path $LauncherDir 'Start-Clarity.ps1')
    $startEv = Get-Content (Join-Path $LauncherDir 'Last-Start.json') -Raw | ConvertFrom-Json
    $runtimeOk = ($startEv.runtime_commit -eq $newHead)
} catch {
    $runtimeOk = $false
}

$gitVerified     = ($newHead -eq $originMain)
$runtimeVerified = $runtimeOk

if ($gitVerified -and $runtimeVerified) {
    $ev = [ordered]@{
        timestamp_utc      = (Get-Date).ToUniversalTime().ToString('o')
        previous_commit    = $oldSha
        current_commit     = $newHead
        origin_main_commit = $originMain
        runtime_commit     = $startEv.runtime_commit
        git_verified       = $true
        runtime_verified   = $true
    }
    $ev | ConvertTo-Json | Set-Content -Path (Join-Path $LauncherDir 'Last-Update.json') -Encoding utf8
    Write-Host "UPDATE COMPLETE"
} else {
    Write-Host "UPDATE INSTALLED BUT RUNTIME VERIFICATION FAILED"
    Write-Host ("git_head=" + $newHead.Substring(0, 12) + " runtime=" + $startEv.runtime_commit.Substring(0, 12))
    # Previous SHA is preserved in Previous-Clarity-Commit.txt for manual rollback.
    # History is intentionally NOT rewritten.
}
