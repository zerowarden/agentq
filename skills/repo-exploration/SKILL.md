---
name: repo-exploration
description: Use for repository maps, file discovery, unknown-symbol search, manifests, and bounded source reads. MUST use bundled agentq wrappers instead of raw tree, find, rg, grep, or whole-file reads. If a TypeScript/JavaScript symbol is already known, route to semantic-code-navigation before lexical search or repeated reads.
license: MIT
compatibility: Requires the bundled agent-toolkit plus Git, ripgrep, and Python 3.10+. ast-grep and Universal Ctags are optional.
metadata:
  version: "1.2.4"
  mutation: "none"
---

# Repository Exploration

```bash
AQ=~/.agents/skills/repo-exploration/scripts/agentq
```

## Mandatory tool policy

For operations covered here, use `agentq` instead of raw `tree`, `find`, `rg`, `grep`, `cat`, or equivalent broad discovery commands. Fall back only when `agentq` cannot express the operation or fails; keep fallback output explicitly bounded.

## Evidence ladder

1. Map once if unfamiliar:

   ```bash
   "$AQ" repo-map
   ```

2. Locate paths before contents:

   ```bash
   "$AQ" files assignment-offer --path packages --limit 40
   ```

3. If an exact TypeScript/JavaScript identifier is known, switch immediately to semantic navigation:

   ```bash
   "$AQ" ts-nav references AssignmentOffer --path packages/contexts/dispatch
   ```

4. Otherwise search exact text; fixed-string is the default:

   ```bash
   "$AQ" search 'assignment offer' --path packages/contexts/dispatch --limit 60
   ```

5. Inspect structure before opening many files:

   ```bash
   "$AQ" outline packages/contexts/dispatch/src --match 'Offer|Assignment' --limit 100
   ```

6. Read only necessary ranges:

   ```bash
   "$AQ" read packages/contexts/dispatch/src/offers.ts:40-150 --max-lines 140
   "$AQ" read packages/contexts/dispatch/src/offers.ts --around 220 --context 30
   ```

7. Before a second overlapping or non-adjacent read of the same source file, narrow first: `ts-nav` for a known TS/JS symbol, otherwise `outline --match` or a more specific `search`. Direct continuation of a truncated adjacent range is fine.

## Task measurement

A task is one independently acceptable implementation, fix, refactor, or review outcome. It is not one prompt, edit, test run, or Codex thread.

- Keep clarification, implementation, debugging, correction, and verification for the same outcome inside one task.
- One long-lived Codex thread may contain several sequential tasks.
- Start another task only when the previous outcome could be accepted independently and the next requested outcome is distinct.
- When continuing in the same thread, rotate atomically:

  ```bash
  agentq task next
  ```

- Use `agentq task abandon` only when the outcome is intentionally cancelled or discarded.
- Do not create task boundaries for status questions, individual edits, failing checks, or verification retries.

## Output discipline

- Default model-visible output is globally capped by `--budget 12000`; narrow scope before raising it.
- If output is truncated, narrow by package, path, type, glob, symbol, or literal.
- Do not read generated output, lockfiles, snapshots, or vendored dependencies unless required.
- Sensitive paths are excluded by default.
- Stop once ownership, implementation, relevant callers, and tests are identified.
