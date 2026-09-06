#!/usr/bin/env bash
# Agora launcher. Double-click on macOS, or run from any directory.
set -e

cd "$(dirname "$0")"

DEFAULT_PORT=8502
APP_TARGET="api.main:app"

# ── stop any instance still running ───────────────────────────────────────────
# Matched by the server's own command line, not by whether a port happens to be
# bound: a stale instance from a closed terminal or a crashed shell otherwise
# keeps serving previous code from some other port, invisibly. Runs before the
# venv work so a reinstall never races a live server.
stop_running() {
    local pids
    pids=$(pgrep -f "uvicorn .*${APP_TARGET}" 2>/dev/null | grep -v "^$$\$" || true)
    [ -z "$pids" ] && return 0
    echo "Stopping a running Agora instance (PID $(echo "$pids" | tr '\n' ' '))..."
    # shellcheck disable=SC2086
    kill $pids 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 0.3
        pgrep -f "uvicorn .*${APP_TARGET}" >/dev/null 2>&1 || return 0
    done
    pids=$(pgrep -f "uvicorn .*${APP_TARGET}" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        # shellcheck disable=SC2086
        kill -9 $pids 2>/dev/null || true
        sleep 0.5
    fi
}
stop_running

# ── interpreter ───────────────────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
    echo ""
    echo "Agora needs Python 3.10 or newer, and python3 isn't on this machine."
    echo "Install it from https://www.python.org/downloads/ and run this again."
    echo ""
    exit 1
fi

VENV_PY=".venv/bin/python3"

# An existing .venv whose interpreter no longer runs (a moved project folder, a
# removed system Python) is rebuilt rather than used.
if [ -d ".venv" ] && ! "$VENV_PY" -c "" >/dev/null 2>&1; then
    echo "The existing virtual environment is unusable — rebuilding it."
    rm -rf .venv
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi

# ── dependencies, reinstalled only when requirements.txt changes ──────────────
STAMP=".venv/.requirements.sha"
CURRENT=$(shasum -a 256 requirements.txt | awk '{print $1}')
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$CURRENT" ]; then
    echo "Installing dependencies..."
    "$VENV_PY" -m pip install -r requirements.txt --quiet
    echo "$CURRENT" > "$STAMP"
fi

# ── port ──────────────────────────────────────────────────────────────────────
# Chromium refuses its own restricted ports before the request reaches the
# server, so the app would be healthy and look dead in the browser.
BLOCKED_PORTS=" 6000 6665 6666 6667 6668 6669 6697 10080 "

find_port() {
    local port=$DEFAULT_PORT
    local limit=$((DEFAULT_PORT + 20))
    while [ "$port" -lt "$limit" ]; do
        if echo "$BLOCKED_PORTS" | grep -q " $port "; then
            port=$((port + 1))
            continue
        fi
        if [ -z "$(lsof -ti :"$port" -sTCP:LISTEN 2>/dev/null | head -1)" ]; then
            echo "$port"
            return 0
        fi
        port=$((port + 1))
    done
    echo ""
    return 1
}

PORT=$(find_port) || {
    echo ""
    echo "No free port between $DEFAULT_PORT and $((DEFAULT_PORT + 20))."
    echo "Close whatever is using them, then run this again."
    echo ""
    exit 1
}

echo "Starting Agora on http://127.0.0.1:$PORT"
(sleep 2 && open "http://127.0.0.1:$PORT") &

exec .venv/bin/uvicorn "$APP_TARGET" --port "$PORT"
