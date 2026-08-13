# Clarity Windows Control Scripts
# Source-controlled under scripts/windows/
#
# Copies the version-controlled launcher scripts from <repo>\scripts\windows into
# $HOME\Clarity-Launcher, backing up any pre-existing script before replacing it,
# and creates Desktop shortcuts for the main entry points.

$ErrorActionPreference = 'Stop'

$RepoDir      = Join-Path $HOME 'clarity'
$ScriptSrc    = Join-Path $RepoDir 'scripts\windows'
$LauncherDir  = Join-Path $HOME 'Clarity-Launcher'
$WshShell     = New-Object -ComObject WScript.Shell

if (-not (Test-Path $LauncherDir)) { New-Item -ItemType Directory -Path $LauncherDir | Out-Null }

$launchers = @(
    'Start-Clarity.ps1',
    'Stop-Clarity.ps1',
    'Update-Clarity.ps1',
    'Rollback-Clarity.ps1',
    'Create-Clarity-Recovery-Bundle.ps1'
)

foreach ($f in $launchers) {
    $src = Join-Path $ScriptSrc $f
    $dst = Join-Path $LauncherDir $f
    if (-not (Test-Path $src)) { Write-Error "Missing source script: $src" }
    if (Test-Path $dst) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        Copy-Item -Path $dst -Destination ($dst + '.backup-' + $stamp) -Force
    }
    Copy-Item -Path $src -Destination $dst -Force
}

function New-Shortcut($name, $script) {
    $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) ($name + '.lnk')
    $target = Join-Path $LauncherDir $script
    $sc = $WshShell.CreateShortcut($lnk)
    $sc.TargetPath = 'powershell.exe'
    $sc.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$target`""
    $sc.WorkingDirectory = $LauncherDir
    $sc.Save()
}

New-Shortcut 'Start Clarity'                  'Start-Clarity.ps1'
New-Shortcut 'Stop Clarity'                   'Stop-Clarity.ps1'
New-Shortcut 'Update Clarity'                 'Update-Clarity.ps1'
New-Shortcut 'Rollback Clarity'               'Rollback-Clarity.ps1'
New-Shortcut 'Create Clarity Recovery Bundle' 'Create-Clarity-Recovery-Bundle.ps1'

Write-Host "Clarity launchers installed to $LauncherDir"
