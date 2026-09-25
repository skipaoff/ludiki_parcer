#!/usr/bin/env bash
# VFP: A Desktop launcher that starts Terminal Ludik the way the Windows shortcut does.
# Changes when: the way the terminal is started changes.
# Anti-goal:
# 1. Copying the terminal anywhere — the launcher points at this repository, wherever it lives.
# 2. Silently overwriting an existing launcher that points somewhere else: --force says it on purpose.
set -euo pipefail

cd "$(dirname "$0")/../.."
REPO="$(pwd)"
LAUNCHER="$HOME/Desktop/Terminal Ludik.command"

if [ -e "$LAUNCHER" ] && [ "${1:-}" != "--force" ]; then
    echo "$LAUNCHER already exists. Run with --force to point it at $REPO." >&2
    exit 1
fi

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Created by scripts/macos/create_desktop_shortcut.sh
cd "$REPO" || exit 1
./ludik.sh
status=\$?
if [ \$status -ne 0 ]; then
    echo
    echo "Terminal Ludik stopped with code \$status."
    read -r -p "Press Enter to close this window."
fi
EOF
chmod +x "$LAUNCHER"

echo "Created: $LAUNCHER"
echo "It opens Terminal.app, starts the terminal and keeps the window while it runs."
