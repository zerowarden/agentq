---
name: change-impact-analysis
description: Estimate the likely blast radius before changing or renaming a shared symbol, file, package, API, contract, schema, migration, config key, or build surface. Combines bounded lexical/import evidence, related tests and docs/config references, package ownership, and optional workspace dependencies. Use for planning and risk classification, not as a semantic proof.
---

# Change Impact Analysis

## Workflow

1. For a package, public entry point, or shared platform surface, inspect package dependents first:

   ```bash
   agentq dependencies --target '@app/dispatch' --depth 2
   ```

2. Run the bounded impact collector on the exact symbol or path:

   ```bash
   agentq impact AssignmentOffer --path packages --limit 120
   agentq impact packages/contracts/src/dispatch.ts --limit 120
   ```

3. For a known TypeScript/JavaScript symbol, use one `agentq ts-nav overview SYMBOL --path <owning-package>` operation to collect the declaration, references, and implementations. Use a primitive action only when the task needs exactly one evidence class. Reconcile semantic references with the lexical lower bound.

4. Inspect only the highest-signal implementation, caller, test, contract, config, migration, and documentation ranges.

5. Report:
   - owning package and public surface;
   - direct and downstream packages;
   - likely affected tests and verification scope;
   - low, medium, or high blast radius;
   - evidence gaps and dynamic behavior not represented.

## Guardrails

- Lexical/import evidence is a lower bound and can include false positives.
- Manifest dependency edges are package-level, not symbol-level.
- Shared configuration, schemas, contracts, migrations, authorization, tenancy, and concurrency surfaces require conservative classification.
- A cap reached on references raises uncertainty; narrow by package for another pass.
- Do not broaden implementation scope merely because adjacent references exist.
