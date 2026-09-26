"""Completion-transfer captures from supplied snippets, with separate labels.

The adapter does not infer semantic bindings from relevance. Candidate excerpts
have unknown source coordinates and binding; the cropped completion context is
an explicitly supplied virtual document. Gold indices and next_line never enter
the input builder. The runtime decision pipeline remains unchanged.
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass

from agentq.core import (
    COMPLETE,
    ContractError,
    SourceRef,
    canonical_digest,
    typed_coverage,
)
from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.contracts import (
    AcquisitionRecord,
    Capability,
    CollectionPlan,
    CollectionStatus,
    DecisionInput,
    EvidencePool,
    Fidelity,
    InspectionRequest,
    Intent,
    MentionPayload,
    ObservationKind,
    RangeTarget,
    RepresentationKind,
    ResolvedTarget,
    SelectionMethod,
    SourceSpan,
    SourceVersion,
    SourceWindowPayload,
    make_observation,
    make_variant,
    with_request_id,
)
from agentq.inspection.policy import compile_policy

from .annotations import BenchmarkLabels, canonical_repository, relative_source_path
from .benchmark_labels import compile_labels
from .capture import make_capture
from .locking import SuiteBuilder
from .models import FixtureSnapshot, JudgmentSet, ReplayCapture, SuiteLock
from .store import CaptureStore


@dataclass(frozen=True)
class Snippet:
    path: str
    identifier: str
    snippet: str

    @property
    def key(self) -> str:
        return canonical_digest(asdict(self))


@dataclass(frozen=True)
class CompletionInput:
    repository: str
    file_path: str
    preceding_code: str
    snippets: tuple[Snippet, ...]

    @property
    def original_task_id(self) -> str:
        """Content identity survives a declared repository fork or rename."""
        return canonical_digest(
            {"path": self.file_path, "preceding_code": self.preceding_code}
        )

    @property
    def task_id(self) -> str:
        return canonical_digest(
            {
                "repo": self.repository,
                "path": self.file_path,
                "preceding_code": self.preceding_code,
            }
        )


def completion_input(row: Mapping[str, object]) -> CompletionInput:
    """Allowlisted selector input; ordering depends only on input content."""
    from .wire.json import as_list, as_mapping, read_str

    repository = canonical_repository(read_str(row.get("repo_name"), "repo_name"))
    path = relative_source_path(read_str(row.get("file_path"), "file_path"))
    code = read_str(row.get("cropped_code"), "cropped_code")
    snippets = []
    for value in as_list(row.get("context"), "context"):
        item = as_mapping(value, "snippet")
        snippets.append(
            Snippet(
                relative_source_path(read_str(item.get("path"), "snippet path")),
                read_str(item.get("identifier"), "snippet identifier"),
                read_str(item.get("snippet"), "snippet text"),
            )
        )
    if not snippets or len({s.key for s in snippets}) != len(snippets):
        raise ContractError("completion candidates must be nonempty and distinct")
    seed = canonical_digest({"repo": repository, "path": path, "code": code})
    shuffled = tuple(
        sorted(snippets, key=lambda snippet: canonical_digest((seed, snippet.key)))
    )
    return CompletionInput(repository, path, code, shuffled)


def capture_completion(
    value: CompletionInput, revision: str
) -> tuple[ReplayCapture, dict[str, str]]:
    """Capture the supplied corpus once, without consulting benchmark labels."""
    case_id = "repobench-" + value.task_id[:24]
    context_path = "supplied-completion/context.py"
    span = SourceSpan(start_line=1, end_line=len(value.preceding_code.splitlines()))
    request = with_request_id(
        InspectionRequest(
            target=RangeTarget(path=context_path, ranges=(span,)),
            intent=Intent.UNDERSTAND,
        ),
        case_id,
    )
    acquisition = AcquisitionRecord(
        acquisition_id=canonical_digest({"case": case_id, "revision": revision}),
        capability=Capability.LEXICAL_MENTIONS,
        provider="repobench-supplied-corpus",
        provider_version=revision,
        method="prechunked documents; no binding analysis",
        effective_scope=tuple(sorted({s.path for s in value.snippets})),
        coverage=typed_coverage(COMPLETE),
        status=CollectionStatus.COMPLETED,
    )
    source = SourceRef(path=context_path, start_line=1, end_line=span.end_line)
    context = make_observation(
        kind=ObservationKind.SOURCE_WINDOW,
        source=source,
        payload=SourceWindowPayload(text=value.preceding_code, span=span),
        source_versions=(
            SourceVersion(
                path=context_path, version=canonical_digest(value.preceding_code)
            ),
        ),
        acquisition_id=acquisition.acquisition_id,
    )
    context_variant = make_variant(
        observation_id=context.observation_id,
        source=source,
        representation=RepresentationKind.EXACT_SOURCE,
        fidelity=Fidelity.EXACT,
        text=value.preceding_code,
        span=span,
    )
    observations = [context]
    variants = [context_variant]
    mapping = {}
    for snippet in value.snippets:
        source = SourceRef(path=snippet.path, symbol=snippet.identifier)
        observation = make_observation(
            kind=ObservationKind.LEXICAL_MENTION,
            source=source,
            payload=MentionPayload(text=snippet.snippet),
            acquisition_id=acquisition.acquisition_id,
            source_versions=(SourceVersion(path=snippet.path, version=snippet.key),),
        )
        variant = make_variant(
            observation_id=observation.observation_id,
            source=source,
            representation=RepresentationKind.EXCERPT,
            fidelity=Fidelity.SUMMARY,
            text=snippet.snippet,
        )
        observations.append(observation)
        variants.append(variant)
        mapping[snippet.key] = variant.variant_id
    resolution = ResolvedTarget(
        target=request.target, method=SelectionMethod.DIRECT_TARGET
    )
    policy = compile_policy(request, resolution)
    decision = DecisionInput(
        request=request,
        resolution=resolution,
        policy=policy,
        collection=CollectionPlan(
            profile="supplied-corpus-v1",
            request_id=request.request_id,
            target=request.target,
        ),
        pool=EvidencePool(
            request_id=request.request_id,
            observations=tuple(observations),
            variants=tuple(variants),
            acquisitions=(acquisition,),
        ),
    )
    capture = make_capture(
        case_id,
        decision,
        snapshot=FixtureSnapshot(
            fixture_id="repobench:" + value.repository,
            fixture_revision=revision,
            content_digest=canonical_digest(asdict(value)),
        ),
        limits=AcquisitionLimits(),
    )
    return capture, mapping


def compile_completion_labels(
    row: Mapping[str, object],
    capture: ReplayCapture,
    mapping: Mapping[str, str],
    revision: str,
) -> JudgmentSet:
    from .wire.json import as_list, as_mapping, read_int, read_str

    # Resolve the gold key from the original row, then find it after shuffling.
    snippets = as_list(row.get("context"), "context")
    index = read_int(row.get("gold_snippet_index"), "gold snippet index", minimum=0)
    if index >= len(snippets):
        raise ContractError("gold snippet index exceeds candidate count")
    item = as_mapping(snippets[index], "gold snippet")
    key = Snippet(
        relative_source_path(read_str(item["path"], "path")),
        read_str(item["identifier"], "identifier"),
        read_str(item["snippet"], "snippet"),
    ).key
    if key not in mapping:
        raise ContractError("gold candidate was lost during corpus adaptation")
    return compile_labels(
        capture,
        BenchmarkLabels(
            label_source="tianyang/repobench_python_v1.1",
            label_revision=revision,
            objective="completion_dependency",
            positive_ids=(mapping[key],),
        ),
    )


def build_repobench(
    rows: Sequence[Mapping[str, object]],
    revision: str,
    store: CaptureStore,
    *,
    suite_id: str = "repobench-transfer-v1",
    families: Mapping[str, str] | None = None,
) -> tuple[SuiteLock, tuple[dict[str, str], ...]]:
    builder = SuiteBuilder(store, suite_id, track="transfer")
    seen: set[tuple[str, str]] = set()
    exclusions = []
    for index, row in enumerate(rows):
        try:
            value = completion_input(row)
            family = canonical_repository(value.repository, families)
            key = (family, value.original_task_id)
            if key in seen:
                raise ContractError("duplicate underlying completion task")
            capture, mapping = capture_completion(value, revision)
            labels = compile_completion_labels(row, capture, mapping, revision)
            builder.add(
                capture.case_id,
                capture,
                judgment=labels,
                repository_family=family,
                original_inst_id=value.original_task_id,
            )
            seen.add(key)
        except ContractError as exc:
            exclusions.append({"row": str(index), "reason": str(exc)})
    return builder.write(), tuple(exclusions)
