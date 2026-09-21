"""Compact repository/workspace map capability."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentq.core import (
    COMPLETE,
    LEXICAL,
    RESULT_LIMIT,
    SAMPLED,
    Coverage,
    typed_coverage,
)
from agentq.discovery.files import list_repo_files
from agentq.execution import run_cmd
from agentq.tooling import language_for


@dataclass(frozen=True)
class LanguageCount:
    language: str
    files: int

    def to_wire(self) -> dict[str, Any]:
        return {"language": self.language, "files": self.files}


@dataclass(frozen=True)
class ExtensionCount:
    extension: str
    files: int

    def to_wire(self) -> dict[str, Any]:
        return {"extension": self.extension, "files": self.files}


@dataclass(frozen=True)
class DirectoryCount:
    path: str
    files: int

    def to_wire(self) -> dict[str, Any]:
        return {"path": self.path, "files": self.files}


@dataclass(frozen=True)
class Manifest:
    """One recognized workspace manifest; optional fields follow its kind."""

    path: str
    kind: str
    name: str | None = None
    scripts: tuple[str, ...] = ()
    dependencies: int | None = None
    dev_dependencies: int | None = None
    workspace: bool | None = None

    def to_wire(self) -> dict[str, Any]:
        if self.kind == "npm":
            return {
                "path": self.path,
                "kind": self.kind,
                "name": self.name,
                "scripts": list(self.scripts),
                "dependencies": self.dependencies,
                "dev_dependencies": self.dev_dependencies,
            }
        if self.kind == "cargo":
            return {
                "path": self.path,
                "kind": self.kind,
                "name": self.name,
                "workspace": self.workspace,
            }
        if self.kind == "python":
            return {"path": self.path, "kind": self.kind, "name": self.name}
        return {"path": self.path, "kind": self.kind}


@dataclass(frozen=True)
class RepoMapRequest:
    root: Path
    max_dirs: int = 40
    max_manifests: int = 40


@dataclass(frozen=True)
class RepoMapResult:
    repo_root: str
    branch: str
    files: int
    bytes: int
    languages: tuple[LanguageCount, ...]
    extensions: tuple[ExtensionCount, ...]
    directories: tuple[DirectoryCount, ...]
    manifests: tuple[Manifest, ...]
    manifests_truncated: bool
    instructions: tuple[str, ...]
    coverage: Coverage
    provenance: str = LEXICAL

    def to_wire(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "branch": self.branch,
            "files": self.files,
            "bytes": self.bytes,
            "languages": [item.to_wire() for item in self.languages],
            "extensions": [item.to_wire() for item in self.extensions],
            "directories": [item.to_wire() for item in self.directories],
            "manifests": [item.to_wire() for item in self.manifests],
            "manifests_truncated": self.manifests_truncated,
            "provenance": self.provenance,
            "coverage": self.coverage.to_wire(),
            "instructions": list(self.instructions),
        }


def _manifest_for(path: Path, rel: str) -> Manifest | None:
    name = path.name.lower()
    if name == "package.json":
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            obj = {}
        return Manifest(
            path=rel,
            kind="npm",
            name=obj.get("name"),
            scripts=tuple(sorted((obj.get("scripts") or {}).keys())[:20]),
            dependencies=len(obj.get("dependencies") or {}),
            dev_dependencies=len(obj.get("devDependencies") or {}),
        )
    if name == "cargo.toml":
        text = path.read_text(encoding="utf-8", errors="replace")
        package = re.search(r"(?ms)^\[package\].*?^name\s*=\s*[\"']([^\"']+)", text)
        workspace = bool(re.search(r"(?m)^\[workspace\]", text))
        return Manifest(
            path=rel,
            kind="cargo",
            name=package.group(1) if package else None,
            workspace=workspace,
        )
    if name == "pyproject.toml":
        text = path.read_text(encoding="utf-8", errors="replace")
        project = re.search(r"(?ms)^\[project\].*?^name\s*=\s*[\"']([^\"']+)", text)
        return Manifest(
            path=rel,
            kind="python",
            name=project.group(1) if project else None,
        )
    if name in {"pnpm-workspace.yaml", "pnpm-workspace.yml"}:
        return Manifest(path=rel, kind="pnpm-workspace")
    return None


def repo_map(request: RepoMapRequest) -> RepoMapResult:
    root = request.root
    files = list_repo_files(root)
    ext_counts: Counter[str] = Counter()
    lang_counts: Counter[str] = Counter()
    dir_counts: Counter[str] = Counter()
    total_bytes = 0
    manifests: list[Manifest] = []
    instructions: list[str] = []
    for rel in files:
        path = root / rel
        ext_counts[path.suffix.lower() or "[none]"] += 1
        lang_counts[language_for(path)] += 1
        parts = Path(rel).parts
        for depth in (1, 2):
            if len(parts) > depth:
                dir_counts["/".join(parts[:depth])] += 1
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        name = path.name.lower()
        if name in {"agents.md", "claude.md", "readme.md", "contributing.md"}:
            instructions.append(rel)
        manifest = _manifest_for(path, rel)
        if manifest is not None:
            manifests.append(manifest)
    branch = run_cmd(
        ["git", "branch", "--show-current"], cwd=root, timeout=5
    ).stdout.strip()
    manifests_truncated = len(manifests) > request.max_manifests
    return RepoMapResult(
        repo_root=str(root),
        branch=branch or "(detached/non-git)",
        files=len(files),
        bytes=total_bytes,
        languages=tuple(
            LanguageCount(language=key, files=value)
            for key, value in lang_counts.most_common(15)
        ),
        extensions=tuple(
            ExtensionCount(extension=key, files=value)
            for key, value in ext_counts.most_common(15)
        ),
        directories=tuple(
            DirectoryCount(path=key, files=value)
            for key, value in dir_counts.most_common(request.max_dirs)
        ),
        manifests=tuple(manifests[: request.max_manifests]),
        manifests_truncated=manifests_truncated,
        instructions=tuple(sorted(instructions)),
        coverage=(
            typed_coverage(SAMPLED, RESULT_LIMIT)
            if manifests_truncated
            else typed_coverage(COMPLETE)
        ),
    )
