---
name: workspace-dependency-inspection
description: Inspect local monorepo package dependencies and dependents from package.json and Cargo workspace manifests. Use to identify owning packages, package-level fan-in or fan-out, cycles, and downstream packages before cross-package changes. Do not use this as a source-import, runtime-loading, or semantic symbol graph.
license: MIT
compatibility: Requires the bundled agent-toolkit and Python 3.10+. Cargo metadata is used locally and offline when Cargo is available.
metadata:
  version: "1.8.0"
  mutation: "none"
---

# Workspace Dependency Inspection

## Workflow

Get a compact workspace-level overview:

```bash
agentq dependencies --limit 100
```

Inspect direct and transitive local dependencies/dependents for one package:

```bash
agentq dependencies --target '@app/dispatch' --depth 2 --limit 80
agentq dependencies --target packages/contexts/dispatch --depth 3
```

For changed-code verification, use the same graph through:

```bash
agentq verify-changed --dry-run
```

This identifies changed packages and affected local dependents before executing checks.

## Interpretation

- Edges come from local dependency declarations in `package.json` and Cargo workspace metadata.
- `dependencies`, `devDependencies`, `peerDependencies`, and `optionalDependencies` remain distinguishable in JSON output.
- A package dependency does not prove that a particular symbol or module is used.
- Source aliases, dynamic imports, runtime plugins, generated code, SQL dependencies, and external services are outside this graph.
- Use LSP references and bounded lexical search for source-level questions.

## Output discipline

- Query one target before requesting deeper traversal.
- Depth two is normally sufficient for implementation planning.
- Treat cycles as architectural evidence to inspect, not automatic defects.
- Use `--format json` only when another deterministic script needs the graph.
