"""Verification planning: changed files to provider analysis to plan.

``plan_verification`` is the ecosystem-agnostic orchestrator. It loads the
optional ``.agentq.toml`` configuration, detects applicable providers, merges
their typed :class:`~agentq.verification.models.ProviderPlan` contributions,
and returns the single :class:`VerificationPlan` the runner executes.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import tomllib

from agentq.core import (
    COMPLETE,
    RESULT_LIMIT,
    SAMPLED,
    STEP_LIMIT,
    AgentQError,
    typed_coverage,
)
from agentq.discovery import list_repo_files
from agentq.workspace import (
    ChangeSet,
    changed_files,
    is_public_contract_change,
    matches_pattern,
    package_manager,
)

from .models import (
    EMPTY_CONFIG,
    CheckKind,
    ProviderPlan,
    VerificationPlan,
    VerifyConfig,
    verification_plan_id,
)
from .planning import make_check
from .providers import PROVIDER_NAMES, VERIFICATION_PROVIDERS

_CONFIGURED_PRIORITY = 5


def load_verify_config(root: Path) -> VerifyConfig:
    path = root / ".agentq.toml"
    if not path.is_file():
        return EMPTY_CONFIG
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentQError(f"invalid .agentq.toml: {exc}") from exc
    verify_raw = data.get("verify")
    verify: dict[str, Any] = verify_raw if isinstance(verify_raw, dict) else {}
    return VerifyConfig(
        providers=_provider_selection(verify),
        commands=_configured_commands(verify),
        ignore=_pattern_list(verify, "ignore"),
        contract_patterns=_pattern_list(verify, "contract_patterns"),
        ownership=_ownership(data),
    )


def _provider_selection(verify: dict[str, Any]) -> tuple[str, ...] | None:
    providers = verify.get("providers")
    if providers is None:
        return None
    if not isinstance(providers, list) or not all(
        isinstance(item, str) for item in providers
    ):
        raise AgentQError(
            ".agentq.toml [verify] providers must be a list of provider names"
        )
    unknown = [item for item in providers if item not in PROVIDER_NAMES]
    if unknown:
        raise AgentQError(
            f".agentq.toml names unknown verification providers: {', '.join(unknown)}; "
            f"known providers: {', '.join(PROVIDER_NAMES)}"
        )
    return tuple(providers)


def _configured_commands(verify: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    commands_raw = verify.get("commands")
    if commands_raw is None:
        return ()
    if not isinstance(commands_raw, list) or not all(
        isinstance(item, str) for item in commands_raw
    ):
        raise AgentQError(
            ".agentq.toml [verify] commands must be a list of command strings"
        )
    commands: list[tuple[str, ...]] = []
    for command in commands_raw:
        argv = shlex.split(command)
        if not argv:
            raise AgentQError(
                ".agentq.toml [verify] commands contains an empty command"
            )
        commands.append(tuple(argv))
    return tuple(commands)


def _pattern_list(verify: dict[str, Any], field: str) -> tuple[str, ...]:
    value = verify.get(field)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise AgentQError(
            f".agentq.toml [verify] {field} must be a list of glob patterns"
        )
    return tuple(value)


def _ownership(data: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    ownership_raw = data.get("ownership")
    if ownership_raw is None:
        return ()
    if not isinstance(ownership_raw, dict):
        raise AgentQError(
            ".agentq.toml [ownership] must be a table of path prefixes to package names"
        )
    ownership: list[tuple[str, str]] = []
    for prefix, name in ownership_raw.items():
        if not isinstance(name, str):
            raise AgentQError(".agentq.toml [ownership] values must be package names")
        ownership.append((str(prefix), name))
    return tuple(ownership)


def plan_verification(
    root: Path,
    *,
    base: str | None = None,
    limit: int = 60,
    mode: str = "standard",
    dependents: str = "auto",
    include_build: bool = False,
    changed_override: Sequence[str] | None = None,
) -> VerificationPlan:
    """Build the merged verification plan for the current change set."""
    config = load_verify_config(root)
    changes = (
        ChangeSet.from_paths(changed_override)
        if changed_override is not None
        else changed_files(root, base)
    )
    ignored: list[str] = []
    if config.ignore and changes.files:
        kept: list[str] = []
        for path in changes.files:
            if any(
                matches_pattern(path.strip("/"), pattern) for pattern in config.ignore
            ):
                ignored.append(path)
            else:
                kept.append(path)
        changes = ChangeSet(files=tuple(kept))

    contract_changed = any(
        is_public_contract_change(path)
        or any(
            matches_pattern(path.strip("/"), pattern)
            for pattern in config.contract_patterns
        )
        for path in changes.files
    )
    repo_files = list_repo_files(root)
    providers = [
        provider
        for provider in VERIFICATION_PROVIDERS
        if (config.providers is None or provider.name in config.providers)
        and provider.detect(root, repo_files)
    ]
    plans = [
        provider.plan(
            root,
            repo_files=repo_files,
            changes=changes,
            limit=limit,
            mode=mode,
            dependents=dependents,
            include_build=include_build,
            contract_changed=contract_changed,
            config=config,
        )
        for provider in providers
    ]

    notes: list[str] = []
    if not changes.files:
        notes.append("no changed files detected")
    if ignored:
        summary = ", ".join(ignored[:8]) + (" …" if len(ignored) > 8 else "")
        notes.append(
            f"{len(ignored)} changed file(s) ignored via .agentq.toml [verify] ignore: {summary}"
        )
    if not plans:
        return _empty_plan(root, changes, mode, dependents, base, limit, notes)
    return _merge_plans(root, plans, config, mode, dependents, base, limit, notes)


def _empty_plan(
    root: Path,
    changes: ChangeSet,
    mode: str,
    dependents: str,
    base: str | None,
    limit: int,
    notes: list[str],
) -> VerificationPlan:
    """No applicable ecosystem: report the change set for manual planning."""
    return VerificationPlan(
        plan_id=verification_plan_id((), mode),
        checks=(),
        repo_root=str(root),
        provider=None,
        package_manager=package_manager(root).value,
        mode=mode,
        dependents=dependents,
        base=base,
        changed_files=tuple(changes.files[:limit]),
        changed_truncated=len(changes.files) > limit,
        unowned=tuple(sorted(changes.files)[:limit]),
        notes=tuple(
            [*notes, "no verification provider applies to this repository"]
        ),
        coverage=typed_coverage(SAMPLED, STEP_LIMIT),
    )


def _merge_plans(
    root: Path,
    plans: Sequence[ProviderPlan],
    config: VerifyConfig,
    mode: str,
    dependents: str,
    base: str | None,
    limit: int,
    notes: list[str],
) -> VerificationPlan:
    """Merge provider checks behind the primary ladder, then configured checks."""
    primary = plans[0]
    emitted = list(primary.checks[:limit])
    seen = {(check.cwd, check.command) for check in emitted}
    pre_cut_total = len(primary.checks) - len(emitted)
    for plan in plans[1:]:
        for check in plan.checks:
            identity = (check.cwd, check.command)
            if identity in seen:
                continue
            seen.add(identity)
            emitted.append(check)
            pre_cut_total += 1
    for argv in config.commands:
        check = make_check(
            kind=CheckKind.CONFIGURED,
            package="(config)",
            package_key=".",
            cwd=".",
            command=argv,
            reason="explicitly configured in .agentq.toml [verify] commands",
            priority=_CONFIGURED_PRIORITY,
            scope="configured",
        )
        if (check.cwd, check.command) in seen:
            continue
        seen.add((check.cwd, check.command))
        emitted.append(check)
        pre_cut_total += 1
    total = pre_cut_total + len(emitted)
    checks = tuple(emitted[:limit])
    sampled = (
        len(primary.changed_files) > limit
        or len(primary.packages) > limit
        or len(primary.checks) > limit
    )
    return VerificationPlan(
        plan_id=verification_plan_id(tuple(emitted), mode),
        checks=checks,
        repo_root=str(root),
        provider=primary.provider,
        package_manager=primary.manager,
        workspace_packages=primary.workspace_packages,
        workspace_edges=primary.workspace_edges,
        mode=mode,
        dependents=dependents,
        base=base,
        docs_only=primary.docs_only,
        changed_files=tuple(primary.changed_files[:limit]),
        changed_truncated=len(primary.changed_files) > limit,
        global_changes=tuple(primary.global_changes[:limit]),
        unowned=tuple(primary.unowned[:limit]),
        changed_packages=primary.changed_packages,
        dependent_packages=primary.dependent_packages,
        affected_packages=primary.affected_packages,
        packages=tuple(primary.packages[:limit]),
        packages_truncated=len(primary.packages) > limit,
        checks_total=total,
        checks_truncated=total > len(checks),
        providers=tuple(plan.summary() for plan in plans),
        ecosystems=tuple(plan.summary() for plan in plans[1:]),
        notes=tuple([*notes, *primary.notes]),
        coverage=(
            typed_coverage(SAMPLED, RESULT_LIMIT, STEP_LIMIT)
            if sampled
            else typed_coverage(COMPLETE)
        ),
    )
