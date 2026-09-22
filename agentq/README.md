# agentq

`agentq` is a local command-line helper for you and your coding agent. It finds the code that matters and returns only the evidence needed, without flooding the chat with thousands of lines of terminal output.

The agent-facing surface is intentionally small:

```text
agentq search    bounded repository search
agentq inspect   symbols, literals, files, and source anchors
agentq continue  resume a truncated result from a continuation cursor
```

Underlying capabilities (Git evidence, workspace graphs, verification planning, mutation planning, telemetry) remain in the package for future adapters; they are not exposed as commands.

## Requirements

- Python 3.10+
- Git
- [ripgrep](https://github.com/BurntSushi/ripgrep)

Optional tools add richer inspection evidence: ast-grep and Universal Ctags for outlines, Difftastic for structural diffs.

## Install

From this repository:

```bash
uv sync
uv run agentq --version
```

`uv sync` installs `agentq` into the project virtual environment. To install the command on your `PATH`, use `uv tool install .`.

## Quick start

Commands use the repository containing your current directory.

```bash
# Find a symbol or literal
agentq search AssignmentOffer --path packages
agentq inspect AssignmentOffer --path packages

# Inspect exact source locations
agentq inspect apps/api/src/routes.ts --lines 40:120

# Resume a truncated result
agentq continue <cursor>
```

Run `agentq --help` or `agentq COMMAND --help` for the full command reference.

## Focused output

`agentq` keeps results small enough to be useful in an AI conversation. It starts with summaries or selected evidence, then gives an exact follow-up command when more output is available.

Common options include:

```text
--path PATH...       narrow the scope
--budget N           cap visible output
--format text|json   choose human or machine output
```

The default output budget is 12,000 characters. Acquisition policy (per-file
sampling, page limits, coverage counting, context) is chosen by `agentq`, not
by the caller.

## Privacy

By default, `agentq`:

- makes no network requests during normal use;
- excludes common sensitive paths from searches and reads;
- redacts common secret-like values from retained command logs; and
- stores operational telemetry rather than source contents, raw queries, raw command arguments, or absolute repository paths.

Repository root, filesystem boundary, sensitive-path policy, and output budgets
are host concerns; the agent-facing commands do not expose them as flags.

Telemetry is local and can be disabled completely:

```bash
export AGENTQ_TELEMETRY=0
```

## More help

```bash
agentq --help
agentq COMMAND --help
```

The skill documentation under [`skills/`](../skills/) contains stricter workflows for coding agents.
