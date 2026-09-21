"""Go verification provider: module units and package-level test targets."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from agentq.core import relpath
from agentq.workspace import ChangeSet, DependencyGraph, Package, manifest_units

from ..models import CheckKind, CheckSpec, ProviderPlan, VerifyConfig
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
        contract_changed: bool,
        config: VerifyConfig,
    ) -> ProviderPlan:
        units = self._packages(root, repo_files)
        graph = DependencyGraph.from_packages(units)
        depth = dependent_depth(mode, dependents)
        grouped, unowned = group_by_units(changes.files, units, config)
        direct_changed_keys = set(grouped)
        docs_only = changes.docs_only
        distances = (
            {}
            if depth == 0
            else graph.dependents(set(direct_changed_keys), depth=depth)
        )
        ordered = graph.dependency_order(set(direct_changed_keys))
        order_rank = {key: index for index, key in enumerate(ordered)}

        rows = []
        checks: list[CheckSpec] = []
        for key in ordered:
            unit = units[key]
            unit_dir = root if key == "." else root / key
            owned_files = sorted(grouped.get(key, []))
            local_files = [relpath(unit_dir, root / path) for path in owned_files]
            module_changed = any(
                PurePosixPath(path).name == "go.mod"
                and (PurePosixPath(path).parent.as_posix() or ".") == key
                for path in owned_files
            )
            scope = (
                "global"
                if module_changed
                else "changed"
                if key in direct_changed_keys
                else "dependent"
            )
            distance = int(distances.get(key, 0))
            rows.append(
                package_row(
                    unit.name, key, scope, distance, local_files, limit, ()
                )
            )
            if docs_only:
                continue
            checks.extend(
                self._module_checks(unit, key, scope, local_files, module_changed)
            )

        notes = [
            "Go checks run package-level tests; cross-package effects are not inferred."
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
                sorted(units[key].name for key in direct_changed_keys)
            ),
            dependent_packages=tuple(sorted(units[key].name for key in distances)),
            affected_packages=tuple(
                units[key].name for key in ordered if key in units
            ),
            packages=tuple(rows),
            checks=sorted_checks(checks, order_rank),
            notes=tuple(notes),
            workspace_packages=len(units),
            workspace_edges=graph.edges(),
        )

    def _packages(self, root: Path, repo_files: Sequence[str]) -> dict[str, Package]:
        units: dict[str, Package] = {}
        for manifest in manifest_units(root, repo_files, "go.mod"):
            try:
                content = manifest.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
            match = re.search(r"(?m)^module\s+(\S+)\s*$", content)
            name = (
                match.group(1)
                if match
                else (
                    root.name
                    if manifest.key == "."
                    else PurePosixPath(manifest.key).name
                )
            )
            units[manifest.key] = Package(path=manifest.key, name=name)
        return units

    def _module_checks(
        self,
        unit: Package,
        key: str,
        scope: str,
        local_files: Sequence[str],
        module_changed: bool,
    ) -> list[CheckSpec]:
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
                    priority=30,
                    scope="global",
                )
            ]
        package_dirs = sorted(
            {
                str(PurePosixPath(path).parent)
                for path in local_files
                if path.endswith(".go")
            }
        )
        targets = [
            "./..." if directory == "." else f"./{directory.removeprefix('./')}/..."
            for directory in package_dirs
        ]
        if not targets:
            return []
        return [
            make_check(
                kind=CheckKind.PACKAGE_TESTS,
                package=unit.name,
                package_key=key,
                cwd=key,
                command=["go", "test", *targets],
                reason="changed Go files belong to these packages",
                priority=20,
                scope=scope,
            )
        ]
