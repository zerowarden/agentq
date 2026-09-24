"""Download a small pinned ContextBench authoring sample, not agentq fixtures.

From the inner agentq/ project:
    uv add --group eval datasets huggingface_hub
    uv run --locked --group eval python scripts/download_contextbench_seed.py

Raw rows include evaluation-only gold context/patches. Never put this directory
inside an inspected checkout. This script does not execute repository code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from datasets import load_dataset
from huggingface_hub import HfApi

DATASET = "Contextbench/ContextBench"


def write_json_new_or_equal(path: Path, value: object) -> None:
    text = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False,
                      allow_nan=False) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise RuntimeError(f"Refusing to replace an existing manifest: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    project = Path(__file__).resolve().parents[1]
    store = project.parent / ".agentq-eval"
    lock = project / "evals/manifests/contextbench-source.json"
    if lock.exists():
        source = json.loads(lock.read_text(encoding="utf-8"))
        if source.get("dataset") != DATASET:
            raise RuntimeError("Existing source manifest names a different dataset.")
        revision = source["revision"]
    else:
        # Resolve a moving branch once, then persist the immutable revision.
        revision = HfApi().dataset_info(DATASET).sha
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Could not obtain a full dataset commit SHA.")
    write_json_new_or_equal(lock, {
        "dataset": DATASET, "configuration": "default",
        "upstream_split": "train", "revision": revision,
    })
    dataset = load_dataset(
        DATASET, "default", split="train", revision=revision,
        cache_dir=str(store / "hub-cache"),
    )
    required = {"instance_id", "repo", "language", "base_commit", "gold_context"}
    if not required.issubset(dataset.column_names):
        raise RuntimeError(f"Dataset schema changed: missing {required - set(dataset.column_names)}")
    rows = sorted(
        (dict(row) for row in dataset
         if str(row["language"]).lower() in {"python", "typescript"}),
        key=lambda row: (str(row["repo"]), str(row["instance_id"])),
    )
    selected: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    for row in rows:
        repo = str(row["repo"])
        if counts[repo] >= 2:
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", str(row["base_commit"])):
            raise RuntimeError(f"Invalid base_commit for {row['instance_id']}")
        # This sample is for development, not a randomized generalization test.
        selected.append(row)
        counts[repo] += 1
        if len(selected) == args.limit:
            break
    if not selected:
        raise RuntimeError("No supported-language rows found; inspect the source schema.")
    raw = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                            allow_nan=False) + "\n" for row in selected)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    destination = store / "imports/contextbench" / revision / f"sample-{digest}.jsonl"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_text(encoding="utf-8") != raw:
        raise RuntimeError("Existing sample failed its content identity check.")
    destination.write_text(raw, encoding="utf-8")
    manifest = project / "evals/manifests" / f"contextbench-sample-{digest[:12]}.json"
    write_json_new_or_equal(manifest, {
        "dataset_revision": revision,
        "sample_digest": digest,
        "artifact_relative_path": str(destination.relative_to(store)),
        "instance_ids": [row["instance_id"] for row in selected],
        "purpose": "development-authoring-only",
        "requested_count": args.limit,
        "actual_count": len(selected),
    })
    print(f"Source lock: {lock}")
    print(f"Raw evaluation-only rows: {destination}")
    print(f"Sample manifest: {manifest}")
    print("Next: review a row, pin its repository base_commit, author a target/intent")
    print("request, capture the runtime pool, and independently annotate witnesses.")
    print("Downloaded benchmark rows are not yet replay captures or approved judgments.")


if __name__ == "__main__":
    main()
