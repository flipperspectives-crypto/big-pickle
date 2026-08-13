# Clarity Windows Control Scripts
# Source-controlled under scripts/windows/
#
# Stops ONLY the Clarity uvicorn process bound to port 7860. Ollama is left
# running. The owning PID is verified by inspecting the process command line
# (must contain both "uvicorn" and "app.main:app") so unrelated processes are
# never killed. No command-line content is persisted to evidence.

$ErrorActionPreference = 'Stop'

$ClarityPort = 7860
$LauncherDir = Join-Path $HOME 'Clarity-Launcher'

function Get-ClarityPid {
    $lines = netstat -ano | Select-String ":$ClarityPort\s"
    foreach ($l in $lines) {
        if ($l -match '\s+(\d+)$') {
            $pid = [int]$matches[1]
            try {
                $cmd = (Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$pid").CommandLine
            } catch { continue }
            if ($cmd -match 'uvicorn' -and $cmd -match 'app\.main:app') {
                return $pid
            }
        }
    }
    return $null
}

$pid = Get-ClarityPid
$processFound = ($null -ne $pid)
$stopped = $false

if ($processFound) {
    Stop-Process -Id $pid -Force
    $stopped = $true
}

if (-not (Test-Path $LauncherDir)) { New-Item -ItemType Directory -Path $LauncherDir | Out-Null }
$evidence = [ordered]@{
    timestamp_utc           = (Get-Date).ToUniversalTime().ToString('o')
    process_found           = $processFound
    clarity_process_stopped = $stopped
}
if ($processFound) { $evidence['pid'] = $pid }

$tmp = Join-Path $LauncherDir 'Last-Stop.json.tmp'
$evidence | ConvertTo-Json | Set-Content -Path $tmp -Encoding utf8
Move-Item -Path $tmp -Destination (Join-Path $LauncherDir 'Last-Stop.json') -Force
# Ollama is intentionally left running.
