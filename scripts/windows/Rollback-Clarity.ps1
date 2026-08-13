# Clarity Windows Control Scripts
# Source-controlled under scripts/windows/
#
# Rolls back to the previous commit recorded by Update-Clarity.ps1. Uses a
# DETACHED HEAD checkout at the rollback target. It never moves origin/main,
# never resets/force-updates main, and never deletes local work. Rollback is
# reported successful ONLY when runtime attestation confirms the running build's
# current_commit equals the rollback target.

$ErrorActionPreference = 'Stop'

$RepoDir      = Join-Path $HOME 'clarity'
$LauncherDir  = Join-Path $HOME 'Clarity-Launcher'

function Get-Sha($ref) { return (git -C $RepoDir rev-parse $ref).Trim() }

function Assert-CleanTree {
    $status = (git -C $RepoDir status --porcelain)
    if ($status.Trim().Length -ne 0) {
        Write-Error "Working tree is not clean; refusing to roll back."
    }
}

$prevFile = Join-Path $LauncherDir 'Previous-Clarity-Commit.txt'
if (-not (Test-Path $prevFile)) {
    Write-Error "No Previous-Clarity-Commit.txt found; cannot roll back."
}
$target = (Get-Content $prevFile -Raw).Trim()
if ($target -notmatch '^[0-9a-f]{40}$') {
    Write-Error "Invalid rollback target SHA: $target"
}
git -C $RepoDir cat-file -e $target
if ($LASTEXITCODE -ne 0) {
    Write-Error "Rollback target commit does not exist: $target"
}

$confirm = Read-Host ("Roll back to " + $target.Substring(0, 12) + "? Type YES to continue")
if ($confirm -ne 'YES') { Write-Host "Rollback cancelled."; exit 0 }

Assert-CleanTree
$fromSha = Get-Sha HEAD

# Stop the running Clarity process.
& (Join-Path $LauncherDir 'Stop-Clarity.ps1')

# Preserve the current SHA so a rollback can later be redone.
Set-Content -Path (Join-Path $LauncherDir 'Redo-Clarity-Commit.txt') -Value $fromSha -Encoding utf8

# Detached HEAD at the rollback target. origin/main is NOT touched.
git -C $RepoDir checkout --detach $target

# Run Start attestation; a mismatch must NOT be reported as success.
try {
    & (Join-Path $LauncherDir 'Start-Clarity.ps1')
    $startEv = Get-Content (Join-Path $LauncherDir 'Last-Start.json') -Raw | ConvertFrom-Json
} catch {
    $startEv = $null
}

$runtimeVerified = ($null -ne $startEv -and $startEv.runtime_commit -eq $target)

$ev = [ordered]@{
    timestamp_utc     = (Get-Date).ToUniversalTime().ToString('o')
    from_commit       = $fromSha
    rollback_commit   = $target
    runtime_commit    = if ($startEv) { $startEv.runtime_commit } else { $null }
    runtime_verified  = $runtimeVerified
}
$ev | ConvertTo-Json | Set-Content -Path (Join-Path $LauncherDir 'Last-Rollback.json') -Encoding utf8

if ($runtimeVerified) {
    Write-Host "ROLLBACK COMPLETE"
} else {
    Write-Host "ROLLBACK INSTALLED BUT RUNTIME VERIFICATION FAILED"
}
