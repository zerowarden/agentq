"""Typed verification contracts: checks, plans, plans-as-documents, runs.

A :class:`ProviderPlan` is one ecosystem's typed contribution. The
:class:`VerificationPlan` aggregates those contributions without collapsing
them into a privileged primary ecosystem: every provider keeps its own
workspace, package, and note facts, while ``checks`` is the complete logical
plan. Display limits are applied only when projecting a plan to the wire.

:class:`VerificationSelection` records which checks a run chose to execute and
which it omitted. :class:`VerificationRun` composes inference, selection, and
execution coverage, so a run cannot report ``passed`` when material
verification was omitted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from pathlib import Path
from typing import Any, Protocol

from agentq.core import (
    COMPLETE,
    HEURISTIC,
    RENDER_OMISSION,
    SAMPLED,
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

VERIFICATION_PLAN_SCHEMA = "agentq.verification-plan/v2"
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


class CheckPhase(IntEnum):
    """Execution phase policy: checks run in ascending phase order.

    Providers declare a ``CheckKind`` only; the central mapping decides when
    that kind runs, so ordering policy lives in exactly one place.
    """

    CONFIGURED = 0
    DIRECT_TESTS = 10
    TEST = 15
    RELATED_TESTS = 20
    CANDIDATE_TESTS = 25
    PACKAGE_TESTS = 30
    MODULE_TESTS = 35
    TYPECHECK = 40
    DEPENDENT_TYPECHECK = 45
    DEPENDENT_TESTS = 50
    LINT = 60
    DEPENDENT_LINT = 65
    BUILD = 70
    DEPENDENT_BUILD = 75
    CUSTOM = 90


CHECK_PHASE_BY_KIND: dict[CheckKind, CheckPhase] = {
    CheckKind.CONFIGURED: CheckPhase.CONFIGURED,
    CheckKind.DIRECT_TESTS: CheckPhase.DIRECT_TESTS,
    CheckKind.TEST: CheckPhase.TEST,
    CheckKind.RELATED_TESTS: CheckPhase.RELATED_TESTS,
    CheckKind.CANDIDATE_TESTS: CheckPhase.CANDIDATE_TESTS,
    CheckKind.PACKAGE_TESTS: CheckPhase.PACKAGE_TESTS,
    CheckKind.MODULE_TESTS: CheckPhase.MODULE_TESTS,
    CheckKind.TYPECHECK: CheckPhase.TYPECHECK,
    CheckKind.DEPENDENT_TYPECHECK: CheckPhase.DEPENDENT_TYPECHECK,
    CheckKind.DEPENDENT_TESTS: CheckPhase.DEPENDENT_TESTS,
    CheckKind.LINT: CheckPhase.LINT,
    CheckKind.DEPENDENT_LINT: CheckPhase.DEPENDENT_LINT,
    CheckKind.BUILD: CheckPhase.BUILD,
    CheckKind.DEPENDENT_BUILD: CheckPhase.DEPENDENT_BUILD,
    CheckKind.CUSTOM: CheckPhase.CUSTOM,
}


def check_phase(kind: CheckKind) -> CheckPhase:
    """The execution phase policy assigns to ``kind``."""
    return CHECK_PHASE_BY_KIND[kind]


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


def verification_plan_id(checks: tuple[CheckSpec, ...], mode: str | None = None) -> str:
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
class ChangeSummary:
    """Cross-provider change facts; provider ownership stays per provider.

    ``unowned`` holds only files that no contributing provider could attribute
    to one of its units, so it is the intersection of the provider reports.
    """

    files: tuple[str, ...] = ()
    docs_only: bool = False
    global_files: tuple[str, ...] = ()
    unowned: tuple[str, ...] = ()

    def to_wire(self, *, display_limit: int) -> dict[str, Any]:
        limit = display_limit if display_limit > 0 else len(self.files)
        files = self.files[:limit]
        global_files = self.global_files[:limit]
        unowned = self.unowned[:limit]
        return {
            "files": list(files),
            "files_truncated": len(files) < len(self.files),
            "docs_only": self.docs_only,
            "global_changes": list(global_files),
            "global_changes_truncated": len(global_files) < len(self.global_files),
            "unowned": list(unowned),
            "unowned_truncated": len(unowned) < len(self.unowned),
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

    def to_wire(self, *, display_limit: int) -> dict[str, Any]:
        limit = display_limit if display_limit > 0 else len(self.packages)
        packages = self.packages[:limit]
        return {
            "name": self.provider,
            "manager": self.manager,
            "docs_only": self.docs_only,
            "workspace_packages": self.workspace_packages,
            "workspace_edges": self.workspace_edges,
            "changed_packages": list(self.changed_packages),
            "dependent_packages": list(self.dependent_packages),
            "affected_packages": list(self.affected_packages),
            "global_changes": list(self.global_changes),
            "unowned": list(self.unowned),
            "packages": [row.to_wire() for row in packages],
            "packages_truncated": len(packages) < len(self.packages),
            "steps_total": len(self.checks),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class VerificationPlan:
    """The aggregated verification plan: provider contributions + all checks.

    ``checks`` is the complete logical plan. ``display_limit`` affects only the
    wire projection; ``coverage`` describes how complete the inference itself
    was. Render omission is composed into :attr:`visible_coverage`, never into
    :attr:`inference_coverage`.
    """

    plan_id: str
    checks: tuple[CheckSpec, ...]
    providers: tuple[ProviderPlan, ...] = ()
    changes: ChangeSummary = field(default_factory=ChangeSummary)
    repo_root: str = ""
    mode: str = "standard"
    dependents: str = "auto"
    base: str | None = None
    display_limit: int = 0
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
        if not isinstance(self.providers, tuple) or not all(
            isinstance(item, ProviderPlan) for item in self.providers
        ):
            raise ContractError(
                "verification plan providers must be a tuple of ProviderPlan"
            )
        if not isinstance(self.changes, ChangeSummary):
            raise ContractError("verification plan changes must be a ChangeSummary")
        require_str(self.mode, "verification plan mode")
        require_int(self.display_limit, "verification plan display limit", minimum=0)

    @property
    def checks_total(self) -> int:
        return len(self.checks)

    @property
    def total_units(self) -> int:
        """Sum of provider units; heterogeneous across ecosystems by design."""
        return sum(provider.workspace_packages for provider in self.providers)

    @property
    def total_edges(self) -> int:
        """Sum of provider local dependency edges across ecosystems."""
        return sum(provider.workspace_edges for provider in self.providers)

    @property
    def docs_only(self) -> bool:
        return self.changes.docs_only

    @property
    def displayed_checks(self) -> tuple[CheckSpec, ...]:
        if self.display_limit <= 0:
            return self.checks
        return self.checks[: self.display_limit]

    @property
    def inference_coverage(self) -> Coverage:
        if self.coverage is not None:
            return self.coverage
        return typed_coverage(COMPLETE)

    def _render_omitted(self) -> bool:
        limit = self.display_limit
        if limit <= 0:
            return False
        return (
            len(self.checks) > limit
            or len(self.changes.files) > limit
            or len(self.changes.global_files) > limit
            or len(self.changes.unowned) > limit
            or any(len(provider.packages) > limit for provider in self.providers)
        )

    @property
    def visible_coverage(self) -> Coverage:
        """Coverage of the plan as projected, including render omission."""
        if self._render_omitted():
            return self.inference_coverage.weakest(
                typed_coverage(SAMPLED, RENDER_OMISSION)
            )
        return self.inference_coverage

    def to_wire(self) -> dict[str, Any]:
        displayed = self.displayed_checks
        return {
            "schema": self.schema,
            "plan_id": self.plan_id,
            "repo_root": self.repo_root,
            "mode": self.mode,
            "dependents": self.dependents,
            "base": self.base,
            "changes": self.changes.to_wire(display_limit=self.display_limit),
            "providers": [
                provider.to_wire(display_limit=self.display_limit)
                for provider in self.providers
            ],
            "total_units": self.total_units,
            "total_edges": self.total_edges,
            "steps": [check.to_wire() for check in displayed],
            "steps_total": self.checks_total,
            "steps_truncated": len(displayed) < self.checks_total,
            "provenance": HEURISTIC,
            "coverage": self.visible_coverage.to_wire(),
            "inference_coverage": self.inference_coverage.to_wire(),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class VerificationSelection:
    """The executed subset of a plan and the checks it omitted."""

    selected: tuple[CheckSpec, ...]
    omitted: tuple[CheckSpec, ...] = ()
    coverage: Coverage = field(default_factory=lambda: typed_coverage(COMPLETE))

    @property
    def available(self) -> tuple[CheckSpec, ...]:
        return (*self.selected, *self.omitted)

    @property
    def limited(self) -> bool:
        return bool(self.omitted)

    def to_wire(self) -> dict[str, Any]:
        return {
            "selected_steps": len(self.selected),
            "omitted_steps": len(self.omitted),
            "limited": self.limited,
            "coverage": self.coverage.to_wire(),
        }


@dataclass(frozen=True)
class VerificationRun:
    """One executed verification plan: selection, results, and status.

    ``coverage`` is the weakest of inference, selection, and execution
    coverage, so a run cannot claim complete verification when any stage
    omitted material checks.
    """

    plan: VerificationPlan
    status: VerificationStatus
    ok: bool
    exit_code: int
    dry_run: bool
    selection: VerificationSelection
    scope: str = "worktree"
    results: tuple[CheckResult, ...] = ()
    execution_coverage: Coverage = field(
        default_factory=lambda: typed_coverage(COMPLETE)
    )
    raw_output_chars: int = 0
    raw_output_lines: int = 0
    duration_seconds: float = 0.0
    executed_steps: int = 0
    passed_steps: int = 0
    failed_steps: int = 0

    @property
    def available_checks(self) -> tuple[CheckSpec, ...]:
        return self.selection.available

    @property
    def selected_checks(self) -> tuple[CheckSpec, ...]:
        return self.selection.selected

    @property
    def steps_limited(self) -> bool:
        return self.selection.limited

    @property
    def visible_coverage(self) -> Coverage:
        return self.plan.inference_coverage.weakest(
            self.selection.coverage, self.execution_coverage
        )

    def to_wire(self) -> dict[str, Any]:
        plan = self.plan
        wire: dict[str, Any] = {
            "repo_root": plan.repo_root,
            "mode": plan.mode,
            "dependents": plan.dependents,
            "base": plan.base,
            "verification_scope": self.scope,
            "dry_run": self.dry_run,
            "changed_files": list(plan.changes.files),
            "providers": [
                provider.to_wire(display_limit=0) for provider in plan.providers
            ],
            "total_units": plan.total_units,
            "total_edges": plan.total_edges,
            "planned_steps": len(self.available_checks),
            "selected_steps": len(self.selected_checks),
            "omitted_steps": len(self.selection.omitted),
            "steps_limited": self.steps_limited,
            "notes": list(plan.notes),
            "provenance": HEURISTIC,
            "coverage": self.visible_coverage.to_wire(),
            "inference_coverage": plan.inference_coverage.to_wire(),
            "selection_coverage": self.selection.coverage.to_wire(),
            "execution_coverage": self.execution_coverage.to_wire(),
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
        config: VerifyConfig,
    ) -> ProviderPlan: ...
