"""Wire codec for the decision configuration.

Scoring, selection, delivery, and output format: every choice that can
change one decision, with its canonical digest.
"""

from __future__ import annotations

import hashlib

from agentq.core import ContractError, canonical_json
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import (
    EvidenceRole,
    Intent,
)
from agentq.inspection.decision import DecisionConfig
from agentq.inspection.scoring import ScoringProfile
from agentq.inspection.selection import SelectionProfile

from ..models import (
    CONFIG_SCHEMA,
)
from .json import (
    as_mapping,
    decode_json,
    exact_keys,
    read_bool,
    read_int,
    read_str,
)


def _scoring_wire(profile: ScoringProfile) -> dict[str, object]:
    return profile.to_wire()


def _scoring_from_wire(value: object, what: str) -> ScoringProfile:
    mapping = as_mapping(value, what)
    exact_keys(mapping, {"profile", "binding_bonus", "intent_priorities"}, what)
    priorities = as_mapping(mapping["intent_priorities"], f"{what}.intent_priorities")
    unknown = sorted(set(priorities) - {intent.value for intent in Intent})
    if unknown:
        raise ContractError(
            f"{what}.intent_priorities has unknown intents: {', '.join(unknown)}"
        )
    missing = sorted(
        intent.value for intent in Intent if intent.value not in priorities
    )
    if missing:
        raise ContractError(
            f"{what}.intent_priorities is missing intents: {', '.join(missing)}"
        )
    known_roles = {role.value for role in EvidenceRole}
    decoded: list[tuple[Intent, tuple[tuple[EvidenceRole, int], ...]]] = []
    for intent in Intent:
        role_map = as_mapping(
            priorities[intent.value], f"{what}.intent_priorities.{intent.value}"
        )
        unknown_roles = sorted(set(role_map) - known_roles)
        if unknown_roles:
            raise ContractError(
                f"{what}.intent_priorities.{intent.value} has unknown roles: "
                f"{', '.join(unknown_roles)}"
            )
        decoded.append(
            (
                intent,
                tuple(
                    (
                        role,
                        read_int(
                            role_map[role.value],
                            f"{what}.intent_priorities.{intent.value}.{role.value}",
                        ),
                    )
                    for role in EvidenceRole
                    if role.value in role_map
                ),
            )
        )
    return ScoringProfile(
        profile=read_str(mapping["profile"], f"{what}.profile"),
        binding_bonus=read_int(mapping["binding_bonus"], f"{what}.binding_bonus"),
        intent_priorities=tuple(decoded),
    )


def _selection_wire(profile: SelectionProfile) -> dict[str, object]:
    return profile.to_wire()


# Selection flags introduced after the first profiles were written: a profile
# that predates one falls back to the behavior it had.
_DEFAULTED_SELECTION_FIELDS = ("variant_fallback", "skip_zero_value")


def _selection_from_wire(value: object, what: str) -> SelectionProfile:
    mapping = as_mapping(value, what)
    exact_keys(
        mapping,
        {
            "profile",
            "reserve_required",
            "role_diversity",
            "per_file_limit",
            "fill_by_score",
            *_DEFAULTED_SELECTION_FIELDS,
        },
        what,
    )
    defaults = SelectionProfile()
    variant_fallback = (
        read_bool(mapping["variant_fallback"], f"{what}.variant_fallback")
        if "variant_fallback" in mapping
        else defaults.variant_fallback
    )
    skip_zero_value = (
        read_bool(mapping["skip_zero_value"], f"{what}.skip_zero_value")
        if "skip_zero_value" in mapping
        else defaults.skip_zero_value
    )
    return SelectionProfile(
        profile=read_str(mapping["profile"], f"{what}.profile"),
        reserve_required=read_bool(
            mapping["reserve_required"], f"{what}.reserve_required"
        ),
        role_diversity=read_bool(mapping["role_diversity"], f"{what}.role_diversity"),
        per_file_limit=read_int(
            mapping["per_file_limit"], f"{what}.per_file_limit", minimum=1
        ),
        fill_by_score=read_bool(mapping["fill_by_score"], f"{what}.fill_by_score"),
        variant_fallback=variant_fallback,
        skip_zero_value=skip_zero_value,
    )


def _delivery_wire(delivery: DeliveryBudget) -> dict[str, object]:
    return {"max_chars": delivery.max_chars, "envelope_chars": delivery.envelope_chars}


def _delivery_from_wire(value: object, what: str) -> DeliveryBudget:
    mapping = as_mapping(value, what)
    exact_keys(mapping, {"max_chars", "envelope_chars"}, what)
    return DeliveryBudget(
        max_chars=read_int(mapping["max_chars"], f"{what}.max_chars", minimum=1),
        envelope_chars=read_int(
            mapping["envelope_chars"], f"{what}.envelope_chars", minimum=0
        ),
    )


def _config_wire(config: DecisionConfig) -> dict[str, object]:
    return {
        "schema": CONFIG_SCHEMA,
        "scoring": _scoring_wire(config.scoring),
        "selection": _selection_wire(config.selection),
        "delivery": _delivery_wire(config.delivery),
        "output_format": config.output_format,
    }


def encode_config(config: DecisionConfig) -> bytes:
    return canonical_json(_config_wire(config)).encode("utf-8")


def decode_config(data: bytes) -> DecisionConfig:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"decision config is not valid UTF-8: {exc}") from exc
    mapping = as_mapping(decode_json(text, what="decision config"), "decision config")
    exact_keys(
        mapping,
        {"schema", "scoring", "selection", "delivery", "output_format"},
        "decision config",
    )
    schema = read_str(mapping["schema"], "decision config.schema")
    if schema != CONFIG_SCHEMA:
        raise ContractError(f"unsupported decision config schema: {schema!r}")
    return DecisionConfig(
        scoring=_scoring_from_wire(mapping["scoring"], "decision config.scoring"),
        selection=_selection_from_wire(
            mapping["selection"], "decision config.selection"
        ),
        delivery=_delivery_from_wire(
            mapping["delivery"], "decision config.delivery"
        ),
        output_format=read_str(
            mapping["output_format"], "decision config.output_format"
        ),
    )


def config_digest(config: DecisionConfig) -> str:
    return hashlib.sha256(encode_config(config)).hexdigest()
