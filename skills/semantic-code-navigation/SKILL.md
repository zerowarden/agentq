---
name: semantic-code-navigation
description: Use first when a TypeScript/JavaScript symbol name or exact source position is known and definition, references, implementations, aliases, or re-exports matter. MUST use bounded agentq ts-nav before broad lexical search or repeated reads; use repo-exploration only to resolve ambiguity or when the TypeScript project cannot load.
license: MIT
compatibility: Requires the bundled agent-toolkit, Node.js, and a TypeScript dependency resolvable from the repository. Uses only local project files.
metadata:
  version: "1.2.4"
  mutation: "none"
---

# Semantic Code Navigation

```bash
AQ=~/.agents/skills/semantic-code-navigation/scripts/agentq
```

## Symbol-first workflow

When the symbol name is known, do not begin with broad `rg` or several source reads.

1. Query it directly, scoped to the likely owning package when possible:

   ```bash
   "$AQ" ts-nav locate AssignmentOffer --path packages/contexts/dispatch
   "$AQ" ts-nav definition AssignmentOffer --path packages/contexts/dispatch
   "$AQ" ts-nav references AssignmentOffer --path packages/contexts/dispatch --limit 60
   "$AQ" ts-nav implementations AssignmentOffer --path packages/contexts/dispatch
   ```

2. If several declarations remain, narrow with another `--path` or select the numbered candidate explicitly:

   ```bash
   "$AQ" ts-nav references AssignmentOffer --path packages/contexts/dispatch --pick 2
   ```

3. Read only the returned declaration/caller ranges that affect the task.

## Exact-position workflow

Use an exact position when the relevant occurrence is already known:

```bash
"$AQ" ts-nav definition --file packages/foo/src/bar.ts --line 42 --column 17
"$AQ" ts-nav references --file packages/foo/src/bar.ts --line 42 --column 17 --limit 60
"$AQ" ts-nav implementations --file packages/foo/src/bar.ts --line 42 --column 17
```

## Guardrails

- A known TS/JS identifier is a semantic-navigation trigger, not merely an ambiguity fallback.
- Use lexical search only to discover an unknown symbol, investigate strings/configuration, or recover when semantic project resolution fails.
- Do not infer semantic equivalence from same-named lexical matches.
- If output is truncated, narrow the owning package before raising limits.
- The repository must expose a usable `tsconfig.json` and local `typescript` package for semantic resolution. Do not install packages or contact the network merely to make this skill work.
