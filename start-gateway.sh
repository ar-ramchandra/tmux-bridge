#!/bin/sh
# Compatibility entry point for existing source-tree deployments.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
export PYTHONUNBUFFERED=1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [ -n "${BRIDGE_PYTHON:-}" ]; then
  exec "$BRIDGE_PYTHON" gateway.py
elif [ -x .venv/bin/python ]; then
  exec .venv/bin/python gateway.py
elif [ -x /usr/bin/python3 ] && /usr/bin/python3 -c 'import fastapi, uvicorn, websockets, cryptography, httpx, psutil' 2>/dev/null; then
  exec /usr/bin/python3 gateway.py
else
  exec python3 gateway.py
fi
