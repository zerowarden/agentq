# Evaluation corrections and frozen nominee

Recorded 2026-09-26. The runtime scorer and selector defaults remain unchanged.
This run retained baseline; it does not establish a scoring improvement.

## Corpus artifacts

| Track | Produced | Verification / limits |
| --- | --- | --- |
| ContextBench source conformance | 588 case specifications | Separate source-reading objective; excluded from transfer nomination. |
| ContextBench task-context transfer | 575 case specifications and label records, preserving 5,344 annotated spans | Five existing Python repository captures compiled labels automatically. Three TypeScript captures could not resolve the repository's TypeScript dependency. The complete new repository acquisition run has not been performed. |
| RepoBench completion transfer | 240 captured, automatically labeled cases from 228 declared repository groups | 141 development, 39 validation, 60 held-out cases. Input-only sampling; other candidate snippets remain unjudged. |
| Native structural stress | 60 captures across all five intents | Construction facts were specified before running the real Python providers. |

The ContextBench import preserves exclusions and annotation provenance. Of the
575 transfer specifications, 511 exclude the five previously inspected repository
families. They are not part of this RepoBench nomination. Unknown fork lineage
is not certified independent: this run used full owner/repository identities and
an empty explicit alias map.

The protocol is [declared separately](../manifests/confirmatory-v3.json). Generated
artifacts reside under `../../../.agentq-eval/confirmatory-v3/`; the
[evaluation guide](../README.md#building-the-automatic-corpora) has reproduction
commands. Raw benchmark data and generated captures are not checked into Git.

## Observed development and validation results

The development search evaluated 212 configurations and retained baseline.
At the primary 12,000-character ceiling, all four matrix cells delivered the
same number of labeled RepoBench dependencies: **95/141 development** and
**22/39 validation**. Baseline wins the declared tie-break.

The budget measurement now distinguishes operating ceilings:

| Absolute ceiling | Development dependencies delivered | Validation dependencies delivered |
| --- | ---: | ---: |
| 6,000 characters | 63/141 | 17/39 |
| 12,000 characters | 95/141 | 22/39 |
| 24,000 characters | 114/141 | 25/39 |

This demonstrates budget sensitivity, not an improved scorer. Supplied RepoBench
snippets do not provide resolved binding facts; the current role/binding scorer
cannot distinguish their task-specific relevance by coefficient fitting alone.

The separate native stress matrix discriminates between selectors. At 12,000
characters, baseline delivers **215/250** construction facts on development and
**20/20** on validation; the challenger delivers **195/250** and **18/20**.
These structural facts are a different objective and are never combined with
RepoBench into one quality score.

## Freeze and verification

The [frozen v3 manifest](../../../.agentq-eval/confirmatory-v3/experiments/frozen-matrix.json)
records baseline as the nominee, both configurations, implementation and metric
fingerprints, budgets, promotion rule, and exact corpus/split identities. Its
identity and complete held-out membership were validated successfully without
evaluating any held-out outcome. **60 cases in 57 declared groups remain sealed.**

Actual rendered output tokens use `tiktoken==0.12.0/cl100k_base`. Both character
and token cost have a 10% growth guardrail; known-negative volume and unjudged
fraction may not increase. The primary coverage regression margin is zero.
Held-out promotion requires at least 20 cases and three declared groups, with
repository-grouped counts and descriptive bootstrap uncertainty.

All six specified instrument checks passed: required-source reservation,
reservation ablation, decoy delivery, honest empty output, dishonest exact-source
satisfaction, and unstable evidence delivery.

Executed verification:

- `pytest -q tests/evals tests/unit/test_inspection_decision.py tests/unit/test_inspection_selection.py tests/unit/test_inspection_scoring.py`: **306 passed, 109 subtests passed**.
- Configured `ruff check`, explicit lint of new evaluation modules/scripts, and
  `git diff --check`: passed.
- Automatic corpus construction, real-provider captures, development fitting,
  separate native/transfer matrices, and freeze validation: completed as above.

No held-out quality result or cross-repository generalization claim is made.
