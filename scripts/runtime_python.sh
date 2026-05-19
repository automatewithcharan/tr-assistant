#!/usr/bin/env bash
# Lightweight wrapper that always invokes the venv Python.
# Referenced by trade-assistant-stop.service and trade-assistant-boot.service.
# Pass all args through unchanged.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -x "$REPO/.venv/bin/python3" ]; then
    PYTHON="$REPO/.venv/bin/python3"
elif [ -x "$REPO/venv/bin/python3" ]; then
    PYTHON="$REPO/venv/bin/python3"
else
    PYTHON="python3"
fi

cd "$REPO"
exec "$PYTHON" "$@"
