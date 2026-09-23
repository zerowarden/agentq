"""Shared adapter helpers: scopes, coordinates, and cached source reads.

Adapters convert their provider's native offsets into the canonical inspection
coordinate convention: one-based lines and columns, columns measured in Unicode
code points, end positions exclusive. TypeScript reports UTF-16 code units;
the Python AST reports UTF-8 byte offsets.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agentq.core import (
    PARTIAL,
    PROVIDER_ERROR,
    Coverage,
    Diagnostic,
    typed_coverage,
)
from agentq.core.languages import language_id_for
from agentq.discovery import list_repo_files

from ..contracts import (
    CandidateTarget,
    CapabilityResult,
    CollectionStatus,
    InspectionTarget,
    Observation,
    SymbolTarget,
)
from ..lifecycle import content_version

TS_JS_LANGUAGES = frozenset({"typescript", "tsx", "javascript", "jsx"})
PYTHON_LANGUAGES = frozenset({"python"})

FileLister = Callable[[Path], Sequence[str]]


def symbol_of(target: InspectionTarget) -> str | None:
    """The symbol name a symbol-like target names, if any."""
    if isinstance(target, SymbolTarget):
        return target.name
    if isinstance(target, CandidateTarget):
        return target.symbol
    return None


def failed_result(message: str, *, code: str = PROVIDER_ERROR) -> CapabilityResult:
    return CapabilityResult(
        status=CollectionStatus.FAILED,
        coverage=typed_coverage(PARTIAL, code),
        diagnostics=(Diagnostic(message=message, code=code),),
    )


def status_for(
    observations: tuple[Observation, ...], coverage: Coverage
) -> CollectionStatus:
    """Completed evidence, a complete empty outcome, or a bounded partial one."""
    if observations:
        return CollectionStatus.COMPLETED
    return (
        CollectionStatus.EMPTY if coverage.is_complete() else CollectionStatus.PARTIAL
    )


def with_skipped(coverage: Coverage, skipped: int) -> Coverage:
    """Unversioned evidence is unattributed; it can never claim completeness."""
    if skipped:
        return coverage.with_failure("unattributed", status=PARTIAL)
    return coverage


def target_scopes(target: InspectionTarget) -> tuple[str, ...]:
    """The repository-relative scopes a target names."""
    return target.scope_paths


def scoped_languages(
    root: Path,
    scopes: tuple[str, ...],
    *,
    lister: FileLister = list_repo_files,
) -> frozenset[str] | None:
    """Languages present in explicit scopes; ``None`` when not enumerable.

    A scope that names a file directly is answered from its suffix. Directory
    scopes enumerate repository files once; a scope matching no file leaves the
    language set unknown rather than excluding an adapter on a guess.
    """
    if not scopes:
        return None
    direct = {language_id_for(scope) for scope in scopes}
    if None not in direct:
        return frozenset(item for item in direct if item is not None)
    files = lister(root)
    languages: set[str] = set()
    for scope in scopes:
        prefix = scope.rstrip("/") + "/"
        scoped = [item for item in files if item == scope or item.startswith(prefix)]
        if not scoped:
            return frozenset()
        languages.update(
            language for item in scoped if (language := language_id_for(item))
        )
    return frozenset(languages)


@dataclass
class SourceCache:
    """Per-inspection text, line, and version cache for repository files."""

    root: Path
    _loaded: dict[str, tuple[str, str] | None] = field(
        default_factory=dict[str, tuple[str, str] | None]
    )

    def _load(self, relative: str) -> tuple[str, str] | None:
        if relative not in self._loaded:
            try:
                data = (self.root / relative).read_bytes()
            except OSError:
                self._loaded[relative] = None
            else:
                self._loaded[relative] = (
                    data.decode("utf-8", errors="replace"),
                    content_version(data),
                )
        return self._loaded[relative]

    def text(self, relative: str) -> str | None:
        loaded = self._load(relative)
        return loaded[0] if loaded is not None else None

    def lines(self, relative: str) -> tuple[str, ...] | None:
        text = self.text(relative)
        return None if text is None else tuple(text.splitlines())

    def version(self, relative: str) -> str | None:
        loaded = self._load(relative)
        return loaded[1] if loaded is not None else None


def utf16_column_to_code_points(line: str, utf16_column: int) -> int:
    """Convert a one-based UTF-16 code-unit column to a code-point column."""
    if utf16_column <= 1:
        return max(1, utf16_column)
    target = utf16_column - 1
    units = 0
    for index, char in enumerate(line):
        if units >= target:
            return index + 1
        units += 2 if ord(char) > 0xFFFF else 1
    return len(line) + 1


def code_point_column_to_utf16(line: str, column: int) -> int:
    """Convert a one-based code-point column to a UTF-16 code-unit column."""
    if column <= 1:
        return max(1, column)
    units = 0
    for index, char in enumerate(line):
        if index + 1 >= column:
            return units + 1
        units += 2 if ord(char) > 0xFFFF else 1
    return units + 1


def utf8_byte_column_to_code_points(line: str, byte_offset: int) -> int:
    """Convert a zero-based UTF-8 byte offset to a one-based code-point column."""
    if byte_offset <= 0:
        return 1
    consumed = 0
    points = 0
    for char in line:
        size = len(char.encode("utf-8"))
        if consumed + size > byte_offset:
            break
        consumed += size
        points += 1
    return points + 1
