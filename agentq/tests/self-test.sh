#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
PYCACHE="${TMPDIR:-/tmp}/agentq-self-test-pycache-$$"
trap 'rm -rf "$PYCACHE"' EXIT
cd "$PROJECT_DIR"
PYTHONPYCACHEPREFIX="$PYCACHE" uv run python -m unittest discover -s tests -t . -p 'test_*.py'
