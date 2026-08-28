---
name: repository-implementation-planner
description: Use when asked to plan a concrete coding implementation from business or technical requirements for an existing repository. MUST ground the plan in inspected repository evidence using agentq, resolve one implementation approach, order work by dependency, and output a concise Markdown handoff another coding agent can execute. Planning is read-only; do not implement the change.
license: MIT
compatibility: Requires the bundled agent-toolkit plus Git, ripgrep, and Python 3.10+. TypeScript semantic inspection additionally requires Node.js and project TypeScript.
metadata:
  version: "1.8.0"
  mutation: "none"
---

# Repository Implementation Planner

## Objective

Produce the smallest **decision-complete, repository-grounded** Markdown plan that another coding agent can execute without rediscovering ownership, choosing among architectural alternatives, or guessing how to verify the result.

Planning is read-only. Do not modify source, configuration, dependencies, schemas, generated files, or Git state. Write a plan file only when the user explicitly requests one; otherwise return the plan in the response.

## Core invariant

Every planned change must satisfy:

`requirement -> verified anchor -> current behavior -> intended delta -> affected dependants -> verification`

If a material link cannot be established from repository evidence, search further. If it still cannot be established, mark the plan `BLOCKED`; never invent a path, symbol, contract, command, or existing behavior.

## Tool routing

Use the PATH-installed `agentq`. Follow the narrowest operation that matches what is already known; do not run discovery commands as a ladder.

| Evidence already known | Preferred operation |
|---|---|
| Unknown repository layout | `agentq repo-map` |
| Unknown/partial identifier, route, string, config key, SQL fragment | `agentq search TERM --path SCOPE --format compact-json` |
| Filename/path fragment | `agentq files FRAGMENT --path SCOPE` |
| Exact TypeScript/JavaScript or Python symbol | `agentq inspect SYMBOL --path OWNER` |
| Known file but unknown structure | `agentq outline FILE --match PATTERN` |
| Known source range | `agentq read FILE:START-END` |
| Shared symbol/file/API/contract/schema/config blast radius | `agentq impact TARGET --path SCOPE` |
| Monorepo package dependants | `agentq dependencies --target PACKAGE --depth 2` |
| Existing Git state/diff relevant to the request | `agentq git-status`, then bounded `agentq git-diff` |

Respect the stricter policies of the installed `repo-exploration`, `semantic-code-navigation`, `change-impact-analysis`, `workspace-dependency-inspection`, `git-change-inspection`, and `targeted-verification` skills when they apply.

Do not use raw broad `tree`, `find`, `rg`, `grep`, `cat`, or unbounded Git output when `agentq` covers the operation. Use a bounded fallback only when `agentq` cannot express the operation or actually fails.

## Workflow

### 1. Normalize the request

Convert the request into compact observable requirements `R1`, `R2`, ... before selecting implementation details.

- Preserve explicit business rules and acceptance criteria.
- Separate requirements from constraints, non-goals, and assumptions.
- Resolve user terminology to repository terminology.
- Infer only minor reversible details from established local patterns.
- Do not silently decide material questions about public APIs, persistence, authorization, tenancy, security, compatibility, data loss, or irreversible migrations.
- Do not ask for clarification until repository evidence has been exhausted. When a material choice remains, recommend one default and mark the plan `BLOCKED`.

### 2. Establish repository state and ownership

Read only enough global context to route the investigation:

1. Applicable repository/subtree instructions such as `AGENTS.md`, `CLAUDE.md`, contribution guidance, or local architecture notes.
2. `agentq git-status` when existing worktree changes could affect interpretation or the requested task.
3. Workspace/package boundaries and relevant manifests.
4. The smallest owning path localized from requirement terms and repository terminology.

Do not emit a repository tour. Do not run `repo-map` when the owning package/path is already known.

### 3. Trace the current implementation path

Trace the relevant behavior end-to-end as applicable:

`entry boundary -> orchestration -> domain logic -> persistence/integration -> returned API/UI behavior`

Inspect complete owning functions or bounded source ranges, not search snippets alone. Establish:

- the owning symbol and its direct callers/callees;
- interfaces, types, schemas, registrations, factories, and dependency wiring;
- the closest analogous implementation already in the repository;
- tests defining current behavior and the nearest test convention;
- generated artifacts and their actual source of truth;
- consumers of any public contract that may change.

For exact TS/JS or Python symbols, prefer one `agentq inspect` overview over repeated lexical searches.

### 4. Expand only where risk requires it

Use impact/dependency analysis when the task changes a shared or externally visible surface.

- **Bug:** establish failing/current behavior, fault-owning symbol, and regression-test location.
- **Feature:** establish the closest analogue, integration points, and complete data/control path.
- **Refactor:** establish the public surface, references and implementations, and behavior-preserving tests.
- **API/schema/migration:** establish source of truth, consumers, compatibility/rollout, generated outputs, and backfill requirements.
- **Cross-package change:** inspect owning package plus direct local dependants; widen only when evidence requires it.

Treat lexical/import and package-graph evidence as lower bounds, not semantic proof.

### 5. Apply the evidence gate

Before placing a change in the plan, verify all applicable conditions:

- existing target path and owning symbol were inspected;
- a new file has a verified parent location and analogous file/convention;
- current behavior and requested delta are distinguishable;
- direct dependants and externally visible side effects are known;
- the local pattern or invariant to preserve is identified;
- the relevant test layer and exact runnable command are known;
- generated files are changed through their source of truth;
- migration, compatibility, authorization, tenancy, concurrency, idempotency, and observability concerns were checked when implicated.

Do not add generic cross-cutting machinery because it is normally desirable. Include it only when the requirement or inspected path makes it necessary.

### 6. Select one implementation

Resolve a single coherent implementation before handoff:

1. Reuse an established repository pattern.
2. Prefer the smallest complete change.
3. Preserve existing public behavior unless change is required.
4. Keep validation close to the behavior it protects.
5. Avoid speculative refactors and future-facing extensibility unrelated to the request.

Do not give the executor a menu of equivalent approaches. State a non-obvious rationale only where it constrains implementation.

### 7. Build the dependency-ordered work sequence

Flatten the implementation dependency graph into executable order. A step is one coherent dependency boundary, not one file and not a vague phase.

Typical ordering when applicable:

`source of truth -> shared contract/type -> domain behavior -> adapter/persistence -> API/UI wiring -> end-to-end verification`

Each step must contain:

- **Targets:** exact repository-relative paths and owning symbols;
- **Change:** concrete current-to-target behavior/data/control-flow delta;
- **Preserve:** only relevant invariants, compatibility rules, and boundaries;
- **Validate:** specific test cases and verified commands, including working directory/package selector when needed.

Use `path/to/file.ts::symbolName` for existing anchors. For new files use `NEW path/to/file.ts`, followed by the existing analogue to follow. Avoid line numbers when a stable symbol exists.

Include signatures, field lists, state transitions, or pseudocode only when they remove material ambiguity. Do not provide full implementation code.

### 8. Stop exploration

Stop when all are true:

- every `R#` maps to a verified anchor or justified new-file location;
- direct callers/callees/consumers and relevant tests were inspected;
- one repository-consistent solution is selected;
- every step has a valid place in dependency order and a verification method;
- no conflicting local pattern or material unanswered decision remains.

Further exploration after this point is context waste unless new evidence contradicts the plan.

### 9. Final audit

Before output:

- every `R#` appears in at least one work step and final acceptance item;
- every named existing path, symbol, and command was verified;
- no step leaves an architectural choice to the executor;
- no requirement is merely repeated without implementation information;
- no unrelated cleanup, generic best practice, or speculative future work is included;
- `READY` is used only when implementation can begin without a material decision.

## Output contract

Return only the Markdown plan unless the user separately asks for explanation. Do not include search transcripts, command logs, repository tours, or a fenced wrapper around the whole plan.

```markdown
# <Task name> - Implementation Plan

**Readiness:** READY | BLOCKED
**Outcome:** <One sentence describing the externally observable result.>
**Execution rule:** Implement in order. If a named anchor, contract, or invariant materially differs from the repository, stop and re-localize rather than inventing a parallel design. If `BLOCKED`, resolve the listed decisions first.

## Requirements

- **R1:** <Observable behavior or constraint.>
- **R2:** <Observable behavior or constraint.>

## Non-goals

- <Only plausible adjacent work deliberately excluded. Omit when unnecessary.>

## Assumptions

- <Only low-risk assumptions that could not be verified. Omit when empty.>

## Repository Grounding

- **Current flow:** `path::symbol` -> `path::symbol` -> `path::symbol`
- `path::symbol` - <Current responsibility and implication for the change.>
- `path::symbol` - <Existing analogue, invariant, or test convention to reuse.>

## Work Sequence

### 1. <Outcome-oriented step> - R1, R2

- **Targets:** `path::symbol`, `NEW path` modelled on `path::analogue`
- **Change:** <Exact current-to-target delta and interfaces/data flow.>
- **Preserve:** <Only relevant invariants/compatibility. Omit when none.>
- **Validate:** <Cases to add/update>; run `<verified command>` from `<working directory>`; expect <observable result>.

### 2. <Next dependency-ordered step> - R2

- **Targets:** ...
- **Change:** ...
- **Preserve:** ...
- **Validate:** ...

## Final Acceptance

- **R1:** <End-to-end observable condition.>
- **R2:** <End-to-end observable condition.>
- `<verified command>` - <Expected successful result.>

## Blocking Decisions

- **Decision:** <Only unresolved material choice. Omit this section when READY.>
  - **Evidence:** <What repository inspection established.>
  - **Recommended default:** <One choice and concise reason.>
  - **Resolve:** <Exact information/decision needed.>
```

## Compression rules

Prefer information that prevents executor rediscovery or wrong-path implementation.

Keep:

- exact anchors and ownership;
- interfaces/data flow/state transitions;
- local analogue or convention;
- invariants and compatibility constraints;
- test cases and exact commands;
- only non-obvious rationale.

Omit:

- search/tool transcripts;
- full repository trees;
- prose summaries of files already identified by anchor;
- generic best practices;
- repeated requirements;
- alternatives already rejected;
- obvious mechanics a coding agent can infer safely from the named local pattern.

A shorter plan is not better if it transfers unresolved decisions to the executor.

