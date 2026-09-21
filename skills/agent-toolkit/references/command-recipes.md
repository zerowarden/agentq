# Compact Command Recipes

Use these only after loading the workflow-specific skill. All paths are repository-relative unless `--allow-outside` is explicit.

```bash
AQ=agentq
```

## Discovery

```bash
"$AQ" repo-map
"$AQ" files offer --path packages --limit 40

# Known TypeScript/JavaScript or Python identifier: use one overview first.
"$AQ" inspect AssignmentOffer --path packages/contexts/dispatch
"$AQ" inspect calculate_total --path packages/services

# Unknown symbol, string, configuration key, or semantic fallback.
"$AQ" search 'assignment offer' --path packages/contexts/dispatch --limit 60 --format compact-json
"$AQ" search 'export\s+(type|interface)\s+Assignment' --regex --type ts --limit 30 --format compact-json
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

A plan materializes exact byte edits, preimage/postimage hashes, and engine
provenance at planning time. Applying a saved plan never rescans the worktree
or re-runs ast-grep; both fresh and loaded routes use the same validation,
policy, lock, journal, and commit path. Legacy plan schemas are refused with a
regeneration message.

```bash
"$AQ" codemod-scan 'OldName' --path packages
"$AQ" codemod-apply 'OldName' 'NewName' --path packages --expect-count 37
"$AQ" codemod-apply 'OldName' 'NewName' --path packages --expect-count 37 --apply
"$AQ" codemod-scan 'OldName' --path packages --plan-out /tmp/rename.json
"$AQ" codemod-apply --plan /tmp/rename.json --apply
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

`agentq run` exits with the wrapped command's shell outcome: the child exit
code, `124` on deadline, `126`/`127` on spawn failure, `130`/`143` on
cancellation, and `70` on a wrapper failure. Shell chains such as
`agentq run -- failing-command && next-command` therefore stop on failure.
JSON output keeps wrapper and child facts separately under `execution`.
Incomplete output capture is a wrapper failure (`70`): a command whose
descendant outlives the leader and retains the pipes is terminated and reported
as `FAIL`, never as an implicit success.

```bash
"$AQ" run -- pnpm --filter @app/dispatch test
"$AQ" run --cwd crates/engine -- cargo check
"$AQ" audit --base origin/main
"$AQ" git-diff --task --hunks
"$AQ" verify
"$AQ" benchmark --warmup 3 --runs 15 --command 'command-a' --command 'command-b'
```

## Statistics

```bash
"$AQ" stats
"$AQ" stats --detailed
"$AQ" stats --watch 2
```
