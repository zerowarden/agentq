"""Capture authored repository cases through the real adapters.

The runner executes each CaseSpec against an isolated checkout with the
default adapter registry, records the prepared DecisionInput as an immutable
capture, and keeps ambiguous or unresolved requests as explicit attempts. It
never writes labels into captures: judgment drafts are compiled separately
from the derived variant aliases, and only when a draft exists.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentq.core import ContractError
from agentq.inspection.adapters import (
    CapabilityRegistry,
    default_registry,
    filesystem_version_reader,
)
from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.contracts import (
    AmbiguousTarget,
    InspectionContext,
    PreparedDecision,
    RepositoryIdentity,
    ResolutionResult,
    UnresolvedTarget,
)
from agentq.inspection.service import inspect

from .annotations import BenchmarkLabels
from .benchmark_labels import compile_labels
from .capture import producers_for
from .codec import capture_digest, decode_case_suite, read_json_file
from .judgments import JudgmentDraft, load_draft_directory, parse_draft
from .locking import SuiteBuilder
from .models import (
    DRAFT_SUITE_SCHEMA,
    AttemptOutcome,
    CaptureAttempt,
    CaseSpec,
    CaseSuite,
    ReplayCapture,
    SuiteLock,
)
from .repository import RepositoryError, repo_identity, repository_snapshot
from .selectors import variant_aliases
from .store import CaptureStore


@dataclass(frozen=True)
class CaptureReport:
    """The outcome of one capture run over a case suite."""

    suite_id: str
    attempts: tuple[CaptureAttempt, ...]
    lock: SuiteLock
    lock_path: Path
    attempts_path: Path


def load_case_suite(path: Path) -> CaseSuite:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ContractError(f"case file is unreadable: {path}") from exc
    return decode_case_suite(data)


def load_draft_suite(path: Path, *, suite_id: str) -> dict[str, JudgmentDraft]:
    """Case id -> authored draft from one judgments.json document."""
    value = read_json_file(path, what="judgment suite")
    if not isinstance(value, dict):
        raise ContractError(f"judgment suite must be a JSON object: {path}")
    if value.get("schema") != DRAFT_SUITE_SCHEMA:
        raise ContractError(
            f"unsupported judgment suite schema: {value.get('schema')!r}"
        )
    if value.get("suite_id") != suite_id:
        raise ContractError(
            f"judgment suite names {value.get('suite_id')!r}, not {suite_id!r}"
        )
    drafts = value.get("drafts")
    if not isinstance(drafts, list):
        raise ContractError("judgment suite drafts must be an array")
    compiled: dict[str, JudgmentDraft] = {}
    for index, entry in enumerate(drafts):
        draft = parse_draft(entry, what=f"judgment suite.drafts[{index}]")
        if draft.case_id in compiled:
            raise ContractError(f"duplicate judgment draft: {draft.case_id!r}")
        compiled[draft.case_id] = draft
    return compiled


def _attempt(
    spec: CaseSpec,
    resolution: ResolutionResult,
    *,
    checkout: Path,
    started_at: str,
    duration_ms: float,
) -> CaptureAttempt:
    common = {
        "case_id": spec.case_id,
        "checkout": str(checkout),
        "started_at": started_at,
        "duration_ms": duration_ms,
    }
    if isinstance(resolution, AmbiguousTarget):
        return CaptureAttempt(
            outcome=AttemptOutcome.AMBIGUOUS,
            reason="ambiguous_target",
            detail=f"{len(resolution.candidates)} declaration candidates retained",
            candidates=tuple(
                f"{item.path}:{item.span.start_line}-{item.span.end_line} "
                f"{item.signature}"
                for item in resolution.candidates
            ),
            **common,
        )
    if isinstance(resolution, UnresolvedTarget):
        detail = "; ".join(item.message for item in resolution.diagnostics)[:400]
        return CaptureAttempt(
            outcome=AttemptOutcome.UNRESOLVED,
            reason=resolution.reason.value,
            detail=detail,
            **common,
        )
    return CaptureAttempt(
        outcome=AttemptOutcome.FAILED,
        reason="no_prepared_input",
        detail="the inspection produced no prepared decision",
        **common,
    )


def capture_case(
    spec: CaseSpec,
    *,
    checkout: Path,
    registry: CapabilityRegistry | None = None,
) -> tuple[ReplayCapture | None, CaptureAttempt]:
    """Run one case against its checkout; a capture or an explicit attempt."""
    started_at = datetime.now(timezone.utc).isoformat()
    began = time.monotonic()
    context = InspectionContext(
        identity=RepositoryIdentity(root=checkout),
        registry=registry if registry is not None else default_registry(),
        source_versions=filesystem_version_reader(checkout),
        limits=AcquisitionLimits(),
    )
    prepared: list[PreparedDecision] = []
    bundle = inspect(spec.request, context, on_prepared=prepared.append)
    duration_ms = (time.monotonic() - began) * 1000.0
    if not prepared:
        return None, _attempt(
            spec,
            bundle.resolution,
            checkout=checkout,
            started_at=started_at,
            duration_ms=duration_ms,
        )
    decision = prepared[0].decision
    # The snapshot is taken after the run: a capture must describe the state
    # that produced it, so a provider that dirtied the tree is rejected here.
    snapshot = repository_snapshot(
        checkout,
        repo_id=repo_identity(
            kind=spec.source.kind,
            root=spec.source.root,
            repo=spec.source.repo,
            commit=spec.source.commit,
        ),
    )
    capture = ReplayCapture(
        case_id=spec.case_id,
        snapshot=snapshot,
        producers=producers_for(decision.pool),
        limits=prepared[0].limits,
        decision=decision,
        capability_report=prepared[0].capabilities,
    )
    return capture, CaptureAttempt(
        case_id=spec.case_id,
        outcome=AttemptOutcome.CAPTURED,
        capture_id=capture_digest(capture),
        checkout=str(checkout),
        started_at=started_at,
        duration_ms=duration_ms,
    )


def _case_checkout(
    spec: CaseSpec, checkout: Path | None, store: CaptureStore
) -> Path:
    """A repository case resolves its own pinned checkout; fixtures use --checkout."""
    if spec.source.kind != "repository":
        if checkout is None:
            raise ContractError(
                f"case {spec.case_id!r} needs an explicit checkout"
            )
        return checkout
    candidate = Path(spec.source.root)
    return candidate if candidate.is_absolute() else store.root / candidate


def _failed_attempt(
    spec: CaseSpec, checkout: Path | None, exc: Exception
) -> CaptureAttempt:
    return CaptureAttempt(
        case_id=spec.case_id,
        outcome=AttemptOutcome.FAILED,
        reason="capture_error",
        detail=str(exc)[:400],
        checkout="" if checkout is None else str(checkout),
        started_at=datetime.now(timezone.utc).isoformat(),
    )


def _drafts_for(
    suite_id: str, path: Path | None
) -> dict[str, JudgmentDraft]:
    if path is None:
        return {}
    if path.is_dir():
        return load_draft_directory(path)
    return load_draft_suite(path, suite_id=suite_id)


def capture_suite(
    case_path: Path,
    checkout: Path | None,
    store: CaptureStore,
    *,
    judgments_path: Path | None = None,
    registry: CapabilityRegistry | None = None,
) -> CaptureReport:
    """Capture every case in one case file; compile judgments when drafts exist."""
    suite = load_case_suite(case_path)
    return capture_suite_cases(
        suite,
        checkout,
        store,
        drafts=_drafts_for(suite.suite_id, judgments_path),
        registry=registry,
    )


def capture_suite_cases(
    suite: CaseSuite,
    checkout: Path | None,
    store: CaptureStore,
    *,
    drafts: Mapping[str, JudgmentDraft] | None = None,
    registry: CapabilityRegistry | None = None,
    labels: Mapping[str, BenchmarkLabels] | None = None,
) -> CaptureReport:
    """Capture every scheduled case; compile judgments when drafts exist.

    A case that cannot be captured at all is retained as a failed attempt, so
    missing checkouts and absent providers stay visible instead of aborting
    the whole run.
    """
    available = drafts or {}
    attempts: list[CaptureAttempt] = []
    tracks = {case.track for case in suite.cases}
    if len(tracks) != 1:
        raise ContractError("capture suites must contain exactly one evaluation track")
    builder = SuiteBuilder(store, suite.suite_id, track=tracks.pop())
    for spec in suite.cases:
        resolved: Path | None = None
        try:
            resolved = _case_checkout(spec, checkout, store)
            capture, attempt = capture_case(
                spec, checkout=resolved, registry=registry
            )
        except (ContractError, RepositoryError) as exc:
            attempts.append(_failed_attempt(spec, resolved, exc))
            continue
        if capture is None:
            attempts.append(attempt)
            continue
        draft = available.get(spec.case_id)
        annotation = (labels or {}).get(spec.case_id)
        try:
            if labels is not None and annotation is None:
                raise ContractError(f"missing benchmark labels for {spec.case_id!r}")
            judgment = None if annotation is None else compile_labels(capture, annotation, checkout=resolved)
        except (ContractError, UnicodeError, OSError) as exc:
            attempts.append(_failed_attempt(spec, resolved, exc))
            continue
        capture_id, _ = builder.add(
            spec.case_id,
            capture,
            draft=draft,
            aliases=(
                variant_aliases(capture.decision.pool)
                if draft is not None
                else None
            ),
            judgment=judgment,
            repository_family=spec.repository_family,
            original_inst_id=spec.original_inst_id,
        )
        if capture_id != attempt.capture_id:
            raise ContractError(
                f"capture identity changed while storing {spec.case_id!r}"
            )
        attempts.append(attempt)
    lock = builder.write()
    lock_path = store.lock_path(suite.suite_id)
    attempts_path = store.write_attempts(suite.suite_id, tuple(attempts))
    return CaptureReport(
        suite_id=suite.suite_id,
        attempts=tuple(attempts),
        lock=lock,
        lock_path=lock_path,
        attempts_path=attempts_path,
    )
