#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BRIDGE_PYTHON=${BRIDGE_PYTHON:-python3}
"$BRIDGE_PYTHON" -c 'import sys; sys.exit("Python 3.10+ is required; 3.12+ is recommended.") if sys.version_info < (3,10) else None'
if [ ! -x .venv/bin/python ]; then
  "$BRIDGE_PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -e .
printf '\nInstalled. Activate with: . .venv/bin/activate\nThen run: tmux-bridge doctor\n'
