---
name: semantic-code-navigation
description: Use when an exact TypeScript/JavaScript symbol or exact source position is known and semantic definition, references, and implementations matter. Prefer one agentq ts-nav overview or agentq inspect call; use lexical repo-exploration only for unknown names, strings/configuration, or semantic-project failure.
license: MIT
compatibility: Requires the bundled agent-toolkit, Node.js, and a TypeScript dependency resolvable from the repository. Uses only local project files.
metadata:
  version: "1.3.0"
  mutation: "none"
---

# Semantic Code Navigation

## One-call default

For a known identifier, start with one overview rather than a sequence of primitive calls:

```bash
agentq ts-nav overview AssignmentOffer --path packages/contexts/dispatch
# Equivalent agent-facing entry point when repository inspection is the intent:
agentq inspect AssignmentOffer --path packages/contexts/dispatch
```

The overview returns the selected declaration, declaration span, definitions, references, implementations, source previews and per-file grouping from one TypeScript language-service process.

If several exact declarations remain, narrow the scope or select the returned candidate:

```bash
agentq ts-nav overview AssignmentOffer --path packages/contexts/dispatch --pick 2
```

Use primitive actions only when the task explicitly needs one class of semantic evidence:

```bash
agentq ts-nav refs AssignmentOffer --path packages/contexts/dispatch
agentq ts-nav def AssignmentOffer --path packages/contexts/dispatch
agentq ts-nav impls AssignmentOffer --path packages/contexts/dispatch
```

The aliases above are intentionally accepted because agents commonly emit them; canonical spellings remain `references`, `definition`, and `implementations`.

## Exact-position compatibility

Both forms are valid:

```bash
agentq ts-nav references --file packages/foo/src/bar.ts --line 42 --column 17
agentq ts-nav references packages/foo/src/bar.ts:42:17
```

## Guardrails

- A simple identifier prefix is not an exact symbol. Use `agentq search PREFIX` to obtain declaration candidates first.
- Do not run `locate`, `definition`, `references`, and `implementations` serially for routine exploration.
- Lexical search is for unknown names, strings/configuration, or recovery when semantic project resolution fails; same-named lexical matches do not prove semantic equivalence.
- If the overview is sampled, narrow the owning package before increasing limits.
- Do not install packages or contact the network merely to make semantic resolution work.
