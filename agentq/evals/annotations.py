"""Independent benchmark annotations; never part of a decision input.

Locations are retained even when acquisition found no matching variant. Missing
annotations mean unjudged, not negative. Dataset provenance is part of the
serialized judgment and therefore of the experiment identity.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath

from agentq.core import ContractError, require_int, require_str

TRACKS = frozenset({"conformance", "stress", "natural", "transfer"})
OBJECTIVES = frozenset(
    {"facet_coverage", "task_context_coverage", "completion_dependency"}
)


def relative_source_path(path: str) -> str:
    require_str(path, "annotation path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or "\\" in path:
        raise ContractError(f"source path must be repository-relative: {path!r}")
    return parsed.as_posix()


def canonical_repository(repo: str, families: Mapping[str, str] | None = None) -> str:
    """Canonical owner/name, with explicitly declared fork aliases.

    A basename is not an identity: unrelated owners can use the same name and
    forks can rename it. Unknown repositories retain their full identity.
    """
    name = (
        repo.removeprefix("https://github.com/").removesuffix(".git").strip("/").lower()
    )
    if len(name.split("/")) != 2 or any(not part for part in name.split("/")):
        raise ContractError(f"repository must be owner/name: {repo!r}")
    aliases = {key.lower(): value.lower() for key, value in (families or {}).items()}
    seen: set[str] = set()
    while name in aliases:
        if name in seen:
            raise ContractError("repository family aliases contain a cycle")
        seen.add(name)
        name = canonical_repository(aliases[name])
    return name


@dataclass(frozen=True, order=True)
class AnnotatedSpan:
    path: str
    start_line: int
    end_line: int

    def __post_init__(self) -> None:
        if relative_source_path(self.path) != self.path:
            raise ContractError("annotation path must be normalized")
        require_int(self.start_line, "annotation start", minimum=1)
        require_int(self.end_line, "annotation end", minimum=self.start_line)


@dataclass(frozen=True)
class BenchmarkLabels:
    label_source: str
    label_revision: str
    objective: str
    positive_spans: tuple[AnnotatedSpan, ...] = ()
    positive_ids: tuple[str, ...] = ()
    known_negative_ids: tuple[str, ...] = ()
    unlabeled_is_negative: bool = False

    def __post_init__(self) -> None:
        require_str(self.label_source, "label source")
        require_str(self.label_revision, "label revision")
        if self.objective not in OBJECTIVES:
            raise ContractError(f"unsupported annotation objective: {self.objective!r}")
        if self.unlabeled_is_negative is not False:
            raise ContractError(
                "unlabeled benchmark evidence cannot be treated as negative"
            )
        if not isinstance(self.positive_spans, tuple) or not all(
            isinstance(span, AnnotatedSpan) for span in self.positive_spans
        ):
            raise ContractError("positive spans must be typed annotations")
        for ids in (self.positive_ids, self.known_negative_ids):
            if not isinstance(ids, tuple) or any(
                not isinstance(value, str) or not value for value in ids
            ):
                raise ContractError("annotation ids must be nonempty strings")
            if len(ids) != len(set(ids)):
                raise ContractError("duplicate annotation ids")
        if set(self.positive_ids) & set(self.known_negative_ids):
            raise ContractError("an annotation cannot be both positive and negative")

    def to_wire(self) -> dict[str, object]:
        return {
            **asdict(self),
            "positive_spans": [asdict(span) for span in self.positive_spans],
            "positive_ids": list(self.positive_ids),
            "known_negative_ids": list(self.known_negative_ids),
        }


def labels_from_wire(value: object) -> BenchmarkLabels:
    from .wire.json import (
        as_list,
        object_fields,
        read_bool,
        read_int,
        read_str,
        read_strings,
    )

    raw = object_fields(
        value,
        "benchmark labels",
        {
            "label_source",
            "label_revision",
            "objective",
            "positive_spans",
            "positive_ids",
            "known_negative_ids",
            "unlabeled_is_negative",
        },
    )
    spans = []
    for item in as_list(raw["positive_spans"], "positive spans"):
        span = object_fields(item, "positive span", {"path", "start_line", "end_line"})
        spans.append(
            AnnotatedSpan(
                read_str(span["path"], "span path"),
                read_int(span["start_line"], "span start", minimum=1),
                read_int(span["end_line"], "span end", minimum=1),
            )
        )
    return BenchmarkLabels(
        label_source=read_str(raw["label_source"], "label source"),
        label_revision=read_str(raw["label_revision"], "label revision"),
        objective=read_str(raw["objective"], "label objective"),
        positive_spans=tuple(spans),
        positive_ids=read_strings(raw["positive_ids"], "positive ids"),
        known_negative_ids=read_strings(raw["known_negative_ids"], "negative ids"),
        unlabeled_is_negative=read_bool(
            raw["unlabeled_is_negative"], "unlabeled_is_negative"
        ),
    )


def merged_spans(spans: Iterable[AnnotatedSpan]) -> tuple[AnnotatedSpan, ...]:
    merged: list[AnnotatedSpan] = []
    for span in sorted(spans):
        if (
            merged
            and span.path == merged[-1].path
            and span.start_line <= merged[-1].end_line + 1
        ):
            previous = merged.pop()
            span = AnnotatedSpan(
                span.path, previous.start_line, max(previous.end_line, span.end_line)
            )
        merged.append(span)
    return tuple(merged)


def covered_lines(
    gold: Iterable[AnnotatedSpan], available: Iterable[AnnotatedSpan]
) -> int:
    left, right = merged_spans(gold), merged_spans(available)
    return sum(
        max(0, min(a.end_line, b.end_line) - max(a.start_line, b.start_line) + 1)
        for a in left
        for b in right
        if a.path == b.path
    )


@dataclass(frozen=True)
class StageCoverage:
    total: int = 0
    pool: int = 0
    initial: int = 0
    delivered: int = 0

    def to_wire(self) -> dict[str, object]:
        return {
            **asdict(self),
            "coverage": None if not self.total else self.delivered / self.total,
            "acquisition_loss": self.total - self.pool,
            "selection_loss": self.pool - self.initial,
            "fitting_loss": self.initial - self.delivered,
        }


@dataclass(frozen=True)
class AnnotationCoverage:
    lines: StageCoverage = StageCoverage()
    files: StageCoverage = StageCoverage()
    documents: StageCoverage = StageCoverage()

    def to_wire(self) -> dict[str, object]:
        return {
            name: getattr(self, name).to_wire()
            for name in ("lines", "files", "documents")
        }


def sum_coverage(values: Iterable[AnnotationCoverage]) -> AnnotationCoverage:
    items = tuple(values)
    return AnnotationCoverage(
        **{
            name: StageCoverage(
                **{
                    stage: sum(getattr(getattr(item, name), stage) for item in items)
                    for stage in ("total", "pool", "initial", "delivered")
                }
            )
            for name in ("lines", "files", "documents")
        }
    )
