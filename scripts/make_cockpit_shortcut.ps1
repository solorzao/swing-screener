# Creates (or overwrites) the Start-menu shortcut that makes the cockpit
# double-clickable: "Swing Screener Cockpit" -> pythonw.exe -m swing_screener.cockpit.
#
# pythonw, not python, so no console window flashes up behind the app. Startup
# failures still surface: with no console the launcher opens a WinAPI message box
# instead of printing into the void (src/swing_screener/cockpit/__main__.py).
#
# Idempotent -- WScript.Shell's CreateShortcut overwrites in place, so re-run this
# after moving the repo or rebuilding the venv. The repo root is derived from THIS
# script's location (scripts/ -> parent), never from the current directory, so it
# works from any CWD. WorkingDirectory = repo root keeps the default
# sqlite:///local.db resolving to the repo's local.db.

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repoRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    Write-Error ("pythonw.exe not found at $pythonw -- create the venv first: " +
        'py -3.12 -m venv .venv; .\.venv\Scripts\python -m pip install -e ".[dev,cockpit]"')
}

$lnk = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Swing Screener Cockpit.lnk'
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($lnk)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '-m swing_screener.cockpit'
$shortcut.WorkingDirectory = $repoRoot
$shortcut.Description = 'Swing Screener Cockpit -- native window over the local screener database'
$shortcut.Save()

Write-Output "Created: $lnk"
