#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

DEFAULT_PORT=8502

# ── venv setup ────────────────────────────────────────────────────────────────
if [ -z "$VIRTUAL_ENV" ]; then
    if [ ! -d ".venv" ]; then
        echo "Creating virtual environment..."
        python3 -m venv .venv
        source .venv/bin/activate
        echo "Installing requirements..."
        pip install -r requirements.txt --quiet
    else
        source .venv/bin/activate
    fi
fi

# ── port selection ─────────────────────────────────────────────────────────────
find_port() {
    local port=$DEFAULT_PORT
    while true; do
        local pid
        pid=$(lsof -ti :"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)
        if [ -z "$pid" ]; then
            echo "$port"
            return
        fi
        local cmd
        cmd=$(ps -p "$pid" -o args= 2>/dev/null || true)
        if echo "$cmd" | grep -q "api.main"; then
            echo "" >&2
            echo "Port $port is already in use by an Agora instance (PID $pid)." >&2
            read -r -p "Kill it and restart? [Y/n] " answer <&2 || answer="n"
            case "$answer" in
                [nN]*)
                    port=$((port + 1))
                    ;;
                *)
                    kill "$pid"
                    sleep 1
                    echo "$port"
                    return
                    ;;
            esac
        else
            port=$((port + 1))
        fi
    done
}

PORT=$(find_port)
echo "Starting Agora on http://127.0.0.1:$PORT"

# Open browser after a short delay to let the server start
(sleep 2 && open "http://127.0.0.1:$PORT") &

exec uvicorn api.main:app --reload --port "$PORT"
