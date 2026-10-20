#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
unset PYTHONPATH
exec .venv/bin/python bridge_cli.py add-node "$@"
