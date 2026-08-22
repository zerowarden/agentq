---
name: agent-toolkit
description: Toolkit maintenance only: validate agentq, diagnose local dependencies/privacy behavior, install optional FOSS tools, or inspect command recipes. Never use implicitly for normal coding, search, Git inspection, or verification.
license: MIT
compatibility: Linux or macOS; Python 3.10+, Git, and ripgrep. Designed for ~/.agents/skills and compatible with OpenCode Agent Skills discovery.
metadata:
  version: "1.2.4"
  network: "runtime-offline"
---

# Agent Toolkit

This skill maintains the shared `agentq` runtime used by the other skills. Ordinary commands operate locally and do not initiate network access.

## First checks

```bash
~/.agents/skills/agent-toolkit/scripts/agentq doctor
~/.agents/skills/agent-toolkit/scripts/validate-skills
```

Use `--format json` only when another program consumes the result.

## Design constraints

- Default to fixed-string search. Regex and AST patterns are explicit modes.
- Cap files, matches, lines, hunks, diagnostics, and line width.
- Exclude sensitive paths and redact common secret-like values by default.
- Keep full command output only in mode-`0600` redacted logs under the sandbox-safe runtime directory.
- Store only allowlisted operational telemetry; never store queries, source text, command arguments, task names, or absolute repository paths.
- Never silently substitute lexical evidence for semantic proof.
- Never mutate files unless a command has an explicit mutation flag.
- Never download packages or execute `npx` during ordinary skill use.

## Shared command surface

```text
files, search, read, repo-map, outline, ts-nav

git-status, git-diff, git-history, git-structural

dependencies, impact

codemod-scan, codemod-apply

run, test-plan, verify-changed, audit, benchmark

task, stats, doctor
```

Inspect flags with:

```bash
agentq <command> --help
```

## Task boundaries

A task is one independently acceptable implementation, fix, refactor, or review outcome. It is deliberately independent from a Codex thread: one thread may contain several sequential tasks, while one task may span several prompts and failed verification loops.

Canonical lifecycle:

```bash
agentq task begin
# investigate, edit, debug, and verify one outcome
agentq task accept
```

Ergonomic forms:

```bash
agentq task             # status
agentq task start       # alias for begin
agentq task done        # alias for accept
agentq task drop        # alias for abandon
agentq task next        # accept current and immediately begin another
```

Do not create a new task for every user message, minor correction, tool call, or retry. Use `next` only after the current result could be reviewed and accepted independently. Use `abandon`/`drop` only when the outcome is intentionally discarded.

Task state is repository/worktree-scoped. Concurrent independent tasks should use separate worktrees. No task names or prompt text are stored.

## Efficiency telemetry

```bash
agentq stats --since 7d
agentq stats --detailed            # adds recent activity
agentq stats --watch 2
agentq stats --plain
```

The operations table reports agentq/tool health separately from wrapped-command pass/fail counts. Interactive terminals use Rich when `python3-rich` is installed; plain and JSON modes remain dependency-free. Telemetry is local, privacy-minimized, and disabled with `AGENTQ_TELEMETRY=0`.

Hot telemetry remains sandbox-safe under `/tmp`. From a normal shell:

```bash
agentq stats --install-persistence
agentq stats --storage
agentq stats --reset
```

Use `--hot-only` to leave archived history untouched and `--all-repos` only to intentionally reset every repository.

## Optional dependencies

Read `references/tooling.md` before installing anything. The bundle works with Git, ripgrep, and Python alone. The installer is dry-run by default:

```bash
~/.agents/skills/agent-toolkit/scripts/install-tools.sh
~/.agents/skills/agent-toolkit/scripts/install-tools.sh --apply
```

## OpenCode integration

OpenCode already has capable built-in read, search, Bash, and LSP tools. Agent Skills are the default integration because they add less tool-schema context. An optional read-only custom-tool adapter is under `assets/opencode-tools/`; install it only after measuring whether it improves invocation reliability.

## Maintenance

After changing any skill or script:

```bash
~/.agents/skills/agent-toolkit/scripts/validate-skills
~/.agents/skills/agent-toolkit/tests/self-test.sh
```

Do not add generated reports, caches, virtual environments, package stores, or `__pycache__` directories.
