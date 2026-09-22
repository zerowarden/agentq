"""Python, Cargo, and Go project-unit discovery adapters.

Each adapter parses its ecosystem's manifests and returns a typed workspace
whose ``graph`` is the ecosystem-neutral
:class:`~agentq.workspace.graph.ProjectGraph`. Unit identity is always
``(ecosystem, repository-relative path)``; package names are display metadata
and may be ambiguous, in which case a name resolves to every matching unit.
"""

from __future__ import annotations

import json
import posixpath
import re
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
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


_CARGO_SECTIONS = ("dependencies", "dev-dependencies", "build-dependencies")
_CARGO_DEPENDENCY_KINDS = {
    "dev": "dev-dependencies",
    "build": "build-dependencies",
}


def discover_cargo(root: Path, repo_files: Sequence[str]) -> CargoWorkspace:
    """Discover workspace crates and their local dependency edges.

    Cargo's own metadata is authoritative when cargo is available locally
    (``--offline --no-deps``: no resolution, no network). Manifest parsing is
    the fallback and models the same local forms: direct path dependencies,
    workspace-inherited dependencies (``dep.workspace = true`` resolved through
    ``[workspace.dependencies]``), dev/build dependencies, and target-specific
    dependency tables.
    """
    if not (root / "Cargo.toml").is_file() and not any(
        PurePosixPath(rel).name == "Cargo.toml" for rel in repo_files
    ):
        return CargoWorkspace(graph=ProjectGraph.from_units({}))
    metadata = _cargo_metadata(root)
    if metadata is not None:
        workspace = _cargo_workspace_from_metadata(root, metadata)
        if workspace is not None:
            return workspace
    return _cargo_workspace_from_manifests(root, repo_files)


def _cargo_metadata(root: Path) -> dict[str, Any] | None:
    """Local ``cargo metadata --no-deps`` output, or ``None`` when unavailable."""
    cargo = shutil.which("cargo")
    if cargo is None:
        return None
    try:
        result = subprocess.run(
            [cargo, "metadata", "--no-deps", "--format-version", "1", "--offline"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        data: Any = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _cargo_key_from_path(root: Path, path: Path) -> str | None:
    """Repository-relative crate key of a manifest file or crate directory.

    ``cargo metadata`` reports member ``manifest_path`` values (``Cargo.toml``
    files) and dependency ``path`` values (crate directories), so both shapes
    normalize to the directory key used by :class:`UnitId`.
    """
    try:
        relative = path.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    if relative.name == "Cargo.toml":
        relative = relative.parent
    key = relative.as_posix()
    return "." if key == "." else key


def _cargo_workspace_from_metadata(
    root: Path, data: dict[str, Any]
) -> CargoWorkspace | None:
    """Normalize ``cargo metadata`` members and local edges into a graph."""
    packages_raw = data.get("packages")
    if not isinstance(packages_raw, list) or not packages_raw:
        return None
    packages = cast("list[Any]", packages_raw)
    resolved_root = root.resolve()
    built = _cargo_metadata_units(resolved_root, packages)
    if built is None:
        return None
    units, by_key = built
    edges = [
        edge
        for package in packages
        for edge in _cargo_metadata_edges(resolved_root, package, by_key)
    ]
    return CargoWorkspace(
        graph=ProjectGraph.from_units(units, edges),
        clippy=_cargo_clippy(root, units.values()),
    )


def _cargo_metadata_units(
    resolved_root: Path, packages: Sequence[Any]
) -> tuple[dict[UnitId, ProjectUnit], dict[str, UnitId]] | None:
    """Workspace-member units keyed by their manifest directory."""
    units: dict[UnitId, ProjectUnit] = {}
    by_key: dict[str, UnitId] = {}
    for package in packages:
        if not isinstance(package, dict):
            return None
        entry = cast("dict[str, Any]", package)
        manifest = entry.get("manifest_path")
        name = entry.get("name")
        if not isinstance(manifest, str) or not isinstance(name, str):
            return None
        key = _cargo_key_from_path(resolved_root, Path(manifest))
        if key is None:
            continue
        unit_id = UnitId(CARGO, key)
        units[unit_id] = ProjectUnit(
            id=unit_id, name=name, manifest=_manifest_path(key, "Cargo.toml")
        )
        by_key[key] = unit_id
    if not units:
        return None
    return units, by_key


def _cargo_metadata_edges(
    resolved_root: Path, package: Any, by_key: Mapping[str, UnitId]
) -> list[DependencyEdge]:
    """Local dependency edges declared by one metadata package."""
    if not isinstance(package, dict):
        return []
    entry = cast("dict[str, Any]", package)
    manifest = entry.get("manifest_path")
    if not isinstance(manifest, str):
        return []
    key = _cargo_key_from_path(resolved_root, Path(manifest))
    source = by_key.get(key) if key is not None else None
    dependencies_raw = entry.get("dependencies")
    if source is None or not isinstance(dependencies_raw, list):
        return []
    edges: list[DependencyEdge] = []
    for dependency in cast("list[Any]", dependencies_raw):
        edge = _cargo_metadata_edge(resolved_root, source, dependency, by_key)
        if edge is not None:
            edges.append(edge)
    return edges


def _cargo_metadata_edge(
    resolved_root: Path,
    source: UnitId,
    dependency: Any,
    by_key: Mapping[str, UnitId],
) -> DependencyEdge | None:
    if not isinstance(dependency, dict):
        return None
    entry = cast("dict[str, Any]", dependency)
    path = entry.get("path")
    if not isinstance(path, str):
        return None
    target_key = _cargo_key_from_path(resolved_root, Path(path))
    target = by_key.get(target_key) if target_key is not None else None
    if target is None or target == source:
        return None
    kind = entry.get("kind")
    return DependencyEdge(
        source=source,
        target=target,
        kind=_CARGO_DEPENDENCY_KINDS.get(str(kind), "dependencies"),
    )


def _cargo_workspace_from_manifests(
    root: Path, repo_files: Sequence[str]
) -> CargoWorkspace:
    """Manifest-level fallback discovery for environments without cargo."""
    workspace_obj = (
        read_toml(root / "Cargo.toml") if (root / "Cargo.toml").is_file() else {}
    )
    workspace_section = as_dict(workspace_obj.get("workspace"))
    patterns = [
        str(item)
        for item in list_field(workspace_section, "members")
        if isinstance(item, str)
    ]
    workspace_dependencies = as_dict(workspace_section.get("dependencies"))

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
    for key, obj in manifests.items():
        unit_id = by_path[key]
        for section, dependency_name, spec in _cargo_dependency_declarations(obj):
            path_value, inherited = _cargo_local_path(
                spec, dependency_name, workspace_dependencies
            )
            if path_value is None:
                continue
            target = by_path.get(
                _cargo_resolve_key("." if inherited else key, path_value)
            )
            if target is not None and target != unit_id:
                edges.append(
                    DependencyEdge(source=unit_id, target=target, kind=section)
                )
    return CargoWorkspace(
        graph=ProjectGraph.from_units(units, edges),
        clippy=_cargo_clippy(root, units.values()),
    )


def _cargo_dependency_declarations(
    obj: dict[str, Any],
) -> Iterator[tuple[str, str, Any]]:
    """Every dependency declaration: standard sections plus target-specific ones."""
    for section in _CARGO_SECTIONS:
        for name, spec in as_dict(obj.get(section)).items():
            yield section, name, spec
    for target_table in as_dict(obj.get("target")).values():
        target_obj = as_dict(target_table)
        for section in _CARGO_SECTIONS:
            for name, spec in as_dict(target_obj.get(section)).items():
                yield section, name, spec


def _cargo_local_path(
    spec: Any, name: str, workspace_dependencies: Mapping[str, Any]
) -> tuple[str | None, bool]:
    """Local ``path`` of a declaration and whether it is inherited from the root."""
    entry = as_dict(spec)
    if entry.get("workspace") is True:
        inherited = as_dict(workspace_dependencies.get(name))
        path = inherited.get("path")
        return (path, True) if isinstance(path, str) else (None, True)
    path = entry.get("path")
    return (path, False) if isinstance(path, str) else (None, False)


def _cargo_resolve_key(base_key: str, path_value: str) -> str:
    resolved = posixpath.normpath(PurePosixPath(base_key) / PurePosixPath(path_value))
    return "." if resolved == "." else resolved.removeprefix("./")


def _cargo_clippy(root: Path, units: Iterable[ProjectUnit]) -> dict[UnitId, bool]:
    workspace_obj = (
        read_toml(root / "Cargo.toml") if (root / "Cargo.toml").is_file() else {}
    )
    workspace_lints = as_dict(as_dict(workspace_obj.get("workspace")).get("lints"))
    clippy: dict[UnitId, bool] = {}
    for unit in units:
        obj = read_toml(root / unit.manifest) if unit.manifest else {}
        crate_lints = as_dict(obj.get("lints"))
        clippy[unit.id] = bool(
            crate_lints.get("clippy") or workspace_lints.get("clippy")
        )
    return clippy


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
