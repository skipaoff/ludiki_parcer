@echo off
rem VFP: One command that prepares and starts Terminal Ludik on Windows and opens it in the browser.
rem Changes when: the Python or interface toolchain, or the startup sequence, changes.
rem Anti-goal:
rem 1. Secrets or settings in this file - settings live in config.toml, secrets in Windows Credential Manager.
rem 2. A window that vanishes on an error when started from the desktop shortcut (--desktop) - it waits for a key.
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Terminal Ludik

set "LUDIK_DESKTOP="
if /i "%~1"=="--desktop" set "LUDIK_DESKTOP=1"

rem Antivirus HTTPS inspection re-signs certificates with a root only the Windows store trusts.
set "NODE_OPTIONS=--use-system-ca"

if not exist ".venv\Scripts\python.exe" (
    where uv >nul 2>nul
    if errorlevel 1 (
        echo uv not found. Install it: winget install -e --id astral-sh.uv --source winget
        goto :failed
    )
    echo Installing Python dependencies...
    uv sync --frozen
    if errorlevel 1 goto :failed
)

if not exist "web\dist\index.html" (
    where npm >nul 2>nul
    if errorlevel 1 (
        echo Node.js not found, the interface cannot be built. Install it: winget install -e --id OpenJS.NodeJS.LTS --source winget
        goto :failed
    )
    echo Building the interface...
    pushd web
    if not exist "node_modules" call npm ci --no-fund --no-audit
    call npm run build
    if errorlevel 1 (
        popd
        goto :failed
    )
    popd
)

if defined LUDIK_DESKTOP (
    echo Terminal Ludik. Keep this window open while the terminal runs. Stop: Ctrl+C.
    echo.
    ".venv\Scripts\python.exe" -m app
) else (
    ".venv\Scripts\python.exe" -m app %*
)
if errorlevel 1 goto :failed
exit /b 0

:failed
if defined LUDIK_DESKTOP (
    echo.
    echo Terminal Ludik stopped with an error. The reason is in the messages above and in ..\ludik-data\logs\terminal.log.
    pause
)
exit /b 1
