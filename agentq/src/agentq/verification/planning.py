"""Shared planning primitives used by every verification provider."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from agentq.core import AgentQError, relpath
from agentq.workspace import ProjectUnit, UnitId, owner_for_file

from .models import (
    CheckKind,
    CheckSpec,
    PackageRow,
    VerifyConfig,
    check_identity,
    check_phase,
)


def dependent_depth(mode: str, dependents: str) -> int | None:
    """How many dependent levels a mode includes; ``None`` means unlimited."""
    if dependents == "none":
        return 0
    if dependents == "direct":
        return 1
    if dependents == "all":
        return None
    # auto
    if mode == "focused":
        return 0
    if mode == "standard":
        return 1
    return None


def package_row(
    name: str,
    directory: str,
    scope: str,
    distance: int,
    local_files: Sequence[str],
    limit: int,
    candidate_tests: Sequence[str],
    *,
    vitest: bool | None = None,
    scripts: Sequence[str] | None = None,
    local_dependencies: Sequence[str] | None = None,
) -> PackageRow:
    return PackageRow(
        name=name,
        directory=directory,
        scope=scope,
        dependent_distance=distance,
        changed=tuple(local_files[:limit]),
        changed_truncated=len(local_files) > limit,
        candidate_tests=tuple(candidate_tests),
        vitest=vitest,
        scripts=tuple(scripts) if scripts is not None else None,
        local_dependencies=(
            tuple(local_dependencies) if local_dependencies is not None else None
        ),
    )


def candidate_tests(
    files: Sequence[str],
    package_dir: Path,
    root: Path,
    all_files: Sequence[str],
    is_test: Callable[[str], Any],
) -> tuple[str, ...]:
    """Every test file whose name or location matches the changed files.

    The full candidate set is returned without an internal cap: providers must
    know the complete target set to decide between a focused check and a
    package-suite widening, and a truncated candidate set could silently omit
    verification targets.
    """
    pkg_rel = relpath(root, package_dir)
    prefix = "" if pkg_rel == "." else pkg_rel.rstrip("/") + "/"
    tests = [path for path in all_files if path.startswith(prefix) and is_test(path)]
    selected: list[str] = []
    changed_stems = {Path(path).stem.split(".")[0].lower() for path in files}
    for test in tests:
        stem = Path(test).stem.split(".")[0].lower()
        if stem in changed_stems or any(
            stem in changed or changed in stem
            for changed in changed_stems
            if len(changed) >= 4
        ):
            selected.append(
                test[len(prefix) :] if prefix and test.startswith(prefix) else test
            )
    return tuple(selected)


def make_check(
    *,
    kind: CheckKind,
    package: str,
    package_key: str,
    cwd: str,
    command: Sequence[str],
    reason: str,
    scope: str,
    distance: int = 0,
) -> CheckSpec:
    argv = tuple(command)
    return CheckSpec(
        check_id=check_identity(cwd, argv),
        kind=kind,
        command=argv,
        package=package,
        package_key=package_key,
        cwd=cwd,
        scope=scope,
        dependent_distance=distance,
        reason=reason,
    )


def group_by_units(
    changed: Sequence[str],
    units: Mapping[UnitId, ProjectUnit],
    config: VerifyConfig,
) -> tuple[dict[UnitId, list[str]], list[str]]:
    """Assign changed files to their deepest owning unit, honoring overrides."""
    grouped: dict[UnitId, list[str]] = {}
    unowned: list[str] = []
    for path in changed:
        owner = _ownership_override(path, owner_for_file(path, units), config, units)
        if owner is None:
            unowned.append(path)
        else:
            grouped.setdefault(owner, []).append(path)
    return grouped, unowned


def _ownership_override(
    path: str,
    default_key: UnitId | None,
    config: VerifyConfig,
    units: Mapping[UnitId, ProjectUnit],
) -> UnitId | None:
    normalized = path.replace("\\", "/").strip("/")
    for prefix, package_name in config.ownership:
        clean = prefix.strip("/")
        if normalized == clean or normalized.startswith(clean + "/"):
            matches = sorted(
                unit_id for unit_id, unit in units.items() if unit.name == package_name
            )
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise AgentQError(
                    f".agentq.toml [ownership] name {package_name!r} matches "
                    f"{len(matches)} units; use a unique package name"
                )
            return default_key  # configured name unknown to this provider
    return default_key


def sorted_checks(
    checks: Sequence[CheckSpec], order_rank: Mapping[str, int]
) -> tuple[CheckSpec, ...]:
    """Order checks by package rank, semantic phase, and kind; drop duplicates."""
    seen: set[tuple[str | None, tuple[str, ...]]] = set()
    ordered: list[CheckSpec] = []
    for check in sorted(
        checks,
        key=lambda item: (
            order_rank.get(item.package_key or "", 9999),
            check_phase(item.kind),
            item.kind.value,
        ),
    ):
        identity = (check.cwd, check.command)
        if identity in seen:
            continue
        seen.add(identity)
        ordered.append(check)
    return tuple(ordered)
