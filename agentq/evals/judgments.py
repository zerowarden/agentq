"""Compile reviewer-authored judgment drafts into capture-bound labels.

A draft names evidence by alias (``target.exact``); the compiler replaces each
alias with the actual variant id the capture contains and binds the result to
that capture. This is deterministic bookkeeping between the reviewer and the
evaluator: the scorer never sees drafts or aliases, and an unknown alias is a
validation error rather than an absent witness.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from agentq.core import ContractError

from .codec import capture_digest
from .models import (
    ExpectedOutcome,
    ExpectedOutcomeKind,
    JudgmentFacet,
    JudgmentSet,
    JudgmentWitness,
    ReplayCapture,
)
from .wire.json import as_list, as_mapping, read_json_file, read_str, read_strings

DRAFT_SCHEMA = "agentq.eval.judgment-draft/v1"
REVIEWED = "reviewed"


@dataclass(frozen=True)
class FacetDraft:
    facet_id: str
    critical: bool
    witness_sets: tuple[tuple[str, ...], ...]
    audit_note: str


@dataclass(frozen=True)
class WitnessDraft:
    witness_id: str
    acceptable_aliases: tuple[str, ...]


@dataclass(frozen=True)
class JudgmentDraft:
    """One human-readable draft before aliases are resolved to variant ids."""

    case_id: str
    basis: str
    review_status: str
    facets: tuple[FacetDraft, ...]
    witnesses: tuple[WitnessDraft, ...]
    irrelevant_aliases: tuple[str, ...]
    expected_outcomes: tuple[ExpectedOutcome, ...]


def _optional_strings(value: object, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return read_strings(value, what)


def _parse_facet(value: object, what: str) -> FacetDraft:
    mapping = as_mapping(value, what)
    critical = mapping.get("critical")
    if not isinstance(critical, bool):
        raise ContractError(f"{what}.critical must be a boolean")
    witness_sets = tuple(
        read_strings(clause, f"{what}.witness_sets[{index}]")
        for index, clause in enumerate(as_list(mapping.get("witness_sets"), what))
    )
    if not witness_sets or any(not clause for clause in witness_sets):
        raise ContractError(f"{what} requires non-empty witness clauses")
    return FacetDraft(
        facet_id=read_str(mapping.get("facet_id"), f"{what}.facet_id"),
        critical=critical,
        witness_sets=witness_sets,
        audit_note=read_str(
            mapping.get("audit_note", ""), f"{what}.audit_note", allow_empty=True
        ),
    )


def _parse_witness(value: object, what: str) -> WitnessDraft:
    mapping = as_mapping(value, what)
    return WitnessDraft(
        witness_id=read_str(mapping.get("witness_id"), f"{what}.witness_id"),
        acceptable_aliases=read_strings(
            mapping.get("acceptable_aliases"), f"{what}.acceptable_aliases"
        ),
    )


def _parse_expected(value: object, what: str) -> ExpectedOutcome:
    mapping = as_mapping(value, what)
    kind = mapping.get("kind")
    if not isinstance(kind, str):
        raise ContractError(f"{what}.kind must be a string")
    try:
        outcome_kind = ExpectedOutcomeKind(kind)
    except ValueError as exc:
        raise ContractError(f"unsupported expected outcome kind: {kind!r}") from exc
    requirement_id = mapping.get("requirement_id")
    status = mapping.get("status")
    code = mapping.get("code")
    minimum = mapping.get("minimum")
    for name, item in (
        ("requirement_id", requirement_id),
        ("status", status),
        ("code", code),
    ):
        if item is not None and not isinstance(item, str):
            raise ContractError(f"{what}.{name} must be a string or null")
    if minimum is not None and (
        isinstance(minimum, bool) or not isinstance(minimum, int)
    ):
        raise ContractError(f"{what}.minimum must be an integer or null")
    return ExpectedOutcome(
        kind=outcome_kind,
        requirement_id=cast("str | None", requirement_id),
        status=cast("str | None", status),
        code=cast("str | None", code),
        minimum=cast("int | None", minimum),
        audit_note=read_str(
            mapping.get("audit_note", ""),
            f"{what}.audit_note",
            allow_empty=True,
        ),
    )


def parse_draft(value: object, *, what: str = "judgment draft") -> JudgmentDraft:
    """Validate one decoded draft document; unknown fields are an error."""
    mapping = as_mapping(value, what)
    allowed = {
        "schema",
        "case_id",
        "basis",
        "review_status",
        "facets",
        "witnesses",
        "irrelevant_aliases",
        "expected_outcomes",
    }
    extra = sorted(set(mapping) - allowed)
    if extra:
        raise ContractError(f"{what} has unknown fields: {', '.join(extra)}")
    schema = mapping.get("schema")
    if schema != DRAFT_SCHEMA:
        raise ContractError(f"unsupported judgment draft schema: {schema!r}")
    return JudgmentDraft(
        case_id=read_str(mapping.get("case_id"), f"{what}.case_id"),
        basis=read_str(mapping.get("basis"), f"{what}.basis"),
        review_status=read_str(
            mapping.get("review_status"), f"{what}.review_status"
        ),
        facets=tuple(
            _parse_facet(item, f"{what}.facets[{index}]")
            for index, item in enumerate(as_list(mapping.get("facets"), what))
        ),
        witnesses=tuple(
            _parse_witness(item, f"{what}.witnesses[{index}]")
            for index, item in enumerate(as_list(mapping.get("witnesses"), what))
        ),
        irrelevant_aliases=_optional_strings(
            mapping.get("irrelevant_aliases"), f"{what}.irrelevant_aliases"
        ),
        expected_outcomes=tuple(
            _parse_expected(item, f"{what}.expected_outcomes[{index}]")
            for index, item in enumerate(
                as_list(mapping.get("expected_outcomes", []), what)
            )
        ),
    )


def load_draft(path: Path) -> JudgmentDraft:
    return parse_draft(read_json_file(path, what="judgment draft"), what=str(path))


def load_draft_directory(directory: Path) -> dict[str, JudgmentDraft]:
    """Case id -> draft from one file per case, case-id ordered."""
    drafts: dict[str, JudgmentDraft] = {}
    for path in sorted(directory.glob("*.json")):
        draft = load_draft(path)
        if draft.case_id in drafts:
            raise ContractError(f"duplicate judgment draft: {draft.case_id!r}")
        drafts[draft.case_id] = draft
    return drafts


def _capture_variant_ids(capture: ReplayCapture) -> frozenset[str]:
    variants = set(capture.decision.pool.variants)
    for acquired in capture.decision.resolution.candidate_evidence:
        variants.update(acquired.variants)
    return frozenset(item.variant_id for item in variants)


def _resolve_alias(
    alias: str, *, aliases: Mapping[str, str], variant_ids: frozenset[str], what: str
) -> str:
    variant_id = aliases.get(alias)
    if variant_id is None:
        raise ContractError(f"{what} names unknown alias {alias!r}")
    if variant_id not in variant_ids:
        raise ContractError(
            f"{what} alias {alias!r} resolves to unknown variant {variant_id!r}"
        )
    return variant_id


def compile_judgments(
    draft: JudgmentDraft,
    aliases: Mapping[str, str],
    capture: ReplayCapture,
) -> JudgmentSet:
    """Bind one reviewed draft to one capture by resolving every alias."""
    if draft.review_status != REVIEWED:
        raise ContractError(
            f"judgment draft for {draft.case_id!r} is not reviewed: "
            f"{draft.review_status!r}"
        )
    if draft.case_id != capture.case_id:
        raise ContractError(
            f"judgment draft case {draft.case_id!r} does not match capture "
            f"{capture.case_id!r}"
        )
    variant_ids = _capture_variant_ids(capture)
    witnesses = tuple(
        JudgmentWitness(
            witness_id=witness.witness_id,
            acceptable_variant_ids=tuple(
                _resolve_alias(
                    alias,
                    aliases=aliases,
                    variant_ids=variant_ids,
                    what=f"witness {witness.witness_id!r}",
                )
                for alias in witness.acceptable_aliases
            ),
        )
        for witness in draft.witnesses
    )
    irrelevant = tuple(
        _resolve_alias(
            alias,
            aliases=aliases,
            variant_ids=variant_ids,
            what="irrelevant evidence",
        )
        for alias in draft.irrelevant_aliases
    )
    return JudgmentSet(
        case_id=draft.case_id,
        capture_id=capture_digest(capture),
        basis=draft.basis,
        review_status=draft.review_status,
        facets=tuple(
            JudgmentFacet(
                facet_id=facet.facet_id,
                critical=facet.critical,
                witness_sets=facet.witness_sets,
                audit_note=facet.audit_note,
            )
            for facet in draft.facets
        ),
        witnesses=witnesses,
        irrelevant_variant_ids=irrelevant,
        expected_outcomes=draft.expected_outcomes,
    )
