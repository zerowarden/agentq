"""Python, Cargo, and Go project-unit discovery adapters.

Each adapter parses its ecosystem's manifests and returns a typed workspace
whose ``graph`` is the ecosystem-neutral
:class:`~agentq.workspace.graph.ProjectGraph`. Unit identity is always
``(ecosystem, repository-relative path)``; package names are display metadata
and may be ambiguous, in which case a name resolves to every matching unit.
"""

from __future__ import annotations

import posixpath
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast

import tomllib  # pyright: ignore[reportMissingTypeStubs]

from agentq.core import as_dict, list_field

from .discovery import manifest_units, matches_pattern
from .graph import DependencyEdge, ProjectGraph, ProjectUnit, UnitId

PYTHON = "python"
CARGO = "cargo"
GO = "go"

_PYTHON_NAME_SEPARATORS = re.compile(r"[-_.]+")
_PYTHON_SPEC_SEPARATORS = re.compile(r"[<>=!;@\[ \]]")


def read_toml(path: Path) -> dict[str, Any]:
    try:
        value: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _manifest_path(key: str, manifest_name: str) -> str:
    return manifest_name if key == "." else f"{key}/{manifest_name}"


def normalize_python_name(name: str) -> str:
    """PEP 503 normalization: runs of ``-``, ``_``, and ``.`` are equivalent."""
    return _PYTHON_NAME_SEPARATORS.sub("-", name.strip()).lower()


def python_dependency_name(spec: str) -> str:
    """Normalized distribution name of one dependency requirement string."""
    return normalize_python_name(
        _PYTHON_SPEC_SEPARATORS.split(spec.strip(), maxsplit=1)[0]
    )


@dataclass(frozen=True)
class PythonTooling:
    """Per-unit Python tooling detected from pyproject configuration."""

    pytest: bool = False
    mypy: bool = False
    pyright: bool = False
    ruff: bool = False


@dataclass(frozen=True)
class PythonWorkspace:
    """Discovered pyproject units, their graph, and per-unit tooling."""

    graph: ProjectGraph
    tooling: Mapping[UnitId, PythonTooling] = field(
        default_factory=dict[UnitId, PythonTooling]
    )


def discover_python(root: Path, repo_files: Sequence[str]) -> PythonWorkspace:
    """Discover every pyproject.toml unit and its normalized local edges."""
    manifests = manifest_units(root, repo_files, "pyproject.toml")
    units: dict[UnitId, ProjectUnit] = {}
    tooling: dict[UnitId, PythonTooling] = {}
    dependencies: dict[UnitId, set[str]] = {}
    by_normalized_name: dict[str, list[UnitId]] = defaultdict(list)
    for manifest in manifests:
        key = manifest.key
        obj = read_toml(manifest.path)
        project = as_dict(obj.get("project"))
        name = str(
            project.get("name")
            or (root.name if key == "." else PurePosixPath(key).name)
        )
        unit_id = UnitId(PYTHON, key)
        units[unit_id] = ProjectUnit(
            id=unit_id, name=name, manifest=_manifest_path(key, "pyproject.toml")
        )
        by_normalized_name[normalize_python_name(name)].append(unit_id)
        tools = as_dict(obj.get("tool"))
        declared = {
            python_dependency_name(item)
            for item in (list_field(project, "dependencies"))
            if isinstance(item, str)
        }
        optional = as_dict(project.get("optional-dependencies"))
        for group in optional.values():
            declared.update(
                python_dependency_name(item)
                for item in cast("list[Any]", group)
                if isinstance(item, str)
            )
        dependencies[unit_id] = declared
        tooling[unit_id] = PythonTooling(
            pytest="pytest" in tools or "pytest" in declared,
            mypy="mypy" in tools,
            pyright="pyright" in tools,
            ruff="ruff" in tools,
        )
    edges: list[DependencyEdge] = []
    for unit_id, declared in dependencies.items():
        for dependency in declared:
            for target in by_normalized_name.get(dependency, []):
                if target != unit_id:
                    edges.append(
                        DependencyEdge(
                            source=unit_id, target=target, kind="project.dependencies"
                        )
                    )
    return PythonWorkspace(graph=ProjectGraph.from_units(units, edges), tooling=tooling)


@dataclass(frozen=True)
class CargoWorkspace:
    """Discovered Cargo crates, their graph, and clippy configuration."""

    graph: ProjectGraph
    clippy: Mapping[UnitId, bool] = field(default_factory=dict[UnitId, bool])


def discover_cargo(root: Path, repo_files: Sequence[str]) -> CargoWorkspace:
    """Discover workspace crates and their manifest path dependencies."""
    workspace_obj = (
        read_toml(root / "Cargo.toml") if (root / "Cargo.toml").is_file() else {}
    )
    workspace_section = as_dict(workspace_obj.get("workspace"))
    patterns = [
        str(item)
        for item in list_field(workspace_section, "members")
        if isinstance(item, str)
    ]
    workspace_lints = as_dict(workspace_section.get("lints"))

    manifests: dict[str, dict[str, Any]] = {}
    for manifest in manifest_units(root, repo_files, "Cargo.toml"):
        key = manifest.key
        obj = read_toml(manifest.path)
        package_raw = obj.get("package")
        if not isinstance(package_raw, dict):
            continue  # a virtual workspace manifest does not own files itself
        if (
            key != "."
            and patterns
            and not any(matches_pattern(key, pattern) for pattern in patterns)
        ):
            continue
        manifests[key] = obj

    units: dict[UnitId, ProjectUnit] = {}
    by_path: dict[str, UnitId] = {}
    for key, obj in manifests.items():
        name = str(obj["package"].get("name") or PurePosixPath(key).name)
        unit_id = UnitId(CARGO, key)
        units[unit_id] = ProjectUnit(
            id=unit_id, name=name, manifest=_manifest_path(key, "Cargo.toml")
        )
        by_path[key] = unit_id

    edges: list[DependencyEdge] = []
    clippy: dict[UnitId, bool] = {}
    for key, obj in manifests.items():
        unit_id = by_path[key]
        for section in ("dependencies", "dev-dependencies"):
            table = as_dict(obj.get(section))
            for _dep_name, spec in table.items():
                path_value = as_dict(spec).get("path")
                if not isinstance(path_value, str):
                    continue
                target_key = posixpath.normpath(
                    PurePosixPath(key) / PurePosixPath(path_value)
                )
                target_key = "." if target_key == "." else target_key.removeprefix("./")
                target = by_path.get(target_key)
                if target is not None and target != unit_id:
                    edges.append(
                        DependencyEdge(source=unit_id, target=target, kind=section)
                    )
        crate_lints = as_dict(obj.get("lints"))
        clippy[unit_id] = bool(
            crate_lints.get("clippy") or workspace_lints.get("clippy")
        )
    return CargoWorkspace(graph=ProjectGraph.from_units(units, edges), clippy=clippy)


@dataclass(frozen=True)
class GoWorkspace:
    """Discovered Go modules and their (currently edge-free) graph."""

    graph: ProjectGraph


def discover_go(root: Path, repo_files: Sequence[str]) -> GoWorkspace:
    """Discover Go modules.

    Package-level dependencies inside a module are not modeled yet, so each
    module is one unit with no outgoing edges; consumers must treat module-wide
    checks as the sound fallback.
    """
    units: dict[UnitId, ProjectUnit] = {}
    for manifest in manifest_units(root, repo_files, "go.mod"):
        key = manifest.key
        try:
            content = manifest.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        match = re.search(r"(?m)^module\s+(\S+)\s*$", content)
        name = (
            match.group(1)
            if match
            else (root.name if key == "." else PurePosixPath(key).name)
        )
        unit_id = UnitId(GO, key)
        units[unit_id] = ProjectUnit(
            id=unit_id, name=name, manifest=_manifest_path(key, "go.mod")
        )
    return GoWorkspace(graph=ProjectGraph.from_units(units))
