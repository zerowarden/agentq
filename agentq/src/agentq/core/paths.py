from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import AgentQError


@dataclass(frozen=True)
class RepoPath:
    """A filesystem path confined to a single repository root.

    The absolute form is resolved (symlinks collapsed) and the relative form is
    computed exactly once so every downstream provider reuses the same value.
    """

    root: Path
    absolute: Path
    relative: str


@dataclass(frozen=True)
class RepoScope:
    """A normalized repository scope: a root plus a confined :class:`RepoPath`."""

    root: Path
    path: RepoPath


def _resolve_absolute(
    root: Path, value: str | Path, *, allow_outside: bool = False
) -> Path:
    """Resolve *value* against *root* and return an absolute, symlink-resolved path.

    A resolved path is accepted only when it equals the root or is a descendant
    of the root, unless ``allow_outside`` is explicitly set (the distinct
    capability used only by ``read --allow-outside``).
    """
    root_abs = Path(root).expanduser().resolve()
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else (root_abs / raw)
    resolved = candidate.resolve()
    if not allow_outside and resolved != root_abs and root_abs not in resolved.parents:
        raise AgentQError(f"path is outside repository root: {resolved}")
    return resolved


def resolve_repo_path(
    root: Path, value: str | Path, *, must_exist: bool = False
) -> RepoPath:
    """Normalize a single user-supplied path into a confined :class:`RepoPath`.

    Symlinks that escape the repository are rejected; symlinks that stay inside
    are accepted because their resolved target remains a descendant of the root.
    """
    root_abs = Path(root).expanduser().resolve()
    absolute = _resolve_absolute(root_abs, value, allow_outside=False)
    if must_exist and not absolute.exists():
        raise AgentQError(f"path does not exist within repository: {value}")
    relative = (
        "." if absolute == root_abs else absolute.relative_to(root_abs).as_posix()
    )
    return RepoPath(root=root_abs, absolute=absolute, relative=relative)


def resolve_repo_scopes(root: Path, values: Sequence[str | Path]) -> list[RepoScope]:
    """Normalize a list of scopes (defaulting to the repository root).

    Every scope is confined and required to exist before any provider receives it.
    """
    root_abs = Path(root).expanduser().resolve()
    items: list[str | Path] = list(values) if values else ["."]
    scopes: list[RepoScope] = []
    for value in items:
        rp = resolve_repo_path(root_abs, value, must_exist=True)
        scopes.append(RepoScope(root=root_abs, path=rp))
    return scopes


def normalize_scopes_for_wire(root: Path, values: Sequence[str | Path]) -> list[str]:
    """Return the provider wire form for *values*: repo-relative POSIX scopes.

    The wire form is canonical: the repository root is exactly ``"."``, entries
    have no trailing slash, absolute form, ``..`` or empty-path synonym. Input
    is confined and required to exist via :func:`resolve_repo_scopes`; the
    absolute root stays a distinct field on the request and never appears on
    the wire as an absolute path.
    """
    return [scope.path.relative for scope in resolve_repo_scopes(root, values)]


def scope_match(path: str, scopes: list[str]) -> bool:
    if not scopes or scopes == ["."]:
        return True
    normalized = path.replace(os.sep, "/")
    for scope in scopes:
        value = scope.replace(os.sep, "/")
        while value.startswith("./"):
            value = value[2:]
        value = value.rstrip("/")
        if (
            value in {"", "."}
            or normalized == value
            or normalized.startswith(value + "/")
        ):
            return True
    return False


def ensure_within(root: Path, path: Path, *, allow_outside: bool = False) -> Path:
    return _resolve_absolute(root, path, allow_outside=allow_outside)


def relpath(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())
