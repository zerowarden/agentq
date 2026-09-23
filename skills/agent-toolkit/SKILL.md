---
name: agent-toolkit
description: Toolkit maintenance only: validate agentq, diagnose local dependencies/privacy behavior, install optional FOSS tools, or inspect command recipes. Never use implicitly for normal coding, search, Git inspection, or verification.
---

# Agent Toolkit

This skill maintains the shared `agentq` runtime used by the other skills. Ordinary commands operate locally and do not initiate network access.

## Command surface

The public surface is deliberately three commands:

```text
agentq search    bounded repository search; fixed-string by default
agentq inspect   single-entry inspection for symbols, paths, and source ranges
agentq continue  resume a truncated result from a continuation cursor
```

`agentq inspect` accepts symbols, existing repository paths, and source ranges; it rejects literal content and directs callers to `agentq search`.

```bash
agentq --version
agentq COMMAND --help
```

Inspect flags with:

```bash
agentq search --help
agentq inspect --help
agentq continue --help
```

## Search

```bash
agentq search 'assignment offer' --path packages --format compact-json
agentq search 'export\s+(type|interface)\s+Assignment' --regex --path packages
```

- Fixed-string is the default; `--regex` is an explicit mode.
- `--path` narrows to one or more repository-relative scopes; repeatable.
- `--format text|json|compact-json`; `compact-json` is the structured form for agents.
- Sensitive paths are excluded by default. Totals report `coverage` and `count_quality`; treat `lower_bound` totals as `>=` values.

## Inspect

```bash
agentq inspect AssignmentOffer --path packages --intent understand
agentq inspect AssignmentOffer --path packages --intent edit
agentq inspect packages/contexts/dispatch/src/offers.ts --lines 40:120
agentq inspect src/service.ts --line 57 --column 12
agentq inspect listOrders --candidate cand-7f3a9c2d4e5b6a708192a3b4
```

- `TARGET` is a symbol name, an existing repository-relative path, or a `symbol:`/`path:` prefixed selector when the string is ambiguous.
- `--intent understand|edit|rename|refactor|impact` selects the evidence emphasis.
- `--line N` (repeatable anchor), `--lines START:END` (repeatable range), and `--column N` (with a single `--line`) apply to file targets.
- `--candidate ID` re-selects a declaration candidate issued for the current repository state.
- `--path` narrows evidence scopes; `--format text|json`; `--debug` writes a stage trace to stderr.

For symbol work, prefer one `inspect --intent` call over repeated exploration. The bundle reports requirements as `satisfied`/`unsatisfied`, selected evidence with compact provenance (`provider`, `method`, `provider_version`, source versions, `effective_scope`, `coverage`), and explicit `gaps`. Treat sampled or partial coverage as incomplete. A `source_unstable` gap means a source changed during inspection: that evidence is omitted and never satisfies a requirement, so re-run the inspection before relying on it.

Intent emphasis:

- `understand` (default) — declaration and representative context.
- `edit` — adds exact declaration source, tests, and owning package.
- `rename` — references and mentions for a rename decision.
- `refactor` — implementations, source, tests, and ownership.
- `impact` — references, implementations, and owning package.

Ambiguous symbols return candidate ids without guessing. Re-run with `--candidate`, a narrower `--path`, or an explicit file/range.

## Continuations

Truncated `search` results include a short cursor (`continue: agentq continue q7H2a`). Run `agentq continue CURSOR` to resume the exact stored operation; cursors are scoped to the repository and session and expire. Other truncated operations return a recovery command to rerun directly.

## Privacy and local state

- Default local-only: no network requests during normal use.
- Common sensitive paths are excluded from searches and reads; secret-like values are redacted from retained command logs.
- Repeat-suppression state is local and bounded; query/command identity uses keyed local digests, never raw queries, source text, command arguments, or absolute repository paths.
- Exact-repeat suppression is controlled solely by `AGENTQ_CONTEXT_CACHE` and requires explicit session identity (`AGENTQ_SESSION_ID` or a recognized host thread id); without one, no suppression state is shared.

## Optional dependencies

Read `references/tooling.md` before installing anything. The bundle works with Git, ripgrep, and Python alone; optional tools add richer outline evidence. The installer is dry-run by default:

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
