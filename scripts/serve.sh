#!/usr/bin/env bash
# Start Family Table for the phones and the desktop. Runs in the foreground: watch the
# requests here, press Ctrl+C to stop. Run from WSL:  ./scripts/serve.sh
#
# Differences from the development command (uvicorn --reload):
#   - no auto-reload, no debug pages, no "server: uvicorn" header
#   - one worker: login throttling and the shopping-list sync live in this one process
#   - only reachable from this computer (127.0.0.1); Tailscale Funnel on Windows
#     forwards the public HTTPS address to it
#   - secure (HTTPS-only) sign-in cookies, because phones arrive over HTTPS
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${FAMILY_TABLE_PORT:-8000}"

# Refuse to start a second copy (two servers would fight over the port).
if ss -ltnH "sport = :$PORT" | grep -q .; then
    echo "Port $PORT is already in use, so Family Table (or something else) is already running:" >&2
    ss -ltnpH "sport = :$PORT" >&2 || true
    echo "Stop that one first (Ctrl+C in its terminal), then run this again." >&2
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "No .venv found. Set it up first (see README: Setup)." >&2
    exit 1
fi

# The value in .env is kept for development; this overrides it for the shared server only.
export COOKIE_SECURE="${COOKIE_SECURE:-true}"

echo "Family Table starting on http://127.0.0.1:$PORT  (Ctrl+C to stop)"
# --proxy-headers / --forwarded-allow-ips: only the Tailscale proxy on this computer
# (127.0.0.1) may say which address a request really came from.
exec .venv/bin/python -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$PORT" --workers 1 \
    --proxy-headers --forwarded-allow-ips 127.0.0.1 \
    --no-server-header --timeout-graceful-shutdown 10
