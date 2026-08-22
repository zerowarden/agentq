---
name: targeted-verification
description: Use for tests, typechecks, lint, builds, and changed-code verification. MUST use workspace-aware agentq verify-changed or bounded agentq run instead of raw verbose verification when covered. Prefer the narrowest affected ladder; do not start watch mode, servers, or interactive prompts.
license: MIT
compatibility: Requires the bundled agent-toolkit and Python 3.10+. Project commands must already be installed. Node/pnpm workspaces and Vitest receive specialized local planning.
metadata:
  version: "1.2.4"
  mutation: "command-dependent"
---

# Targeted Verification

```bash
AQ=~/.agents/skills/targeted-verification/scripts/agentq
```

## Changed-code default

Plan without execution when scope is uncertain:

```bash
"$AQ" verify-changed --dry-run
"$AQ" verify-changed --dry-run --base origin/main
```

Then execute the workspace-aware ladder:

```bash
"$AQ" verify-changed
```

`standard` mode verifies changed packages plus direct local dependents. Use narrower or broader modes deliberately:

```bash
"$AQ" verify-changed --mode focused
"$AQ" verify-changed --mode thorough
```

The legacy spelling `verified-changed` is accepted as an alias; prefer `verify-changed` in new instructions.

## Explicit command fallback

Use `agentq run` when the repository has a known command that the planner cannot infer:

```bash
"$AQ" run -- pnpm --filter @app/dispatch test -- assignment-offer.test.ts
"$AQ" run --timeout 1200 -- pnpm --filter api typecheck
"$AQ" run --cwd crates/engine -- cargo test query_parser
```

Use `--label` only when several retained logs would otherwise be ambiguous.

## Evidence discipline

- Use returned diagnostics and summaries; do not paste complete logs into context.
- Read only a narrow range from the exact returned log path when more evidence is required:

  ```bash
  "$AQ" read --allow-outside <returned-log-path>:120-190
  ```

- Escalate only after narrower checks pass or project policy requires it.
- Do not rerun the same broad failing command without using its diagnostics.
- Never treat `partial` or `unverified` as passing.
- If an active task now satisfies its complete acceptance criteria, use `agentq task accept`. If another independently acceptable outcome begins immediately in the same Codex thread, use `agentq task next`. Do not rotate tasks for a narrow passing check, correction, or verification retry; use `agentq task abandon` only when the outcome is intentionally discarded.

## Runtime behavior

- Logs and child `XDG_CACHE_HOME` use a private sandbox-writable temporary directory.
- `--offline` disables common package-manager network paths when dependencies are already present.
- Full redacted logs are mode `0600`; model-visible output remains character-bounded.
