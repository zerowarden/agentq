#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHONPYCACHEPREFIX="${TMPDIR:-/tmp}/agentq-self-test-pycache-$$" python3 "$SCRIPT_DIR/test_agentq.py"
rm -rf "${TMPDIR:-/tmp}/agentq-self-test-pycache-$$"
