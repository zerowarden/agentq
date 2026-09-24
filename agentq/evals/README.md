# Evaluation apparatus

Developer-only tooling that measures whether `agentq`'s delivered evidence is
actually useful. This package is never imported by the installed runtime.

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
