#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
CACHE_DIR="${TMPDIR:-/tmp}/agentq-scale-test-pycache-$$"
trap 'rm -rf "$CACHE_DIR"' EXIT
PYTHONPYCACHEPREFIX="$CACHE_DIR" \
  python3 "$SCRIPT_DIR/scale_benchmark.py" "$@"
