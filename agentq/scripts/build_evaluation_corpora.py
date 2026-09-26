"""Compile cached benchmark labels and construct native stress repositories.

Run from agentq/: python -m scripts.build_evaluation_corpora --store ../.agentq-eval/v3
No benchmark results are evaluated here. Holdout membership is chosen from input
identity before nomination; previously inspected ContextBench families are
excluded from the confirmatory cohort, while their labels remain usable.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from agentq.core import canonical_digest, canonical_json
from evals.annotations import canonical_repository
from evals.generated import build_generated
from evals.importer import (
    import_rows,
    split_for_family,
    write_cases,
    write_labels,
    write_new_or_equal,
)
from evals.repobench import build_repobench, completion_input
from evals.store import CaptureStore

PROJECT = Path(__file__).resolve().parents[1]


def split_document(
    assignments: dict[str, str], groups: dict[str, str]
) -> dict[str, object]:
    return {
        "schema": "agentq.eval.splits/v1",
        "assignments": assignments,
        "groups": groups,
        "rule": "stable sha256 family split, 60/20/20; input-only sampling; prior exposed families excluded from confirmation",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--repobench-limit", type=int, default=240)
    parser.add_argument("--generated-seeds", type=int, default=1)
    parser.add_argument(
        "--families", type=Path, help="declared repository/fork aliases"
    )
    args = parser.parse_args()
    store = CaptureStore(args.store.resolve())
    families = {} if args.families is None else json.loads(args.families.read_text())
    previous_cases = list(
        (PROJECT / "evals/cases/external/source-conformance").glob("*.json")
    )
    exposed = {
        canonical_repository(json.loads(p.read_text())["source"]["repo"], families)
        for p in previous_cases
    }
    source = json.loads(
        (PROJECT / "evals/manifests/contextbench-source.json").read_text()
    )
    from datasets import Dataset

    cached = PROJECT.parent / ".agentq-eval/hub-cache"
    arrow = next(
        (
            cached / "Contextbench___context_bench/default/0.0.0" / source["revision"]
        ).glob("*.arrow")
    )
    rows = [dict(row) for row in Dataset.from_file(str(arrow))]
    imported = import_rows(rows, revision=source["revision"], families=families)
    corpus = store.root / "corpora/contextbench"
    write_cases(imported.source_conformance, corpus / "source-conformance")
    write_cases(imported.context_selection, corpus / "context-selection")
    write_labels(imported.labels, corpus / "labels")
    unseen = [
        case
        for case in imported.context_selection
        if case.repository_family not in exposed
    ]
    write_cases(unseen, corpus / "confirmation-cases")
    write_new_or_equal(
        corpus / "splits.json",
        (
            canonical_json(
                split_document(
                    {
                        case.case_id: split_for_family(case.repository_family)
                        for case in unseen
                    },
                    {
                        case.repository_family: split_for_family(case.repository_family)
                        for case in unseen
                    },
                )
            )
            + "\n"
        ).encode(),
    )
    context_report = {
        "source": source,
        "rows": len(rows),
        "source_conformance": len(imported.source_conformance),
        "context_selection": len(imported.context_selection),
        "positive_spans": sum(
            len(label.positive_spans) for label in imported.labels.values()
        ),
        "confirmatory_cases": len(unseen),
        "previously_exposed_families": sorted(exposed),
        "exclusions": [
            {"instance_id": item.instance_id, "reason": item.reason}
            for item in imported.excluded
        ],
    }
    store.write_artifact(corpus / "import-report.json", canonical_json(context_report))

    import pyarrow.parquet as pq

    source = json.loads((PROJECT / "evals/manifests/repobench-source.json").read_text())
    parquet_root = (
        cached
        / "repobench/datasets--tianyang--repobench_python_v1.1/snapshots"
        / source["revision"]
    )
    rows = [
        row
        for filename in source["files"]
        for row in pq.read_table(parquet_root / filename).to_pylist()
    ]
    # Eligibility depends on input size, not gold labels or baseline success.
    eligible, exclusions = [], []
    for index, row in enumerate(rows):
        try:
            value = completion_input(row)
            family = canonical_repository(value.repository, families)
            if (
                family in exposed
                or len(value.preceding_code) > 2500
                or len(value.snippets) < 3
            ):
                continue
            eligible.append((value.task_id, family, row))
        except ValueError as exc:
            exclusions.append({"row": index, "reason": str(exc)})
    selected, counts = [], Counter()
    for _, family, row in sorted(
        eligible, key=lambda entry: canonical_digest(("repobench-cohort-v1", entry[0]))
    ):
        if counts[family] >= 2:
            continue
        selected.append(row)
        counts[family] += 1
        if len(selected) >= args.repobench_limit:
            break
    lock, failures = build_repobench(
        selected, source["revision"], store, families=families
    )
    assignments = {
        case.case_id: split_for_family(case.repository_family) for case in lock.cases
    }
    store.write_artifact(
        store.root / "repobench-splits.json",
        canonical_json(
            split_document(
                assignments, {family: split_for_family(family) for family in counts}
            )
        ),
    )
    # Persist exact source-task membership before any decision outcomes are read.
    store.write_artifact(
        store.root / "repobench-cohort.json",
        canonical_json(
            {
                "source": source,
                "input_sampling": "sha256 task order; max two per family; cropped context <=2500 chars; >=3 candidates",
                "cases": {
                    case.case_id: {
                        "family": case.repository_family,
                        "original_inst_id": case.original_inst_id,
                    }
                    for case in lock.cases
                },
                "previously_exposed_families": sorted(exposed),
                "fork_aliases": families,
                "split_counts": dict(Counter(assignments.values())),
                "exclusions": [*exclusions, *failures],
            }
        ),
    )
    generated = build_generated(store, seeds=args.generated_seeds)
    store.write_artifact(
        store.root / "generated-splits.json",
        canonical_json(
            split_document(
                {
                    case.case_id: split_for_family(case.repository_family)
                    for case in generated.cases
                },
                {
                    case.repository_family: split_for_family(case.repository_family)
                    for case in generated.cases
                },
            )
        ),
    )
    print(
        canonical_json(
            {
                "contextbench": {
                    key: value
                    for key, value in context_report.items()
                    if key != "exclusions"
                },
                "repobench_cases": len(lock.cases),
                "repobench_groups": len(counts),
                "repobench_splits": dict(Counter(assignments.values())),
                "native_structural_cases": len(generated.cases),
            }
        )
    )


if __name__ == "__main__":
    main()
