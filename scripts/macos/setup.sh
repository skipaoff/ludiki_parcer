#!/usr/bin/env bash
# VFP: The one-time setup of a Mac for Terminal Ludik — PostgreSQL cluster, database and the password in the keychain.
# Changes when: the database layout, the PostgreSQL version or where secrets live changes.
# Anti-goal:
# 1. Printing the database password or writing it to a file — it is generated here and goes straight to the keychain.
# 2. Touching a cluster that already exists — the script stops instead, so a second run cannot wipe data.
# 3. Installing Homebrew or anything outside ludik-data — what is missing is named, not installed behind your back.
set -euo pipefail

cd "$(dirname "$0")/../.."
REPO="$(pwd)"
DATA="$(cd .. && pwd)/ludik-data"
PGDATA="$DATA/pgdata"
LOGS="$DATA/logs"
SERVICE="ludik"          # keyring's service name, the same one app/keystore/keystore.py reads
ACCOUNT="postgres:ludik" # keyring's account name for the database password
DB_USER="ludik"
DB_NAME="ludik"

say() { printf '\n== %s\n' "$1"; }

say "Checking what is installed"
missing=0
check() {
    if command -v "$1" >/dev/null 2>&1; then
        echo "  $1: $(command -v "$1")"
    else
        echo "  $1 is missing — install it: brew install $2" >&2
        missing=1
    fi
}
check uv uv
check node node
check npm node

# Homebrew keeps versioned formulae out of the PATH, so the bin directory is found or told to the script.
PG_BIN="${PG_BIN:-$(brew --prefix postgresql@18 2>/dev/null || echo /nonexistent)/bin}"
if [ -x "$PG_BIN/initdb" ]; then
    echo "  PostgreSQL: $("$PG_BIN/postgres" --version)"
else
    echo "  PostgreSQL 18 is missing — install it:" >&2
    echo "      brew install postgresql@18" >&2
    echo "      brew tap timescale/tap && brew install timescaledb   # then follow the caveats it prints" >&2
    echo "  Installed elsewhere? Run: PG_BIN=/path/to/postgres/bin $0" >&2
    missing=1
fi
[ "$missing" -eq 0 ] || { echo; echo "Install what is listed above and run this again." >&2; exit 1; }

if [ -d "$PGDATA" ]; then
    echo
    echo "A cluster already exists in $PGDATA — nothing to set up." >&2
    echo "To start over, stop it ('$PG_BIN/pg_ctl' -D '$PGDATA' stop) and remove that directory yourself." >&2
    exit 1
fi

say "Creating the cluster in $PGDATA"
mkdir -p "$DATA" "$LOGS"
password="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)"
pwfile="$(mktemp)"
trap 'rm -f "$pwfile"' EXIT
printf '%s' "$password" >"$pwfile"
"$PG_BIN/initdb" -D "$PGDATA" -U "$DB_USER" --auth-local=scram-sha-256 --auth-host=scram-sha-256 --pwfile="$pwfile" --encoding=UTF8 >/dev/null
rm -f "$pwfile"

# The keychain first: from here on the password exists nowhere else, and the cluster is useless without it.
say "Saving the password in the login keychain ($SERVICE / $ACCOUNT)"
security add-generic-password -U -s "$SERVICE" -a "$ACCOUNT" -w "$password" >/dev/null

say "Settings: loopback only, TimescaleDB preloaded, telemetry off"
{
    echo "listen_addresses = '127.0.0.1'"
    echo "shared_preload_libraries = 'timescaledb'"
    echo "timescaledb.telemetry_level = 'off'"
} >>"$PGDATA/postgresql.conf"

say "Starting the cluster"
"$PG_BIN/pg_ctl" -D "$PGDATA" -l "$LOGS/postgres.log" -w -t 60 start >/dev/null

say "Creating the database $DB_NAME"
PGPASSWORD="$password" "$PG_BIN/createdb" -h 127.0.0.1 -U "$DB_USER" "$DB_NAME"
unset password

if [ ! -x "$REPO/.venv/bin/python" ]; then
    say "Installing Python dependencies"
    uv sync --frozen
fi

say "Checking that the terminal can reach the database"
# macOS asks once whether Python may read the item: click "Always Allow".
"$REPO/.venv/bin/python" - <<'PY'
import asyncio

import asyncpg

from app.keystore.keystore import DB_PASSWORD, Keystore
from app.system.log_setup import SecretRedactor


async def main() -> None:
    password = Keystore(SecretRedactor()).get(DB_PASSWORD)
    if not password:
        raise SystemExit("  the keychain has no password under ludik / postgres:ludik")
    connection = await asyncpg.connect(host="127.0.0.1", port=5432, user="ludik", database="ludik", password=password)
    version = await connection.fetchval("SHOW server_version")
    timescale = await connection.fetchval("SELECT default_version FROM pg_available_extensions WHERE name = 'timescaledb'")
    await connection.close()
    print(f"  PostgreSQL {version}")
    if timescale:
        print(f"  TimescaleDB {timescale} is available")
    else:
        raise SystemExit("  TimescaleDB is NOT available: follow the caveats of 'brew install timescaledb', then restart the cluster")


asyncio.run(main())
PY

cat <<EOF

Done. The cluster is in $PGDATA and listens on 127.0.0.1:5432.

Next:
  cp config.example.toml config.toml     # in it, set pg_bin_dir = "$PG_BIN"
  ./ludik.sh

The terminal starts the cluster itself when it is not running, applies the migrations, serves
http://127.0.0.1:8765 and opens the browser. Stop it with Ctrl+C.
EOF
