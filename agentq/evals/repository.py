"""Repository snapshots for capture provenance and acceptance checks.

A snapshot records the checkout's commit, tree, and manifest digests. The
acceptance check recomputes them, so a source or configuration change after a
capture is detected instead of silently replayed.
"""

from __future__ import annotations

import re
from pathlib import Path

from agentq.core import AgentQError, canonical_digest
from agentq.execution import run_cmd

from .models import RepositorySnapshot

CONFIGURATION_NAMES = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "requirements.txt",
        "package.json",
        "tsconfig.json",
        "deno.json",
        "Makefile",
    }
)

_INDEX_ENTRY = re.compile(
    r"^(?P<mode>\d{6}) (?P<blob>[0-9a-f]{40,64}) \d+\t(?P<path>.+)$"
)


class RepositoryError(RuntimeError):
    """The checkout cannot be read as a Git repository."""


def _git(root: Path, *args: str) -> str:
    if not root.is_dir():
        raise RepositoryError(f"checkout is missing: {root}")
    try:
        result = run_cmd(["git", *args], cwd=root, timeout=30)
    except (AgentQError, OSError) as exc:
        raise RepositoryError(f"git could not run in {root}: {exc}") from exc
    if result.returncode != 0:
        raise RepositoryError(
            f"git {' '.join(args)} failed in {root}: {result.stderr.strip()}"
        )
    return result.stdout


def _entries(root: Path) -> tuple[tuple[str, str, str], ...]:
    entries: list[tuple[str, str, str]] = []
    for line in _git(root, "ls-files", "-s").splitlines():
        match = _INDEX_ENTRY.match(line)
        if match is None:
            raise RepositoryError(f"unrecognized git index entry: {line!r}")
        entries.append((match["mode"], match["blob"], match["path"]))
    return tuple(sorted(entries, key=lambda item: item[2]))


def _digest(entries: tuple[tuple[str, str, str], ...]) -> str:
    return canonical_digest({"files": [list(item) for item in entries]})


def _is_configuration(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in CONFIGURATION_NAMES or name.startswith(".")


def repository_snapshot(root: Path, *, repo_id: str) -> RepositorySnapshot:
    """The current immutable identity of one clean checkout.

    A dirty working tree has no immutable identity: it is rejected rather than
    silently captured, so acceptance cannot pass on uncommitted edits.
    """
    if _git(root, "status", "--porcelain").strip():
        raise RepositoryError(f"checkout is not clean: {root}")
    entries = _entries(root)
    source = tuple(item for item in entries if not _is_configuration(item[2]))
    configuration = tuple(item for item in entries if _is_configuration(item[2]))
    return RepositorySnapshot(
        repo_id=repo_id,
        commit=_git(root, "rev-parse", "HEAD").strip(),
        tree=_git(root, "rev-parse", "HEAD^{tree}").strip(),
        source_manifest_digest=_digest(source),
        configuration_manifest_digest=_digest(configuration),
    )


def snapshot_accepts(snapshot: RepositorySnapshot, root: Path) -> bool:
    return snapshot_mismatch(snapshot, root) is None


def snapshot_mismatch(snapshot: RepositorySnapshot, root: Path) -> str | None:
    """A readable reason the checkout no longer matches its snapshot."""
    try:
        current = repository_snapshot(root, repo_id=snapshot.repo_id)
    except RepositoryError as exc:
        return str(exc)
    if current.commit != snapshot.commit:
        return f"commit changed: {snapshot.commit[:12]} -> {current.commit[:12]}"
    if current.tree != snapshot.tree:
        return "tree changed"
    if current.source_manifest_digest != snapshot.source_manifest_digest:
        return "source files changed"
    if (
        current.configuration_manifest_digest
        != snapshot.configuration_manifest_digest
    ):
        return "configuration files changed"
    return None


def repo_identity(
    *, kind: str, root: str, repo: str = "", commit: str = ""
) -> str:
    """A machine-independent repository id for one declared source."""
    if repo and commit:
        return canonical_digest(
            {"kind": kind, "repo": repo, "commit": commit}, length=16
        )
    return canonical_digest({"kind": kind, "root": root}, length=16)
