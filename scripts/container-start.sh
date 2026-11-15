#!/bin/sh
# Starts Family Table inside the Docker container (see Dockerfile). Not for use outside Docker:
# use scripts/serve.sh there.
set -eu

# Never start on an empty data folder by accident (for example a mistyped path, which Docker
# Desktop creates as a new empty folder): that would quietly begin a brand-new, empty database.
if [ ! -f /data/family_table.db ] && [ "${FAMILY_TABLE_NEW_DATABASE:-}" != "yes" ]; then
    echo "No family database (family_table.db) in the mounted data folder." >&2
    echo "Check FAMILY_TABLE_DATA / the data folder in compose.yaml. To really start an empty" >&2
    echo "Family Table, run once with FAMILY_TABLE_NEW_DATABASE=yes." >&2
    exit 1
fi
if [ ! -w /data ]; then
    echo "The data folder isn't writable by the container user ($(id -u)). It should belong to your" >&2
    echo "WSL user (usually 1000); if Docker created it, it belongs to root: fix with sudo chown." >&2
    exit 1
fi

# Production mode: no reload, one worker (login throttling and list sync live in one process).
# 0.0.0.0 is inside the container only; compose.yaml publishes it on 127.0.0.1 of the host.
# Forwarded client addresses are trusted only from the Docker gateway (FORWARDED_ALLOW_IPS).
exec python -m uvicorn app.main:app \
    --host 0.0.0.0 --port 8000 --workers 1 \
    --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}" \
    --no-server-header --timeout-graceful-shutdown 10
