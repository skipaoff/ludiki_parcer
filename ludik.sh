#!/usr/bin/env bash
# VFP: One command that prepares and starts Terminal Ludik on macOS and Linux and opens it in the browser.
# Changes when: the Python or interface toolchain, or the startup sequence, changes.
# Anti-goal:
# 1. Secrets or settings in this file — settings live in config.toml, secrets in the OS keychain.
# 2. Doing the first-time setup — PostgreSQL, the role and the password are scripts/macos/setup.sh, once.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
    if ! command -v uv >/dev/null 2>&1; then
        echo "uv not found. Install it: brew install uv" >&2
        exit 1
    fi
    echo "Installing Python dependencies..."
    uv sync --frozen
fi

if [ ! -f "web/dist/index.html" ]; then
    if ! command -v npm >/dev/null 2>&1; then
        echo "Node.js not found, the interface cannot be built. Install it: brew install node" >&2
        exit 1
    fi
    echo "Building the interface..."
    (
        cd web
        if [ ! -d node_modules ]; then
            npm ci --no-fund --no-audit
        fi
        npm run build
    )
fi

exec .venv/bin/python -m app "$@"
