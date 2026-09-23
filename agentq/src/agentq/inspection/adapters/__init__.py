"""Default adapter composition boundary.

This package is the only place where inspection meets concrete navigation,
discovery, and workspace primitives. Adapters normalize their provider results
into inspection contracts; the service receives a registry assembled here (or
by a test) and never imports an adapter itself.
"""

from __future__ import annotations

from pathlib import Path

from ..capabilities import CapabilityRegistry
from ..contracts import CapabilityHandler, SourceVersionReader
from ..lifecycle import content_version
from .python import PythonInspectionAdapter
from .repository import RepositoryInspectionAdapter
from .typescript import TypeScriptInspectionAdapter

__all__ = [
    "CapabilityRegistry",
    "PythonInspectionAdapter",
    "RepositoryInspectionAdapter",
    "TypeScriptInspectionAdapter",
    "default_handlers",
    "default_registry",
    "filesystem_version_reader",
]


def default_handlers() -> tuple[CapabilityHandler, ...]:
    """The production adapter set; explicit and replaceable."""
    return (
        TypeScriptInspectionAdapter(),
        PythonInspectionAdapter(),
        RepositoryInspectionAdapter(),
    )


def default_registry() -> CapabilityRegistry:
    return CapabilityRegistry(default_handlers())


def filesystem_version_reader(root: Path) -> SourceVersionReader:
    """Read repository content versions for lifecycle validation."""

    def read(relative_path: str) -> str | None:
        try:
            return content_version((root / relative_path).read_bytes())
        except OSError:
            return None

    return read
