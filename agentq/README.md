# agentq

`agentq` is a local command-line helper for you and your coding agent. It finds the code that matters and returns only the evidence needed, without flooding the chat with thousands of lines of terminal output.

The agent-facing surface is intentionally small:

```text
agentq search    bounded repository search; fixed-string by default
agentq inspect   single-entry inspection for symbols, paths, and source ranges
agentq continue  resume a truncated result from a continuation cursor
```

`inspect` resolves symbols, existing repository paths, and explicit source ranges. It rejects literal content and directs callers to `search`.

## Requirements

- Python 3.10+
- Git
- [ripgrep](https://github.com/BurntSushi/ripgrep)

Optional tools add richer inspection evidence: ast-grep and Universal Ctags for path outlines.

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
# Search for a literal or an explicit regex
agentq search AssignmentOffer --path packages
agentq search 'export\s+(type|interface)\s+Assignment' --regex --path packages

# Inspect a symbol, file, directory, or source range
agentq inspect AssignmentOffer --path packages --intent understand
agentq inspect AssignmentOffer --path packages --intent edit
agentq inspect apps/api/src/routes.ts --lines 40:120

# Resume a truncated result
agentq continue <cursor>
```

Run `agentq --help` or `agentq COMMAND --help` for the full command reference.

## Inspection bundles

`inspect` returns one bounded bundle for one entry point:

- requirements with `satisfied`/`unsatisfied` status and an explicit reason;
- selected evidence with compact acquisition provenance (`provider`, `method`, `provider_version`, source versions, `effective_scope`, `coverage`);
- resolution candidates when a symbol is ambiguous instead of a guessed declaration;
- explicit `gaps` for truncation, unavailable adapters, and unstable sources.

The `--intent` flag selects the evidence emphasis:

```text
understand  declaration and representative context (default)
edit        exact declaration source, tests, and owning package
rename      references and mentions for a rename decision
refactor    implementations, source, tests, and ownership
impact      references, implementations, and owning package
```

Common options include:

```text
--path PATH...          narrow the scope
--intent INTENT         choose the evidence emphasis
--line N                source anchor when TARGET is a file (repeatable)
--lines START:END       explicit source range when TARGET is a file (repeatable)
--column N              exact location with a single --line
--candidate ID          re-select a reported declaration candidate
--format text|json      choose human or machine output
--debug                 write a structured stage trace to stderr
```

`search` additionally accepts `--regex` and `--format compact-json`.

The default output budget is 12,000 characters. Acquisition policy (per-file
sampling, page limits, coverage counting, context) and the delivery budget are
chosen by `agentq`, not by the caller.

## Privacy

By default, `agentq`:

- makes no network requests during normal use;
- excludes common sensitive paths from searches and reads; and
- redacts common secret-like values from retained command logs.

Repository root, filesystem boundary, sensitive-path policy, and output budgets
are host concerns; the agent-facing commands do not expose them as flags.

## More help

```bash
agentq --help
agentq COMMAND --help
```

The skill documentation under [`skills/`](../skills/) contains stricter workflows for coding agents.
