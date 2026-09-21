"""Workspace capability: typed package discovery, graph, and change sets."""

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
    discover_workspace,
    manifest_units,
    matches_pattern,
    nearest_manifest,
    package_manager,
    workspace_patterns,
)
from .graph import DependencyGraph, owner_for_file
from .models import ManifestUnit, Package, PackageManager, Workspace

__all__ = [
    "ChangeSet",
    "DependencyGraph",
    "ManifestUnit",
    "Package",
    "PackageManager",
    "Workspace",
    "changed_files",
    "discover_workspace",
    "find_script",
    "has_vitest",
    "is_docs_only",
    "is_global_change",
    "is_public_contract_change",
    "manifest_units",
    "matches_pattern",
    "nearest_manifest",
    "owner_for_file",
    "package_exec_argv",
    "package_manager",
    "script_argv",
    "workspace_patterns",
]
