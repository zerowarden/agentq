---
name: targeted-verification
description: Use for tests, typechecks, lint, builds, and changed-code verification. MUST use workspace-aware agentq verify or bounded agentq run instead of raw verbose verification when covered. Canonical verify automatically uses the active task baseline when present; do not start watch mode, servers, or interactive prompts.
license: MIT
compatibility: Requires the bundled agent-toolkit and Python 3.10+. Project commands must already be installed. Node/pnpm workspaces and Vitest receive specialized local planning.
metadata:
  version: "1.8.0"
  mutation: "command-dependent"
---

# Targeted Verification

## Changed-code default

Use the canonical command:

```bash
agentq verify --dry-run
agentq verify
```

With an active agentq task, `verify` automatically scopes to files changed since that task baseline. Without an active task it verifies the worktree; `--base origin/main` requests base-scoped verification. Use explicit forms only when overriding that automatic choice:

```bash
agentq verify-task                 # force active-task scope
agentq verify-changed              # force worktree/base behavior
agentq verify --base origin/main
```

`agentq task changes`, `agentq git-diff --task`, and `agentq test-plan --task` expose the task baseline explicitly.

`standard` mode verifies changed packages plus direct local dependents. Use narrower or broader modes deliberately:

```bash
agentq verify --mode focused
agentq verify --mode thorough
```

The legacy spelling `verified-changed` remains accepted for compatibility. New instructions should use `verify` unless an explicit scope override is required. Dry-run results are reported as `DRY-RUN`, not as pending work.

## Explicit command fallback

Use `agentq run` when the repository has a known command that the planner cannot infer:

```bash
agentq run -- pnpm --filter @app/dispatch test -- assignment-offer.test.ts
agentq run --timeout 1200 -- pnpm --filter api typecheck
agentq run --cwd crates/engine -- cargo test query_parser
```

Use `--label` only when several retained logs would otherwise be ambiguous.

## Evidence discipline

- Use returned diagnostics and summaries; do not paste complete logs into context.
- Read only a narrow range from the exact returned log path when more evidence is required:

  ```bash
  agentq read --allow-outside <returned-log-path>:120-190
  ```

- Escalate only after narrower checks pass or project policy requires it.
- Do not rerun the same broad failing command without using its diagnostics.
- Never treat `partial` or `unverified` as passing.
- If an active task now satisfies its complete acceptance criteria, use `agentq task accept`. If another independently acceptable outcome begins immediately in the same Codex thread, use `agentq task next`. Do not rotate tasks for a narrow passing check, correction, or verification retry; use `agentq task abandon` only when the outcome is intentionally discarded.

## Runtime behavior

- Logs and child `XDG_CACHE_HOME` use a private sandbox-writable temporary directory.
- `--offline` disables common package-manager network paths when dependencies are already present.
- Full redacted logs are mode `0600`; model-visible output remains character-bounded.
