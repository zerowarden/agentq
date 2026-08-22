# Compact Command Recipes

Use these only after loading the workflow-specific skill. All paths are repository-relative unless `--allow-outside` is explicit.

```bash
AQ=~/.agents/skills/agent-toolkit/scripts/agentq
```

## Discovery

```bash
"$AQ" repo-map
"$AQ" files offer --path packages --limit 40

# Known TypeScript/JavaScript identifier: navigate semantically first.
"$AQ" ts-nav locate AssignmentOffer --path packages/contexts/dispatch
"$AQ" ts-nav references AssignmentOffer --path packages/contexts/dispatch --limit 60

# Unknown symbol, string, configuration key, or semantic fallback.
"$AQ" search 'assignment offer' --path packages/contexts/dispatch --limit 60
"$AQ" search 'export\s+(type|interface)\s+Assignment' --regex --type ts --limit 30
"$AQ" outline packages/contexts/dispatch/src --public --limit 120
"$AQ" read packages/contexts/dispatch/src/offers.ts:50-180
```

## Git

```bash
"$AQ" git-status
"$AQ" git-diff
"$AQ" git-diff --staged --patch --path apps/api/src --max-lines 500
"$AQ" git-diff --base origin/main --path packages/contexts/dispatch
"$AQ" git-history --path packages/contexts/dispatch --limit 15
```

## Dependencies and impact

```bash
"$AQ" dependencies --target '@app/dispatch' --depth 2
"$AQ" impact AssignmentOffer --path packages --limit 120
```

## Refactoring

```bash
"$AQ" codemod-scan 'OldName' --path packages
"$AQ" codemod-apply 'OldName' 'NewName' --path packages --expect-count 37
"$AQ" codemod-apply 'OldName' 'NewName' --path packages --expect-count 37 --apply
```

## Task boundaries

```bash
# Status is the default action.
"$AQ" task

# One independently acceptable outcome. A thread may contain several.
"$AQ" task begin
# ...investigate, edit, debug, and verify the same outcome...
"$AQ" task next       # accept current outcome and begin the next
"$AQ" task accept     # finish without starting another
"$AQ" task abandon    # only when intentionally discarded
```

## Verification and review

```bash
"$AQ" run -- pnpm --filter @app/dispatch test
"$AQ" run --cwd crates/engine -- cargo check
"$AQ" audit --base origin/main
"$AQ" benchmark --warmup 3 --runs 15 --command 'command-a' --command 'command-b'
```

## Statistics

```bash
"$AQ" stats
"$AQ" stats --detailed
"$AQ" stats --watch 2
```
