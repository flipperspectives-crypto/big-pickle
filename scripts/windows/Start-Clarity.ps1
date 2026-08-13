# Clarity Windows Control Scripts
# Source-controlled under scripts/windows/
#
# Starts Clarity on Windows with full runtime attestation. The expected commit is
# derived from the ACTUAL checked-out repo (git rev-parse HEAD); it is never
# hard-coded into this script. No real inference is performed by readiness checks.

$ErrorActionPreference = 'Stop'

$RepoDir      = Join-Path $HOME 'clarity'
$LauncherDir  = Join-Path $HOME 'Clarity-Launcher'
$DataDir      = Join-Path $RepoDir 'data'
$VenvPython   = Join-Path $RepoDir '.venv\Scripts\python.exe'
$OllamaUrl    = 'http://127.0.0.1:11434'
$ClarityUrl   = 'http://127.0.0.1:7860'
$DbPath       = Join-Path $DataDir 'gateway.db'

function Write-Step($msg) { Write-Host "[start] $msg" }

function Get-Json($uri) {
    try { return Invoke-RestMethod -Uri $uri -TimeoutSec 5 -ErrorAction Stop }
    catch { return $null }
}

function Get-ExpectedCommit {
    $sha = (git -C $RepoDir rev-parse HEAD).Trim()
    if ($sha -notmatch '^[0-9a-f]{40}$') {
        Write-Error "Expected commit is not a strict 40-char hex SHA: $sha"
    }
    return $sha
}

function Test-Ollama {
    try {
        $null = Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 2 -ErrorAction Stop
        return $true
    } catch { return $false }
}

function Wait-For($name, $scriptBlock, $seconds = 30) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try { if (& $scriptBlock) { Write-Step "gate passed: $name"; return $true } } catch { }
        Start-Sleep -Seconds 1
    }
    Write-Error "readiness gate FAILED: $name"
}

# ---- preconditions --------------------------------------------------------
Write-Step "verifying repo at $RepoDir"
if (-not (Test-Path (Join-Path $RepoDir '.git'))) {
    Write-Error "Clarity repo not found at $RepoDir"
}
if (-not (Test-Path $VenvPython)) {
    Write-Error "Virtualenv python not found at $VenvPython"
}

$expected = Get-ExpectedCommit
Write-Step ("expected commit " + $expected.Substring(0, 12))

# ---- Ollama --------------------------------------------------------------
if (Test-Ollama) {
    Write-Step "Ollama already reachable"
} else {
    Write-Step "starting Ollama"
    Start-Process -FilePath 'ollama' -ArgumentList 'serve' -WindowStyle Hidden
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-Ollama) { $ok = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ok) { Write-Error "Ollama did not become ready" }
}

# ---- start Clarity --------------------------------------------------------
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir | Out-Null }
$env:GATEWAY_DB = $DbPath
$env:OLLAMA_BASE_URL = $OllamaUrl
Write-Step ("starting Clarity on " + $ClarityUrl)
$null = Start-Process -FilePath $VenvPython `
    -ArgumentList '-m','uvicorn','app.main:app','--host','0.0.0.0','--port','7860' `
    -WorkingDirectory $RepoDir -WindowStyle Hidden

# ---- readiness gates (no inference) --------------------------------------
Wait-For 'health'                  { (Get-Json "$ClarityUrl/health").status -eq 'ok' }

$build = Get-Json "$ClarityUrl/v1/build"
Wait-For 'build.current_commit == git HEAD' { $build.current_commit -eq $expected }

$diag = Get-Json "$ClarityUrl/v1/diagnostics"
Wait-For 'diagnostics.process_healthy' { $diag.gateway.process_healthy -eq $true }

$models = Get-Json "$ClarityUrl/v1/models"
$localCount = @($models.data | Where-Object { $_.id -like 'local:*' }).Count
Wait-For 'at least one local:* model' { $localCount -ge 1 }

$qwen3 = @($models.data | Where-Object { $_.id -eq 'local:qwen3:1.7b' }).Count
Wait-For 'local:qwen3:1.7b present' { $qwen3 -ge 1 }

# ---- final runtime attestation -------------------------------------------
$build = Get-Json "$ClarityUrl/v1/build"
if ($build.current_commit -ne $expected) {
    Write-Host "RUNTIME BUILD MISMATCH"
    Write-Host ("expected=" + $expected.Substring(0, 12) + " runtime=" + $build.current_commit.Substring(0, 12))
    Write-Error "runtime build commit does not match expected Git HEAD"
}

Write-Host "CLARITY READY"
Start-Process $ClarityUrl

# ---- evidence -------------------------------------------------------------
if (-not (Test-Path $LauncherDir)) { New-Item -ItemType Directory -Path $LauncherDir | Out-Null }
$evidence = [ordered]@{
    timestamp_utc                = (Get-Date).ToUniversalTime().ToString('o')
    expected_commit              = $expected
    runtime_commit               = $build.current_commit
    asset_version                = $build.asset_version
    health_ok                    = $true
    diagnostics_process_healthy  = $true
    local_models_ready           = $true
    qwen3_ready                  = $true
}
$tmp = Join-Path $LauncherDir 'Last-Start.json.tmp'
$evidence | ConvertTo-Json | Set-Content -Path $tmp -Encoding utf8
Move-Item -Path $tmp -Destination (Join-Path $LauncherDir 'Last-Start.json') -Force
