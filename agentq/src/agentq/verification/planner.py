"""Verification planning: changed files to provider analysis to plan.

``plan_verification`` is the ecosystem-agnostic orchestrator. It loads the
optional ``.agentq.toml`` configuration, detects applicable providers, and
aggregates their typed :class:`~agentq.verification.models.ProviderPlan`
contributions into one :class:`VerificationPlan`. Each provider keeps its own
workspace and package facts; the merged plan holds the complete logical check
plan plus cross-provider change facts.

``limit`` bounds per-check argument selection inside providers. Display
truncation is a separate ``display_limit`` applied only when projecting the
plan to the wire, and execution selects a bounded subset later in the runner.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import tomllib  # pyright: ignore[reportMissingTypeStubs]

from agentq.core import (
    COMPLETE,
    PARTIAL,
    PROVIDER_UNAVAILABLE,
    UNATTRIBUTED,
    AgentQError,
    Coverage,
    typed_coverage,
)
from agentq.discovery import list_repo_files
from agentq.workspace import ChangeSet, changed_files, matches_pattern

from .models import (
    EMPTY_CONFIG,
    ChangeSummary,
    CheckKind,
    CheckSpec,
    ProviderPlan,
    VerificationPlan,
    VerifyConfig,
    verification_plan_id,
)
from .planning import make_check
from .providers import PROVIDER_NAMES, VERIFICATION_PROVIDERS


def load_verify_config(root: Path) -> VerifyConfig:
    path = root / ".agentq.toml"
    if not path.is_file():
        return EMPTY_CONFIG
    try:
        data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AgentQError(f"invalid .agentq.toml: {exc}") from exc
    verify_raw = data.get("verify")
    verify: dict[str, Any] = (
        cast("dict[str, Any]", verify_raw) if isinstance(verify_raw, dict) else {}
    )
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
        isinstance(item, str) for item in cast("list[Any]", providers)
    ):
        raise AgentQError(
            ".agentq.toml [verify] providers must be a list of provider names"
        )
    known: list[str] = cast("list[str]", providers)
    unknown = [item for item in known if item not in PROVIDER_NAMES]
    if unknown:
        raise AgentQError(
            f".agentq.toml names unknown verification providers: {', '.join(unknown)}; "
            f"known providers: {', '.join(PROVIDER_NAMES)}"
        )
    return tuple(known)


def _configured_commands(verify: dict[str, Any]) -> tuple[tuple[str, ...], ...]:
    commands_raw = verify.get("commands")
    if commands_raw is None:
        return ()
    if not isinstance(commands_raw, list) or not all(
        isinstance(item, str) for item in cast("list[Any]", commands_raw)
    ):
        raise AgentQError(
            ".agentq.toml [verify] commands must be a list of command strings"
        )
    commands: list[tuple[str, ...]] = []
    for command in cast("list[str]", commands_raw):
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
        isinstance(item, str) for item in cast("list[Any]", value)
    ):
        raise AgentQError(
            f".agentq.toml [verify] {field} must be a list of glob patterns"
        )
    return tuple(cast("list[str]", value))


def _ownership(data: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    ownership_raw = data.get("ownership")
    if ownership_raw is None:
        return ()
    if not isinstance(ownership_raw, dict):
        raise AgentQError(
            ".agentq.toml [ownership] must be a table of path prefixes to package names"
        )
    ownership: list[tuple[str, str]] = []
    for prefix, name in cast("dict[str, Any]", ownership_raw).items():
        if not isinstance(name, str):
            raise AgentQError(".agentq.toml [ownership] values must be package names")
        ownership.append((str(prefix), name))
    return tuple(ownership)


def plan_verification(
    root: Path,
    *,
    base: str | None = None,
    limit: int = 60,
    display_limit: int = 0,
    mode: str = "standard",
    dependents: str = "auto",
    include_build: bool = False,
    changed_override: Sequence[str] | None = None,
) -> VerificationPlan:
    """Build the merged verification plan for the current change set.

    ``limit`` bounds provider-level file selection inside check arguments.
    ``display_limit`` bounds only the wire projection; zero means unbounded.
    """
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
        return _empty_plan(root, changes, mode, dependents, base, display_limit, notes)
    return _merge_plans(
        root, plans, config, changes, mode, dependents, base, display_limit, notes
    )


def _empty_plan(
    root: Path,
    changes: ChangeSet,
    mode: str,
    dependents: str,
    base: str | None,
    display_limit: int,
    notes: list[str],
) -> VerificationPlan:
    """No applicable ecosystem: report the change set for manual planning."""
    return VerificationPlan(
        plan_id=verification_plan_id((), mode),
        checks=(),
        providers=(),
        changes=ChangeSummary(
            files=changes.files,
            docs_only=changes.docs_only,
            global_files=changes.global_files,
        ),
        repo_root=str(root),
        mode=mode,
        dependents=dependents,
        base=base,
        display_limit=display_limit,
        notes=tuple([*notes, "no verification provider applies to this repository"]),
        coverage=typed_coverage(PARTIAL, PROVIDER_UNAVAILABLE),
    )


def _merge_plans(
    root: Path,
    plans: Sequence[ProviderPlan],
    config: VerifyConfig,
    changes: ChangeSet,
    mode: str,
    dependents: str,
    base: str | None,
    display_limit: int,
    notes: list[str],
) -> VerificationPlan:
    """Aggregate provider contributions into one complete logical plan.

    Configured commands occupy ``CheckPhase.CONFIGURED``, the earliest phase,
    so an explicit user command is never ordered behind inferred checks.
    """
    emitted: list[CheckSpec] = []
    seen: set[tuple[str | None, tuple[str, ...]]] = set()
    for argv in config.commands:
        check = make_check(
            kind=CheckKind.CONFIGURED,
            package="(config)",
            package_key=".",
            cwd=".",
            command=argv,
            reason="explicitly configured in .agentq.toml [verify] commands",
            scope="configured",
        )
        if (check.cwd, check.command) in seen:
            continue
        seen.add((check.cwd, check.command))
        emitted.append(check)
    for plan in plans:
        for check in plan.checks:
            identity = (check.cwd, check.command)
            if identity in seen:
                continue
            seen.add(identity)
            emitted.append(check)
    checks = tuple(emitted)
    summary = _change_summary(plans, changes)
    return VerificationPlan(
        plan_id=verification_plan_id(checks, mode),
        checks=checks,
        providers=tuple(plans),
        changes=summary,
        repo_root=str(root),
        mode=mode,
        dependents=dependents,
        base=base,
        display_limit=display_limit,
        notes=tuple([*notes, *_provider_notes(plans)]),
        coverage=_inference_coverage(summary),
    )


def _change_summary(plans: Sequence[ProviderPlan], changes: ChangeSet) -> ChangeSummary:
    """Cross-provider change facts; unowned means every provider disowned it."""
    unowned_sets = [set(plan.unowned) for plan in plans]
    unowned: set[str] = (
        unowned_sets[0].intersection(*unowned_sets[1:]) if unowned_sets else set()
    )
    global_files: set[str] = set()
    for plan in plans:
        global_files.update(plan.global_changes)
    return ChangeSummary(
        files=changes.files,
        docs_only=changes.docs_only,
        global_files=tuple(sorted(global_files)),
        unowned=tuple(sorted(unowned)),
    )


def _inference_coverage(summary: ChangeSummary) -> Coverage:
    """How completely the change set could be attributed to verification units."""
    if summary.docs_only:
        return typed_coverage(COMPLETE)
    if summary.unowned:
        return typed_coverage(PARTIAL, UNATTRIBUTED)
    return typed_coverage(COMPLETE)


def _provider_notes(plans: Sequence[ProviderPlan]) -> list[str]:
    notes: list[str] = []
    for plan in plans:
        notes.extend(plan.notes)
    return list(dict.fromkeys(notes))
