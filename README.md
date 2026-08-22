# agentq

`agentq` is a local CLI for inspecting, modifying, and verifying source repositories with bounded output.

It is intended for coding-agent workflows where raw commands such as `rg`, `git diff`, test runners, and compiler output can otherwise add large amounts of unnecessary context.

`agentq` does not make network requests during normal operation. Repository searches and reads exclude common sensitive paths by default, and command logs redact common secret-like values.

## Requirements

Required:

* Python 3.10+
* Git
* [ripgrep](https://github.com/BurntSushi/ripgrep)

Useful optional tools:

* `rich` - terminal rendering for `agentq stats`
* `ast-grep` - syntax-aware outlines and codemods
* Universal Ctags - symbol outline fallback
* Difftastic - structural file diffs
* Hyperfine - reproducible benchmarks
* `jq` - JSON inspection
* `fd`, Tokei, ShellCheck, shfmt, Gitleaks

Check the current environment with:

```bash
agentq doctor
```

## Installation

```bash
mkdir -p ~/.local/bin
ln -sf "$PWD/agent-toolkit/scripts/agentq" ~/.local/bin/agentq
```

Make sure `~/.local/bin` is on `PATH`, then check:

```bash
agentq --version
agentq doctor
```

Most commands operate on the repository containing the current directory. Use `--repo` to select another repository explicitly:

```bash
agentq git-status --repo ~/Development/project
```

## Common options

Most commands support:

```text
--repo PATH          repository root
--format text|json   human or machine-readable output
--budget N           maximum model-visible characters
```

The default output budget is 12,000 characters.

## Repository exploration

### Find files

```bash
agentq files
agentq files assignment-offer
agentq files route --path apps/api
```

Results are bounded and sensitive paths are excluded by default.

### Search source

Search is fixed-string by default:

```bash
agentq search AssignmentOffer
agentq search 'foo.bar(' --path apps/api
```

Use regex explicitly:

```bash
agentq search '^export .*AssignmentOffer' \
  --regex \
  --path packages
```

Useful narrowing options include:

```bash
agentq search AssignmentOffer \
  --path packages/contexts/dispatch \
  --limit 40 \
  --per-file 5 \
  --context 2
```

### Read source ranges

```bash
agentq read packages/dispatch/src/offers.ts
agentq read packages/dispatch/src/offers.ts --start 40 --end 120
agentq read packages/dispatch/src/offers.ts --around 85 --context 20
```

Multiple files can be supplied in one command.

### Repository map

```bash
agentq repo-map
```

Returns a compact view of the repository and detected workspace manifests.

### Symbol outline

```bash
agentq outline apps/api/src
agentq outline packages/dispatch --match Assignment
agentq outline packages/dispatch --public
```

`agentq` uses ast-grep or Ctags when available and falls back when they are not installed.

## TypeScript and JavaScript navigation

For a known TypeScript or JavaScript symbol, prefer semantic navigation over repeated lexical searches.

Locate candidate declarations:

```bash
agentq ts-nav locate AssignmentOffer \
  --path packages/contexts/dispatch
```

Find a definition:

```bash
agentq ts-nav definition AssignmentOffer \
  --path packages/contexts/dispatch
```

Find semantic references:

```bash
agentq ts-nav references AssignmentOffer \
  --path packages/contexts/dispatch \
  --limit 60
```

Find implementations:

```bash
agentq ts-nav implementations AssignmentRepository \
  --path packages
```

Exact source positions are also supported:

```bash
agentq ts-nav references \
  apps/api/src/routes.ts:42:17
```

If a symbol is ambiguous, narrow the `--path` or select the returned candidate.

## Git inspection

### Status

```bash
agentq git-status
```

Uses Git porcelain output and returns a compact change summary.

### Diff

Start with a summary:

```bash
agentq git-diff
```

Request a bounded patch only when needed:

```bash
agentq git-diff \
  --patch \
  --path apps/api/src \
  --max-lines 300
```

Other useful modes:

```bash
agentq git-diff --staged
agentq git-diff --unstaged
agentq git-diff --base origin/main
agentq git-diff --range HEAD~3..HEAD
```

### History

```bash
agentq git-history
agentq git-history --path packages/dispatch --limit 15
```

### Structural diff

When Difftastic is installed:

```bash
agentq git-structural apps/api/src/routes.ts
```

## Dependencies and impact

Inspect local workspace dependencies:

```bash
agentq dependencies
agentq dependencies --target @opsblock/dispatch
agentq dependencies --target @opsblock/dispatch --depth 2
```

Estimate the blast radius of a symbol, file, directory, or public surface:

```bash
agentq impact AssignmentOffer
agentq impact packages/contexts/dispatch
agentq impact AssignmentOffer --path apps/api
```

Impact analysis is evidence for further inspection, not a complete static program analysis.

## Codemods

Always inspect a codemod before applying it.

### Scan

```bash
agentq codemod-scan OldName \
  --path packages
```

Regex mode:

```bash
agentq codemod-scan 'old_[a-z_]+' \
  --mode regex \
  --path packages
```

AST mode requires ast-grep:

```bash
agentq codemod-scan '$A && $A()' \
  --mode ast \
  --lang ts \
  --rewrite '$A?.()' \
  --path apps/web
```

### Apply

`codemod-apply` is a dry run unless `--apply` is supplied:

```bash
agentq codemod-apply OldName NewName \
  --path packages \
  --expect-count 37
```

Apply after reviewing the result:

```bash
agentq codemod-apply OldName NewName \
  --path packages \
  --expect-count 37 \
  --apply
```

`--expect-count` and `--max-files` can be used as safety guards.

## Running commands

Use `agentq run` to execute a command while keeping its model-visible output compact:

```bash
agentq run -- pnpm test
agentq run -- pnpm --filter @opsblock/api typecheck
agentq run -- cargo test
```

Useful options:

```bash
agentq run \
  --timeout 300 \
  --max-diagnostics 20 \
  --tail-lines 20 \
  -- \
  pnpm test
```

The full redacted command log is retained locally.

`--label` is optional and only gives the retained log a recognizable filename:

```bash
agentq run --label api-typecheck -- pnpm --filter api typecheck
```

## Affected verification

### Plan

Inspect what should be verified without running anything:

```bash
agentq test-plan
agentq test-plan --base origin/main
```

Modes:

```bash
agentq test-plan --mode focused
agentq test-plan --mode standard
agentq test-plan --mode thorough
```

### Verify

Run affected checks:

```bash
agentq verify-changed
```

Include committed branch changes relative to a base:

```bash
agentq verify-changed --base origin/main
```

Useful modes:

```bash
agentq verify-changed --mode focused
agentq verify-changed --mode standard
agentq verify-changed --mode thorough
```

Control dependent packages:

```bash
agentq verify-changed --dependents none
agentq verify-changed --dependents direct
agentq verify-changed --dependents all
```

Inspect the plan without running commands:

```bash
agentq verify-changed --dry-run
```

Other options include:

```text
--continue-on-failure
--include-build
--skip-lint
--offline
--timeout N
--max-steps N
--max-diagnostics N
```

`verified-changed` is retained as an alias for `verify-changed`.

## Task boundaries

Task tracking measures work per independently acceptable outcome.

One task is not one prompt, edit, test run, or Codex thread. A single thread may contain several sequential tasks.

Start a task:

```bash
agentq task begin
```

Check it:

```bash
agentq task status
```

Accept completed work:

```bash
agentq task accept
```

`done` is an alias:

```bash
agentq task done
```

If a completed task is immediately followed by another distinct task in the same thread or worktree:

```bash
agentq task next
```

This accepts the current task and starts the next one atomically.

Abandon work only when the outcome is intentionally discarded:

```bash
agentq task abandon
```

`cancel` is an alias.

A useful rule is:

> One task = one independently reviewable or acceptable outcome.

Investigation, implementation, failed attempts, debugging, and verification for that outcome should stay inside the same task.

For concurrent independent tasks, use separate Git worktrees.

## Statistics

Show the current repository's activity:

```bash
agentq stats
```

Detailed output, including recent operations:

```bash
agentq stats --detailed
```

Choose a time window:

```bash
agentq stats --since 24h
agentq stats --since 7d
agentq stats --since 30d
agentq stats --since all
```

Filter operations:

```bash
agentq stats --operation read
agentq stats --operation search --operation run
```

Watch the dashboard live:

```bash
agentq stats --watch 2
```

Show all observed repositories:

```bash
agentq stats --all-repos
```

Machine-readable output:

```bash
agentq stats --format json
```

The displayed token figure is a proxy based on visible characters divided by four. It is intended for comparing tool-output volume, not provider billing.

### Telemetry persistence

Hot telemetry is written to a private directory under `/tmp` so sandboxed coding agents can write it without broader home-directory permissions.

Install the user-level persistence timer:

```bash
agentq stats --install-persistence
```

The default archive interval is five minutes.

Inspect storage:

```bash
agentq stats --storage
```

Archive immediately:

```bash
agentq stats --archive-only
```

Remove the timer:

```bash
agentq stats --remove-persistence
```

Persistent history is stored under:

```text
${XDG_STATE_HOME:-~/.local/state}/agentq/
```

### Reset statistics

Reset telemetry for the current repository:

```bash
agentq stats --reset
```

Reset only volatile `/tmp` telemetry:

```bash
agentq stats --reset --hot-only
```

Reset telemetry for all repositories:

```bash
agentq stats --reset --all-repos
```

Reset is blocked while an `agentq task` is active unless `--force` is supplied.

## Patch audit

Inspect the current patch for common mechanical problems:

```bash
agentq audit
agentq audit --staged
agentq audit --base origin/main
```

The audit checks for bounded heuristic findings such as conflict markers, suspicious suppressions, debug output, whitespace problems, and secret-like additions.

It is not a substitute for code review.

## Benchmarking

Benchmark commands with Hyperfine when available:

```bash
agentq benchmark \
  --command 'pnpm test' \
  --warmup 1 \
  --runs 5
```

Compare commands:

```bash
agentq benchmark \
  --command 'rg foo src' \
  --command 'git grep foo -- src' \
  --runs 10
```

A local Python fallback is used when Hyperfine is unavailable.

## Privacy

Normal `agentq` commands operate locally.

By default:

* common credential and secret paths are excluded from search and reads;
* common secret-like values are redacted from retained command logs;
* telemetry stores operational metadata rather than source contents, search queries, command arguments, or absolute repository paths;
* telemetry can be disabled with:

```bash
export AGENTQ_TELEMETRY=0
```

Use `--include-sensitive` only when access to normally excluded paths is deliberate.

## JSON output

Most commands support structured output:

```bash
agentq search AssignmentOffer --format json
agentq git-status --format json
agentq stats --format json
```
