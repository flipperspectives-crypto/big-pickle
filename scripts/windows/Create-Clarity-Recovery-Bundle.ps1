# Clarity Windows Control Scripts
# Source-controlled under scripts/windows/
#
# Builds a timestamped, SAFE recovery bundle under $HOME\Clarity-Recovery.
# The archive contains recovery evidence and source only. Runtime state, the
# database (gateway.db), API keys, environment variables, provider credentials,
# WireGuard configs, private keys, .env files, browser/session data, prompts,
# responses, and raw logs are intentionally EXCLUDED.
#
# The RECOVERY_README.txt and SHA256MANIFEST.txt are written INSIDE the staging
# directory so that Compress-Archive actually includes them in the final ZIP.
# The git bundle is only marked successful when its exit code is 0 AND the file
# exists (PowerShell 5.1 does not reliably throw on a native nonzero exit).

$ErrorActionPreference = 'Stop'

$RepoDir      = Join-Path $HOME 'clarity'
$LauncherDir  = Join-Path $HOME 'Clarity-Launcher'
$RecoveryDir  = Join-Path $HOME 'Clarity-Recovery'

if (-not (Test-Path $RecoveryDir)) { New-Item -ItemType Directory -Path $RecoveryDir | Out-Null }

$stamp     = Get-Date -Format 'yyyyMMdd-HHmmss'
$BundleDir = Join-Path $RecoveryDir ("Clarity-Recovery-" + $stamp)
$Staging   = Join-Path $BundleDir 'staging'
New-Item -ItemType Directory -Path $Staging | Out-Null

$current     = (git -C $RepoDir rev-parse HEAD).Trim()
$originMain  = (git -C $RepoDir rev-parse origin/main).Trim()
$checkpoint  = (git -C $RepoDir rev-list -n 1 clarity-local-v1.0.0).Trim()

# Git bundle of the repo. Check $LASTEXITCODE explicitly; do NOT trust try/catch.
$bundleOk = $false
$bundlePath = Join-Path $Staging 'clarity.bundle'
git -C $RepoDir bundle create $bundlePath --all
if ($LASTEXITCODE -eq 0 -and (Test-Path $bundlePath)) { $bundleOk = $true }

# Safe launcher source scripts.
Copy-Item -Path (Join-Path $RepoDir 'scripts\windows\*.ps1') -Destination $Staging -Force

# Safe evidence JSONs only (present if previously produced).
foreach ($ev in @('Last-Start.json','Last-Stop.json','Last-Update.json','Last-Rollback.json')) {
    $src = Join-Path $LauncherDir $ev
    if (Test-Path $src) { Copy-Item -Path $src -Destination $Staging -Force }
}

# Recovery README (written INSIDE staging so it lands in the ZIP).
$readme = @"
Clarity Recovery Bundle
Generated: $(Get-Date -Format 'o')

Current commit:               $current
Origin/main commit:           $originMain
Frozen checkpoint (clarity-local-v1.0.0): $checkpoint
Git bundle included:          $bundleOk

Verify bundle hashes:
  Compare SHA256MANIFEST.txt against the included files with any SHA-256 tool.

Restore the git bundle:
  git clone clarity.bundle clarity-restore
  (or) git fetch clarity.bundle

IMPORTANT: Runtime state, the database (gateway.db), API keys, environment
variables, provider credentials, WireGuard configs, private keys, .env files,
browser/session data, prompts, responses, and raw logs are intentionally
EXCLUDED from this archive. This bundle contains evidence and source only.
"@
Set-Content -Path (Join-Path $Staging 'RECOVERY_README.txt') -Value $readme -Encoding utf8

# SHA-256 manifest (written INSIDE staging; excludes itself).
$manifest = @()
Get-ChildItem $Staging -Recurse -File | Where-Object { $_.Name -ne 'SHA256MANIFEST.txt' } | ForEach-Object {
    $hash = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
    $rel  = $_.FullName.Substring($Staging.Length + 1)
    $manifest += "$hash  $rel"
}
Set-Content -Path (Join-Path $Staging 'SHA256MANIFEST.txt') -Value ($manifest -join "`n") -Encoding utf8

# Archive the staging CONTENTS (now includes README + manifest).
$zip = Join-Path $RecoveryDir ("Clarity-Recovery-$stamp.zip")
Compress-Archive -Path ($Staging + '\*') -DestinationPath $zip -Force

if (-not (Test-Path $zip)) {
    Write-Error "Recovery ZIP was not created: $zip"
}

# Remove temporary staging ONLY after the ZIP is confirmed.
Remove-Item $Staging -Recurse -Force

Write-Host "Recovery bundle created: $zip"
