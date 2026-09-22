---
name: agent-toolkit
description: Toolkit maintenance only: validate agentq, diagnose local dependencies/privacy behavior, install optional FOSS tools, or inspect command recipes. Never use implicitly for normal coding, search, Git inspection, or verification.
---

# Agent Toolkit

This skill maintains the shared `agentq` runtime used by the other skills. Ordinary commands operate locally and do not initiate network access.

## First checks

```bash
agentq doctor
```

Use `search --format compact-json` for structured search results. Use legacy `--format json` only when a consumer requires its compatibility fields.

## Design constraints

- Default to fixed-string search. Regex and AST patterns are explicit modes.
- Cap files, matches, lines, hunks, diagnostics, and line width.
- Exclude sensitive paths and redact common secret-like values by default.
- Keep full command output only in mode-`0600` redacted logs under the sandbox-safe runtime directory.
- Store only allowlisted operational telemetry; query/command identity uses keyed local HMAC fingerprints, never raw queries, source text, command arguments, task names, or absolute repository paths.
- When `AGENTQ_TELEMETRY=0`, normal commands must not read, write, or create telemetry storage; telemetry never influences query or suppression behavior.
- Repeat suppression is controlled solely by `AGENTQ_CONTEXT_CACHE` and requires explicit session or task identity (`AGENTQ_SESSION_ID`, a host thread ID, or an active agentq task); without one, no suppression state is shared. Continuation cursors use the same session scoping.
- Use only the bounded, hashed context cache for exact-repeat suppression; normal exploration commands must never scan telemetry history.
- Never silently substitute lexical evidence for semantic proof.
- Never mutate files unless a command has an explicit mutation flag.
- Never download packages or execute `npx` during ordinary skill use.

## Shared command surface

```text
files, search, read, repo-map, outline, inspect, ts-nav

git-status, git-diff, git-history, git-structural

dependencies, impact

codemod-scan, codemod-apply

run, test-plan, verify, verify-changed, verify-task, audit, benchmark

task, stats, doctor
```

Inspect flags with:

```bash
agentq <command> --help
```

## Evidence quality

Every evidence-producing command reports `provenance` (semantic, syntactic, lexical, or heuristic) and `coverage` (`{"status": complete|sampled|partial|unknown, "reason": [...]}`). Treat sampled/partial evidence as incomplete; never claim a fact is proven when coverage is not complete.

For symbol work, prefer one `inspect --intent` call over repeated exploration:

- `--intent locate` — candidates only, minimal output.
- `--intent understand` (default) — declaration, references, provider metadata.
- `--intent edit` — adds the declaration body, related tests, owning package, and a verification scope. If the bundle has an unambiguous declaration, sufficient context, representative references, and verification scope, stop exploring and edit.

If `inspect` reports candidates across languages (`kind: "ambiguous"`), narrow with `--lang typescript|python` or `--path`; no language silently wins because it was queried first. `impact` reports observations plus an explicitly uncalibrated heuristic summary — reconstruct breadth from the observations, not from a scalar.

Search totals follow `--coverage fast|auto|exact`: `auto` (default) scans once and reports exact totals unless the scan cap is reached; `fast` never runs a counting pass; `exact` preserves exhaustive counting. When `count_quality` is `lower-bound`, treat totals as `>=` values instead of exact counts.

Truncated `search` and `git-diff` results return a short continuation cursor (`continue: agentq continue q7H2a`). Run `agentq continue CURSOR` to resume the exact stored operation; cursors are scoped to the repository and session and expire after one hour. Other truncated operations return a display-only recovery command to rerun directly.

## Verification configuration

`test-plan` and `verify` detect the Node, Python, Cargo, and Go ecosystems and plan one deduplicated verification ladder across every detected ecosystem. An optional `.agentq.toml` at the repository root augments provider inference:

```toml
[verify]
providers = ["node", "python"]   # restrict planning to named providers
commands = ["make check"]        # extra planned checks, run first
ignore = ["generated/**"]        # exclude changed files from planning
contract_patterns = ["api/**"]   # extra public-contract paths

[ownership]
"libs/core" = "core-pkg"         # attribute a path prefix to a package name
```

Configuration augments provider inference; detection never requires it. Unknown provider names or malformed tables are rejected with a deterministic error.

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
agentq stats --detail              # failures, command chains, navigation, accepted-task outcomes, verification
agentq stats --recent 8            # detailed mode plus 8 recent operations
agentq stats --watch 2
agentq stats --plain
```

The operations table reports only agentq/CLI health. Wrapped project-command outcomes are summarized separately. Accepted tasks track calls by command, visible characters, estimated tokens, same-context overlap, exact suppression, expanded retries, verification result, and correction calls. Character counts are authoritative; token counts are labeled estimates, and lower output with more retries or missed verification is a regression. `--detail` does not add a generic recent-command list; use `--recent N` when that transcript view is useful. Interactive terminals use the built-in ANSI renderer; plain and JSON modes remain dependency-free. Telemetry is local, privacy-minimized, and disabled with `AGENTQ_TELEMETRY=0`.

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
~/.agents/agentq/scripts/install-tools.sh
~/.agents/agentq/scripts/install-tools.sh --apply
```

## OpenCode integration

OpenCode already has capable built-in read, search, Bash, and LSP tools. Agent Skills are the default integration because they add less tool-schema context. An optional read-only custom-tool adapter is under `~/.agents/agentq/integrations/opencode/`; install it only after measuring whether it improves invocation reliability.

## Maintenance

After changing any skill or script:

```bash
~/.agents/agentq/tests/self-test.sh
```

Do not add generated reports, caches, virtual environments, package stores, or `__pycache__` directories.
