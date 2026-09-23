# Compact Command Recipes

Use these only after loading the workflow-specific skill. All paths are repository-relative.

```bash
AQ=agentq
```

## Known symbol

```bash
"$AQ" inspect AssignmentOffer --path packages/contexts/dispatch
"$AQ" inspect calculate_total --path packages/services --intent understand
"$AQ" inspect listOrders --path src --intent edit
```

## Unknown symbol, string, or configuration key

```bash
"$AQ" search 'assignment offer' --path packages/contexts/dispatch --format compact-json
"$AQ" search 'export\s+(type|interface)\s+Assignment' --regex --path packages --format compact-json
"$AQ" search 'AGENTQ_TELEMETRY' --path src --format compact-json
```

## Known file or source range

```bash
"$AQ" inspect packages/contexts/dispatch/src/offers.ts --lines 50:180
"$AQ" inspect src/service.ts --line 57 --column 12
"$AQ" inspect packages/contexts/dispatch/src --intent understand
```

## Ambiguity and candidates

```bash
# Several declarations share the name: the bundle lists candidate ids.
"$AQ" inspect duplicate --path packages

# Re-select one candidate against the current repository state.
"$AQ" inspect duplicate --path packages --candidate cand-7f3a9c2d4e5b6a708192a3b4
```

## Intent-driven inspection

```bash
"$AQ" inspect listOrders --path src --intent rename     # references and mentions
"$AQ" inspect listOrders --path src --intent refactor   # implementations and source
"$AQ" inspect listOrders --path src --intent impact     # dependents and ownership
```

## Continuations

```bash
# A truncated search prints: continue: agentq continue q7H2a
"$AQ" continue q7H2a
```

## Debugging a bundle

```bash
"$AQ" inspect listOrders --path src --debug --format json
```
