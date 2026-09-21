"""Typed workspace models: packages, manifests, and discovery results.

A discovered workspace is a mapping of repository-relative package paths to
:class:`Package` units. Graph behavior lives in :mod:`agentq.workspace.graph`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class PackageManager(str, Enum):
    """Node package manager inferred from lock and workspace files."""

    NPM = "npm"
    PNPM = "pnpm"
    YARN = "yarn"
    BUN = "bun"


@dataclass(frozen=True)
class Package:
    """One package unit and its declared dependency edges.

    ``dependencies`` holds names of sibling local packages only;
    ``dependency_kinds`` records which manifest fields declared each edge.
    ``declared_dependencies`` keeps every declared name (local or external) and
    ``runtime_dependencies`` keeps the runtime/dev subset used for root-level
    tooling detection.
    """

    path: str
    name: str
    root: bool = False
    private: bool = False
    scripts: Mapping[str, str] = field(default_factory=dict)
    dependencies: frozenset[str] = frozenset()
    dependency_kinds: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    declared_dependencies: frozenset[str] = frozenset()
    runtime_dependencies: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ManifestUnit:
    """One discovered manifest file and the package directory it owns."""

    key: str
    path: Path


@dataclass(frozen=True)
class Workspace:
    """A discovered package workspace: manager, patterns, and packages."""

    manager: PackageManager
    patterns: tuple[str, ...]
    packages: Mapping[str, Package]
