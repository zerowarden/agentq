"""Download only the pinned RepoBench shard; never execute dataset code."""

import json
from pathlib import Path

from huggingface_hub import hf_hub_download


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    source = json.loads((project / "evals/manifests/repobench-source.json").read_text())
    cache = project.parent / ".agentq-eval/hub-cache/repobench"
    for filename in source["files"]:
        path = hf_hub_download(
            source["dataset"],
            filename,
            repo_type="dataset",
            revision=source["revision"],
            cache_dir=cache,
        )
        print(path)


if __name__ == "__main__":
    main()
