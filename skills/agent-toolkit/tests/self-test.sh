#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYCACHE="${TMPDIR:-/tmp}/agentq-self-test-pycache-$$"
trap 'rm -rf "$PYCACHE"' EXIT
PYTHONPYCACHEPREFIX="$PYCACHE" python3 -m unittest discover -s "$SCRIPT_DIR" -p 'test_*.py'
