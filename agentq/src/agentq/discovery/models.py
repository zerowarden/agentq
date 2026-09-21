"""Typed models shared across discovery capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FileEntry:
    """One ranked repository file."""

    path: str
    role: str
    language: str

    def to_wire(self) -> dict[str, Any]:
        return {"path": self.path, "role": self.role, "language": self.language}


@dataclass(frozen=True)
class PackageManifest:
    """The nearest owning package manifest for a repository path."""

    path: str
    kind: str
    name: str | None = None
    scripts: tuple[str, ...] | None = None

    def to_wire(self) -> dict[str, Any]:
        data: dict[str, Any] = {"path": self.path, "kind": self.kind}
        if self.kind == "npm" and self.scripts is not None:
            data["name"] = self.name
            data["scripts"] = list(self.scripts)
        return data
