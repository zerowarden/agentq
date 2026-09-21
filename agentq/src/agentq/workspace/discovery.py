"""Workspace manifest parsing and package discovery.

This module owns how a repository declares packages: package-manager
detection, workspace glob patterns, manifest parsing, and the discovery walk
that produces a typed :class:`~agentq.workspace.models.Workspace`.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from agentq.core import relpath
from agentq.discovery import PackageManifest, list_repo_files

from .models import ManifestUnit, Package, PackageManager, Workspace

DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def package_manager(root: Path) -> PackageManager:
    if (
        (root / "pnpm-lock.yaml").exists()
        or (root / "pnpm-workspace.yaml").exists()
        or (root / "pnpm-workspace.yml").exists()
    ):
        return PackageManager.PNPM
    if (root / "yarn.lock").exists():
        return PackageManager.YARN
    if (root / "bun.lock").exists() or (root / "bun.lockb").exists():
        return PackageManager.BUN
    return PackageManager.NPM


def _strip_yaml_scalar(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0:1] in {"'", '"'} and value[-1:] == value[0:1]:
        value = value[1:-1]
    else:
        value = value.split(" #", 1)[0].strip()
    return value


def _pnpm_workspace_patterns(root: Path) -> list[str]:
    path = root / "pnpm-workspace.yaml"
    if not path.is_file():
        path = root / "pnpm-workspace.yml"
    if not path.is_file():
        return []
    patterns: list[str] = []
    active = False
    packages_indent = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if not active:
            if re.fullmatch(r"packages\s*:\s*", stripped):
                active = True
                packages_indent = indent
            continue
        if indent <= packages_indent and not stripped.startswith("-"):
            break
        match = re.match(r"^\s*-\s*(.+?)\s*$", raw)
        if match:
            value = _strip_yaml_scalar(match.group(1))
            if value:
                patterns.append(value)
    return patterns


def _package_json_workspace_patterns(root: Path) -> list[str]:
    obj = _read_json(root / "package.json")
    value = obj.get("workspaces")
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    if isinstance(value, dict) and isinstance(value.get("packages"), list):
        return [str(item) for item in value["packages"] if isinstance(item, str)]
    return []


def workspace_patterns(root: Path) -> tuple[str, ...]:
    patterns = _pnpm_workspace_patterns(root) or _package_json_workspace_patterns(root)
    # Preserve order but remove duplicates.
    return tuple(
        dict.fromkeys(
            pattern.strip().rstrip("/") for pattern in patterns if pattern.strip()
        )
    )


def _brace_expand(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    out: list[str] = []
    for item in match.group(1).split(","):
        out.extend(
            _brace_expand(
                pattern[: match.start()] + item.strip() + pattern[match.end() :]
            )
        )
    return out


def matches_pattern(path: str, pattern: str) -> bool:
    path = path.strip("/") or "."
    pattern = pattern.strip().strip("/") or "."
    if pattern == ".":
        return path == "."
    if fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern):
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return False


def _allowed_by_patterns(path: str, patterns: Sequence[str]) -> bool:
    if not patterns:
        return True
    positives: list[str] = []
    negatives: list[str] = []
    for original in patterns:
        negative = original.startswith("!")
        value = original[1:] if negative else original
        for expanded in _brace_expand(value):
            (negatives if negative else positives).append(expanded)
    included = (
        any(matches_pattern(path, pattern) for pattern in positives)
        if positives
        else True
    )
    excluded = any(matches_pattern(path, pattern) for pattern in negatives)
    return included and not excluded


def _package_scripts(obj: dict[str, Any]) -> dict[str, str]:
    scripts_obj = obj.get("scripts")
    return {
        str(key): str(value)
        for key, value in (scripts_obj.items() if isinstance(scripts_obj, dict) else [])
        if isinstance(value, str)
    }


def _dependency_edges(
    obj: dict[str, Any], local_names: set[str]
) -> tuple[frozenset[str], dict[str, tuple[str, ...]], frozenset[str], frozenset[str]]:
    local: set[str] = set()
    declared: set[str] = set()
    runtime: set[str] = set()
    kinds: dict[str, list[str]] = defaultdict(list)
    for field_name in DEPENDENCY_FIELDS:
        value = obj.get(field_name)
        if not isinstance(value, dict):
            continue
        for dependency in value:
            dependency_name = str(dependency)
            declared.add(dependency_name)
            if field_name in {"dependencies", "devDependencies"}:
                runtime.add(dependency_name)
            if dependency_name in local_names:
                local.add(dependency_name)
                kinds[dependency_name].append(field_name)
    return (
        frozenset(local),
        {key: tuple(value) for key, value in kinds.items()},
        frozenset(declared),
        frozenset(runtime),
    )


def discover_workspace(root: Path) -> Workspace:
    """Discover every workspace package declared by the repository manifests."""
    patterns = workspace_patterns(root)
    manifests = [
        rel
        for rel in list_repo_files(root)
        if PurePosixPath(rel).name == "package.json"
    ]
    if (root / "package.json").is_file() and "package.json" not in manifests:
        manifests.insert(0, "package.json")

    raw: list[tuple[str, dict[str, Any], bool]] = []
    for rel in sorted(set(manifests)):
        package_path = PurePosixPath(rel).parent.as_posix()
        package_path = "." if package_path == "." else package_path
        is_root = package_path == "."
        if not is_root and not _allowed_by_patterns(package_path, patterns):
            continue
        obj = _read_json(root / rel)
        if not obj:
            continue
        raw.append((package_path, obj, is_root))

    names = {
        str(obj.get("name")): path
        for path, obj, _ in raw
        if isinstance(obj.get("name"), str) and obj.get("name")
    }
    packages: dict[str, Package] = {}
    for path, obj, is_root in raw:
        name = str(
            obj.get("name") or (root.name if is_root else PurePosixPath(path).name)
        )
        dependencies, dependency_kinds, declared, runtime = _dependency_edges(
            obj, set(names)
        )
        packages[path] = Package(
            path=path,
            name=name,
            root=is_root,
            private=bool(obj.get("private")),
            scripts=_package_scripts(obj),
            dependencies=dependencies,
            dependency_kinds=dependency_kinds,
            declared_dependencies=declared,
            runtime_dependencies=runtime,
        )
    return Workspace(
        manager=package_manager(root), patterns=patterns, packages=packages
    )


def manifest_units(
    root: Path, repo_files: Sequence[str], manifest_name: str
) -> tuple[ManifestUnit, ...]:
    """Locate every ``manifest_name`` file and the package directory it owns."""
    manifests = [rel for rel in repo_files if PurePosixPath(rel).name == manifest_name]
    if (root / manifest_name).is_file() and manifest_name not in manifests:
        manifests.insert(0, manifest_name)
    units: list[ManifestUnit] = []
    for rel in sorted(set(manifests)):
        parent = PurePosixPath(rel).parent.as_posix()
        units.append(
            ManifestUnit(key="." if parent == "." else parent, path=root / rel)
        )
    return tuple(units)


def nearest_manifest(root: Path, target: Path) -> PackageManifest | None:
    """Walk up from target to the repository root looking for a package manifest."""
    current = target if target.is_dir() else target.parent
    while True:
        for name, kind in (
            ("package.json", "npm"),
            ("Cargo.toml", "cargo"),
            ("pyproject.toml", "python"),
        ):
            path = current / name
            if not path.exists():
                continue
            relative = relpath(root, path)
            if name != "package.json":
                return PackageManifest(path=relative, kind=kind)
            try:
                obj = json.loads(path.read_text(encoding="utf-8"))
                scripts = tuple(sorted((obj.get("scripts") or {}).keys()))
            except Exception:
                return PackageManifest(path=relative, kind=kind)
            return PackageManifest(
                path=relative, kind=kind, name=obj.get("name"), scripts=scripts
            )
        if current == root:
            break
        current = current.parent
    return None
