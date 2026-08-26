---
name: repo-exploration
description: Use for repository structure, unknown symbols, literals/configuration, file discovery, and bounded source reads. MUST use agentq rather than raw broad tree/find/rg/grep/cat. For an exact TypeScript/JavaScript or Python symbol, use one agentq inspect overview operation rather than a lexical discovery ladder.
license: MIT
compatibility: Requires the bundled agent-toolkit plus Git, ripgrep, and Python 3.10+. TypeScript semantic inspection additionally requires Node.js and project TypeScript. ast-grep and Universal Ctags are optional.
metadata:
  version: "1.4.0"
  mutation: "none"
---

# Repository Exploration

## Mandatory tool policy

For operations covered here, use the PATH-installed `agentq`. Do not use raw broad `tree`, `find`, `rg`, `grep`, `cat`, or equivalent discovery unless agentq cannot express the operation or actually fails. Keep any fallback explicitly bounded.

## Choose exactly one starting operation

Do not execute these as a ladder. Select the narrowest row matching what is already known.

| Known evidence | Start with | Purpose |
|---|---|---|
| Exact TS/JS symbol | `agentq inspect SYMBOL --path OWNER` | Definition, references, implementations and previews in one operation |
| Exact Python symbol | `agentq inspect SYMBOL --path OWNER` | AST definitions and bounded lexical AST references in one operation |
| Exact TS/JS source position | `agentq ts-nav overview SYMBOL` when name is known; otherwise `agentq ts-nav references path.ts:LINE:COLUMN` | Semantic navigation without lexical rediscovery |
| Identifier prefix / uncertain spelling | `agentq search PREFIX --path SCOPE --format compact-json` | Declaration candidates and grouped lexical usage |
| String, SQL fragment, route, config key/value | `agentq search 'LITERAL' --path SCOPE --context 3 --format compact-json` | Contextual lexical snippets |
| Known file/range | `agentq read FILE:START-END` | Only required source |
| Several known lines in one file | `agentq inspect FILE --line 30 110 150 --context 5` | Merge source windows and emit each line once |
| Known file but unknown structure | `agentq outline FILE --match 'PATTERN'` | Structural landmarks before reading |
| Unknown repository layout | `agentq repo-map` | One compact repository map |
| Filename/path fragment only | `agentq files FRAGMENT --path SCOPE` | Locate candidate paths |

`search --view auto --format compact-json` is normally sufficient for Agentq-driven exploration. Use text when direct human reading is the goal. Do not add `--limit`, `--budget`, or `--samples-per-file` on the first call unless the task itself requires a hard bound different from the defaults.

## Stop conditions

- A complete contextual search result answers an exact literal/config lookup: stop; do not read the same lines again.
- `inspect`/`ts-nav overview` returns the declaration and relevant semantic callers: read only a declaration/body range that is actually required for implementation.
- If a header is `[sampled]` or `[partial]`, use its exact continuation; do not restart discovery.
- If symbol resolution is ambiguous, narrow `--path` or use the numbered `--pick`; do not restart discovery from repository root.
- Do not run `repo-map` when the owning path is already known.
- Do not run `files` before `search` when the search scope is already known.
- Do not run `locate`, `definition`, `references`, and `implementations` sequentially; use `overview`/`inspect`.
- Batch known files and anchors in one invocation: `agentq read app.py:30,85,140 lib.py:20-45,110 --context 5`. For exact ranges, use `agentq read a.ts:20-70 b.ts:90-140 c.ts:1-45`.
- For several anchors in one file, `agentq inspect FILE --line 30 110 150 --context 5` is equivalent. Explicit ranges are repeatable with `--lines 30:50 --lines 108:125`; overlapping windows are merged.
- Unchanged same-task overlap is omitted. Use `--repeat` only when the complete range must be emitted again.

## Task measurement

A task is one independently acceptable implementation, fix, refactor, or review outcome, not a prompt, edit, test run, or Codex thread. Keep investigation, implementation, debugging and verification for one outcome in the same task. One thread may contain several sequential tasks; use `agentq task next` only after the current outcome could be accepted independently.

## Output discipline

Paths are grouped once per search file block. Search totals describe repository coverage separately from rendered samples. Sensitive paths remain excluded by default. Stop once ownership, implementation, relevant callers and tests are established.
