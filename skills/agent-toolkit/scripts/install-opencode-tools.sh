#!/usr/bin/env bash
set -euo pipefail
apply=0
if [[ "${1:-}" == "--apply" ]]; then
  apply=1
elif [[ $# -gt 0 && "${1:-}" != "-h" && "${1:-}" != "--help" ]]; then
  echo "usage: install-opencode-tools.sh [--apply]" >&2
  exit 2
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SOURCE="$SCRIPT_DIR/../assets/opencode-tools/agentq.ts"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
CONFIG_HOME="${CONFIG_HOME%/}"
TARGET_DIR="$CONFIG_HOME/opencode/tools"
TARGET="$TARGET_DIR/agentq.ts"

echo "Optional OpenCode adapter"
echo "  source: $SOURCE"
echo "  target: $TARGET"
echo "  tools:  agentq_search, agentq_inspect, agentq_git (read-only)"
echo "  note:   custom tool schemas consume context; Agent Skills alone are preferred initially"

if ((apply == 0)); then
  echo "Dry run only. Re-run with --apply to install."
  exit 0
fi

mkdir -p "$TARGET_DIR"
if [[ -e "$TARGET" ]]; then
  backup="$TARGET.backup-$(date -u +%Y%m%dT%H%M%SZ)"
  cp -a "$TARGET" "$backup"
  echo "Backed up existing adapter to $backup"
fi
cp -a "$SOURCE" "$TARGET"
echo "Installed $TARGET. Restart OpenCode to reload custom tools."
