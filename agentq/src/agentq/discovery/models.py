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
