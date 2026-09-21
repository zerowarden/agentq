#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_DIR="$(dirname -- "$SCRIPT_DIR")"
CACHE_DIR="${TMPDIR:-/tmp}/agentq-scale-test-pycache-$$"
trap 'rm -rf "$CACHE_DIR"' EXIT
cd "$PROJECT_DIR"
PYTHONPYCACHEPREFIX="$CACHE_DIR" \
  uv run python tests/scale_benchmark.py "$@"
