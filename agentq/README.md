# agentq

`agentq` is a local command-line helper for you and your coding agent. It finds the code that matters, summarizes changes, and runs checks without flooding the chat with thousands of lines of terminal output.

Think of it as a quieter toolbox for vibe-coding: less scrolling, less repeated reading, and more room for the agent to focus on your actual code.

It can help you:

- find files, text, and symbols;
- read only the relevant parts of large files;
- review Git changes in manageable chunks;
- estimate what a change might affect;
- run tests, lint, typechecks, and builds with concise results;
- apply guarded codemods; and
- track how efficiently an agent is working.

Everything runs locally. Common secret and credential paths are excluded by default, and retained command logs redact common secret-like values.

## Requirements

- Python 3.10+
- Git
- [ripgrep](https://github.com/BurntSushi/ripgrep)

Optional tools add richer outlines, diffs, audits, and benchmarks: ast-grep, Universal Ctags, Difftastic, ShellCheck, Gitleaks, Hyperfine, and others.

Check what is available:

```bash
agentq doctor
```

## Install

From this repository:

```bash
uv sync
uv run agentq --version
uv run agentq doctor
```

`uv sync` installs `agentq` into the project virtual environment. To install the command on your `PATH`, use `uv tool install .`.

## Quick start

Most commands use the repository containing your current directory.

```bash
# Understand the repo
agentq repo-map
agentq search AssignmentOffer
agentq inspect AssignmentOffer --path packages
agentq read apps/api/src/routes.ts:40-120

# Review changes
agentq git-status
agentq git-diff
agentq audit

# Run checks without noisy output
agentq run -- pnpm test
agentq verify
```

These examples cover the usual loop: explore, edit, review, and verify. Run `agentq --help` or `agentq COMMAND --help` for the full command reference.

## Focused output

`agentq` keeps results small enough to be useful in an AI conversation. It starts with summaries or selected evidence, then gives an exact follow-up command when more output is available.

Common options include:

```text
--repo PATH          choose a repository
--path PATH...       narrow the scope
--budget N           cap visible output
--format text|json   choose human or machine output
```

The default output budget is 12,000 characters. Use `--repeat` when you intentionally want to show unchanged evidence again.

## A practical agent workflow

Start by locating the smallest useful piece of code. Inspect the change before asking for a full patch, then verify the affected area before widening to larger checks.

For independently reviewable pieces of work, task boundaries keep measurements and diffs scoped to that outcome:

```bash
agentq task begin
# explore, edit, and verify
agentq task accept
```

While a task is active, `agentq verify` and `agentq git-diff --task` focus on changes made for that task, even if the worktree was already dirty.

## Safe changes

Codemods are dry runs unless explicitly applied. Match counts and file limits can be used as guardrails.

```bash
agentq codemod-apply OldName NewName --path packages --expect-count 12
```

Review the preview, then add `--apply` when it is correct.

Impact analysis can point out likely callers, tests, docs, and package dependents before a shared name or file changes:

```bash
agentq impact AssignmentOffer
```

Treat the result as a guide for further inspection, not proof that every runtime dependency was found.

## Stats

`agentq stats` shows tool reliability, output volume, repeated reading, task activity, and verification outcomes. Use it to spot noisy or wasteful agent workflows without storing source code in telemetry.

```bash
agentq stats
agentq stats --detail
```

Telemetry is local and can be disabled completely:

```bash
export AGENTQ_TELEMETRY=0
```

## Privacy

By default, `agentq`:

- makes no network requests during normal use;
- excludes common sensitive paths from searches and reads;
- redacts common secret-like values from retained command logs; and
- stores operational telemetry rather than source contents, raw queries, raw command arguments, or absolute repository paths.

Use `--include-sensitive` only when access to excluded paths is deliberate.

## More help

```bash
agentq --help
agentq COMMAND --help
```

The skill documentation under [`skills/`](../skills/) contains stricter workflows for coding agents.
