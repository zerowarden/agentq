---
name: mechanical-refactor
description: Execute controlled mechanical renames, literal or regex replacements, and syntax-aware ast-grep codemods with match counts, representative samples, dry runs, explicit mutation, bounded diffs, and targeted verification. Use only after the desired transformation and invariants are already decided; do not use to invent architecture or semantics.
license: MIT
compatibility: Requires the bundled agent-toolkit, Git, ripgrep, and Python 3.10+. ast-grep is required for syntax-aware transformations.
metadata:
  version: "1.8.0"
  mutation: "explicit-only"
---

# Mechanical Refactor

## Required sequence

1. Inspect the current Git state and refuse to conflate unrelated edits:

   ```bash
   agentq git-status
   ```

2. Count and sample without mutation:

   ```bash
   agentq codemod-scan 'OldName' --path packages --samples 15
   agentq codemod-scan '$A && $A()' --mode ast --lang ts --rewrite '$A?.()' --path apps/web
   ```

3. Confirm that representative matches cover every intended syntactic and semantic shape. Separate heterogeneous cases rather than using one clever regex.

4. Run the apply command without `--apply` once. Add an expected-count guard when the count is stable:

   ```bash
   agentq codemod-apply 'OldName' 'NewName' --path packages --expect-count 37
   ```

5. Mutate only with explicit authorization from the task:

   ```bash
   agentq codemod-apply 'OldName' 'NewName' --path packages --expect-count 37 --apply
   ```

6. Inspect a bounded diff and run the smallest relevant formatter, typecheck, and tests.

## Choosing a mode

- `fixed`: default for identifiers, paths, text, and TypeScript syntax containing regex metacharacters.
- `regex`: only for genuinely textual patterns with carefully reviewed captures.
- `ast`: for syntax-dependent transformations where lexical replacement can alter comments, strings, shadowed names, or unrelated constructs.

## Guardrails

- Dry-run is the default; never bypass it.
- `--max-files` and `--expect-count` are safety controls, not inconveniences.
- Do not transform sensitive files or binary/generated artifacts.
- A successful replacement count does not prove semantic correctness.
- For exported symbol renames, use LSP rename when available; use this skill for controlled bulk shapes or follow-up text.
- Do not combine architectural movement, behavior changes, and a mechanical codemod in one opaque patch.
