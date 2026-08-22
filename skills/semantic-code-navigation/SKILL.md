---
name: semantic-code-navigation
description: Use for type-aware TypeScript/JavaScript definition, reference, and implementation lookup when lexical rg results are ambiguous or would require opening many files. MUST use bounded agentq ts-nav for supported semantic queries; fall back to repo-exploration only when TypeScript project resolution is unavailable.
license: MIT
compatibility: Requires the bundled agent-toolkit, Node.js, and a TypeScript dependency resolvable from the repository. Uses only local project files.
metadata:
  version: "1.2.1"
  mutation: "none"
---

# Semantic Code Navigation

```bash
AQ=~/.agents/skills/semantic-code-navigation/scripts/agentq
```

## Workflow

1. Locate the symbol occurrence with `repo-exploration` if its file/position is not already known.
2. Resolve semantic identity from the exact source position:

   ```bash
   "$AQ" ts-nav definition --file packages/foo/src/bar.ts --line 42 --column 17
   "$AQ" ts-nav references --file packages/foo/src/bar.ts --line 42 --column 17 --limit 60
   "$AQ" ts-nav implementations --file packages/foo/src/bar.ts --line 42 --column 17
   ```

3. Read only the returned ranges that affect the task.
4. If a query is truncated, narrow to the owning package or inspect the highest-signal references before raising limits.

## Guardrails

- Do not substitute a broad `rg SymbolName` when semantic identity matters.
- Do not infer semantic equivalence from same-named lexical matches.
- The repository must expose a usable `tsconfig.json` and local `typescript` package. If not, use bounded lexical/syntactic exploration and state the limitation.
- Do not install TypeScript or contact the network merely to make this skill work.
