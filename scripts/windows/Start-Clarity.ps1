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

# Safe short-SHA: never calls .Substring() on null/unknown input.
function ShortSha($s) {
    if ($s -is [string] -and $s -match '^[0-9a-f]{40}$') { return $s.Substring(0, 12) }
    if ($s -is [string] -and $s -match '^[0-9a-f]{7,}$') { return $s.Substring(0, [Math]::Min(12, $s.Length)) }
    return 'unknown'
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

# True only when something is LISTENING on the given port (not an ephemeral
# established connection).
function Test-PortListening($port) {
    $out = netstat -ano | Select-String ":$port\s+.*LISTENING"
    return ($null -ne $out)
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
Write-Step ("expected commit " + (ShortSha $expected))

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

# ---- port safety: reuse an existing, proven Clarity build -----------------
$reuse = $false
if (Test-PortListening 7860) {
    $hb = Get-Json "$ClarityUrl/health"
    $bb = Get-Json "$ClarityUrl/v1/build"
    if ($hb -and $hb.status -eq 'ok' -and $bb -and $bb.current_commit -eq $expected) {
        Write-Step ("reusing existing Clarity on " + $ClarityUrl)
        $reuse = $true
    } else {
        Write-Host "PORT 7860 OCCUPIED OR BUILD MISMATCH"
        Write-Host ("expected=" + (ShortSha $expected) + " runtime=" + (ShortSha $bb.current_commit))
        Write-Error "port 7860 is occupied by a process that is not the expected Clarity build; aborting without killing it"
    }
}

# ---- start Clarity only if not reusing ------------------------------------
if (-not $reuse) {
    if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir | Out-Null }
    $env:GATEWAY_DB = $DbPath
    $env:OLLAMA_BASE_URL = $OllamaUrl
    Write-Step ("starting Clarity on " + $ClarityUrl)
    $null = Start-Process -FilePath $VenvPython `
        -ArgumentList '-m','uvicorn','app.main:app','--host','0.0.0.0','--port','7860' `
        -WorkingDirectory $RepoDir -WindowStyle Hidden
}

# ---- readiness gates (no inference): EVERY retry does a FRESH GET ---------
Wait-For 'health' {
    $h = Get-Json "$ClarityUrl/health"
    return ($h -and $h.status -eq 'ok')
}

Wait-For 'build.current_commit == git HEAD' {
    $b = Get-Json "$ClarityUrl/v1/build"
    return ($b -and $b.current_commit -eq $expected)
}

Wait-For 'diagnostics.process_healthy' {
    $d = Get-Json "$ClarityUrl/v1/diagnostics"
    return ($d -and $d.gateway.process_healthy -eq $true)
}

Wait-For 'at least one local:* model' {
    $m = Get-Json "$ClarityUrl/v1/models"
    return ($m -and @($m.data | Where-Object { $_.id -like 'local:*' }).Count -ge 1)
}

Wait-For 'local:qwen3:1.7b present' {
    $m2 = Get-Json "$ClarityUrl/v1/models"
    return ($m2 -and @($m2.data | Where-Object { $_.id -eq 'local:qwen3:1.7b' }).Count -ge 1)
}

# ---- final runtime attestation (fresh GET) -------------------------------
$finalBuild = Get-Json "$ClarityUrl/v1/build"
if (-not $finalBuild -or $finalBuild.current_commit -ne $expected) {
    Write-Host "RUNTIME BUILD MISMATCH"
    Write-Host ("expected=" + (ShortSha $expected) + " runtime=" + (ShortSha $finalBuild.current_commit))
    Write-Error "runtime build commit does not match expected Git HEAD"
}

Write-Host "CLARITY READY"
Start-Process $ClarityUrl

# ---- evidence -------------------------------------------------------------
if (-not (Test-Path $LauncherDir)) { New-Item -ItemType Directory -Path $LauncherDir | Out-Null }
$evidence = [ordered]@{
    timestamp_utc                = (Get-Date).ToUniversalTime().ToString('o')
    expected_commit              = $expected
    runtime_commit               = $finalBuild.current_commit
    asset_version                = $finalBuild.asset_version
    health_ok                    = $true
    diagnostics_process_healthy  = $true
    local_models_ready           = $true
    qwen3_ready                  = $true
}
$tmp = Join-Path $LauncherDir 'Last-Start.json.tmp'
$evidence | ConvertTo-Json | Set-Content -Path $tmp -Encoding utf8
Move-Item -Path $tmp -Destination (Join-Path $LauncherDir 'Last-Start.json') -Force
