# VFP: Puts a "Terminal Ludik" shortcut on the Windows desktop that starts the terminal with ludik.cmd --desktop.
# Changes when: the launcher, its arguments or the icon change, or the repository moves (run it again from the new place).
# Anti-goal:
# 1. Hard-coded user paths - the desktop and the repository are found at run time.
# 2. Silently replacing somebody else's shortcut - an existing one with the same name is updated only with -Force.

param([switch]$Force)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$launcher = Join-Path $repo "ludik.cmd"
$icon = Join-Path $repo "assets\ludik.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$path = Join-Path $desktop "Terminal Ludik.lnk"

if (-not (Test-Path $launcher)) { throw "ludik.cmd not found in $repo" }
if ((Test-Path $path) -and -not $Force) {
    Write-Output "Shortcut already exists: $path (run with -Force to update it)"
    exit 0
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = $launcher
$shortcut.Arguments = "--desktop"
$shortcut.WorkingDirectory = $repo
$shortcut.Description = "Terminal Ludik - cross-exchange gap terminal"
$shortcut.WindowStyle = 1
if (Test-Path $icon) { $shortcut.IconLocation = "$icon,0" }
$shortcut.Save()
Write-Output "Shortcut created: $path"
