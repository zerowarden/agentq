---
name: git-change-inspection
description: Use for Git status, diff, history, patch review, and high-signal patch-quality audit. MUST use bounded agentq git-* and audit commands instead of raw Git inspection whenever an equivalent wrapper exists. Read-only: never commit, reset, checkout, stash, clean, rebase, push, or rewrite history.
license: MIT
compatibility: Requires the bundled agent-toolkit and Git. Difftastic is optional for one-file syntax-aware diffs.
metadata:
  version: "1.8.0"
  mutation: "none"
---

# Git Change Inspection

## Mandatory tool policy

Use `agentq git-*` for Git inspection covered by this skill. Do not issue raw unbounded `git diff`, `git show`, or `git log` merely because it is shorter to type.

## Evidence ladder

1. Establish state:
   ```bash
   agentq git-status --limit 80
   ```
2. Read summaries before patch bodies:
   ```bash
   agentq git-diff
   agentq git-diff --staged
   agentq git-diff --base origin/main
   ```
3. Request patch text only for relevant scope:
   ```bash
   agentq git-diff --patch --path apps/api/src/routes --max-lines 500
   ```
4. Use structural diff only for a dense single file:
   ```bash
   agentq git-structural apps/api/src/routes/jobs.ts --max-lines 400
   ```
5. Inspect bounded history only when intent/regression origin matters:
   ```bash
   agentq git-history --path packages/contexts/dispatch --limit 15
   ```
6. Before handoff, run the integrated mechanical audit:
   ```bash
   agentq audit
   ```

## Guardrails

- Untracked files appear in status but not normal diff output; read only relevant files with repo-exploration.
- Sensitive-file diff bodies are omitted.
- A truncated patch is a signal to narrow scope, not raise every cap.
- An identical task/thread-local diff may be summarized as unchanged instead of re-emitted. Use `agentq git-diff --repeat` only when the patch body must be shown again.
- `git diff --check` failures are high priority.
- This provides evidence, not semantic correctness or security approval.
