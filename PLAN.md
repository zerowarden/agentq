# `agentq` Implementation Plan

**Status:** Proposed implementation backlog  
**Baseline:** Current `agentq` source from the original `agents.tar.gz` bundle reviewed on 2026-08-28  
**Primary objective:** Make `agentq` a safe, low-context, evidence-preserving repository interface for coding agents, with bounded execution and guarded mutation.

---

## 1. Architectural Direction

The strongest role for `agentq` is not to reimplement a shell. It should provide a higher-level repository interface that reduces the number of exploratory calls, limits irrelevant output, preserves evidence quality, and makes destructive operations safer.

The target model is:

```text
Agent intent
    │
    ▼
┌──────────────────────────────┐
│ agentq command API           │
│                              │
│ - inspect                    │
│ - search                     │
│ - read                       │
│ - diff                       │
│ - impact                     │
│ - codemod                    │
│ - verify                     │
│ - run                        │
└──────────────┬───────────────┘
               │
               ▼
      Normalized repository intent
               │
               ▼
┌──────────────────────────────┐
│ Evidence planner             │
│                              │
│ - repository boundaries      │
│ - provider selection         │
│ - coverage policy            │
│ - token/output budget        │
│ - provenance                 │
└──────────────┬───────────────┘
               │
      ┌────────┼─────────┐
      ▼        ▼         ▼
   lexical   semantic  execution
   rg/git    TS/Python tests/tools
      └────────┼─────────┘
               ▼
       Canonical evidence model
               │
               ▼
      bounded agent response
```

State should remain local and should not change command semantics merely because telemetry is enabled:

```text
Local agentq state
    ├─ explicit session/task state
    ├─ seen-evidence/context state
    ├─ continuations
    ├─ optional semantic cache
    └─ optional telemetry
```

### Design principles

1. **Repository confinement is an invariant**, not a convention implemented independently by individual commands.
2. **Dry-run and mutation semantics must be identical.**
3. **Evidence must state its provenance and completeness.**
4. **Telemetry must never change query semantics.**
5. **Context suppression must require an explicit execution context.**
6. **Mutation safety is more important than codemod throughput.**
7. **Semantic state may be cached, but stateless behavior remains the correctness fallback.**
8. **Do not optimize only for characters printed.** Optimize total task cost while preserving task correctness.
9. **Do not add specialized commands unless they reduce real agent work.**
10. **Do not rewrite the CLI in Rust merely to reduce startup time.** Remove avoidable Python startup work first and benchmark the result.
11. **Do not force every arbitrary program through a behavior-changing execution environment.** `agentq run` must make its environment policy explicit.
12. Preserve the current strengths: bounded evidence, fixed-string search defaults, multi-window reads, task baselines, bounded Git inspection, semantic navigation, and skill-level stop conditions.

---

# 2. Master Execution Checklist

## Phase 0 — Correctness and security invariants

- [x] **AQ-001 — Introduce repository-scoped path and scope primitives**
- [x] **AQ-002 — Make codemod planning and application semantically identical**
- [x] **AQ-003 — Add immutable codemod plans and preimage fingerprints**
- [x] **AQ-004 — Replace duplicated secret handling with one streaming redactor**
- [x] **AQ-005 — Add regression/property tests for the P0 invariants**

## Phase 1 — State, sessions, and execution semantics

- [x] **AQ-006 — Decouple context suppression from telemetry**
- [x] **AQ-007 — Require explicit session/task identity for repeat suppression**
- [x] **AQ-008 — Replace mutable JSON state with a transactional SQLite/WAL state store**
- [x] **AQ-009 — Remove unnecessary cache-key/workspace computation on disabled paths**
- [x] **AQ-010 — Add explicit `run` execution profiles and safe log-retention policies**

## Phase 2 — Stable protocol and command architecture

- [x] **AQ-011 — Introduce a canonical evidence envelope with provenance and coverage**
- [x] **AQ-012 — Refactor the CLI into a lazy command registry**
- [x] **AQ-013 — Redesign `impact` around observations rather than an uncalibrated score**
- [x] **AQ-014 — Make `inspect` the primary repository-understanding operation**
- [x] **AQ-015 — Handle mixed-language and partial semantic evidence explicitly**

## Phase 3 — Search, continuation, navigation, and verification

- [x] **AQ-016 — Add `fast`, `auto`, and `exact` search-coverage policies**
- [x] **AQ-017 — Replace verbose continuation commands with local continuation cursors**
- [x] **AQ-018 — Introduce semantic-navigation provider interfaces**
- [ ] **AQ-019 — Add an optional persistent TypeScript semantic worker/daemon**
- [x] **AQ-020 — Refactor verification into language/ecosystem providers**
- [ ] **AQ-021 — Fix verification ordering around information value and dependency constraints**
- [ ] **AQ-022 — Enforce a no-implicit-download execution policy**

## Phase 4 — Telemetry, testing, empirical evaluation, and documentation

- [ ] **AQ-023 — Separate telemetry storage/analytics from runtime behavior**
- [ ] **AQ-024 — Strengthen telemetry privacy and retention**
- [ ] **AQ-025 — Split the test suite into fast, integration, semantic, scale, and agent-evaluation tiers**
- [ ] **AQ-026 — Build shell-vs-agentq end-to-end A/B task evaluation**
- [ ] **AQ-027 — Update skills and documentation around evidence quality and stop conditions**
- [ ] **AQ-028 — Rationalize the public command surface without removing useful providers**
- [ ] **AQ-029 — Add schema/version migrations and a release gate**

---

# 3. Detailed Tasks

## AQ-001 — Introduce repository-scoped path and scope primitives

**Priority:** P0  
**Type:** Correctness / security  
**Dependencies:** None

### Goals and objectives

Make it impossible for a repository-scoped operation to accidentally inspect or mutate a path outside the selected repository simply because one backend forgot to call `ensure_within()`.

Repository confinement should be enforced before a command reaches `rg`, `ast-grep`, Git, a language service, or direct filesystem access.

### Current problem

`agentq_lib/common.py` already provides `ensure_within()`, but use is inconsistent.

A verified example is `agentq_lib/impact.py`:

```python
target_path = root / target
```

`impact_data()` then passes that path to `_nearest_manifest()`. A target such as:

```bash
agentq impact ../outside/package.json --repo /repo
```

can therefore inspect a manifest outside `/repo`.

Other commands are protected only because a particular downstream backend currently validates a scope. That is fragile. For example, ordinary search-backed codemod scopes receive search validation, whereas AST codemod scopes are passed directly to `ast-grep`.

### Real scenario addressed

A coding agent is working in:

```text
/work/acme-service
```

and generates:

```bash
agentq impact ../shared/package.json
```

because it incorrectly inferred repository layout.

The expected behavior is a deterministic refusal. `agentq` must not silently inspect `/work/shared/package.json`, even though the file is locally accessible.

This matters for:

- privacy;
- isolation between worktrees;
- avoiding accidental edits to sibling repositories;
- preventing a malformed agent scope from turning a bounded operation into a host-filesystem operation.

### Implementation details

Create a dedicated repository-path abstraction, preferably in a new module:

```text
agentq_lib/paths.py
```

Recommended internal structures:

```python
@dataclass(frozen=True)
class RepoPath:
    root: Path
    absolute: Path
    relative: str

@dataclass(frozen=True)
class RepoScope:
    root: Path
    path: RepoPath
```

Provide constructors such as:

```python
resolve_repo_path(root, value) -> RepoPath
resolve_repo_scopes(root, values) -> list[RepoScope]
```

Requirements:

- Expand user input.
- Convert relative paths against `root`.
- Resolve symlinks.
- Reject a resolved path unless it is `root` or a descendant of `root`.
- Represent the repository-relative path once and reuse it.
- Do not provide `allow_outside=True` to ordinary repository commands.
- If a future command genuinely needs external paths, expose a distinct type and explicit capability instead of weakening `RepoPath`.

Refactor operations to accept normalized paths/scopes rather than arbitrary strings where practical.

At minimum update:

- `impact_data()`;
- `_nearest_manifest()`;
- codemod fixed/regex scopes;
- AST codemod scopes;
- `inspect`;
- TypeScript navigation;
- Git structural inspection;
- `run --cwd`;
- file reads;
- any new provider interface.

`_nearest_manifest()` must also terminate strictly at the repository root rather than relying on parent traversal behavior.

### Affected files

Existing:

- `skills/agent-toolkit/scripts/agentq_lib/common.py`
- `skills/agent-toolkit/scripts/agentq_lib/impact.py`
- `skills/agent-toolkit/scripts/agentq_lib/codemod.py`
- `skills/agent-toolkit/scripts/agentq_lib/inspectops.py`
- `skills/agent-toolkit/scripts/agentq_lib/tsnav.py`
- `skills/agent-toolkit/scripts/agentq_lib/search.py`
- `skills/agent-toolkit/scripts/agentq_lib/gitops.py`
- `skills/agent-toolkit/scripts/agentq_lib/runops.py`

New:

- `skills/agent-toolkit/scripts/agentq_lib/paths.py`

Tests:

- `skills/agent-toolkit/tests/...`

### Success criteria

- Every user-controlled filesystem scope is normalized once before provider dispatch.
- `../outside`, absolute external paths, and symlinks escaping the repository are rejected.
- `impact` cannot inspect a manifest outside the root.
- AST codemods cannot operate outside the root.
- Valid paths within the repository continue to work.
- No mutation command exposes an implicit external-path bypass.

### Required tests

Cover:

- `../outside`;
- absolute external path;
- nested valid path;
- repository root itself;
- symlink inside repository pointing outside;
- symlink inside repository pointing inside;
- non-existent path whose resolved parent would escape;
- scopes passed to every mutation backend.

---

## AQ-002 — Make codemod planning and application semantically identical

**Priority:** P0  
**Type:** Correctness  
**Dependencies:** AQ-001

### Goals and objectives

Guarantee:

\[
M_{\text{plan}}(p,F)=M_{\text{apply}}(p,F)
\]

where \(M\) is the set of matches for pattern \(p\) over files \(F\).

The dry run must describe the actual transformation engine that will mutate the files.

### Current problem

Regex discovery/counting currently uses ripgrep's regex engine, while application uses Python `re`.

A verified example is:

```regex
\p{L}+
```

Ripgrep accepts this pattern. Python `re` does not. The dry run can therefore report matches and the apply path can then fail with:

```text
re.PatternError
```

Patterns accepted by both engines can also differ subtly in semantics.

### Real scenario addressed

An agent performs:

```bash
agentq codemod-scan '\p{L}+' --mode regex --rewrite X
```

The scan appears safe and reports a known match count.

The agent then applies the same operation, expecting the guarded dry run to be authoritative. The application crashes or mutates a different set of spans.

For a mutation tool, that invalidates the purpose of the dry run.

### Implementation details

Use one regex implementation for both planning and mutation.

Recommended initial decision: **use Python `re` for regex-mode codemods**, because mutation is already implemented in Python and correctness is more important than maximum scan throughput.

Do not use `rg` to determine the authoritative regex match set.

Suggested design:

1. Resolve candidate repository files through the repository file index/listing.
2. Apply scope and sensitive-file filters.
3. Compile the regex once with Python `re`.
4. Scan files with that compiled expression.
5. Produce exact match locations/counts.
6. Use the same compiled expression for replacement.

Keep:

- fixed mode using literal string semantics;
- AST mode using `ast-grep`.

AST planning and AST mutation must likewise use the same `ast-grep` pattern/rewrite semantics and the same validated scopes.

If performance later becomes problematic, implement a common Rust-regex mutation backend rather than reintroducing two regex languages.

### Error handling

Invalid regex syntax must produce an `AgentQError` with compact diagnostics. Do not expose a raw Python traceback for user input.

### Affected files

- `agentq_lib/codemod.py`
- potentially `agentq_lib/search.py` only if shared helpers are extracted
- `agentq.py`
- codemod tests

### Success criteria

- Any regex accepted by dry run is accepted by apply.
- Dry run and apply report the same match set when files have not changed.
- Invalid Python regex syntax fails before presenting a valid codemod plan.
- Fixed and AST modes retain their current behavior.
- No raw `re.PatternError` traceback reaches the agent.

### Required tests

- Unicode/category-style syntax rejected consistently if unsupported.
- Lookaround.
- Capture groups.
- Backreferences in replacement strings.
- Multiline matches.
- Zero-width matches.
- Empty replacement.
- Files with invalid UTF-8.
- Binary files.
- No-match behavior.
- Exact match-set equality between scan and apply planning.

---

## AQ-003 — Add immutable codemod plans and preimage fingerprints

**Priority:** P0  
**Type:** Mutation safety  
**Dependencies:** AQ-001, AQ-002

### Goals and objectives

Protect against the repository changing between review and mutation.

A match-count guard alone is insufficient.

### Real scenario addressed

The agent scans a codemod and sees 12 matches.

Another tool or agent changes one file. The repository still contains 12 matches, but they are now different spans.

A subsequent:

```bash
agentq codemod-apply ... --expect-count 12 --apply
```

passes the count guard despite no longer applying the reviewed transformation.

### Implementation details

Introduce a codemod plan schema, for example:

```json
{
  "schema": "agentq.codemod-plan/v1",
  "engine": "python-re",
  "pattern": "...",
  "rewrite": "...",
  "scopes": ["src"],
  "files": [
    {
      "path": "src/a.ts",
      "sha256": "...",
      "matches": [
        {"start": 10, "end": 15}
      ]
    }
  ]
}
```

Generate:

```text
plan_id = SHA256(canonical_plan_without_plan_id)
```

Support:

```bash
agentq codemod-scan ... --plan-out /tmp/plan.json
agentq codemod-apply --plan /tmp/plan.json --apply
```

For backward compatibility, `codemod-apply PATTERN REWRITE --apply` may recompute a plan immediately before mutation, but should clearly state that it is applying a newly generated plan rather than a previously reviewed plan.

Before mutation:

- verify plan schema/version;
- re-hash every input file;
- reject if any preimage differs;
- revalidate repository confinement;
- verify the selected engine is available;
- verify the plan's pattern/rewrite metadata.

Prepare all output bytes before replacing any file.

For multi-file mutation, implement rollback on replacement failure:

1. read and retain original bytes;
2. prepare temporary outputs;
3. replace targets;
4. if a later replacement fails, restore already-replaced files from retained preimages;
5. surface a high-severity error if rollback itself fails.

### Affected files

- `agentq_lib/codemod.py`
- new `agentq_lib/codemod_plan.py` if separation is useful
- `agentq.py`
- tests
- mechanical-refactor skill documentation

### Success criteria

- A reviewed plan cannot apply to changed preimages.
- `plan_id` is deterministic.
- Mutation cannot silently proceed on a stale plan.
- Partial file-update failures trigger rollback.
- The applied result reports plan ID, changed files, replacement counts, and remaining matches.

---

## AQ-004 — Replace duplicated secret handling with one streaming redactor

**Priority:** P0  
**Type:** Correctness / privacy  
**Dependencies:** None

### Goals and objectives

Use a single redaction implementation for:

- command output;
- file reads;
- diagnostics;
- logs;
- tails;
- future providers.

### Current problem

Private-key handling exists in multiple modules and has diverged.

A verified `runops.py` failure mode is a private-key BEGIN and END marker on the same line. The current loop sets `in_private_key=True`, does not evaluate the END marker on that same branch, and can then suppress unrelated subsequent lines.

Example:

```text
-----BEGIN PRIVATE KEY-----...-----END PRIVATE KEY-----
ERROR after key
```

The legitimate error can disappear because the reader still believes it is inside the key block.

### Real scenario addressed

A build tool accidentally prints a compact environment value containing a private key followed by the actual compiler error.

`agentq run` should redact the secret and still return the compiler error. Losing post-secret diagnostics can cause an agent to misdiagnose a failure.

### Implementation details

Create:

```text
agentq_lib/redaction.py
```

Implement:

```python
redact_text(text: str) -> str

class StreamingRedactor:
    def feed(self, chunk: str) -> str: ...
    def finish(self) -> str: ...
```

The streaming state machine must handle:

- BEGIN and END on separate lines;
- BEGIN and END on the same line;
- multiple blocks in one chunk;
- markers split across input chunks;
- unterminated blocks;
- secret-like key/value patterns;
- text immediately before and after a secret.

All outputs used for diagnostics and tail extraction must be generated *after* redaction.

Do not maintain separate BEGIN/END logic in `search.py` and `runops.py`.

### Affected files

- `agentq_lib/common.py`
- `agentq_lib/search.py`
- `agentq_lib/runops.py`
- new `agentq_lib/redaction.py`
- tests

### Success criteria

- No private-key material appears in rendered output, diagnostics, tail, or stored logs.
- Unrelated material before and after a key is preserved.
- Same-line key blocks no longer suppress subsequent output.
- One implementation owns redaction semantics.

---

## AQ-005 — Add regression/property tests for the P0 invariants

**Priority:** P0  
**Type:** Test infrastructure  
**Dependencies:** AQ-001 through AQ-004

### Goals and objectives

Turn the verified failures into permanent invariants.

### Implementation details

Add targeted tests for:

1. Repository path confinement.
2. Regex planning/application equivalence.
3. Codemod stale-plan rejection.
4. Codemod rollback behavior.
5. Streaming redaction.
6. Sensitive-path exclusion across every mutation provider.

Use deterministic randomized tests or a development-only property-testing library such as Hypothesis. Do not add a runtime dependency merely for tests.

### Success criteria

A future backend refactor cannot reintroduce these bugs without failing the fast test suite.

---

## AQ-006 — Decouple context suppression from telemetry

**Priority:** P1  
**Type:** State semantics  
**Dependencies:** None

### Current problem

`context_cache_enabled()` currently requires both:

```text
AGENTQ_TELEMETRY
AGENTQ_CONTEXT_CACHE
```

This couples observability to command behavior.

### Real scenario addressed

A user disables telemetry for privacy but still wants repeated read suppression inside a Codex task.

Today disabling telemetry can also disable the context mechanism. Conversely, turning telemetry on can enable behavior whose semantic effect is unrelated to telemetry.

### Implementation details

Change semantics to:

```python
context_cache_enabled() -> env_enabled("AGENTQ_CONTEXT_CACHE", default=...)
telemetry_enabled()     -> env_enabled("AGENTQ_TELEMETRY", default=...)
```

No query result, suppression rule, or continuation behavior should depend on telemetry being enabled.

Telemetry may observe these mechanisms, but must not control them.

### Affected files

- `agentq_lib/context_cache.py`
- `agentq_lib/telemetry.py`
- `agentq_lib/runtime.py`
- tests
- README/environment-variable documentation

### Success criteria

All four combinations of telemetry/context-cache settings behave independently and predictably.

---

## AQ-007 — Require explicit session/task identity for repeat suppression

**Priority:** P1  
**Type:** Correctness  
**Dependencies:** AQ-006

### Current problem

When no task or thread exists, `_context()` falls back to:

```text
recent-session
```

for the repository.

Unrelated agents can therefore share suppression state for the cache TTL.

### Real scenario addressed

Agent A reads `auth.ts` during one unrelated task.

Three hours later Agent B works in the same repository with no recognized thread ID. Agent B requests the same source range and receives suppression because Agent A already saw it.

That is not safe: context visibility is specific to an agent conversation, not a repository.

### Implementation details

Resolve context in this order:

```text
active agentq task ID
    ↓
explicit AGENTQ_SESSION_ID
    ↓
recognized host thread ID (e.g. CODEX_THREAD_ID)
    ↓
no identity → repeat suppression disabled
```

Do **not** invent a repository-global pseudo-session.

Add:

```text
AGENTQ_SESSION_ID
```

as a first-class host integration contract.

Hash external identifiers before storage.

### Success criteria

- Two sessions in one repository never suppress one another.
- No session identity means no repeat suppression.
- Explicit task identity remains stronger than thread/session identity.
- Stored state contains no raw external conversation identifier.

---

## AQ-008 — Replace mutable JSON state with a transactional SQLite/WAL state store

**Priority:** P1  
**Type:** Reliability / concurrency  
**Dependencies:** AQ-006, AQ-007

### Goals and objectives

Replace several bespoke local JSON persistence mechanisms with one small transactional local database.

### Current problem

Context state uses atomic rename but has read-modify-write race conditions across concurrent processes. Task state has similar concurrency risks. Telemetry has accumulated substantial storage and synchronization logic.

Atomic file replacement does not make a read-modify-write sequence transactional.

### Real scenario addressed

Two coding agents read different source ranges at nearly the same time:

1. both load the same context JSON;
2. each appends a different range;
3. both replace the file;
4. the last writer wins;
5. one agent's state is lost.

### Implementation details

Use Python's standard-library `sqlite3`.

Suggested database:

```text
state.db
```

Use:

```sql
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=...;
```

Initial tables:

```text
schema_migrations
context_entries
task_state
continuations
```

Possible later tables:

```text
telemetry_events
semantic_cache_metadata
```

Keep source contents out of the state database unless a feature explicitly requires them.

`context_entries` should contain approximately:

```text
repo_id
context_id
command
evidence_key
created_at
expires_at
```

`task_state` should contain the active task and baseline metadata.

Use transactions for update operations and TTL cleanup.

Keep database permissions private.

### Migration

- Read old JSON state once if present.
- Import compatible records.
- Rename or delete the old state after successful migration.
- Never silently merge malformed legacy state.

### Affected files

- `agentq_lib/context_cache.py`
- `agentq_lib/tasking.py`
- `agentq_lib/runtime.py`
- new `agentq_lib/state.py`
- eventually telemetry modules
- tests

### Success criteria

- Concurrent processes do not lose valid state.
- WAL mode is enabled.
- State migrations are versioned.
- Context and task semantics no longer depend on ad-hoc JSON locking.
- A corrupt state record does not make repository queries unusable.

---

## AQ-009 — Remove unnecessary cache-key/workspace computation on disabled paths

**Priority:** P1  
**Type:** Performance  
**Dependencies:** AQ-006

### Current problem

`emit_cached()` computes an operation cache key before it determines whether repeat suppression is active.

`operation_cache_key()` calls `workspace_identity()`, which executes Git commands and stats changed files.

This is avoidable work if caching is disabled.

### Implementation details

Short-circuit first:

```python
if not context_cache_enabled():
    emit(...)
    return
```

Only calculate:

- workspace identity;
- repeat fingerprint;
- context lookup

when the relevant feature is active.

Within a single invocation, memoize workspace identity if multiple operations need it.

Do not introduce a persistent workspace hash cache until profiling proves it is useful.

### Success criteria

A command with context caching disabled performs no cache-related Git/status/stat calls.

Add a test with mocked command execution to prove this.

---

## AQ-010 — Add explicit `run` execution profiles and safe log-retention policies

**Priority:** P1  
**Type:** Execution semantics / privacy  
**Dependencies:** AQ-004

### Current problem

`run_compact()` changes environment variables including:

```text
XDG_CACHE_HOME
CI
TERM
NO_COLOR
PAGER
GIT_PAGER
npm settings
pip settings
```

These changes can alter program behavior, not merely reduce output.

### Real scenarios addressed

1. A build passes under `CI=1` but behaves differently in an ordinary developer environment.
2. Redirecting `XDG_CACHE_HOME` forces a compiler to rebuild a cache and makes every agent call slower.
3. A tool detects `TERM=dumb` and changes output format.
4. A successful command log retains proprietary output longer than needed.

### Implementation details

Add:

```bash
agentq run --profile transparent
agentq run --profile compact
agentq run --profile ci
agentq run --profile offline
```

Recommended semantics:

**transparent**
- preserve process environment;
- intercept/redact output only;
- do not set `CI`;
- do not redirect `XDG_CACHE_HOME`.

**compact**
- disable decorative color/pagers;
- suppress known update-notifier noise;
- preserve behavior-sensitive cache and CI settings.

**ci**
- compact behavior plus `CI=1`.

**offline**
- compact/CI policy as explicitly documented;
- set supported package-manager offline controls;
- clearly state that environment flags are not a complete network sandbox.

Add separate:

```text
--isolated-cache
```

if a private `XDG_CACHE_HOME` is desired.

Backward compatibility:

- keep `--offline` as an alias during migration;
- version any default-profile change.

### Log retention

Recommended policy:

- failed-command redacted log: retain temporarily;
- successful-command log: delete after extracting compact output unless `--keep-log`;
- add TTL cleanup;
- add total-size quota;
- keep mode `0600`;
- expose retention decision in structured output.

### Success criteria

- The selected profile is explicit in JSON/text output.
- `transparent` does not mutate behavior-sensitive environment variables.
- Successful runs do not accumulate unlimited logs.
- Failure logs remain available for debugging.
- All stored material is passed through the shared redactor.

---

## AQ-011 — Introduce a canonical evidence envelope with provenance and coverage

**Priority:** P1  
**Type:** Protocol / architecture  
**Dependencies:** AQ-001

### Goals and objectives

Make search, navigation, inspection, impact, diff, and verification describe evidence using the same vocabulary.

### Current problem

Concepts such as:

- complete;
- sampled;
- semantic;
- lexical;
- heuristic;
- truncated

already exist, but each command encodes them differently.

An agent should not need command-specific knowledge to determine whether a result is semantic proof, lexical evidence, or a heuristic suggestion.

### Implementation details

Introduce:

```text
agentq_lib/evidence.py
```

Suggested envelope:

```json
{
  "schema": "agentq/2",
  "operation": "inspect",
  "status": "ok",
  "coverage": {
    "status": "sampled",
    "reason": ["reference_limit"]
  },
  "evidence": [
    {
      "path": "src/foo.ts",
      "range": {"start_line": 42, "end_line": 58},
      "kind": "definition",
      "provenance": "semantic",
      "traits": ["source"],
      "text": "..."
    }
  ],
  "diagnostics": [],
  "continuation": null
}
```

Standard provenance values:

```text
semantic
syntactic
lexical
heuristic
```

Standard coverage values:

```text
complete
sampled
partial
unknown
```

Examples:

- TypeScript compiler definition → `semantic`
- Python AST definition → `syntactic`
- Python attribute/name reference scan → `lexical` unless true name binding is implemented
- impact fan-out inference → `heuristic`
- search hit → `lexical`

Renderers may remain optimized for text, but JSON should converge on this schema.

### Compatibility

Add a schema version and provide a transition period for existing compact JSON consumers.

### Success criteria

- Every evidence-producing command reports provenance.
- Every bounded operation reports completeness.
- Parse/provider failures can downgrade coverage to `partial` or `unknown`.
- No renderer claims `[complete]` when a provider silently skipped malformed inputs.

---

## AQ-012 — Refactor the CLI into a lazy command registry

**Priority:** P1  
**Type:** Maintainability / startup performance  
**Dependencies:** AQ-011 recommended but not mandatory

### Current problem

`agentq.py` eagerly imports most subsystems before the selected command is known.

In the earlier profiling environment, a clean Python invocation placed a material fraction of trivial command latency in imports and parser construction rather than the repository operation itself.

### Implementation details

Introduce declarative command metadata, for example:

```python
CommandSpec(
    name="search",
    summary="bounded repository search",
    handler="agentq_lib.search:search_data",
    renderer="agentq_lib.search:render_search",
    mutation="read",
    path_policy="repo",
)
```

Suggested structure:

```text
agentq_lib/commands/
    __init__.py
    registry.py
    search_command.py
    inspect_command.py
    ...
```

The top-level process should:

1. parse global command selection;
2. lazy-import only the selected command module;
3. build only its detailed parser;
4. dispatch.

`agentq --help` may load command metadata but should not import heavy semantic/telemetry implementations.

### Benchmarking

Measure under identical environments:

- cold process wall time;
- warm process wall time;
- max RSS;
- import time;
- actual operation time.

Use relative targets rather than machine-specific universal thresholds. A reasonable goal is to remove at least 30–50% of avoidable startup overhead for trivial operations without harming maintainability.

### Non-goal

Do not rewrite the CLI in Rust solely for startup time at this stage.

---

## AQ-013 — Redesign `impact` around observations rather than an uncalibrated score

**Priority:** P1  
**Type:** Evidence quality  
**Dependencies:** AQ-011

### Current problem

`impact.py` currently assigns fixed points such as:

- source fan-out thresholds;
- import fan-out;
- shared/public path;
- docs/config references;
- missing direct test references.

The score is coherent as a heuristic, but it is not empirically calibrated. A numeric score can imply more statistical meaning than exists.

### Real scenario addressed

An agent sees:

```text
blast radius: HIGH (score 7)
```

and treats `7` as an objective risk estimate.

In reality, the value is a manually weighted rule set.

### Implementation details

Make the primary response observational:

```text
public/shared surface: yes
lexical source fanout: 31 files
import-pattern fanout: 12 files
direct test references: 0
config/schema references: 4
owning package: @acme/jobs
semantic import evidence: unavailable
coverage: sampled
```

If a summary is retained:

```json
"heuristic_summary": {
  "level": "high",
  "calibrated": false,
  "rules": [...]
}
```

Do not make the score the headline.

Over time, compare heuristic predictions with actual historical change breadth and verification failures before treating weights as calibrated.

### Success criteria

An agent can reconstruct *why* impact is considered broad without trusting an opaque scalar.

---

## AQ-014 — Make `inspect` the primary repository-understanding operation

**Priority:** P1  
**Type:** Agent ergonomics / token efficiency  
**Dependencies:** AQ-011, AQ-015

### Goals and objectives

Answer:

> What is this thing, where is it defined, what depends on it, and what evidence do I need before editing it?

with as few agent calls as possible.

### Current state to preserve

`inspect` already:

- detects files/directories;
- supports source windows;
- tries TypeScript semantic navigation;
- falls back to Python navigation;
- then falls back to lexical search.

This is a good foundation.

### Implementation details

Add:

```bash
agentq inspect TARGET --intent locate
agentq inspect TARGET --intent understand
agentq inspect TARGET --intent edit
```

Recommended behavior:

**locate**
- definitions/candidates only;
- minimal output.

**understand**
- declaration;
- signature/type information when available;
- representative references;
- owning package/module;
- relevant tests.

**edit**
- complete declaration body if reasonably bounded;
- surrounding local definitions required to understand it;
- semantic references ranked by likely mutation relevance;
- directly related tests;
- public/export surface;
- package ownership;
- dependent-package evidence if inexpensive;
- configuration/schema relationships;
- suggested verification scope.

Respect the global evidence budget. Do not automatically dump full files.

### Agent stop condition

The associated skill should tell an agent to stop exploring when the edit bundle has:

- an unambiguous declaration;
- sufficient implementation context;
- representative or complete references appropriate to the task;
- relevant tests/verification scope.

This reduces repeated exploratory calls more effectively than merely shrinking each search result.

### Success criteria

Common edits that currently require `search → read → ts-nav → test-plan` can often begin from one `inspect --intent edit` call.

---

## AQ-015 — Handle mixed-language and partial semantic evidence explicitly

**Priority:** P1  
**Type:** Correctness / evidence quality  
**Dependencies:** AQ-011

### Current problems

`inspect` currently tries TypeScript first, then Python, then lexical search. If TypeScript finds a candidate, Python candidates are not considered.

Python parsing also tolerates syntax errors in some paths, but downstream rendering can still appear complete.

### Real scenarios addressed

1. A monorepo contains both a TypeScript `Config` and a Python `Config`. The tool silently selects the TypeScript result.
2. One Python source file cannot be parsed. The navigation result omits it but is rendered as if complete.

### Implementation details

Introduce provider result metadata:

```text
provider
candidate_count
provenance
coverage
errors
```

For an unqualified symbol:

- query all applicable low-cost providers;
- merge candidates;
- if candidates span languages/modules, report ambiguity;
- allow `--lang`, `--path`, or `--pick` to narrow.

Provider failure behavior:

- one malformed Python file → coverage `partial`, not `complete`;
- unavailable TypeScript runtime → provider diagnostic plus fallback;
- parse errors should be summarized without flooding output.

### Success criteria

No language silently wins merely because it is queried first.

---

## AQ-016 — Add `fast`, `auto`, and `exact` search-coverage policies

**Priority:** P2  
**Type:** Performance / evidence semantics  
**Dependencies:** AQ-011

### Current state

Search already distinguishes complete vs sampled output and deliberately bounds visible evidence. Preserve this.

The current implementation performs an exact counting pass and then a match-collection pass for many searches.

### Problem

Exact global cardinality is not always worth scanning the relevant corpus twice.

For initial exploration, knowing:

```text
> 5000 matches, sampled
```

may be more useful per millisecond than obtaining the precise count `13,842`.

### Implementation details

Add:

```bash
agentq search QUERY --coverage fast
agentq search QUERY --coverage auto
agentq search QUERY --coverage exact
```

**fast**
- single evidence-collection pass where possible;
- may terminate at a scan/evidence bound;
- reports lower-bound counts;
- never labels the result complete unless it actually exhausted the search.

**exact**
- preserve exact matching-file/line totals;
- may perform additional counting work.

**auto**
- choose exact for narrow/cheap searches;
- choose fast for broad/high-cardinality searches;
- expose the chosen strategy in structured output.

Do not reuse approximate search counting for codemod guards. Mutation planning remains exact.

### Success criteria

- Broad exploratory search requires materially less repository scanning in `fast`.
- `exact` preserves current exhaustive semantics.
- The user/agent can always distinguish approximate/lower-bound counts from exact counts.

---

## AQ-017 — Replace verbose continuation commands with local continuation cursors

**Priority:** P2  
**Type:** Token efficiency  
**Dependencies:** AQ-008, AQ-011

### Current state

Several renderers already emit useful continuation commands. Preserve reproducibility.

### Improvement

Instead of repeatedly returning:

```text
continue: agentq ts-nav overview SomeSymbol --path ... --pick ... --limit ...
```

return:

```text
continue: agentq continue q7H2a
```

### Implementation details

Store continuation records in the local state database:

```text
cursor
repo_id
context_id
operation
canonical_arguments
workspace_generation
created_at
expires_at
```

Properties:

- random/unpredictable cursor token;
- scoped to repository and session/task;
- short TTL;
- invalidated when required workspace generation/preimage changes;
- no network dependency.

Structured output can retain the full reproducible command in a non-default/debug field:

```json
"continuation": {
  "cursor": "q7H2a",
  "command": "...",
  "expires_at": "..."
}
```

### Success criteria

Normal agent-visible continuations are short while reproducibility remains available.

---

## AQ-018 — Introduce semantic-navigation provider interfaces

**Priority:** P2  
**Type:** Architecture  
**Dependencies:** AQ-011, AQ-015

### Goals and objectives

Separate semantic intent from TypeScript/Python-specific implementation.

### Implementation details

Define a provider protocol, e.g.:

```python
class NavigationProvider(Protocol):
    name: str

    def supports(self, request: NavigationRequest) -> bool: ...
    def locate(self, request: NavigationRequest) -> EvidenceEnvelope: ...
    def overview(self, request: NavigationRequest) -> EvidenceEnvelope: ...
```

Initial providers:

```text
TypeScriptProvider
PythonProvider
LexicalFallbackProvider
```

Keep lexical fallback explicitly lower-provenance.

The provider layer will later support a daemon without changing command semantics.

### Success criteria

`inspect` and `ts-nav` no longer need to know process-launch details for each language provider.

---

## AQ-019 — Add an optional persistent TypeScript semantic worker/daemon

**Priority:** P2  
**Type:** Performance optimization  
**Dependencies:** AQ-018  
**Ship condition:** Benchmark first

### Current problem

TypeScript navigation launches Node and builds language-service state per invocation. In large monorepos, repeated project discovery/program construction can dominate semantic-navigation latency.

### Important constraint

Do not make the daemon a correctness dependency.

### Implementation details

Target architecture:

```text
agentq CLI
    │
    ├─ cheap/stateless operation
    │
    └─ semantic request
            │
            ▼
       per-worktree agentqd
            ├─ TypeScript DocumentRegistry
            ├─ loaded TS Programs
            ├─ workspace graph
            └─ filesystem generation
```

Requirements:

- per-worktree identity;
- local IPC only;
- versioned protocol;
- idle timeout;
- automatic stale-process recovery;
- filesystem invalidation;
- memory ceiling;
- no source upload/network transmission;
- stateless fallback if unavailable.

Do not persist TypeScript compiler objects to disk. Keep expensive semantic objects in process memory.

### Benchmark gate

Before implementation, record semantic navigation latency on:

- small repository;
- medium monorepo;
- large monorepo with many `tsconfig` files.

Ship the daemon only if repeated calls show a meaningful end-to-end gain.

### Success criteria

Repeated semantic calls are significantly faster without changing returned evidence semantics.

---

## AQ-020 — Refactor verification into language/ecosystem providers

**Priority:** P2  
**Type:** Architecture / generality  
**Dependencies:** AQ-011

### Current problem

Verification planning has strong Node/Vitest assumptions. The concepts are general, but the implementation is not yet provider-oriented.

### Implementation details

Define:

```python
class VerificationProvider(Protocol):
    def detect(self, repo) -> Detection: ...
    def ownership(self, changes) -> Ownership: ...
    def plan(self, changes, mode) -> list[Check]: ...
```

Initial providers:

### Node provider

Preserve and refactor current:

- workspace package discovery;
- Vitest direct/related tests;
- package scripts;
- local dependency graph;
- typecheck/lint/build planning.

### Python provider

Detect from `pyproject.toml` and local environment:

- pytest;
- unittest where appropriate;
- Ruff;
- mypy/pyright if configured;
- package ownership.

### Cargo provider

Support:

- `cargo test -p`;
- `cargo check -p`;
- `cargo clippy -p` when configured/available;
- workspace dependents.

### Go provider

Support:

- package-level `go test`;
- affected module/package inference.

### Repository configuration

Add optional:

```text
.agentq.toml
```

for:

- custom verification commands;
- package ownership;
- ignored paths;
- public-contract patterns;
- provider enable/disable;
- forbidden dynamic-install behavior.

Configuration augments provider inference; it should not become mandatory.

### Success criteria

Node remains at least as capable as before, while non-Node repositories can receive principled verification plans.

---

## AQ-021 — Fix verification ordering around information value and dependency constraints

**Priority:** P2  
**Type:** Correctness / efficiency  
**Dependencies:** AQ-020

### Current problem

The planner says:

> Run the earliest falsifying step first.

But existing sorting gives package order precedence over check priority:

```python
(order_rank[package], priority, kind)
```

A lower-information check in an earlier package can therefore precede a direct, high-information test elsewhere.

### Implementation details

Model verification phases explicitly:

```text
Phase 1 — nearest/high-information falsifiers
    changed tests
    directly related tests

Phase 2 — changed-package contract checks
    typecheck
    package tests

Phase 3 — direct dependent checks
    dependent typecheck/tests

Phase 4 — broad validation
    lint
    build
    wider workspace tests
```

Dependency ordering should be a constraint only where one check genuinely requires another package to be built or validated first.

Represent:

```json
{
  "phase": 1,
  "information_rank": 10,
  "depends_on_checks": []
}
```

Then topologically order true dependencies within each phase.

### Success criteria

The first executed check is normally the cheapest high-information falsifier, independent of arbitrary package enumeration order.

---

## AQ-022 — Enforce a no-implicit-download execution policy

**Priority:** P2  
**Type:** Reproducibility / security  
**Dependencies:** AQ-020

### Current concern

`workspace.py` can generate `bunx` commands. Package-manager execution helpers can potentially fetch missing tools depending on package manager and version.

`agentq` should not silently download executable code merely because a verification command is inferred.

### Real scenario addressed

An agent asks for verification in a fresh checkout where Vitest is not installed locally. A package-manager helper downloads and executes a version from the registry without explicit authorization.

That changes:

- security assumptions;
- reproducibility;
- network usage;
- task latency.

### Implementation details

Default invariant:

> Inferred `agentq` operations may execute installed/local tools, but may not implicitly install or download missing executables.

Prefer:

- local `node_modules/.bin/...`;
- package-manager `exec` forms that are proven not to install;
- existing virtual-environment binaries;
- Cargo/Go tools already available.

If a tool is missing:

```text
verification unavailable: vitest is referenced but not installed locally
```

Optionally support an explicit future:

```text
--allow-tool-download
```

but never enable it by default.

Add package-manager-specific tests.

### Success criteria

No verification or navigation path triggers an implicit package download under default settings.

---

## AQ-023 — Separate telemetry storage/analytics from runtime behavior

**Priority:** P2  
**Type:** Maintainability  
**Dependencies:** AQ-008 recommended

### Current problem

`telemetry.py` has become one of the largest runtime modules and implements:

- event collection;
- storage;
- locking;
- rotation;
- identity;
- aggregation;
- cohort analysis;
- rendering;
- persistence.

Meanwhile context and task state have separate stores.

### Implementation details

Split by responsibility:

```text
agentq_lib/telemetry/
    __init__.py
    events.py
    store.py
    privacy.py
    aggregate.py
    render.py
```

or equivalent modules if keeping a flat package is preferred.

Telemetry must consume runtime events but must not be imported by ordinary command code to determine command semantics.

If AQ-008's SQLite store proves suitable, store telemetry events in separate tables or a separate telemetry database with clear retention boundaries.

### Success criteria

Disabling telemetry does not change repository query, mutation, session, or continuation behavior.

---

## AQ-024 — Strengthen telemetry privacy and retention

**Priority:** P2  
**Type:** Privacy  
**Dependencies:** AQ-023

### Goals

Telemetry should answer performance/effectiveness questions without creating a secondary repository-content store.

### Implementation details

- HMAC/hash repository identifiers and labels by default.
- Avoid storing raw query strings.
- Avoid raw absolute paths.
- Avoid source snippets.
- Keep operation fingerprints private/local.
- Add event TTL.
- Add maximum on-disk size.
- Document exactly what is stored.
- Make human-readable repository names opt-in rather than default.

Telemetry should record facts such as:

```text
operation class
duration
visible characters
evidence records
coverage status
repeat suppression
provider
failure category
```

not proprietary content.

### Success criteria

Telemetry can be safely inspected without reconstructing meaningful repository source or user query text.

---

## AQ-025 — Split the test suite into fast, integration, semantic, scale, and agent-evaluation tiers

**Priority:** P1/P2  
**Type:** Developer productivity  
**Dependencies:** Ongoing

### Current problem

The full self-test is substantial enough that it did not complete inside the initial 120-second review windows. That does not mean it is failing, but it makes it unsuitable as the only feedback loop.

### Target structure

```text
tests/
    unit/
    integration/
    semantic/
    invariants/
    scale/
    agent_eval/
```

Suggested execution tiers:

### Fast

Run on every edit:

- pure functions;
- path confinement;
- redaction;
- codemod planning;
- state transitions;
- evidence schema;
- parser/renderer behavior.

Target: seconds, not minutes.

### Integration

Run:

- real `rg`;
- Git repositories;
- process execution;
- SQLite concurrency;
- package-manager command generation.

### Semantic

Fixtures for:

- TypeScript projects;
- Python projects;
- malformed source;
- symbol ambiguity;
- multi-project workspaces.

### Scale

Maintain:

- 10k;
- 100k;
- larger synthetic event/search workloads.

Do not gate every small edit on the largest benchmark.

### Agent evaluation

End-to-end tasks described in AQ-026.

### Success criteria

A developer can get fast correctness feedback while the comprehensive suite remains available in CI/release validation.

---

## AQ-026 — Build shell-vs-agentq end-to-end A/B task evaluation

**Priority:** P2  
**Type:** Product validation  
**Dependencies:** Core refactors substantially complete

### Goals and objectives

Measure whether `agentq` actually improves coding-agent performance.

Do not use output characters or `agentq` call counts as the sole objective.

### Problem with a narrow metric

An output compressor can reduce visible characters by hiding a necessary definition. The agent may then:

- make three more calls;
- edit the wrong file;
- need a revert;
- fail the task.

The first call looked efficient while the total task became worse.

### Evaluation objective

Measure approximately:

\[
J =
w_T T_{\text{total-context}}
+
w_L L_{\text{wall}}
+
w_C N_{\text{calls}}
+
w_R N_{\text{recovery}}
\]

subject to:

\[
P(\text{correct task completion}) \ge p_{\min}
\]

where total context includes:

\[
T_{\text{total-context}} =
T_{\text{skill instructions}}
+
T_{\text{tool schemas}}
+
T_{\text{invocations}}
+
T_{\text{results}}
+
T_{\text{corrections}}
\]

### Test corpus

Create real maintenance tasks such as:

- locate and fix a bug;
- rename a public symbol;
- update an API with callers;
- modify a schema and update tests;
- diagnose a failing test;
- change a shared utility;
- understand an unfamiliar module;
- perform a mechanical migration;
- inspect a dirty worktree without disturbing prior changes.

Run the same coding-agent model/configuration under:

**A — conventional shell/code tools**

versus:

**B — agentq-oriented workflow**

### Record

- final patch correctness;
- tests passed;
- total input/output tokens;
- tool-call count;
- wall-clock duration;
- repeated source reads;
- failed/recovery calls;
- edit/revert count;
- amount of repository content surfaced;
- task completion rate.

### Release interpretation

An operation should be routed through `agentq` by default only when evidence shows a net benefit or when it provides a strong safety property.

This is especially important for the original ambition that coding agents make *all* command calls through `agentq`. That policy should be an empirical outcome, not an assumption.

---

## AQ-027 — Update skills and documentation around evidence quality and stop conditions

**Priority:** P2  
**Type:** Agent behavior  
**Dependencies:** AQ-011, AQ-014

### Current strength

The skill routing already tries to tell agents when to stop exploring. Preserve and strengthen this.

### Implementation details

Update:

- `skills/agent-toolkit/SKILL.md`
- `skills/repo-exploration/SKILL.md`
- `skills/semantic-code-navigation/SKILL.md`
- `skills/change-impact-analysis/SKILL.md`
- `skills/mechanical-refactor/SKILL.md`
- `skills/targeted-verification/SKILL.md`
- references and command recipes
- OpenCode integration docs

Standardize vocabulary:

```text
semantic proof
syntactic evidence
lexical evidence
heuristic inference
complete
sampled
partial
unknown
```

Document stop conditions such as:

> If `inspect --intent edit` identifies an unambiguous declaration, provides the required implementation body, shows relevant callers/tests, and reports sufficient coverage for the requested change, do not repeat broad search.

Document when direct `agentq run --profile transparent` is preferable to a specialized command.

### Success criteria

The skills teach the agent to reason about evidence quality, not simply memorize command names.

---

## AQ-028 — Rationalize the public command surface without removing useful providers

**Priority:** P3  
**Type:** UX / maintainability  
**Dependencies:** AQ-012, AQ-014, AQ-020

### Goals

Reduce the number of conceptual operations an agent must learn.

Recommended primary model:

```text
inspect     understand an entity
search      find evidence
read        retrieve exact source
diff        understand changes
impact      assess change surface
codemod     guarded mechanical mutation
verify      establish confidence
run         bounded arbitrary escape hatch
```

Specialized functionality such as:

- TypeScript navigation actions;
- dependency graph internals;
- structural diff;
- test planning

can remain available as advanced commands/providers.

Do not remove commands merely to make the list short. The goal is to make the normal routing policy simpler.

### Migration

- retain existing aliases for at least one compatibility version;
- add deprecation notices only after the replacement flow is demonstrably better;
- update skill files before removing any documented command.

---

## AQ-029 — Add schema/version migrations and a release gate

**Priority:** P1/P2  
**Type:** Release engineering  
**Dependencies:** All relevant schema changes

### Goals

Prevent another release from claiming architectural changes that are not actually present.

### Required versioned artifacts

Version:

- evidence schema;
- state database schema;
- codemod plan schema;
- daemon protocol;
- telemetry event schema.

### Release gate

Before packaging a release:

1. Run fast tests.
2. Run integration tests.
3. Run semantic fixtures.
4. Run P0 invariant tests.
5. Run scale tests where appropriate.
6. Run package audit.
7. Build archive.
8. Extract archive into a clean directory.
9. Execute smoke tests against the **extracted archive**, not the source tree.
10. Compare changed implementation files against the previous release.
11. Produce a machine-readable changelog/diff summary.
12. Verify archive hash.
13. Verify archive paths contain no traversal/absolute members.
14. Confirm planned tasks marked implemented correspond to actual source diffs and tests.
15. Only then publish the downloadable bundle.

Recommended release metadata:

```json
{
  "version": "...",
  "source_commit": "...",
  "previous_version": "...",
  "changed_files": [...],
  "tests": {...},
  "schemas": {...},
  "sha256": "..."
}
```

### Success criteria

A release validation report can prove both:

- the archive is structurally valid;
- the claimed implementation changes actually exist.

---

# 4. Cross-Cutting Implementation Decisions

## 4.1 Do not merge telemetry and semantic behavior

The dependency direction should be:

```text
command behavior
      │
      └────► event emission
                 │
                 ▼
             telemetry
```

Never:

```text
telemetry setting ───► command semantics
```

---

## 4.2 Prefer immutable request/evidence objects internally

As the system grows, passing dictionaries with slightly different shapes across commands will become a source of errors.

Use `dataclass`, `TypedDict`, or similarly explicit structures for:

```text
RepoPath
RepoScope
EvidenceRecord
EvidenceEnvelope
Coverage
NavigationRequest
VerificationCheck
CodemodPlan
RunProfile
```

Serialization should happen at the boundary.

---

## 4.3 Preserve stateless fallbacks

Caching may improve latency, but correctness must not depend on local cached state.

If:

- SQLite state is unavailable;
- the semantic daemon is unavailable;
- a continuation has expired;

the command should either:

- execute statelessly; or
- return a clear recoverable error.

---

## 4.4 Treat mutation and read commands differently

Read commands may use:

- sampling;
- approximate lower bounds;
- cursors;
- heuristic ranking.

Mutation planning may not use approximate match sets as safety guards.

For codemods:

```text
exact plan
+ exact preimages
+ repository confinement
+ explicit apply
```

are mandatory.

---

## 4.5 No silent evidence upgrades

A lexical result must not be presented as semantic because it happens to look convincing.

A malformed parser input must not still yield `complete`.

Use explicit provenance and coverage at every layer.

---

## 4.6 Optimize for information gain

A useful conceptual target for exploratory queries is:

\[
\frac{I(E;\theta)}
{\text{tokens}(E)+\lambda\cdot\text{latency}(E)}
\]

where:

- \(E\) is returned evidence;
- \(\theta\) is the unresolved codebase fact.

This suggests prioritizing:

1. declaration/definition;
2. owning module/package;
3. representative callers;
4. related tests;
5. public/configuration surfaces;

rather than merely maximizing exact corpus counts.

This is a design heuristic, not an empirically proven objective. AQ-026 should validate whether it corresponds to better task outcomes.

---

# 5. Recommended New Internal Layout

Do not reorganize everything at once. Introduce modules as the corresponding tasks are implemented.

A plausible end state is:

```text
agentq_lib/
├── commands/
│   ├── registry.py
│   ├── inspect.py
│   ├── search.py
│   ├── read.py
│   ├── impact.py
│   ├── codemod.py
│   ├── verify.py
│   └── run.py
├── providers/
│   ├── navigation/
│   │   ├── base.py
│   │   ├── typescript.py
│   │   ├── python.py
│   │   └── lexical.py
│   └── verification/
│       ├── base.py
│       ├── node.py
│       ├── python.py
│       ├── cargo.py
│       └── go.py
├── evidence.py
├── paths.py
├── redaction.py
├── state.py
├── codemod_plan.py
├── search.py
├── gitops.py
├── workspace.py
├── tasking.py
├── budgeting.py
├── telemetry/
│   ├── events.py
│   ├── store.py
│   ├── privacy.py
│   ├── aggregate.py
│   └── render.py
└── ...
```

Do not create empty abstraction layers. Extract a module only when at least one concrete task requires it.

---

# 6. Implementation Order and Merge Strategy

Recommended sequence:

```text
AQ-001  repository boundaries
   │
   ├──► AQ-002 regex consistency
   │       └──► AQ-003 codemod plans
   │
   └──► provider/path refactors later

AQ-004 shared redaction
   └──► AQ-010 run profiles/logs

AQ-006 telemetry decoupling
   └──► AQ-007 session identity
           └──► AQ-008 SQLite state
                   └──► AQ-017 cursors

AQ-009 cache fast path
AQ-012 lazy command registry

AQ-011 evidence protocol
   ├──► AQ-013 impact redesign
   ├──► AQ-014 inspect intents
   ├──► AQ-015 mixed-language handling
   ├──► AQ-018 navigation providers
   └──► AQ-020 verification providers

AQ-016 search coverage policy

AQ-018 navigation providers
   └──► AQ-019 optional TS daemon

AQ-020 verification providers
   ├──► AQ-021 ordering
   └──► AQ-022 no-download policy

AQ-023/AQ-024 telemetry refactor/privacy

AQ-025 test tiers — applied continuously
AQ-026 A/B evaluation — after stable feature set
AQ-027/AQ-028 docs and command UX
AQ-029 release gate
```

### Suggested pull-request boundaries

Avoid one giant rewrite.

Recommended PR sequence:

1. **PR 1:** path confinement + regression tests.
2. **PR 2:** codemod engine consistency + plan fingerprints.
3. **PR 3:** shared redactor + run redaction tests.
4. **PR 4:** telemetry/context decoupling + explicit sessions.
5. **PR 5:** SQLite state migration.
6. **PR 6:** run profiles/log retention.
7. **PR 7:** evidence schema.
8. **PR 8:** impact redesign + partial/ambiguous evidence.
9. **PR 9:** `inspect --intent`.
10. **PR 10:** CLI registry/lazy import.
11. **PR 11:** search coverage modes + cursors.
12. **PR 12:** verification provider refactor/order/no-download.
13. **PR 13:** semantic provider abstraction.
14. **PR 14:** daemon experiment, only if benchmarks justify it.
15. **PR 15:** telemetry decomposition/privacy.
16. **PR 16:** A/B evaluation harness + docs/skills.
17. **PR 17:** command-surface cleanup and release tooling.

Each PR should leave the CLI operational and preserve compatibility unless the PR explicitly implements a versioned migration.

---

# 7. Minimum Release Gate Before Calling the Refactor Complete

The refactor is not complete merely because all planned modules exist.

The release should satisfy all of the following.

## Security/correctness

- [ ] No repository command reads outside the root through `..`, absolute paths, or symlink escape.
- [ ] No mutation command writes outside the repository.
- [ ] Codemod scan and apply use identical matching semantics.
- [ ] Stale codemod plans are rejected.
- [ ] Multi-file codemod failures cannot silently leave an unknown partial state.
- [ ] Secret redaction survives arbitrary streaming/chunk boundaries.
- [ ] Telemetry does not control command semantics.
- [ ] Unidentified sessions do not use shared repeat suppression.
- [ ] Verification does not implicitly download executable tools.

## Evidence quality

- [ ] Every evidence record has provenance.
- [ ] Every bounded result has coverage status.
- [ ] Parser/provider failures downgrade coverage appropriately.
- [ ] Mixed-language ambiguity is surfaced.
- [ ] `impact` exposes observations before heuristic conclusions.

## Performance

- [ ] Cache-disabled commands do not calculate cache workspace identity.
- [ ] Lazy CLI loading reduces trivial-operation startup overhead relative to baseline.
- [ ] `search --coverage fast` avoids unnecessary exhaustive counting.
- [ ] Semantic daemon is shipped only if repeated semantic-navigation benchmarks justify it.

## State/reliability

- [ ] SQLite state uses WAL and transactions.
- [ ] Concurrent session/context updates do not lose records.
- [ ] Cursor/session records have TTL.
- [ ] State schemas are migrated explicitly.

## Agent effectiveness

- [ ] End-to-end A/B tasks compare agentq with ordinary shell workflows.
- [ ] Correct task completion is not degraded.
- [ ] Total context/tool-call burden is measured, not merely visible output size.
- [ ] Skill instructions include clear stop conditions.

## Packaging

- [ ] The packaged archive is smoke-tested after extraction.
- [ ] Source diffs confirm every claimed implementation change.
- [ ] SHA-256 is generated and verified.
- [ ] Archive traversal checks pass.
- [ ] Release report distinguishes tests passed from recommendations still outstanding.

---

# 8. What Should Not Be Changed Without Evidence

The following existing ideas are sound and should not be discarded casually:

- fixed-string search as the default;
- bounded search/read output;
- complete-vs-sampled signaling;
- whole-record output budgeting rather than arbitrary string slicing;
- multi-window reads;
- bounded Git hunks;
- task baseline awareness in dirty worktrees;
- semantic TypeScript navigation;
- Python syntax-aware definitions;
- lexical fallback when semantic tooling is unavailable;
- explicit `--apply` for mutation;
- focused verification before broad verification;
- skill-level guidance that discourages repeated exploration.

Likewise, avoid premature changes such as:

- rewriting all of `agentq` in Rust;
- building a full language-server daemon for every language;
- creating a universal repository index before measurements show it is needed;
- forcing telemetry to be always-on;
- replacing useful specialized providers merely to make the CLI superficially smaller;
- making `.agentq.toml` mandatory;
- treating the heuristic impact score as a statistical model;
- assuming that every shell command is automatically more efficient when wrapped by `agentq`.

---

# 9. Final Target

A mature `agentq` should allow a coding agent to ask:

```text
What is this symbol?
What source do I need to read?
What will this change affect?
Can this mechanical change be applied safely?
What is the cheapest useful verification?
Have I already seen this exact evidence in this task?
```

and receive a small, locally derived, provenance-aware answer.

The core value proposition should therefore be:

> `agentq` converts underspecified repository questions into bounded, high-confidence evidence and guarded actions while minimizing unnecessary agent context and tool round-trips.

The implementation should be judged against that outcome rather than against the narrower objective of wrapping as many shell commands as possible.

