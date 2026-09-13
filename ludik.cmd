@echo off
rem VFP: One command that prepares and starts Terminal Ludik on Windows and opens it in the browser.
rem Changes when: the Python or interface toolchain, or the startup sequence, changes.
rem Anti-goal:
rem 1. Secrets or settings in this file - settings live in config.toml, secrets in Windows Credential Manager.
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem Antivirus HTTPS inspection re-signs certificates with a root only the Windows store trusts.
set "NODE_OPTIONS=--use-system-ca"

if not exist ".venv\Scripts\python.exe" (
    where uv >nul 2>nul
    if errorlevel 1 (
        echo uv not found. Install it: winget install -e --id astral-sh.uv --source winget
        exit /b 1
    )
    echo Installing Python dependencies...
    uv sync --frozen
    if errorlevel 1 exit /b 1
)

if not exist "web\dist\index.html" (
    where npm >nul 2>nul
    if errorlevel 1 (
        echo Node.js not found, the interface cannot be built. Install it: winget install -e --id OpenJS.NodeJS.LTS --source winget
        exit /b 1
    )
    echo Building the interface...
    pushd web
    if not exist "node_modules" call npm ci --no-fund --no-audit
    call npm run build
    if errorlevel 1 (
        popd
        exit /b 1
    )
    popd
)

".venv\Scripts\python.exe" -m app %*
exit /b %errorlevel%
