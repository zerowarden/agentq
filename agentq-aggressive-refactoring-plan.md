# `agentq` Aggressive Refactoring Plan

**Repository:** `zerowarden/agentq`  
**Refactoring target:** current `main` implementation reviewed on 2026-09-21  
**Primary objective:** aggressively restructure and simplify `agentq` without introducing new product features  
**Backwards compatibility:** explicitly **not** a requirement  
**Execution model:** implement the phases in order; each phase must leave the repository passing its relevant verification gates before the next phase begins  
**Project root:** `agentq/` (the standalone Python project inside this repository; the repository-level `skills/` tree is outside the project)

---

## 0. Purpose of This Document

This document is an implementation handoff for a coding agent.

The task is **not** to extend `agentq`'s feature set. It is to refactor the existing implementation so that:

1. `agentq` becomes a real installable Python package instead of an application embedded inside `skills/`.
2. Types and semantic contracts are clear at module boundaries.
3. Responsibilities are explicit and cohesive.
4. The architecture is easy for future coding agents to discover and reason about.
5. The CLI becomes a thin adapter rather than the structural center of the application.
6. Existing large "utility" or `*ops` modules are decomposed around actual capabilities and reasons for change.
7. Redundant shims and aliases are removed only when their redundancy is sufficiently certain.
8. The refactor prepares `agentq` for a later redesign of the model-facing interface without implementing that redesign yet.

This document intentionally permits substantial structural changes and renames.

Do **not** preserve old internal paths, package names, import locations, or launcher structures merely for backwards compatibility.

The repository should, however, preserve the existing behavior and safety invariants unless this document explicitly identifies something as safe to remove.

---

# 1. Refactoring Principles

The refactor should follow established refactoring principles rather than merely splitting large files.

Relevant families of refactorings include:

- **Extract Method / Extract Class** when a module contains multiple independent responsibilities.
- **Move Method / Move Field** when behavior is located away from the state or domain concept that owns it.
- **Introduce Parameter Object** when long argument lists represent a coherent semantic concept.
- **Separate Query from Modifier** when reading and state mutation are mixed.
- **Remove Middle Man** when wrappers have no independent behavior.
- **Replace Primitive Obsession / Data Clumps with explicit domain types** where stable semantic structures repeatedly travel together.
- **Replace Conditional Dispatcher with explicit polymorphic/provider boundaries** where providers already represent distinct implementations.
- **Inline unnecessary abstractions** where an abstraction has no independent semantic value.

These techniques should be applied based on *reasons for change*, not on arbitrary file-size rules.

A 500–700 line module may remain acceptable when it represents one cohesive state machine or subsystem. A smaller module should still be split if it contains unrelated responsibilities.

Avoid over-engineering. In particular, do not introduce a generic Clean Architecture/Controller/Service/Repository hierarchy simply for aesthetic consistency.

---

# 2. Current Architectural Diagnosis

The current implementation contains several good foundations:

- `Coverage`
- `ProviderResult`
- `ExecutionSpec`
- `ExecutionOutcome`
- `MutationPlan`
- typed mutation contracts
- navigation-provider abstractions
- verification-provider concepts
- explicit delivery receipts
- process-supervision contracts
- invariant-focused tests

These should be preserved where appropriate.

The architectural problem is that these strong concepts coexist with an older CLI-centric and dictionary-heavy structure.

## 2.1 Product ownership is inverted

The implementation originally lived under:

```text
skills/agent-toolkit/scripts/agentq_lib/
```

This made the `skills` tree appear to own the application.

Multiple skills also contain forwarding launchers that route back to the toolkit implementation.

The correct ownership is the standalone Python project root:

```text
agentq/
```

with the implementation at:

```text
agentq/src/agentq/
```

and the repository-level:

```text
skills/
```

containing only agent-facing instructions and related static skill assets.

Skills should depend on the installed `agentq` package; `agentq` must not live inside a skill.

---

## 2.2 Large modules represent entire subsystems

Current examples observed during review include approximately:

```text
telemetry.py       ~4,063 lines
search.py          ~2,238 lines
verification.py    ~1,448 lines
inspectops.py      ~1,347 lines
gitops.py          ~1,145 lines
codemod.py           ~826 lines
continuations.py     ~845 lines
process.py           ~610 lines
state.py             ~588 lines
common.py            ~553 lines
```

File size alone is not the problem.

The issue is that several of these files have multiple independent reasons to change.

For example, `search.py` owns:

- repository file lookup
- lexical search
- bounded source reading
- repository maps
- outlines
- continuation planning
- output rendering

Those are separate capabilities.

By contrast, much of `process.py` represents a process-supervision state machine and may remain comparatively large if it remains cohesive after re-homing.

---

## 2.3 Typed contracts exist, but dictionaries still dominate internal boundaries

The repository has typed models, yet many operations still follow a pattern such as:

```text
arguments
    ↓
*_data(...)
    ↓
dict[str, Any]
    ↓
renderer
```

Stable semantic structures should instead follow:

```text
TypedRequest
    ↓
operation(...)
    ↓
TypedResult
```

Dictionaries should primarily remain at:

- JSON/wire boundaries
- external process parsing boundaries
- SQLite encoding/decoding boundaries
- renderer serialization boundaries
- genuinely local temporary calculations

A dictionary that crosses several modules and is interpreted by multiple components is a domain model in disguise and should generally become typed.

---

## 2.4 `common.py` is a responsibility sink

`common.py` currently includes concerns such as:

- errors
- ANSI/text utilities
- path classification
- sensitive-path handling
- tool discovery
- tool versions
- subprocess invocation
- repository-root discovery
- path confinement
- repository file listing
- cache directories
- private logs
- output bounding
- language detection
- JSON-lines parsing
- formatting helpers

This module should eventually cease to exist.

Do **not** rename it to `utils.py`.

Move each responsibility to its actual owner.

---

## 2.5 CLI command metadata has multiple sources of truth

Current command knowledge is spread across areas such as:

- parser command declarations
- `_HANDLERS`
- `KNOWN_OPERATIONS`
- telemetry-specific command sets
- request codecs
- continuation codecs

This produces shotgun surgery.

The CLI itself should have one source of truth for parser/dispatch metadata.

Application capabilities should not depend on that CLI registry.

---

## 2.6 `OperationRequest` is more generic in theory than in implementation

`contracts/request.py` enumerates many operations.

However, current request codecs are implemented only for a narrow subset, notably:

```text
search
git-diff
```

Likewise, argv reconstruction for typed continuations supports only those operations.

Do not "complete" the abstraction merely for conceptual symmetry.

Instead, distinguish:

```text
Capability Request
Invocation Context
Continuation/Wire Envelope
```

If `OperationRequest` remains, narrow it to the purpose it genuinely serves.

Do not fossilize the current CLI command vocabulary into the future model-facing API.

---

## 2.7 Persistence has no explicit domain ownership

`state.py` currently contains:

- DB connection handling
- migrations
- legacy imports
- receipt persistence
- fragment persistence
- task state
- continuations
- continuation artifacts

These should share database infrastructure but not a single generic state module.

---

## 2.8 Emission has too many responsibilities

`cli/emit.py` currently participates in:

- rendering
- budget projection
- stdout encoding/writing
- continuation cursor attachment
- repeat suppression
- delivery-manifest extraction
- delivery receipt construction
- receipt persistence
- output attribution
- telemetry-facing result construction

This should be decomposed so the CLI transport does not own delivery policy.

---

## 2.9 Telemetry is effectively a separate application

`telemetry.py` currently owns multiple independent concerns:

- event construction
- fingerprints
- hot storage
- archive storage
- systemd persistence
- event normalization
- analytics
- cohort comparisons
- retry behavior
- read-efficiency statistics
- task-efficiency statistics
- verification statistics
- terminal report rendering
- watch mode

It should be refactored only after the rest of the application boundaries stabilize.

---

# 3. Architectural Objective

The target architecture should be **capability-first with thin adapters**.

Conceptually:

```text
Skills ───────────────┐
CLI ──────────────────┼──> agentq capability API
future model API ─────┘           │
                                  ├── discovery
                                  ├── navigation
                                  ├── git
                                  ├── workspace
                                  ├── execution
                                  ├── verification
                                  └── mutation
                                       │
                         ┌─────────────┼─────────────┐
                         │             │             │
                    persistence     delivery      telemetry
```

The most important dependency rule is:

> **The CLI is a leaf adapter. It must not be the application spine.**

A search operation should conceptually become:

```text
SearchRequest
    ↓
search(...)
    ↓
SearchResult
```

Then different adapters can consume the same capability:

```text
CLI adapter
    SearchRequest → SearchResult → text/json renderer

future model adapter
    SearchRequest → SearchResult → evidence selection/bundle

continuation service
    SearchRequest → persisted continuation state
```

This is the architectural preparation needed for the later model-facing redesign.

Do **not** implement that new model-facing API in this refactor.

---

# 4. Recommended Target Package Structure

Use the following as the target ownership model.

Exact filenames may change when implementation evidence justifies it, but the responsibility boundaries should remain recognizable.

```text
agentq/
├── pyproject.toml
├── README.md
├── ARCHITECTURE.md
│
├── src/
│   └── agentq/
│       ├── __init__.py
│       ├── __main__.py
│       │
│       ├── cli/
│       │   ├── main.py
│       │   ├── registry.py
│       │   ├── arguments.py
│       │   ├── errors.py
│       │   ├── rendering.py
│       │   └── transport.py
│       │
│       ├── core/
│       │   ├── errors.py
│       │   ├── paths.py
│       │   ├── evidence.py
│       │   ├── budget.py
│       │   └── runtime.py
│       │
│       ├── discovery/
│       │   ├── models.py
│       │   ├── files.py
│       │   ├── search.py
│       │   ├── read.py
│       │   ├── repo_map.py
│       │   └── outline.py
│       │
│       ├── navigation/
│       │   ├── models.py
│       │   ├── resolution.py
│       │   ├── inspect.py
│       │   └── providers/
│       │       ├── python.py
│       │       ├── typescript.py
│       │       └── lexical.py
│       │
│       ├── git/
│       │   ├── models.py
│       │   ├── status.py
│       │   ├── diff.py
│       │   ├── history.py
│       │   └── structural.py
│       │
│       ├── workspace/
│       │   ├── models.py
│       │   ├── discovery.py
│       │   └── graph.py
│       │
│       ├── execution/
│       │   ├── models.py
│       │   ├── supervisor.py
│       │   └── run.py
│       │
│       ├── verification/
│       │   ├── models.py
│       │   ├── planner.py
│       │   ├── runner.py
│       │   └── providers/
│       │       ├── node.py
│       │       ├── python.py
│       │       ├── cargo.py
│       │       └── go.py
│       │
│       ├── mutation/
│       │   ├── models.py
│       │   ├── scan.py
│       │   ├── plan.py
│       │   ├── journal.py
│       │   └── apply.py
│       │
│       ├── continuations/
│       │   ├── models.py
│       │   └── service.py
│       │
│       ├── delivery/
│       │   ├── models.py
│       │   ├── rendering.py
│       │   ├── receipts.py
│       │   └── suppression.py
│       │
│       ├── persistence/
│       │   ├── database.py
│       │   ├── migrations.py
│       │   ├── receipts.py
│       │   ├── tasks.py
│       │   └── continuations.py
│       │
│       └── telemetry/
│           ├── models.py
│           ├── recorder.py
│           ├── storage.py
│           ├── archive.py
│           ├── report.py
│           └── analytics/
│               ├── efficiency.py
│               ├── chains.py
│               ├── failures.py
│               ├── tasks.py
│               └── verification.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contracts/
│   └── invariants/
│
└── integrations/
    └── opencode/
```

`agentq/` is the project root. In this workspace it lives inside the repository
as `.agents/agentq/`, while `skills/` remains at the repository root outside
the Python project and consumes the installed `agentq` package.

---

# 5. Naming and Discoverability Rules

The refactored codebase should follow these rules.

## 5.1 No generic dumping-ground modules

Do not introduce or preserve generic modules such as:

```text
common.py
utils.py
helpers.py
misc.py
```

unless the module represents a genuinely cohesive semantic abstraction.

## 5.2 Avoid historical `*ops.py` names

Names such as:

```text
gitops.py
inspectops.py
runops.py
verifychanged.py
```

reflect implementation history rather than responsibility.

Replace them with capability-oriented names.

## 5.3 Entry surfaces should be obvious

A future coding agent looking for search should be able to discover an API approximately like:

```python
from agentq.discovery.search import SearchRequest, SearchResult, search
```

rather than having to inspect:

```text
common
contracts
requests
search
cli.commands.discovery
cli.emit
```

to understand one operation.

## 5.4 Module names should identify either:

1. a capability, or
2. a concrete infrastructure responsibility.

---

# 6. Type Strategy

Use the rule:

> **Every cross-module semantic boundary gets a type. Local intermediate structures may remain simple Python data structures where they are clearer.**

Good candidates include:

```text
SearchRequest
SearchResult

ReadRequest
ReadResult
SourceWindow

DiffRequest
DiffSelection
DiffResult
DiffFile
DiffHunk

TaskState
TaskChanges

InspectRequest
InspectResult
CandidateRef
ReferenceEvidence
EditBundle

VerificationPlan
CheckSpec
CheckResult

MutationPlan
MutationOutcome
```

Do not introduce speculative object hierarchies such as:

```text
SearchManager
SearchCoordinator
SearchServiceFactory
SearchRepositoryFacade
```

unless runtime state, interchangeable implementations, or dependency injection genuinely require them.

Prefer typed dataclasses, enums, protocols, and plain functions.

---

# 7. Explicit Non-Goals

The coding agent must **not** do the following during this refactor unless required to preserve behavior:

- Add new model-facing commands.
- Implement the future single evidence-bundle API.
- Add new search heuristics.
- Redesign evidence relevance scoring.
- Change the intended semantics of `inspect --intent`.
- Add new codemod capabilities.
- Add new language providers.
- Change telemetry metrics merely because the implementation is moving.
- Replace `argparse` with Typer, Click, or another CLI framework.
- Introduce a dependency-injection framework.
- Introduce a generic service/repository architecture.
- Preserve old internal imports for compatibility.
- Add backwards-compatibility re-export packages merely to keep `agentq_lib` imports working.
- Create abstractions solely to reduce line count.
- Remove a CLI command merely because it looks awkward unless its redundancy is proven.

---

# 8. Removal Policy

Backwards compatibility is not required, but removals still need evidence.

Use three categories.

## Category A — Remove immediately when its replacement exists

Examples currently considered high-confidence:

- skill-local `scripts/agentq` forwarding wrappers
- toolkit `scripts/agentq` shell launcher once `[project.scripts]` exists
- toolkit `scripts/agentq.py` bootstrap once the package entry point exists
- `agentq_lib` package name
- dynamic `contracts.__getattr__` export mechanism once the contract cycle is removed
- `cli/types.py` once its types have real owners
- `verified-changed` alias, assuming repository-wide search confirms no distinct behavior or documentation contract

## Category B — Remove only after repository-wide reference audit

Examples:

- task action aliases
- `--offline` alias
- `--stat`
- legacy continuation argv support
- old telemetry schema/import paths
- OpenCode adapter compatibility logic
- historical migration paths

## Category C — Preserve during this refactor

Unless implementation evidence proves exact redundancy, retain:

- core discovery operations
- `verify`
- `verify-changed`
- task-scoped verification semantics
- delivery receipts
- repeat suppression
- continuation behavior
- process cleanup/supervision
- mutation review/apply safety
- path confinement
- redaction
- coverage semantics

---

# 9. Sequence of Work

This section is the authoritative implementation order.

**The coding agent should implement one phase at a time.**

Do not start a later phase while an earlier phase has unresolved test failures, architectural regressions, or incomplete acceptance criteria.

A useful invocation style is:

```text
Implement Phase 1 only from agentq-aggressive-refactoring-plan.md.
Do not begin Phase 2.
Run the required verification and report any deviations.
```

Then repeat for Phase 2, Phase 3, and so on.

---

# Phase 0 — Establish the Refactoring Baseline

## Objective

Freeze observable behavior and identify the safety/contract tests that must survive the structural refactor.

## Work

1. Inventory existing CLI commands and aliases.
2. Inventory current package imports.
3. Inventory test files and identify:
   - P0 safety invariants
   - mutation safety
   - process supervisor behavior
   - exit-code behavior
   - delivery/receipt behavior
   - continuation behavior
   - edit-bundle behavior
   - coverage behavior
4. Record current test invocation(s); the project root is `agentq/`, so the
   equivalent commands are `uv run pytest`, `uv run ruff check .`, and
   `uv run pyright` executed from `agentq/`.
5. Run the full existing test suite if the environment permits.
6. Run Ruff and Pyright with the current configuration.
7. Record any pre-existing failures before editing code.

Important existing tests include:

```text
test_p0_invariants.py
test_delivery_receipts.py
test_mutation_plans.py
test_mutation_safety.py
test_process_supervisor.py
test_exit_contract.py
test_edit_bundle.py
test_diff_followups.py
test_coverage_contract.py
test_provider_coverage.py
test_scope_protocol.py
test_ts_scope_integration.py
```

## Do not

- restructure production code yet
- fix unrelated behavior
- redesign CLI syntax
- modify result schemas

## Completion Gate

Phase 0 is complete when:

- baseline test status is documented
- known pre-existing failures are separated from refactor-induced failures
- safety-critical tests are identified
- the coding agent can state how it will verify Phase 1

---

# Phase 1 — Convert `agentq` into a Real Installable Package

## Objective

Move the implementation out of `skills/` without intentionally changing internal architecture or runtime semantics.

## Required Target

Move the implementation, tests, and project metadata into the standalone project root:

```text
skills/agent-toolkit/scripts/agentq_lib/   →   agentq/src/agentq/
skills/agent-toolkit/tests/                →   agentq/tests/
pyproject.toml                             →   agentq/pyproject.toml
uv.lock                                    →   agentq/uv.lock
README.md                                  →   agentq/README.md
```

The mechanical test move happens in this phase so the project is
self-contained; test reorganization remains Phase 3.

Change imports from:

```python
agentq_lib.foo
```

to:

```python
agentq.foo
```

Update `agentq/pyproject.toml`.

The project should be named:

```toml
[project]
name = "agentq"
```

Add a proper console entry point:

```toml
[project.scripts]
agentq = "agentq.cli.main:main"
```

Add:

```text
agentq/src/agentq/__main__.py
```

so this also works:

```bash
python -m agentq
```

## Packaging Constraints

Do not add an `agentq_lib` compatibility package.

Do not leave the canonical implementation under `skills/`.

The project must be self-contained under `agentq/`; the repository-level
`skills/` tree is not part of the Python project.

Tests must import the installed/source package via `agentq`.

## Documentation Changes

Update `agentq/README.md` installation instructions so the repository no
longer instructs users to symlink:

```text
skills/agent-toolkit/scripts/agentq
```

Use a normal package installation workflow appropriate to the repository
(for example `uv sync` plus `uv run agentq`).

## Verification

Run from the project root `agentq/`:

```bash
uv sync
uv run agentq --version
uv run python -m agentq --version
uv run agentq doctor
uv run pytest
uv run ruff check .
uv run pyright
```

Adjust exact commands to the repository's chosen environment/tool runner.

## Completion Gate

Phase 1 is complete only when:

- `agentq/` is the standalone project root
- `agentq/src/agentq/` is the canonical implementation
- tests live under `agentq/tests/`
- no production imports use `agentq_lib`
- `agentq` console entry point works
- `python -m agentq` works
- existing tests pass to baseline
- no feature semantics were intentionally changed

---

# Phase 2 — Remove Obsolete Launchers and Skill-Owned Application Shims

## Objective

Remove packaging debris that is redundant after Phase 1.

## Work

Delete the toolkit launcher chain:

```text
skills/agent-toolkit/scripts/agentq
skills/agent-toolkit/scripts/agentq.py
```

Delete identical per-skill forwarding wrappers:

```text
skills/*/scripts/agentq
```

where repository-wide inspection confirms they contain only forwarding logic.

Update every `SKILL.md` or related documentation so skills invoke:

```bash
agentq ...
```

rather than a relative skill script.

Move:

```text
skills/agent-toolkit/assets/opencode-tools/
```

to a product-level integration location such as:

```text
agentq/integrations/opencode/
```

Update installation scripts accordingly.

If `install-tools.sh` is primarily an environment/bootstrap script rather than a skill implementation detail, move it to an appropriate top-level tooling location. Do not force this move if ownership remains genuinely skill-specific.

## Removal Audit

Before deletion, search the entire repository for references to:

```text
skills/agent-toolkit/scripts/agentq
scripts/agentq
agentq.py
agentq_lib
```

Resolve every reference.

## Completion Gate

- skills no longer own or wrap the application executable
- no documentation points to deleted launchers
- package entry point is the only canonical `agentq` executable
- tests pass

---

# Phase 3 — Reorganize Tests Before Deep Production Refactoring

## Objective

Make behavior discoverable before moving the implementation into many capability packages.

## Work

Phase 1 already moved the test suite mechanically from
`skills/agent-toolkit/tests/` into the project at `agentq/tests/`.

Organize it primarily by responsibility.

Suggested shape:

```text
tests/
├── unit/
├── integration/
│   └── cli/
│       ├── test_search.py
│       ├── test_read.py
│       ├── test_inspect.py
│       ├── test_git.py
│       ├── test_verify.py
│       └── test_mutation.py
├── contracts/
└── invariants/
```

The ~4,800-line `test_agentq.py` should be split.

Do not rewrite their assertions during the same change unless imports/fixtures require it.

Preserve test names where practical for history/searchability.

## Goal

A future agent asking:

> "Where is Git diff continuation behavior tested?"

should be able to locate the answer from the filesystem.

## Completion Gate

- no giant catch-all integration test file remains under `agentq/tests/`
- invariant tests remain clearly identifiable
- contract serialization tests are separate from CLI integration tests
- test behavior is unchanged
- full test suite passes

---

# Phase 4 — Re-home Core Types and Remove the Contract Import Cycle

## Objective

Give foundational types stable owners and eliminate lazy import machinery used to avoid cycles.

## Current Problem

The dependency shape includes relationships such as:

```text
evidence.py → contracts._base
contracts.result → evidence.py
```

`contracts/__init__.py` consequently uses dynamic exports via `__getattr__`.

## Work

Create the minimal core:

```text
core/errors.py
core/evidence.py
core/paths.py
core/budget.py
core/runtime.py
```

Move domain-specific models to their domains.

Examples:

```text
contracts/execution.py
    → execution/models.py

contracts/mutation.py
    → mutation/models.py

contracts/events.py
    → telemetry/models.py
```

Move request/result models to the capability that owns them where possible.

Do not retain `contracts/` merely as a compatibility barrel.

Delete the dynamic:

```python
_EXPORTS
__getattr__
__dir__
```

mechanism when no longer needed.

Remove the Pyright workaround that exists only because lazy `__all__` exports cannot be statically resolved.

## Design Rule

A type belongs in `core` only if multiple independent capabilities genuinely depend on it.

Do not turn `core` into the new `common.py`.

## Completion Gate

- no dynamic contract barrel remains
- no import cycle requires lazy import hacks
- execution/mutation/telemetry models live with their domains
- tests pass
- Pyright can resolve direct imports normally

---

# Phase 5 — Introduce Architectural Dependency Tests

## Objective

Prevent the old CLI-centric dependency shape from reappearing.

## Rules to Enforce

At minimum:

```text
agentq.cli may depend on capabilities.

Capabilities must not import agentq.cli.

Core must not import capability packages.

Capability functions must not accept argparse.Namespace.

Capability functions must not print to stdout/stderr.

Persistence may depend on core/domain models but must not depend on CLI.

Telemetry may observe capability outcomes but capability domains should not depend on telemetry presentation/reporting.
```

Implement these with lightweight static tests or AST/import inspection.

Do not adopt an external architecture framework unless clearly justified.

## Completion Gate

- dependency rules are executable tests
- violations fail CI/test execution
- current code passes the new rules

---

# Phase 6 — Unify CLI Registration

## Objective

Remove duplicate parser and handler registries.

## Current Problem

CLI metadata and execution dispatch are separate.

## Work

Create a CLI-local command specification, approximately:

```python
@dataclass(frozen=True)
class CommandSpec:
    name: str
    help: str
    configure: Callable[[ArgumentParser], None]
    execute: CommandHandler
```

The parser uses this registry.

The dispatcher uses the same registry.

Remove the standalone `_HANDLERS` mapping.

Do not expose `CommandSpec` to core/capability code.

Do not use CLI command registration as the application capability registry.

## Alias Handling

Represent CLI aliases explicitly and locally.

If `verified-changed` is confirmed to be a pure duplicate of `verify-changed`, remove it in this phase.

Do not remove semantically distinct commands.

## Completion Gate

- one CLI source of truth controls parser + handler mapping
- no duplicate handler table
- CLI behavior remains equivalent except explicitly removed exact aliases
- tests pass

---

# Phase 7 — Make `cli.main` Thin

## Objective

Make the CLI entry point an adapter rather than an orchestrator.

## Move Out

Move:

- process cancellation/signal coordination → execution infrastructure
- telemetry recording coordination → telemetry boundary/helper
- error serialization → `cli/errors.py`
- output transport → `cli/transport.py`

## Desired Shape

`main()` should become conceptually close to:

```text
parse
resolve repository
dispatch
render/transport
return exit code
```

No capability should depend on `main.py`.

## Do Not

- replace argparse
- redesign public CLI grammar
- introduce the future model API

## Completion Gate

- `main()` has only entry-point concerns
- signal/process lifecycle code has a concrete owner
- error rendering is separated
- tests pass

---

# Phase 8 — Dissolve `common.py`

## Objective

Remove the generic responsibility sink.

## Suggested Moves

```text
AgentQError
AgentQCancelled
    → core/errors.py

repo_root
ensure_within
relpath
    → core/paths.py

sensitive path classification
    → core/path_policy.py or security/path_policy.py

find_executable
tool_version
    → tooling.py

run_cmd
    → execution/

cache/runtime directories
    → core/runtime.py

bound_output
compact_line
    → delivery/rendering.py or CLI rendering where appropriate

repository file listing
    → discovery/

language detection
    → capability/tooling owner
```

Do not create `utils.py`.

Delete `common.py` after all imports are migrated.

## Completion Gate

- `common.py` no longer exists
- no replacement generic dumping-ground module exists
- all moved functions have semantically clear owners
- tests pass
- architecture tests pass

---

# Phase 9 — Split the Discovery Subsystem

## Objective

Decompose the current `search.py` by capability.

## Target

Split into approximately:

```text
discovery/files.py
discovery/search.py
discovery/read.py
discovery/repo_map.py
discovery/outline.py
```

## Introduce Typed Boundaries

Examples:

```text
FilesRequest / FilesResult
SearchRequest / SearchResult
ReadRequest / ReadResult / SourceWindow
RepoMapRequest / RepoMapResult
OutlineRequest / OutlineResult
```

Do not force a single universal request type across these operations.

## Rendering

Rendering should not live mixed inside the collection algorithm.

Use either:

```text
discovery/rendering.py
```

or CLI/delivery renderer implementations if they are purely presentation-level.

Prefer keeping semantic result construction independent from rendering.

## Continuations

Do not let discovery operations construct arbitrary CLI strings directly.

Use typed continuation descriptions or the continuation service.

## Completion Gate

- original monolithic `search.py` is gone
- each discovery capability has a clear public entry point
- CLI adapters invoke typed capability functions
- renderers do not control collection
- tests pass

---

# Phase 10 — Refactor Navigation and Inspection

## Objective

Preserve the good provider abstraction and make inspection a typed orchestration capability.

## Preserve

The existing concept:

```text
NavigationProvider
TypeScriptProvider
PythonProvider
LexicalFallbackProvider
```

is useful.

## Target

```text
navigation/providers/typescript.py
navigation/providers/python.py
navigation/providers/lexical.py
navigation/resolution.py
navigation/inspect.py
navigation/models.py
```

## Strengthen Existing Models

Preserve and refine concepts such as:

```text
CandidateRef
TargetIdentity
ReferenceEvidence
EditCoverage
EditBundle
```

Where stable semantic fields are still stored as nested dictionaries, replace them with explicit models.

Do not replace truly provider-specific opaque payloads with unnecessary abstraction if the orchestrator does not interpret them.

## Completion Gate

- `inspectops.py` is gone
- provider implementations are discoverable
- `inspect` orchestration consumes typed provider outcomes
- edit bundle construction is explicit
- ambiguity behavior remains tested
- tests pass

---

# Phase 11 — Split Git Capabilities

## Objective

Replace `gitops.py` with capability-owned modules.

## Target

```text
git/status.py
git/diff.py
git/history.py
git/structural.py
git/models.py
```

## Typed Models

Introduce or consolidate:

```text
DiffRequest
DiffSelection
DiffResult
DiffFile
DiffHunk
DiffGuard
StatusResult
HistoryResult
```

Do not split `git/diff.py` merely to achieve a low line count.

Diff selection, guard validation, hunk extraction, bounded patch generation, and follow-up metadata may remain together if cohesive.

## Completion Gate

- `gitops.py` is gone
- Git result semantics are typed
- continuation logic consumes typed diff selection/result data
- hunk follow-up tests remain passing
- tests pass

---

# Phase 12 — Normalize Workspace Representation

## Objective

Give package/workspace discovery and graph operations a reusable model.

## Target

```text
workspace/models.py
workspace/discovery.py
workspace/graph.py
```

## Move Concerns

Separate:

```text
package manager detection
workspace manifest parsing
workspace package discovery
dependency graph building
owner resolution
dependent traversal
change classification
package command construction
```

according to responsibility.

Avoid duplicating workspace interpretation in:

```text
dependencies
impact
verification
```

## Completion Gate

- workspace discovery produces typed models
- graph operations consume those models
- verification and dependency analysis share the same representation
- tests pass

---

# Phase 13 — Refactor Execution and Verification Together

## Objective

Turn verification into an explicit plan/execute pipeline.

## Execution

Re-home `process.py` to something like:

```text
execution/supervisor.py
```

Preserve and strengthen:

```text
CancellationToken
ExecutionSpec
ExecutionOutcome
```

Do not arbitrarily fragment a cohesive process-supervision state machine.

## Verification Providers

Move providers into:

```text
verification/providers/node.py
verification/providers/python.py
verification/providers/cargo.py
verification/providers/go.py
```

## Planning

Replace dictionary `_step(...)` structures with typed:

```text
CheckSpec
VerificationPlan
```

Create:

```text
verification/planner.py
```

which performs:

```text
changed files
    ↓
workspace/provider analysis
    ↓
VerificationPlan
```

## Execution

Create:

```text
verification/runner.py
```

which performs:

```text
VerificationPlan
    ↓
execution supervisor
    ↓
CheckResult[]
```

Absorb the historical responsibilities of:

```text
testplan.py
verifychanged.py
```

into planner/runner where appropriate.

Delete those historical modules when migration is complete.

## Completion Gate

- verification planning and execution are separate
- provider files are individually discoverable
- `VerificationPlan` is used end-to-end
- no parallel dictionary representation duplicates the typed plan
- exit behavior remains correct
- tests pass

---

# Phase 14 — Refactor Mutation Around Existing Typed Contracts

## Objective

Preserve existing mutation safety while eliminating dictionary/ownership duplication.

## Target

```text
mutation/models.py
mutation/scan.py
mutation/plan.py
mutation/journal.py
mutation/apply.py
```

Use existing concepts such as:

```text
MutationPlan
ApplyPolicy
ChangedFile
MutationOutcome
```

from plan generation through application.

Do not build a typed plan, flatten it to an ad-hoc dictionary, and reconstruct it later.

## Preserve

- stale-plan rejection
- repository binding
- path confinement
- symlink safety
- reviewed-plan behavior
- journaling/recovery
- rollback semantics
- post-check behavior

## Completion Gate

- `codemod.py` no longer contains scan, planning, persistence, and apply concerns together
- mutation contracts remain authoritative
- safety invariant tests pass
- no weakening of mutation safeguards

---

# Phase 15 — Split Persistence by Domain

## Objective

Replace generic `state.py` ownership with explicit storage responsibilities.

## Target

```text
persistence/database.py
persistence/migrations.py
persistence/receipts.py
persistence/tasks.py
persistence/continuations.py
```

A shared database object/module may own connection lifecycle and migrations.

Domain persistence modules should expose narrow operations.

Examples:

```python
tasks.load(...)
tasks.store(...)
tasks.delete(...)

receipts.record(...)
receipts.fragment_hits(...)

continuations.store(...)
continuations.load(...)
continuations.store_artifact(...)
```

Use typed persistence records where stable.

## Important

Do not add repository interfaces merely because the directory is called `persistence`.

Interfaces/protocols are justified only if multiple implementations or testing seams need them.

## Completion Gate

- `state.py` is gone
- database lifecycle has one owner
- receipts/tasks/continuations have separate persistence surfaces
- migrations remain deterministic
- legacy migration behavior remains unless explicitly proven obsolete
- tests pass

---

# Phase 16 — Refactor Continuations

## Objective

Separate continuation semantics from persistence and CLI display strings.

## Target

```text
continuations/models.py
continuations/service.py
```

Persistence belongs under:

```text
persistence/continuations.py
```

## Clarify Supported Operations

If typed continuation replay exists only for specific operations such as search and Git diff, represent that truth explicitly.

Do not define a universal continuation codec that is mostly unimplemented.

## Legacy Handling

Keep `LegacyArgv` support during this phase unless a repository-wide migration/reference audit proves it can be removed without losing persisted-state compatibility that is still intentionally supported.

## Completion Gate

- continuation models are explicit
- continuation service does not perform raw SQL
- capability code does not construct free-form shell commands
- continuation tests pass

---

# Phase 17 — Separate Rendering, Transport, Delivery, and Suppression

## Objective

Decompose `cli/emit.py` and related emission logic.

## Target Responsibilities

```text
delivery/rendering.py
    budget-aware rendering abstractions

delivery/receipts.py
    delivery receipt construction/domain logic

delivery/suppression.py
    repeat/exposure suppression semantics

cli/transport.py
    stdout/stderr writing and flush behavior
```

Persistence of receipts belongs in:

```text
persistence/receipts.py
```

## Standardize Renderer Signature

Remove runtime signature inspection such as:

```python
inspect.signature(formatter)
```

Replace loose formatter typing with a real protocol or consistent callable shape.

Conceptually:

```python
def render(
    result: T,
    *,
    budget: OutputBudget,
) -> RenderedText:
    ...
```

## Introduce Parameter Object

`finalize_output()` currently carries too many independent parameters.

Replace the long parameter list with an immutable context object where appropriate, for example:

```text
DeliveryContext
EmissionContext
```

Possible fields:

```text
request identity
repository identity
consumer/context identity
encoding
output view
attribution
budget/truncation state
```

Do not create an enormous god-object; group only values that genuinely travel together.

## Critical Invariant

Receipts must only represent bytes actually written/flushed according to the existing delivery contract.

Do not regress the delivery tests.

## Completion Gate

- `cli/emit.py` is gone or reduced to a very thin adapter
- rendering no longer writes stdout
- stdout transport no longer owns suppression logic
- receipt persistence no longer lives in CLI code
- renderer signatures are consistent
- delivery receipt tests pass

---

# Phase 18 — Decompose Telemetry Last

## Objective

Break the 4,000+ line telemetry subsystem apart only after operation/result boundaries have stabilized.

## Target

```text
telemetry/models.py
telemetry/recorder.py
telemetry/storage.py
telemetry/archive.py
telemetry/report.py
telemetry/analytics/
    efficiency.py
    chains.py
    failures.py
    tasks.py
    verification.py
```

## Suggested Responsibility Mapping

### `telemetry/archive.py`

Own:

- hot/archive paths
- locking
- rotation
- systemd timer/service installation
- persistence administration

### `telemetry/recorder.py`

Own:

- event construction
- fingerprints
- event measurement normalization
- `record_event`

### `telemetry/storage.py`

Own:

- JSONL loading
- event decoding/normalization
- archive/hot event reads

### `telemetry/analytics/*`

Own individual analyses:

- read efficiency
- cohort comparison
- retries
- transitions/chains
- failure breakdown
- task efficiency
- verification metrics

### `telemetry/report.py`

Own:

- presentation model
- text/ANSI rendering
- watch mode

## Type Direction

Use `TelemetryEvent` or a refined typed event object rather than a giant free-form argument list.

The desired dependency is:

```text
capability produces result
    ↓
telemetry adapter observes result
```

not:

```text
capability changes its data model to satisfy telemetry internals
```

## Completion Gate

- `telemetry.py` monolith is gone
- analytics modules have individually named responsibilities
- telemetry is observational
- statistics tests remain passing
- no feature changes to metric definitions unless required to preserve correctness

---

# Phase 19 — Final CLI Simplification and Proven Removals

## Objective

Perform deletion only after the new architecture makes redundancy unambiguous.

## Audit

Repository-wide search for:

- unused CLI aliases
- dead renderer adapters
- obsolete continuation formats
- unused re-export modules
- compatibility paths
- stale helper functions
- duplicated path normalization
- duplicate output-budget logic
- duplicate workspace ownership logic
- duplicate coverage conversion helpers

## Safe Candidate

If still present and confirmed equivalent:

```text
verified-changed
```

should be removed as an exact alias of `verify-changed`.

## Do Not Automatically Remove

These require explicit evidence:

```text
verify-changed
verify-task
task action aliases
--offline
--stat
legacy telemetry schemas
legacy continuation representation
```

## Completion Gate

- every removal has a documented reason
- no compatibility-only wrapper remains without an intentional compatibility requirement
- tests pass

---

# Phase 20 — Tighten Static Analysis

## Objective

Make typing and complexity checks architectural constraints instead of partial checks.

## Pyright

Current configuration uses partial `basic` checking.

Progress toward:

1. include all `src/agentq`
2. make core/domain model packages strict
3. make newly refactored capability packages strict
4. make the whole production package strict
5. delete legacy exemptions

The end state should prefer:

```text
typeCheckingMode = "strict"
```

for production code, unless a clearly documented external-tool boundary requires a local exception.

Do not globally suppress errors to achieve this.

Use narrow casts/type guards at dynamic boundaries.

## Ruff

The current complexity ignore list should be treated as a burn-down list.

Do not add new production modules to the ignore list.

Each phase should reduce it.

The desired final state is no product-code complexity exemptions unless there is a compelling, documented state-machine reason.

## Completion Gate

- full production package is statically checked
- strict typing is enabled wherever practical
- no generic `Any`-heavy cross-module API remains
- complexity exemptions are eliminated or narrowly justified
- tests pass

---

# Phase 21 — Final Architecture Documentation

## Objective

Make the repository self-explanatory to future agents.

Create:

```text
ARCHITECTURE.md
```

Keep it concise and structural.

It should explain:

1. dependency direction
2. capability map
3. request → operation → result flow
4. delivery flow
5. persistence ownership
6. telemetry observation flow
7. navigation provider extension point
8. verification provider extension point
9. P0 invariants
10. where the CLI adapter begins/ends
11. which components are expected to change when the future model-facing API is introduced

Do not produce an enormous documentation mirror of the source code.

## Completion Gate

A new coding agent should be able to answer:

```text
Where is search implemented?
Where is search rendered?
Where are continuations stored?
Where are delivery receipts stored?
Where do I add a language navigation provider?
Where do I add a verification provider?
What code is allowed to import the CLI?
Where are the mutation safety invariants tested?
```

without repository-wide wandering.

---

# 10. Phase Dependency Graph

The recommended order is not arbitrary.

```text
0 Baseline
    ↓
1 Package extraction
    ↓
2 Launcher/shim removal
    ↓
3 Test organization
    ↓
4 Core model ownership
    ↓
5 Architecture rules
    ↓
6 CLI registry
    ↓
7 Thin CLI main
    ↓
8 Remove common.py
    ↓
9 Discovery
    ↓
10 Navigation / inspect
    ↓
11 Git
    ↓
12 Workspace
    ↓
13 Execution / verification
    ↓
14 Mutation
    ↓
15 Persistence
    ↓
16 Continuations
    ↓
17 Delivery
    ↓
18 Telemetry
    ↓
19 Final removals
    ↓
20 Static-analysis tightening
    ↓
21 Architecture documentation
```

Important dependency rationale:

- Package extraction happens before architecture changes so import movement can be verified independently.
- Test reorganization happens early so later capability changes have discoverable behavioral protection.
- Core model ownership is fixed before capability decomposition to avoid moving types repeatedly.
- `common.py` is removed only after the correct infrastructure/capability owners exist.
- Workspace normalization precedes verification because verification consumes workspace ownership/graphs.
- Persistence precedes final continuation/delivery cleanup because both depend on state storage.
- Telemetry is last because its current implementation observes almost every subsystem.

---

# 11. Per-Phase Working Protocol for the Coding Agent

For every phase:

## Before Editing

1. Read this document's phase.
2. Inspect the actual current repository state.
3. Search for all call sites before moving/removing anything.
4. Identify tests directly covering the affected code.
5. Note whether previous phases have altered the paths named in this document.

## During Editing

1. Make structural changes only within the scope of the current phase.
2. Avoid unrelated cleanup unless required for the phase.
3. Do not opportunistically add features.
4. Do not preserve old internal interfaces unless they remain useful.
5. Prefer mechanical moves before behavioral rewrites.
6. Keep semantic changes separate from file movement where practical.

## After Editing

Run:

```text
targeted tests for affected subsystem
P0/invariant tests where relevant
full tests
Ruff
Pyright
```

Then inspect:

```text
git diff --stat
git diff
```

Confirm no accidental behavior changes.

## Phase Report

At completion of each phase, report:

```text
Phase implemented
Files moved
Files deleted
Major types introduced/moved
Behavior intentionally changed
Behavior intentionally preserved
Tests run
Static checks run
Any deviations from the plan
Any newly discovered architectural issue deferred to a later phase
```

Do not silently absorb new scope into the current phase.

---

# 12. Refactoring Heuristics for Function-Level Cleanup

Once modules have correct ownership, functions should be made leaner.

Use the following heuristics.

## Extract when

A function:

- performs orchestration and low-level parsing
- mutates state and formats output
- contains several nested semantic phases
- has a large argument list representing hidden domain structures
- contains conditionals selecting provider-specific behavior that providers can own
- returns a large loosely defined dictionary interpreted elsewhere

## Do not extract when

The extracted function would:

- merely wrap one call
- have a vague name such as `_process_data`
- require more parameters than the original local code
- hide a simple linear algorithm
- separate inseparable parts of a state machine

## Target orchestration style

Prefer functions that read as a sequence of named semantic operations:

```python
selection = resolve_diff_selection(...)
guard = validate_diff_guard(...)
files = collect_diff_files(...)
hunks = index_hunks(...)
return DiffResult(...)
```

rather than long functions combining parsing, execution, projection, serialization, and rendering.

---

# 13. Rules for Typed Boundaries

## Prefer

```python
@dataclass(frozen=True)
class SearchRequest:
    query: str
    scopes: tuple[RepoPath, ...]
    options: SearchOptions
```

over:

```python
def search_data(root, query, paths, limit, context, max_chars, ...):
    ...
```

## Prefer

```python
SearchResult
```

over a public return type of:

```python
dict[str, Any]
```

when multiple modules depend on the fields.

## Permit dictionaries for

- decoded external command payloads before validation
- JSON serialization
- local grouping/aggregation
- provider-specific opaque metadata not interpreted outside the provider
- SQLite row encoding/decoding at the persistence boundary

## Require validation at boundaries

Any data crossing from:

```text
subprocess
filesystem
SQLite
JSON
CLI
```

into a typed domain should be validated once.

Do not repeatedly revalidate the same object internally.

---

# 14. CLI Design Guidance During This Refactor

The CLI should become leaner now, but its grammar should not be broadly redesigned yet.

## Do now

- remove duplicate registrations
- separate argparse from capability logic
- standardize rendering
- reduce handler logic
- remove exact aliases proven redundant
- make output/exit-code handling explicit

## Defer

- renaming all commands into nested groups
- replacing `search`, `read`, and `inspect` with a new model-facing command
- intent-driven evidence orchestration
- single evidence-bundle command
- scoring/ranking redesign

The later model-facing redesign should be able to add or replace an adapter over the capability APIs created by this refactor.

---

# 15. Specific Current Modules and Recommended Destination

Use this as a migration reference, not as a requirement to perform all moves mechanically in one commit. Module paths in this table are relative to the package root `agentq/src/agentq/`.

| Current module | Recommended ownership |
|---|---|
| `common.py` | dissolve across `core`, `execution`, `delivery`, `discovery` |
| `paths.py` | `core/paths.py` |
| `runtime.py` | `core/runtime.py` |
| `evidence.py` | `core/evidence.py` |
| `budgeting.py` | `core/budget.py` or `delivery/rendering.py` depending on semantic split |
| `search.py` | `discovery/*` |
| `pythonnav.py` | `navigation/providers/python.py` |
| `tsnav.py` | `navigation/providers/typescript.py` plus JS helper asset |
| `navigation.py` | `navigation/resolution.py` + models/providers |
| `inspectops.py` | `navigation/inspect.py` + `navigation/models.py` |
| `gitops.py` | `git/*` |
| `workspace.py` | `workspace/*` |
| `deps.py` | workspace/dependency analysis capability |
| `impact.py` | impact capability using workspace/discovery APIs |
| `process.py` | `execution/supervisor.py` |
| `runops.py` | `execution/run.py` |
| `verification.py` | `verification/planner.py` + providers |
| `testplan.py` | absorb into verification planner |
| `verifychanged.py` | absorb into verification runner |
| `codemod.py` | `mutation/scan.py` + `mutation/plan.py` |
| `mutation_apply.py` | `mutation/apply.py` + optional `journal.py` |
| `state.py` | `persistence/*` |
| `continuations.py` | `continuations/*` |
| `context_cache.py` | mostly `delivery/suppression.py` plus narrowly owned cache logic |
| `emission.py` | `delivery/models.py` / `delivery/receipts.py` |
| `cli/emit.py` | `delivery/rendering.py`, `delivery/suppression.py`, `cli/transport.py` |
| `output_attribution.py` | delivery/telemetry boundary depending on final ownership |
| `telemetry.py` | `telemetry/*` |
| `tasking.py` | task domain + `persistence/tasks.py` |
| `doctor.py` | CLI/application diagnostics capability |
| `benchmark.py` | benchmark capability; retain only if still intentional |
| `audit.py` | audit capability; do not mix with Git core |

---

# 16. Current Removals Considered High Confidence

The following are worth actively validating and removing.

## 16.1 Skill forwarding launchers

Current repeated wrappers under:

```text
skills/*/scripts/agentq
```

forward to the toolkit command.

Once `agentq` installs a console script, these provide no independent behavior.

Remove them.

## 16.2 Toolkit shell launcher

Current:

```text
skills/agent-toolkit/scripts/agentq
```

exists to locate and execute `agentq.py`.

A package console entry point replaces it.

Remove it after Phase 1.

## 16.3 Python bootstrap file

Current:

```text
skills/agent-toolkit/scripts/agentq.py
```

exists as a thin entry point.

Use:

```text
agentq.cli.main:main
```

and:

```text
python -m agentq
```

instead.

Remove it.

## 16.4 `verified-changed`

Current parser/handler inspection indicates:

```text
verify
verify-changed
verified-changed
verify-task
```

all route through the same handler, with `verified-changed` appearing to be an exact naming alias of `verify-changed`.

Before deletion, search all docs/tests/source references.

If no independent semantics are found, delete `verified-changed`.

Do **not** infer that `verify-changed` itself is redundant with `verify`; current task-aware behavior can differ.

## 16.5 Dynamic contracts barrel

Delete dynamic lazy export machinery when the import cycle no longer exists.

Do not replace it with another large re-export barrel unless a small stable public API actually benefits from one.

---

# 17. Current Areas That Should Not Be Simplified Prematurely

## Process supervisor

`process.py` is comparatively large, but process lifetime, cancellation, group signaling, capture, cleanup, deadlines, and exit-code semantics form a real state machine.

Refactor for clarity, but do not split it arbitrarily by method count.

## Mutation apply path

Transaction-like apply/recovery behavior has strong safety coupling.

Keep journal, prepare, commit, rollback, and post-check semantics explicit.

Do not abstract them behind generic "storage" or "transaction manager" layers.

## Evidence coverage

Coverage status, reasons, counts, and provenance are important correctness metadata.

Do not reduce them to booleans such as:

```text
complete = True
```

during simplification.

## Delivery receipts

These are critical to repeat suppression correctness.

Preserve the rule that only evidence actually delivered can become suppressible.

---

# 18. Testing Strategy During the Refactor

The testing hierarchy should become:

## Unit tests

Test pure or narrowly scoped components:

- path normalization
- coverage merge
- request validation
- diff selection
- verification plan ordering
- mutation plan validation
- telemetry calculations

## Contract tests

Test:

- wire serialization/deserialization
- invalid schema rejection
- unknown field rejection
- canonical digest behavior
- result invariants

## Integration tests

Test:

- CLI → capability → output
- subprocess-backed navigation
- Git repository behavior
- verification execution
- state persistence
- continuation replay

## Invariant tests

Keep explicit tests for properties that must never regress:

- repository path confinement
- sensitive-file policy
- mutation plan scope
- scan/apply equivalence
- stale mutation rejection
- rollback
- delivery-after-write semantics
- no false coverage completion
- child-process cleanup
- wrapped command exit-code preservation

---

# 19. Acceptance Criteria for the Entire Refactor

The refactor is complete when all of the following statements are true.

## Packaging

- `agentq` is an installable Python package under `agentq/src/agentq`.
- `agentq/` is the standalone project root containing tests and pyproject metadata.
- `skills/` contains no canonical implementation code.
- there is one normal console entry point.
- `python -m agentq` works.
- `agentq_lib` no longer exists.

## Architecture

- CLI is a leaf adapter.
- capability code never accepts `argparse.Namespace`.
- capability code never directly prints output.
- core does not depend on CLI or capabilities.
- persistence concerns are explicitly owned.
- telemetry observes operation outcomes rather than shaping them.

## Types

- cross-module semantic boundaries are typed.
- stable result schemas are not represented as ad-hoc dictionaries.
- existing useful contracts are preserved or migrated rather than duplicated.
- no dynamic import hacks are needed to make contracts importable.

## Discoverability

- there is no `common.py`.
- there is no generic `utils.py`.
- historical `gitops.py`, `inspectops.py`, `runops.py`, and `verifychanged.py` names are gone.
- large capability modules have clear ownership.
- provider implementations are easy to locate.
- tests are organized by capability.

## CLI

- parser and handler metadata have one source of truth.
- command handlers are thin adapters.
- rendering has a consistent interface.
- output transport is separate from operation logic.

## Persistence

- database infrastructure is centralized.
- task, receipt, and continuation state have separate persistence APIs.
- SQL is not scattered across capability code.

## Delivery

- rendering, stdout transport, suppression, and receipt persistence are separated.
- delivery receipt correctness remains unchanged.
- partial/truncated output cannot falsely mark undelivered evidence as delivered.

## Telemetry

- telemetry monolith is decomposed.
- analytics are independently named modules.
- telemetry recording consumes stable typed facts.

## Quality

- full tests pass.
- P0 invariant tests pass.
- Ruff passes.
- Pyright checks the full production package.
- complexity exemptions are eliminated or narrowly justified.
- `ARCHITECTURE.md` accurately explains the resulting structure.

---

# 20. Refactor Completion Invariant

The finished design should embody this transformation.

Before:

```text
CLI command
    ↓
argparse.Namespace
    ↓
large module
    ↓
loosely shaped dictionary
    ↓
renderer / suppression / telemetry / persistence
```

After:

```text
Adapter
    ↓
Typed capability request
    ↓
Capability operation
    ↓
Typed capability result
    ├── renderer
    ├── continuation service
    ├── delivery
    └── telemetry observer
```

Formally, the stable application core should approximate:

\[
\text{Request}_C
\xrightarrow{\;C\;}
\text{Result}_C
\]

for capability \(C\), while transport-specific transformations remain outside:

\[
\text{CLI Args}
\rightarrow
\text{Request}_C
\]

and:

\[
\text{Result}_C
\rightarrow
\text{CLI Output}
\]

This separation is what will allow a later model-facing API to replace or supplement the CLI interaction layer without forcing another rewrite of repository discovery, navigation, Git analysis, verification, mutation, or persistence.

---

# 21. Suggested Instructions to Give a Coding Agent

For the first implementation task, use:

> Implement **Phase 0 and Phase 1 only** from `agentq-aggressive-refactoring-plan.md`.
>
> Treat the document as the architectural specification. Do not begin Phase 2.
>
> `agentq/` is the standalone Python project root; repository-level `skills/` is outside the project.
>
> First inspect the current repository because paths may have changed since the plan was authored. Preserve existing behavior and safety invariants. Do not introduce new features and do not add compatibility shims for the old `agentq_lib` package.
>
> At completion, run the Phase 1 verification gates and report:
>
> - files moved
> - imports changed
> - package metadata changes
> - tests/static checks run
> - any pre-existing failures
> - any deviations from the plan
>
> Stop after Phase 1.

For the next task:

> Implement **Phase 2 only** from `agentq-aggressive-refactoring-plan.md`.
>
> Confirm Phase 1's `agentq/` project structure exists before editing. Do not begin Phase 3.
>
> Perform repository-wide reference searches before deleting launchers or wrappers. Remove only shims made redundant by the installed `agentq` entry point.
>
> Run the phase verification gates and stop after reporting completion.

For subsequent phases, use the same pattern:

> Implement **Phase N only** from `agentq-aggressive-refactoring-plan.md`. Confirm all prerequisites from the previous phase exist. Do not begin Phase N+1. Preserve behavior unless the phase explicitly authorizes a removal.

This phased prompting is preferred over telling a coding agent to execute the entire document in one run.

---

# 22. Final Guidance

The goal is **not** to produce the maximum number of modules.

The goal is to reach a system where ownership is obvious.

A future contributor should be able to infer:

```text
Search behavior       → discovery/search.py
Diff behavior         → git/diff.py
Symbol resolution     → navigation/
Verification planning → verification/planner.py
Process execution     → execution/
Mutation safety       → mutation/
Persistent state      → persistence/
Delivery semantics    → delivery/
Telemetry analysis    → telemetry/
CLI grammar           → cli/
```

The strongest signal that the refactor is succeeding is that changing the CLI no longer requires changes inside the capabilities, and changing one capability no longer requires understanding the entire CLI/emission/telemetry stack.

Do not optimize for backwards compatibility.

Do optimize for:

- explicit ownership
- typed boundaries
- local reasoning
- discoverability
- narrow dependency direction
- preserved invariants
- future replacement of the model-facing adapter
