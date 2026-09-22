"""Workspace capability: typed unit discovery, project graph, and change sets."""

from __future__ import annotations

from .changes import (
    ChangeSet,
    changed_files,
    is_docs_only,
    is_global_change,
    is_public_contract_change,
)
from .commands import (
    find_script,
    has_vitest,
    package_exec_argv,
    script_argv,
)
from .discovery import (
    discover_node,
    manifest_units,
    matches_pattern,
    nearest_manifest,
    package_manager,
    workspace_patterns,
)
from .ecosystems import (
    CargoWorkspace,
    GoWorkspace,
    PythonTooling,
    PythonWorkspace,
    discover_cargo,
    discover_go,
    discover_python,
    normalize_python_name,
    python_dependency_name,
    read_toml,
)
from .graph import (
    DependencyEdge,
    ProjectGraph,
    ProjectUnit,
    UnitId,
    owner_for_file,
)
from .models import ManifestUnit, NodePackage, NodeWorkspace, PackageManager

__all__ = [
    "CargoWorkspace",
    "ChangeSet",
    "DependencyEdge",
    "GoWorkspace",
    "ManifestUnit",
    "NodePackage",
    "NodeWorkspace",
    "PackageManager",
    "ProjectGraph",
    "ProjectUnit",
    "PythonTooling",
    "PythonWorkspace",
    "UnitId",
    "changed_files",
    "discover_cargo",
    "discover_go",
    "discover_node",
    "discover_python",
    "find_script",
    "has_vitest",
    "is_docs_only",
    "is_global_change",
    "is_public_contract_change",
    "manifest_units",
    "matches_pattern",
    "nearest_manifest",
    "normalize_python_name",
    "owner_for_file",
    "package_exec_argv",
    "package_manager",
    "python_dependency_name",
    "read_toml",
    "script_argv",
    "workspace_patterns",
]
