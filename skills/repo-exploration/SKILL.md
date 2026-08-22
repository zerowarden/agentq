---
name: repo-exploration
description: Use for repository exploration, file discovery, code search, symbol location, manifests, and bounded source reads. MUST use bundled agentq wrappers instead of raw tree, find, rg, grep, or whole-file reads when agentq covers the operation. Do not use for Git diff review, semantic TypeScript identity, codemods, or verification.
license: MIT
compatibility: Requires the bundled agent-toolkit plus Git, ripgrep, and Python 3.10+. ast-grep and Universal Ctags are optional.
metadata:
  version: "1.2.1"
  mutation: "none"
---

# Repository Exploration

```bash
AQ=~/.agents/skills/repo-exploration/scripts/agentq
```

## Mandatory tool policy

For operations covered here, use `agentq` instead of raw `tree`, `find`, `rg`, `grep`, `cat`, or equivalent broad discovery commands. Fall back only when `agentq` cannot express the operation or fails; keep any fallback explicitly bounded.

## Evidence ladder

1. Map once if unfamiliar:
   ```bash
   "$AQ" repo-map
   ```
2. Locate paths before contents:
   ```bash
   "$AQ" files assignment-offer --path packages --limit 40
   ```
3. Search exact text; fixed-string is the default:
   ```bash
   "$AQ" search 'AssignmentOffer' --path packages/contexts/dispatch --limit 60
   ```
4. Inspect structure before opening many files:
   ```bash
   "$AQ" outline packages/contexts/dispatch/src --match 'Offer|Assignment' --limit 100
   ```
5. Read only necessary ranges:
   ```bash
   "$AQ" read packages/contexts/dispatch/src/offers.ts:40-150 --max-lines 140
   "$AQ" read packages/contexts/dispatch/src/offers.ts --around 220 --context 30
   ```
6. When same-named symbols, re-exports, aliases, or actual call/reference identity matter, switch to `semantic-code-navigation` rather than broadening lexical search.

## Output discipline

- Default model-visible output is globally capped by `--budget 12000`; narrow scope before raising it.
- If output is truncated, narrow by package, path, type, glob, symbol, or literal.
- Do not read generated output, lockfiles, snapshots, or vendored dependencies unless required.
- Sensitive paths are excluded by default.
- Stop once ownership, implementation, relevant callers, and tests are identified.
