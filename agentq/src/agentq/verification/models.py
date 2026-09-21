"""Typed verification contracts: checks, plans, plans-as-documents, runs.

A :class:`ProviderPlan` is one ecosystem's typed contribution. The
:class:`VerificationPlan` is the merged plan document and the only source for
the ``test-plan`` wire projection. :class:`VerificationRun` carries the
executed :class:`CheckResult` records for the ``verify`` wire projection.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from agentq.core import (
    COMPLETE,
    HEURISTIC,
    RESULT_LIMIT,
    SAMPLED,
    STEP_LIMIT,
    ContractError,
    Coverage,
    canonical_digest,
    optional_int,
    optional_str,
    require_int,
    require_str,
    require_unique_strings,
    typed_coverage,
)
from agentq.workspace import ChangeSet

VERIFICATION_PLAN_SCHEMA = "agentq.verification-plan/v1"
CHECK_RESULT_SCHEMA = "agentq.check-result/v1"


class CheckKind(str, Enum):
    """The verification reason a check exists."""

    TEST = "test"
    DIRECT_TESTS = "direct-tests"
    RELATED_TESTS = "related-tests"
    CANDIDATE_TESTS = "candidate-tests"
    PACKAGE_TESTS = "package-tests"
    MODULE_TESTS = "module-tests"
    DEPENDENT_TESTS = "dependent-tests"
    TYPECHECK = "typecheck"
    DEPENDENT_TYPECHECK = "dependent-typecheck"
    LINT = "lint"
    DEPENDENT_LINT = "dependent-lint"
    BUILD = "build"
    DEPENDENT_BUILD = "dependent-build"
    CONFIGURED = "configured"
    CUSTOM = "custom"


class CheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    ERROR = "error"


class VerificationStatus(str, Enum):
    CLEAN = "clean"
    PLANNED = "planned"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED_DOCS = "skipped-docs"
    UNVERIFIED = "unverified"


def check_identity(cwd: str | None, command: tuple[str, ...]) -> str:
    """Deterministic identity of one check: its working directory and argv."""
    return canonical_digest({"cwd": cwd, "argv": list(command)}, length=16)


def verification_plan_id(
    checks: tuple[CheckSpec, ...], mode: str | None = None
) -> str:
    """Deterministic identity for a complete check plan."""
    return canonical_digest(
        {"mode": mode, "checks": [check.to_wire() for check in checks]}
    )


@dataclass(frozen=True)
class CheckSpec:
    """One executable check: argv, working directory, and its reason."""

    check_id: str
    kind: CheckKind
    command: tuple[str, ...]
    priority: int = 50
    package: str | None = None
    package_key: str | None = None
    cwd: str | None = None
    scope: str = "changed"
    dependent_distance: int = 0
    reason: str = ""

    def __post_init__(self) -> None:
        require_str(self.check_id, "check id")
        if not isinstance(self.kind, CheckKind):
            raise ContractError("check kind must be a CheckKind")
        if (
            not isinstance(self.command, tuple)
            or not self.command
            or not all(isinstance(item, str) and item for item in self.command)
        ):
            raise ContractError("check command must be a non-empty tuple of strings")
        require_int(self.priority, "check priority", minimum=0)
        optional_str(self.package, "check package")
        optional_str(self.package_key, "check package key")
        optional_str(self.cwd, "check cwd")
        require_str(self.scope, "check scope")
        require_int(self.dependent_distance, "check dependent distance", minimum=0)
        require_str(self.reason, "check reason", allow_empty=True)

    def to_wire(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "kind": self.kind.value,
            "priority": self.priority,
            "package": self.package,
            "package_key": self.package_key,
            "cwd": self.cwd,
            "scope": self.scope,
            "dependent_distance": self.dependent_distance,
            "argv": list(self.command),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CheckResult:
    """One executed check: shell result plus the observed run facts."""

    check_id: str
    status: CheckStatus
    kind: CheckKind | None = None
    package: str | None = None
    scope: str = "changed"
    dependent_distance: int = 0
    cwd: str | None = None
    index: int = 0
    command: tuple[str, ...] = ()
    exit_code: int = 0
    timed_out: bool = False
    duration_ms: int = 0
    output_lines: int = 0
    output_chars: int = 0
    diagnostics: tuple[str, ...] = ()
    diagnostics_truncated: bool = False
    tail: tuple[str, ...] = ()
    log: str | None = None
    log_retention: str = "deleted"
    child_returncode: int | None = None
    child_signal: int | None = None
    coverage: Coverage = field(default_factory=Coverage)
    schema: str = CHECK_RESULT_SCHEMA

    def __post_init__(self) -> None:
        require_str(self.check_id, "check result id")
        if not isinstance(self.status, CheckStatus):
            raise ContractError("check result status must be a CheckStatus")
        if self.kind is not None and not isinstance(self.kind, CheckKind):
            raise ContractError("check result kind must be a CheckKind")
        optional_str(self.package, "check result package")
        optional_str(self.cwd, "check result cwd")
        require_int(self.index, "check result index", minimum=0)
        require_int(self.exit_code, "check result exit code", minimum=0, maximum=255)
        require_int(self.duration_ms, "check result duration", minimum=0)
        require_int(self.output_lines, "check result output lines", minimum=0)
        require_int(self.output_chars, "check result output chars", minimum=0)
        optional_int(
            self.child_returncode,
            "check result child return code",
            minimum=-255,
            maximum=255,
        )
        optional_int(
            self.child_signal, "check result child signal", minimum=1, maximum=255
        )
        if not isinstance(self.coverage, Coverage):
            raise ContractError("check result coverage must be a Coverage")
        if self.status is CheckStatus.PASSED and (
            self.exit_code != 0 or self.child_returncode not in (None, 0)
        ):
            raise ContractError("a passed check cannot report a failure")
        if self.status is CheckStatus.FAILED and not self._records_failure():
            raise ContractError("a failed check must record a failure cause")

    def _records_failure(self) -> bool:
        return (
            self.exit_code != 0
            or self.child_returncode not in (None, 0)
            or self.timed_out
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "check_id": self.check_id,
            "status": self.status.value,
            "kind": self.kind.value if self.kind is not None else None,
            "package": self.package,
            "scope": self.scope,
            "dependent_distance": self.dependent_distance,
            "cwd": self.cwd,
            "index": self.index,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_ms / 1000, 3),
            "duration_ms": self.duration_ms,
            "output_lines": self.output_lines,
            "output_chars": self.output_chars,
            "diagnostics": list(self.diagnostics),
            "diagnostics_truncated": self.diagnostics_truncated,
            "tail": list(self.tail),
            "log": self.log,
            "log_retention": self.log_retention,
            "child_returncode": self.child_returncode,
            "child_signal": self.child_signal,
            "coverage": self.coverage.to_wire(),
        }


@dataclass(frozen=True)
class PackageRow:
    """One planned package: scope, changed files, and candidate tests."""

    name: str
    directory: str
    scope: str
    dependent_distance: int
    changed: tuple[str, ...]
    changed_truncated: bool
    candidate_tests: tuple[str, ...]
    vitest: bool | None = None
    scripts: tuple[str, ...] | None = None
    local_dependencies: tuple[str, ...] | None = None

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "name": self.name,
            "dir": self.directory,
            "scope": self.scope,
            "dependent_distance": self.dependent_distance,
            "changed": list(self.changed),
            "changed_truncated": self.changed_truncated,
            "candidate_tests": list(self.candidate_tests),
        }
        if self.vitest is not None:
            wire["vitest"] = self.vitest
        if self.scripts is not None:
            wire["scripts"] = list(self.scripts)
        if self.local_dependencies is not None:
            wire["local_dependencies"] = list(self.local_dependencies)
        return wire


@dataclass(frozen=True)
class ProviderSummary:
    """One ecosystem's contribution as reported in the merged plan."""

    name: str
    manager: str
    workspace_packages: int
    workspace_edges: int
    changed_packages: tuple[str, ...]
    dependent_packages: tuple[str, ...]
    steps_total: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "manager": self.manager,
            "workspace_packages": self.workspace_packages,
            "workspace_edges": self.workspace_edges,
            "changed_packages": list(self.changed_packages),
            "dependent_packages": list(self.dependent_packages),
            "steps_total": self.steps_total,
        }


@dataclass(frozen=True)
class ProviderPlan:
    """One ecosystem's typed planning result before cross-ecosystem merging."""

    provider: str
    manager: str
    docs_only: bool
    changed_files: tuple[str, ...]
    global_changes: tuple[str, ...]
    unowned: tuple[str, ...]
    changed_packages: tuple[str, ...]
    dependent_packages: tuple[str, ...]
    affected_packages: tuple[str, ...]
    packages: tuple[PackageRow, ...]
    checks: tuple[CheckSpec, ...]
    notes: tuple[str, ...]
    workspace_packages: int
    workspace_edges: int

    def summary(self) -> ProviderSummary:
        return ProviderSummary(
            name=self.provider,
            manager=self.manager,
            workspace_packages=self.workspace_packages,
            workspace_edges=self.workspace_edges,
            changed_packages=self.changed_packages,
            dependent_packages=self.dependent_packages,
            steps_total=len(self.checks),
        )


@dataclass(frozen=True)
class VerificationPlan:
    """The merged verification plan document and its executable checks."""

    plan_id: str
    checks: tuple[CheckSpec, ...]
    repo_root: str = ""
    provider: str | None = None
    package_manager: str = ""
    workspace_packages: int = 0
    workspace_edges: int = 0
    mode: str = "standard"
    dependents: str = "auto"
    base: str | None = None
    docs_only: bool = False
    changed_files: tuple[str, ...] = ()
    changed_truncated: bool = False
    global_changes: tuple[str, ...] = ()
    unowned: tuple[str, ...] = ()
    changed_packages: tuple[str, ...] = ()
    dependent_packages: tuple[str, ...] = ()
    affected_packages: tuple[str, ...] = ()
    packages: tuple[PackageRow, ...] = ()
    packages_truncated: bool = False
    checks_total: int = 0
    checks_truncated: bool = False
    providers: tuple[ProviderSummary, ...] = ()
    ecosystems: tuple[ProviderSummary, ...] = ()
    notes: tuple[str, ...] = ()
    coverage: Coverage | None = None
    schema: str = VERIFICATION_PLAN_SCHEMA

    def __post_init__(self) -> None:
        require_str(self.plan_id, "verification plan id")
        if not isinstance(self.checks, tuple) or not all(
            isinstance(item, CheckSpec) for item in self.checks
        ):
            raise ContractError("verification plan checks must be a tuple of CheckSpec")
        require_unique_strings(
            [item.check_id for item in self.checks], "verification plan checks"
        )
        require_str(self.mode, "verification plan mode")

    @property
    def visible_coverage(self) -> Coverage:
        if self.coverage is not None:
            return self.coverage
        if self.changed_truncated or self.packages_truncated:
            return typed_coverage(SAMPLED, RESULT_LIMIT, STEP_LIMIT)
        return typed_coverage(COMPLETE)

    def to_wire(self) -> dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "provider": self.provider,
            "package_manager": self.package_manager,
            "workspace_packages": self.workspace_packages,
            "workspace_edges": self.workspace_edges,
            "mode": self.mode,
            "dependents": self.dependents,
            "base": self.base,
            "docs_only": self.docs_only,
            "changed_files": list(self.changed_files),
            "changed_truncated": self.changed_truncated,
            "provenance": HEURISTIC,
            "coverage": self.visible_coverage.to_wire(),
            "global_changes": list(self.global_changes),
            "unowned": list(self.unowned),
            "changed_packages": list(self.changed_packages),
            "dependent_packages": list(self.dependent_packages),
            "affected_packages": list(self.affected_packages),
            "packages": [row.to_wire() for row in self.packages],
            "packages_truncated": self.packages_truncated,
            "steps": [check.to_wire() for check in self.checks],
            "steps_total": self.checks_total,
            "steps_truncated": self.checks_truncated,
            "providers": [summary.to_wire() for summary in self.providers],
            "ecosystems": [summary.to_wire() for summary in self.ecosystems],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class VerificationRun:
    """One executed verification plan: selection, results, and status."""

    plan: VerificationPlan
    status: VerificationStatus
    ok: bool
    exit_code: int
    dry_run: bool
    available_checks: tuple[CheckSpec, ...]
    selected_checks: tuple[CheckSpec, ...]
    scope: str = "worktree"
    results: tuple[CheckResult, ...] = ()
    steps_limited: bool = False
    raw_output_chars: int = 0
    raw_output_lines: int = 0
    duration_seconds: float = 0.0
    executed_steps: int = 0
    passed_steps: int = 0
    failed_steps: int = 0

    @property
    def visible_coverage(self) -> Coverage:
        if self.steps_limited:
            return typed_coverage(SAMPLED, STEP_LIMIT)
        return typed_coverage(COMPLETE)

    def to_wire(self) -> dict[str, Any]:
        plan = self.plan
        wire: dict[str, Any] = {
            "repo_root": plan.repo_root,
            "mode": plan.mode,
            "dependents": plan.dependents,
            "base": plan.base,
            "verification_scope": self.scope,
            "dry_run": self.dry_run,
            "workspace_packages": plan.workspace_packages,
            "workspace_edges": plan.workspace_edges,
            "changed_files": list(plan.changed_files),
            "changed_packages": list(plan.changed_packages),
            "dependent_packages": list(plan.dependent_packages),
            "affected_packages": list(plan.affected_packages),
            "global_changes": list(plan.global_changes),
            "unowned": list(plan.unowned),
            "planned_steps": len(self.available_checks),
            "selected_steps": len(self.selected_checks),
            "steps_limited": self.steps_limited,
            "notes": list(plan.notes),
            "provenance": HEURISTIC,
            "coverage": self.visible_coverage.to_wire(),
            "status": self.status.value,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "executed_steps": self.executed_steps,
            "passed_steps": self.passed_steps,
            "failed_steps": self.failed_steps,
            "duration_seconds": self.duration_seconds,
            "raw_output_chars": self.raw_output_chars,
            "raw_output_lines": self.raw_output_lines,
            "results": [result.to_wire() for result in self.results],
        }
        if self.dry_run:
            wire["plan"] = [check.to_wire() for check in self.selected_checks]
        return wire


@dataclass(frozen=True)
class VerifyConfig:
    """Optional `.agentq.toml` verification configuration."""

    providers: tuple[str, ...] | None = None
    commands: tuple[tuple[str, ...], ...] = ()
    ignore: tuple[str, ...] = ()
    contract_patterns: tuple[str, ...] = ()
    ownership: tuple[tuple[str, str], ...] = ()


EMPTY_CONFIG = VerifyConfig()


class VerificationProvider(Protocol):
    """One ecosystem's detection and typed planning contribution."""

    name: str
    manager: str

    def detect(self, root: Path, repo_files: Sequence[str]) -> bool: ...

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
    ) -> ProviderPlan: ...
