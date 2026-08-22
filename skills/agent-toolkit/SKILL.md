---
name: agent-toolkit
description: Toolkit maintenance only: validate agentq, diagnose local dependencies/privacy behavior, install optional FOSS tools, or inspect command recipes. Never use implicitly for normal coding, search, Git inspection, or verification.
license: MIT
compatibility: Linux or macOS; Python 3.10+, Git, and ripgrep. Designed for ~/.agents/skills and compatible with OpenCode Agent Skills discovery.
metadata:
  version: "1.2.3"
  network: "runtime-offline"
---

# Agent Toolkit

This skill maintains the shared `agentq` runtime used by the other skills. The runtime is local-only: ordinary commands do not initiate network access. External tool installation is separate and explicit.

## First checks

Run from any repository:

```bash
~/.agents/skills/agent-toolkit/scripts/agentq doctor
~/.agents/skills/agent-toolkit/scripts/validate-skills
```

Use `--format json` when another script must consume the result.

## Design constraints

- Default to fixed-string search. Regex and AST patterns are explicit modes.
- Cap files, matches, lines, hunks, diagnostics, and line width.
- Exclude sensitive paths and redact common secret-like values by default.
- Keep complete command output only in local mode-`0600` redacted logs under the sandbox-safe runtime directory.
- Store only allowlisted operational telemetry; never store queries, source text, command arguments, or absolute repository paths.
- Never silently substitute lexical evidence for semantic proof.
- Never mutate files unless a command has an explicit mutation flag.
- Never download packages or execute `npx` during ordinary skill use.

## Shared command surface

```text
files, search, read, repo-map, outline

git-status, git-diff, git-history, git-structural

dependencies, impact

codemod-scan, codemod-apply

run, test-plan, verify-changed, audit, benchmark

task, stats, doctor
```

Inspect command-specific flags with:

```bash
~/.agents/skills/agent-toolkit/scripts/agentq <command> --help
```

## Local efficiency telemetry

For long-lived threads that contain several distinct fixes/features, use explicit task boundaries so stats are not distorted by thread length:

```bash
agentq task begin
agentq task status
agentq task accept   # only after the work unit meets its acceptance criteria
agentq task abandon  # if the work unit is dropped
```

Task state is repository/worktree-scoped, so boundaries may be marked from Codex or a separate shell. Concurrent independent work should use separate worktrees. No task names or prompt text are stored.

Inspect current-repository activity without exposing task content:

```bash
~/.agents/skills/agent-toolkit/scripts/agentq stats --since 7d
~/.agents/skills/agent-toolkit/scripts/agentq stats --watch 2
~/.agents/skills/agent-toolkit/scripts/agentq stats --plain
```

Interactive terminals use Rich when `python3-rich` is installed; plain and JSON modes remain dependency-free. Tool failures are kept separate from child-command failures, unknown reduction is not reported as zero, and timestamps display locally unless `--utc` is set. Telemetry is local, privacy-minimized, and disabled with `AGENTQ_TELEMETRY=0`. Hot telemetry remains sandbox-safe under `/tmp`; install the optional user-level systemd archive timer from a normal shell with `agentq stats --install-persistence`. Inspect storage with `agentq stats --storage`. Reset current-repository history with `agentq stats --reset`; use `--hot-only` to leave archived history untouched and `--all-repos` to intentionally reset every repository.

## Optional dependencies

Read `references/tooling.md` before installing anything. The bundle works with Git, ripgrep, and Python alone. Install optional tools only for capabilities you will use.

The human-facing installer is dry-run by default:

```bash
~/.agents/skills/agent-toolkit/scripts/install-tools.sh
~/.agents/skills/agent-toolkit/scripts/install-tools.sh --apply
```

## OpenCode integration

OpenCode already has competent built-in read, search, Bash, and LSP tools. Agent Skills alone are the default integration because they add less tool-schema context. An optional, read-only custom-tool adapter is provided in `assets/opencode-tools/`; install it only after measuring whether your model invokes the skills reliably.

## Maintenance

After changing any skill or script:

```bash
~/.agents/skills/agent-toolkit/scripts/validate-skills
~/.agents/skills/agent-toolkit/tests/self-test.sh
```

Do not add generated reports, caches, virtual environments, package stores, or `__pycache__` directories to the bundle.
