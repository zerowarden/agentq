#!/usr/bin/env bash
set -euo pipefail

apply=0
update=0
no_cargo=0

usage() {
  cat <<'EOF'
Usage: install-tools.sh [--apply] [--update] [--no-cargo]

Without --apply, prints the local tool status and installation plan only.
--apply      install available Kubuntu/Ubuntu packages and missing Cargo tools
--update     run apt-get update before package installation
--no-cargo   skip ast-grep/difftastic/hyperfine/tokei Cargo installation
EOF
}

while (($#)); do
  case "$1" in
    --apply) apply=1 ;;
    --update) update=1 ;;
    --no-cargo) no_cargo=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! command -v apt-cache >/dev/null 2>&1; then
  echo "This installer targets Kubuntu/Ubuntu (apt). See references/tooling.md for manual installation." >&2
  exit 2
fi

required=(git ripgrep python3)
recommended=(jq universal-ctags shellcheck shfmt fd-find python3-rich)
optional_apt=(hyperfine tokei)

available=()
for package in "${required[@]}" "${recommended[@]}" "${optional_apt[@]}"; do
  if apt-cache show "$package" >/dev/null 2>&1; then
    available+=("$package")
  fi
done

cat <<EOF
Local, privacy-preserving tool installation plan

APT packages available on this system:
  ${available[*]:-(none detected)}

Cargo tools (if Cargo is installed and --no-cargo is not set):
  ast-grep difftastic
  hyperfine/tokei only when not already available after APT installation

Optional terminal renderer:
  python3-rich — responsive stats dashboard; plain fallback remains built in

Not auto-installed:
  Gitleaks — install a pinned official release separately, then configure it per repository

No project files are modified. Network access occurs only when --apply is used.
EOF

if ((apply == 0)); then
  echo
  echo "Dry run only. Re-run with --apply after reviewing this plan."
  exit 0
fi

if ((update)); then
  sudo apt-get update
fi
if ((${#available[@]})); then
  sudo apt-get install -y --no-install-recommends "${available[@]}"
fi

mkdir -p "$HOME/.local/bin"
if ! command -v fd >/dev/null 2>&1 && command -v fdfind >/dev/null 2>&1; then
  ln -sfn "$(command -v fdfind)" "$HOME/.local/bin/fd"
fi

if ((no_cargo == 0)); then
  if command -v cargo >/dev/null 2>&1; then
    if ! command -v ast-grep >/dev/null 2>&1; then
      cargo install --locked ast-grep
    fi
    if ! command -v difft >/dev/null 2>&1; then
      cargo install --locked difftastic
    fi
    if ! command -v hyperfine >/dev/null 2>&1; then
      cargo install --locked hyperfine
    fi
    if ! command -v tokei >/dev/null 2>&1; then
      cargo install --locked tokei
    fi
  else
    echo "Cargo is not installed; skipped ast-grep and difftastic." >&2
  fi
fi

cat <<EOF

Installation complete. Ensure these directories are in PATH where applicable:
  $HOME/.local/bin
  $HOME/.cargo/bin

Then run:
  $HOME/.agents/skills/agent-toolkit/scripts/agentq doctor
EOF
