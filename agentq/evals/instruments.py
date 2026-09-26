"""Specified negative controls for quality and truthfulness measurements."""

from dataclasses import replace

from agentq.core import ContractError
from agentq.inspection.budgeting import DeliveryBudget
from agentq.inspection.contracts import DecisionDelivered, ObservationKind
from agentq.inspection.decision import DecisionConfig

from .build_fixtures import build_fixture
from .codec import capture_digest, decision_input_digest
from .metrics import evaluate_decision
from .models import FixtureSnapshot, JudgmentFacet, JudgmentSet, JudgmentWitness
from .runner import evaluate_capture


def check_instruments() -> dict[str, object]:
    """A reserved zero-score source competes with a positive-score lexical decoy.

    At the declared 900-character boundary either artifact fits, but both do
    not. Required reservation must protect source. Disabling it must lose the
    source facet without causing a false satisfaction claim. Additional forged
    outcomes verify that the evaluator rejects dishonesty and unstable delivery.
    """
    fixture = build_fixture("lexical-decoy")
    decision = fixture.capture.decision
    observations = tuple(
        o
        for o in decision.pool.observations
        if o.kind in (ObservationKind.SOURCE_WINDOW, ObservationKind.LEXICAL_MENTION)
    )
    ids = {o.observation_id for o in observations}
    pool = replace(
        decision.pool,
        observations=observations,
        variants=tuple(v for v in decision.pool.variants if v.observation_id in ids),
    )
    decision = replace(
        decision,
        pool=pool,
        policy=replace(
            decision.policy,
            requirements=tuple(
                r
                for r in decision.policy.requirements
                if r.requirement_id == "target_source"
            ),
        ),
    )
    capture = replace(
        fixture.capture,
        decision=decision,
        snapshot=FixtureSnapshot(
            "reservation-canary", "1", decision_input_digest(decision)
        ),
    )
    source = next(
        v
        for v in pool.variants
        if pool.observation(v.observation_id).kind is ObservationKind.SOURCE_WINDOW
    )
    decoy = next(
        v
        for v in pool.variants
        if pool.observation(v.observation_id).kind is ObservationKind.LEXICAL_MENTION
    )
    judgment = JudgmentSet(
        capture.case_id,
        capture_digest(capture),
        "structural_truth",
        "construction_spec",
        (JudgmentFacet("source", True, (("source",),)),),
        (JudgmentWitness("source", (source.variant_id,)),),
        (decoy.variant_id,),
    )
    base = DecisionConfig(delivery=DeliveryBudget(max_chars=900, envelope_chars=256))
    base = replace(base, selection=replace(base.selection, role_diversity=False))
    baseline, good = evaluate_capture(capture, judgment, base)
    ablated_config = replace(
        base, selection=replace(base.selection, reserve_required=False)
    )
    _, ablated = evaluate_capture(capture, judgment, ablated_config)
    empty_config = replace(
        base,
        selection=replace(base.selection, reserve_required=False, fill_by_score=False),
    )
    _, empty = evaluate_capture(capture, judgment, empty_config)
    if not isinstance(baseline, DecisionDelivered) or baseline.bundle.selection is None:
        raise ContractError("instrument baseline failed to deliver an envelope")
    forged = replace(
        baseline,
        bundle=replace(
            baseline.bundle, selection=replace(baseline.bundle.selection, selected=())
        ),
    )
    dishonest = evaluate_decision(capture, forged, judgment, decision_config=base)
    unstable_capture = replace(
        capture,
        decision=replace(
            decision,
            pool=replace(pool, unstable_observation_ids=(source.observation_id,)),
        ),
    )
    unstable_judgment = replace(judgment, capture_id=capture_digest(unstable_capture))
    unstable = evaluate_decision(
        unstable_capture, baseline, unstable_judgment, decision_config=base
    )
    checks = {
        "required_reservation_preserves_source": good.critical_delivered == 1
        and not good.violations,
        "reservation_disabled_loses_source": ablated.critical_delivered == 0
        and not ablated.violations,
        "decoy_preference_delivers_known_negative": ablated.known_irrelevant_variants_delivered
        == 1,
        "empty_selector_loses_quality_honestly": empty.critical_delivered == 0
        and not empty.violations,
        "false_exact_source_claim_is_rejected": any(
            "claims exact source" in message for message in dishonest.violations
        ),
        "unstable_delivery_is_rejected": any(
            "unstable" in message for message in unstable.violations
        ),
    }
    if not all(checks.values()):
        raise ContractError(f"evaluation instrument checks failed: {checks}")
    return {"case": "reservation-canary", "ceiling_chars": 900, "checks": checks}
