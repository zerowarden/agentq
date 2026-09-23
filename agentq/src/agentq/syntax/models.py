"""Canonical syntax records shared by outline and navigation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OutlineSymbol:
    """One definition discovered by a syntax engine."""

    name: str
    kind: str | None
    file: str
    line: int | None
    signature: str
    scope: str | None = None
    language: str | None = None
    end_line: int | None = None
    column: int | None = None

    def to_wire(self, *, variant: str) -> dict[str, Any]:
        if variant == "python":
            return {
                "name": self.name,
                "kind": self.kind,
                "file": self.file,
                "line": self.line,
                "end_line": self.end_line,
                "column": self.column,
                "signature": self.signature,
                "scope": self.scope,
                "language": self.language,
            }
        if variant == "ctags":
            return {
                "name": self.name,
                "kind": self.kind,
                "file": self.file,
                "line": self.line,
                "signature": self.signature,
                "scope": self.scope,
                "language": self.language,
            }
        return {
            "name": self.name,
            "kind": self.kind,
            "file": self.file,
            "line": self.line,
            "signature": self.signature,
            "language": self.language,
        }


@dataclass(frozen=True)
class OutlineParseError:
    """One file a syntax engine could not parse."""

    path: str
    error: str

    def to_wire(self) -> dict[str, str]:
        return {"path": self.path, "error": self.error}
