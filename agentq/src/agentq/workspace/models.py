"""Typed workspace models: Node packages, manifests, and discovery results.

Node-specific metadata stays in :class:`NodePackage` / :class:`NodeWorkspace`.
Unit identity and dependency edges live in the ecosystem-neutral
:class:`~agentq.workspace.graph.ProjectGraph`, keyed by
:class:`~agentq.workspace.graph.UnitId`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .graph import ProjectGraph, UnitId


class PackageManager(str, Enum):
    """Node package manager inferred from lock and workspace files."""

    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"
    BUN = "bun"


@dataclass(frozen=True)
class NodePackage:
    """Node-only package metadata; the graph owns identity and edges.

    ``declared_dependencies`` keeps every declared name (local or external) and
    ``runtime_dependencies`` keeps the runtime/dev subset used for root-level
    tooling detection.
    """

    name: str
    root: bool = False
    private: bool = False
    scripts: Mapping[str, str] = field(default_factory=dict[str, str])
    declared_dependencies: frozenset[str] = frozenset()
    runtime_dependencies: frozenset[str] = frozenset()


@dataclass(frozen=True)
class NodeWorkspace:
    """A discovered Node workspace: manager, patterns, graph, and metadata."""

    manager: PackageManager
    patterns: tuple[str, ...]
    graph: ProjectGraph
    packages: Mapping[UnitId, NodePackage]


@dataclass(frozen=True)
class ManifestUnit:
    """One discovered manifest file and the package directory it owns."""

    key: str
    path: Path
