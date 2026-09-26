# Evaluation apparatus

Developer-only tooling that measures whether `agentq`'s delivered evidence is
actually useful. This package is never imported by the installed runtime.

## Two suites, never one headline

`import-rows` authors two suites from the same pinned rows, and they are
captured, split, and reported separately:

- **source-conformance** anchors the request on the first gold span exactly as
  the annotator recorded it. The range policy reserves that exact source, so
  the suite is a source-reading correctness gate: it answers "was the requested
  span delivered", not "was the right evidence selected".
- **context-selection** anchors the request on the changed symbol the patch
  names, scoped to that symbol's package. The pool then holds competing
  declarations, references, and mentions, and reviewer-authored judgments
  witness the row's other gold spans among them. This is the scoring corpus.

Split groups are namespaced by suite and each suite has its own split file, so
the two cannot be averaged into one headline score by accident. The remaining
gold spans stay in the evaluation-only authoring sample; the reviewer reads
them there when authoring the capture-bound draft.

For `capture --cases-dir`, the default suite id is the directory name. An
explicit `--suite` overrides it. Keep the two corpora in separate experiment
commands; the repeatable `--suite` option can combine arbitrary suite locks.

Frozen experiments bind the inspection and core source trees and all Python
evaluation code, including metric aggregation and promotion rules. Changing
any of that code requires a new freeze, even when profile names are unchanged.
Fingerprints are cached per process; restart the command after editing code.

## How judgments work

Human review produces a grading rubric that a program can check. The evaluator
never reads prose; it only asks whether specific captured evidence is present.
One reviewed case yields one **judgment**, built from four terms:

- **Judgment** — the reviewer's complete rulebook for one case. It holds the
  facets, the evidence that witnesses them, any evidence judged irrelevant, and
  the expected outcomes.
- **Facet** — one requirement on that rulebook, judged as a unit and marked
  `critical` (must be present) or not (nice to have). Real examples:
  `editable-source`, `target-identity`, `implementation-visible`, `gold-span`.
  Critical-facet recall and all-critical-present rate are the primary metrics.
- **Witness** — one piece of evidence that checks a facet off, stated as a set
  of acceptable captured variants. Any one of them satisfies the witness (OR).
  A witness like `implementation.representation` accepts either
  `implementation.exact` or `implementation.excerpt`.
- **Clause** — a bundle of witnesses that only count together (AND). A facet may
  offer several clauses as alternative ways to satisfy it; one fully satisfied
  clause is enough (OR). In code, a clause is a `witness_set`.

A real example from the `basic-edit` case: the facet `consumer-contract` holds
when clause A holds (witness `caller.use` **and** witness `target.identity`) or
clause B holds (witness `test.mention`).

```text
witness = OR over its acceptable variants
clause  = AND over the witnesses it names
facet   = OR over its clauses
```

Why it is built this way:

- No prose parsing: the evaluator asks whether one of the named variant ids is
  present in the acquired pool, the initial selection, or the final delivery.
- Labels are capture-bound: reviewers name evidence with readable aliases
  (`target.exact`), and compilation replaces each alias with the immutable
  variant id stored in that capture, so a rubric cannot drift or silently pass.
- Unjudged evidence is neither credited nor treated as irrelevant.

Where this lives: judgment drafts and compilation in `judgments.py`; facet and
witness checks plus metrics in `metrics.py`; capture-bound label records in
`models.py`.
