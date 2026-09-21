"""Changed-file discovery and path classification.

``changed_files`` collects the worktree/base change set; :class:`ChangeSet`
carries its classification (global configuration, docs-only) so every
consumer reasons about the same typed value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentq.core import AgentQError
from agentq.execution import run_cmd

DOC_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".adoc", ".txt"})
GLOBAL_BASENAMES = frozenset(
    {
        "package.json",
        "pnpm-workspace.yaml",
        "pnpm-workspace.yml",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "bun.lock",
        "bun.lockb",
        "turbo.json",
        "nx.json",
        "tsconfig.json",
        "tsconfig.base.json",
        "eslint.config.js",
        "eslint.config.mjs",
        "eslint.config.cjs",
        "eslint.config.ts",
        "vitest.workspace.ts",
        "vitest.workspace.js",
    }
)
_GLOBAL_CONFIG_RE = re.compile(
    r"(?:tsconfig|eslint|vitest|vite|jest)[^/]*\.(?:json|js|cjs|mjs|ts)",
    re.I,
)


@dataclass(frozen=True)
class ChangeSet:
    """Repository-relative changed paths with their change classification."""

    files: tuple[str, ...] = ()

    @classmethod
    def from_paths(cls, paths: Iterable[str]) -> ChangeSet:
        return cls(files=tuple(sorted(set(paths))))

    @property
    def global_files(self) -> tuple[str, ...]:
        """Changed files that can alter behavior across the whole workspace."""
        return tuple(path for path in self.files if is_global_change(path))

    @property
    def docs_only(self) -> bool:
        return is_docs_only(self.files)


def changed_files(root: Path, base: str | None = None) -> ChangeSet:
    """Collect changed paths from an optional base diff plus the worktree."""
    changed: set[str] = set()
    if base:
        result = run_cmd(
            ["git", "diff", "--name-only", "-z", f"{base}...HEAD"], cwd=root, timeout=30
        )
        if result.returncode not in (0, 1):
            raise AgentQError(f"unable to diff base {base!r}")
        changed.update(item for item in result.stdout.split("\0") if item)
    result = run_cmd(
        ["git", "status", "--porcelain=v1", "-z"], cwd=root, timeout=30, check=True
    )
    records = [item for item in result.stdout.split("\0") if item]
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) >= 4:
            status = record[:2]
            path = record[3:]
            if ("R" in status or "C" in status) and index + 1 < len(records):
                path = records[index + 1]
                index += 1
            changed.add(path)
        index += 1
    return ChangeSet.from_paths(changed)


def is_global_change(path: str) -> bool:
    normalized = path.strip("/")
    if "/" not in normalized and normalized in GLOBAL_BASENAMES:
        return True
    if normalized.startswith(".github/workflows/"):
        return True
    return bool(_GLOBAL_CONFIG_RE.fullmatch(normalized))


def is_docs_only(paths: Sequence[str]) -> bool:
    if not paths:
        return False
    return all(
        Path(path).suffix.lower() in DOC_SUFFIXES
        or path.startswith("docs/")
        or PurePosixPath(path).name.lower() in {"readme", "license", "changelog"}
        for path in paths
    )


def is_public_contract_change(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = PurePosixPath(normalized).name
    return (
        name in {"package.json", "index.ts", "index.tsx", "index.js", "index.jsx"}
        or name.endswith(".d.ts")
        or "/contracts/" in f"/{normalized}"
        or "/types/" in f"/{normalized}"
        or "/public/" in f"/{normalized}"
        or "/exports/" in f"/{normalized}"
    )
