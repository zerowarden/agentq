"""Go verification provider: module units and module-wide test targets."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from agentq.core import relpath
from agentq.workspace import ChangeSet, ProjectUnit, UnitId, discover_go

from ..models import CheckKind, CheckSpec, PackageRow, ProviderPlan, VerifyConfig
from ..planning import (
    dependent_depth,
    group_by_units,
    make_check,
    package_row,
    sorted_checks,
)


class GoVerificationProvider:
    name = "go"
    manager = "go"

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool:
        return (root / "go.mod").is_file() or any(
            PurePosixPath(rel).name == "go.mod" for rel in repo_files
        )

    def plan(
        self,
        root: Path,
        *,
        repo_files: Sequence[str],
        changes: ChangeSet,
        limit: int,
        mode: str,
        dependents: str,
        include_build: bool,
        config: VerifyConfig,
    ) -> ProviderPlan:
        workspace = discover_go(root, repo_files)
        graph = workspace.graph
        units = graph.units
        depth = dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changes.files, units, config)
        direct_changed = set(grouped)
        docs_only = changes.docs_only
        distances = {} if depth == 0 else graph.dependents(direct_changed, depth=depth)
        ordered = graph.dependency_order(direct_changed | set(distances))
        order_rank = {unit_id.path: index for index, unit_id in enumerate(ordered)}

        rows: list[PackageRow] = []
        checks: list[CheckSpec] = []
        for unit_id in ordered:
            unit = units[unit_id]
            unit_dir = root if unit_id.path == "." else root / unit_id.path
            owned_files = sorted(grouped.get(unit_id, []))
            local_files = [relpath(unit_dir, root / path) for path in owned_files]
            module_changed = any(
                PurePosixPath(path).name == "go.mod"
                and (PurePosixPath(path).parent.as_posix() or ".") == unit_id.path
                for path in owned_files
            )
            scope = (
                "global"
                if module_changed
                else "changed" if unit_id in direct_changed else "dependent"
            )
            distance = int(distances.get(unit_id, 0))
            rows.append(
                package_row(
                    unit.name, unit_id.path, scope, distance, local_files, limit, ()
                )
            )
            if docs_only:
                continue
            checks.extend(
                self._module_checks(unit, unit_id, scope, local_files, module_changed)
            )

        notes = [
            "Go checks run module-wide (go test ./...) because package reverse "
            "dependencies are not inferred yet."
        ]
        if docs_only:
            notes.append(
                "All detected changes are documentation-like; no code verification step was inferred."
            )
        if unowned:
            notes.append(
                "Some changed files are not owned by a discovered go.mod module."
            )
        return ProviderPlan(
            provider=self.name,
            manager=self.manager,
            docs_only=docs_only,
            changed_files=tuple(changes.files),
            global_changes=(),
            unowned=tuple(unowned),
            changed_packages=tuple(
                sorted(units[unit_id].name for unit_id in direct_changed)
            ),
            dependent_packages=tuple(
                sorted(units[unit_id].name for unit_id in distances)
            ),
            affected_packages=tuple(units[unit_id].name for unit_id in ordered),
            packages=tuple(rows),
            checks=sorted_checks(checks, order_rank),
            notes=tuple(notes),
            workspace_packages=len(units),
            workspace_edges=graph.edge_count(),
            limitations=(
                "Go package reverse dependencies are not modelled, so checks "
                "stay module-wide",
            ),
        )

    def _module_checks(
        self,
        unit: ProjectUnit,
        unit_id: UnitId,
        scope: str,
        local_files: Sequence[str],
        module_changed: bool,
    ) -> list[CheckSpec]:
        """Plan module-wide Go tests.

        Package reverse dependencies are not inferred yet, so a narrower
        ``go test ./pkg/...`` could miss consumers of a changed exported
        identifier. Until package dependencies are derived (for example from
        ``go list``), the sound fallback is to test the whole module.
        """
        key = unit_id.path
        if module_changed:
            return [
                make_check(
                    kind=CheckKind.MODULE_TESTS,
                    package=unit.name,
                    package_key=key,
                    cwd=key,
                    command=["go", "test", "./..."],
                    reason=(
                        "the module definition changed, so the whole module is verified"
                    ),
                    scope="global",
                )
            ]
        if not any(path.endswith(".go") for path in local_files):
            return []
        return [
            make_check(
                kind=CheckKind.PACKAGE_TESTS,
                package=unit.name,
                package_key=key,
                cwd=key,
                command=["go", "test", "./..."],
                reason=(
                    "changed Go files can alter exported identifiers; package "
                    "reverse dependencies are not inferred, so the whole module "
                    "is tested"
                ),
                scope=scope,
            )
        ]
