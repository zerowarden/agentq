"""Import pinned external authoring rows into reviewed-able case records.

This is the only module that reads evaluation-only authoring material
(problem statements, patches, gold spans). It validates each row strictly,
authors cases for two separate suites, excludes malformed rows with explicit
reasons, and assigns grouped splits so repositories, forks, and task families
cannot cross development, validation, and holdout boundaries. It never applies
a patch and never copies task text into a case record.

The two suites answer different questions and are never scored together:

- ``source-conformance`` anchors the request on the first gold span exactly as
  the annotator recorded it. The range policy then reserves that exact source,
  so the suite is a source-reading correctness gate, not a scoring corpus.
- ``context-selection`` anchors the request on the changed symbol named by the
  patch and scopes it to that symbol's package. The pool then holds competing
  declarations, references, and mentions, and reviewer-authored judgments
  witness the row's other gold spans among them. The gold context itself stays
  in the evaluation-only authoring sample: the reviewer reads it there when
  authoring the capture-bound draft, and it never enters a case record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentq.core import ContractError, canonical_json
from agentq.inspection.contracts import (
    InspectionRequest,
    Intent,
    RangeTarget,
    SourceSpan,
    SymbolTarget,
)

from .codec import decode_case_suite, decode_json, encode_case_spec
from .models import CaseSource, CaseSpec
from .repository import RepositoryError, run_git
from .store import CaptureStore

SUPPORTED_LANGUAGES = frozenset({"python", "typescript"})
SPLITS = ("development", "validation", "holdout")
SPLIT_SCHEMA = "agentq.eval.splits/v1"
CHECKOUT_ROOT = "checkouts"
GOLD_CONTEXT_FILE_KEYS = ("file", "path")

SOURCE_CONFORMANCE = "source-conformance"
CONTEXT_SELECTION = "context-selection"
SUITE_KINDS = (SOURCE_CONFORMANCE, CONTEXT_SELECTION)

_CASE_ID = re.compile(r"[A-Za-z0-9_.-]+")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_DIFF_SOURCE = re.compile(r"^--- a/(.+)$")
_HUNK_HEADER = re.compile(r"^@@ .*? @@(.*)$")
_SYMBOL_PATTERNS = (
    re.compile(r"\b(?:async\s+)?def\s+([^\W\d]\w*)"),
    re.compile(r"\bclass\s+([^\W\d]\w*)"),
    re.compile(r"\b(?:function|interface|type|enum|namespace)\s+([^\W\d][\w$]*)"),
    re.compile(r"\b(?:const|let|var)\s+([^\W\d][\w$]*)\s*="),
)


@dataclass(frozen=True)
class GoldSpan:
    """One annotated source location from an authoring row."""

    path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ChangedSymbol:
    """The declaration a patch hunk header names, and the file that holds it."""

    path: str
    name: str


@dataclass(frozen=True)
class ExcludedRow:
    """One row that could not become a case, with a visible reason."""

    instance_id: str
    reason: str


@dataclass(frozen=True)
class AdaptedRow:
    """Both suites' verdicts for one raw authoring row.

    A row that fails shared validation carries one exclusion and no cases; a
    valid row always carries its source-conformance case and either its
    context-selection case or the exclusion that kept it out of that suite.
    """

    instance_id: str
    source_conformance: CaseSpec | None
    context_selection: CaseSpec | None
    excluded: tuple[ExcludedRow, ...] = ()


@dataclass(frozen=True)
class ImportReport:
    """Accepted cases per suite plus every visible exclusion."""

    source_conformance: tuple[CaseSpec, ...]
    context_selection: tuple[CaseSpec, ...]
    excluded: tuple[ExcludedRow, ...]


def load_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    """Read a JSONL authoring sample; duplicate keys are rejected per line."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"authoring rows are unreadable: {path}") from exc
    rows: list[Mapping[str, object]] = []
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        value = decode_json(line, what=f"authoring row {index}")
        if not isinstance(value, dict):
            raise ContractError(f"authoring row {index} is not a JSON object")
        rows.append(value)
    if not rows:
        raise ContractError(f"authoring sample is empty: {path}")
    return tuple(rows)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_gold_spans(value: object) -> tuple[tuple[GoldSpan, ...], str | None]:
    """Decode gold_context into validated annotated source locations."""
    if isinstance(value, str):
        try:
            decoded = decode_json(value, what="gold_context")
        except ContractError as exc:
            return (), f"gold_context is not valid JSON: {exc}"
    else:
        decoded = value
    if not isinstance(decoded, list) or not decoded:
        return (), "gold_context must be a non-empty JSON array"
    spans: list[GoldSpan] = []
    for index, item in enumerate(decoded):
        if not isinstance(item, dict):
            return (), f"gold_context[{index}] is not an object"
        path = next(
            (
                item[key]
                for key in GOLD_CONTEXT_FILE_KEYS
                if isinstance(item.get(key), str)
            ),
            None,
        )
        start = item.get("start_line")
        end = item.get("end_line")
        if not isinstance(path, str) or not path:
            return (), f"gold_context[{index}] names no file"
        if not _is_int(start) or not _is_int(end) or start < 1 or end < start:
            return (), f"gold_context[{index}] has an invalid span"
        spans.append(GoldSpan(path=path, start_line=start, end_line=end))
    return tuple(spans), None


def parse_changed_symbol(patch: object) -> ChangedSymbol | None:
    """The first declaration a unified-diff hunk header names, if any.

    Patch hunk headers carry the enclosing declaration as context; they are the
    evaluation-only anchor for a context-selection request. A header with no
    declaration (a file creation, a bare import, an empty context) is skipped.
    """
    if not isinstance(patch, str):
        return None
    path: str | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            path = None
        if line.startswith("--- "):
            source = _DIFF_SOURCE.match(line)
            # Captures inspect the base commit, before renames and additions.
            path = None if source is None else source.group(1)
            continue
        header = _HUNK_HEADER.match(line)
        if header is None or path is None:
            continue
        name = _declared_name(header.group(1))
        if name is not None:
            return ChangedSymbol(path=path, name=name)
    return None


def _declared_name(context: str) -> str | None:
    for pattern in _SYMBOL_PATTERNS:
        match = pattern.search(context)
        if match is not None:
            return match.group(1)
    return None


def package_scope(path: str) -> str:
    """The package directory that holds one repository-relative file.

    A top-level file has no relative directory to scope, so it scopes itself.
    """
    directory, separator, _ = path.rpartition("/")
    return directory if separator else path


def family_key(repo: str) -> str:
    """Repo name without owner, so forks stay in one split group."""
    return repo.rsplit("/", 1)[-1]


def checkout_root(repo: str, commit: str) -> str:
    """The store-relative checkout root for one pinned repository commit."""
    return f"{CHECKOUT_ROOT}/{repo.replace('/', '__')}/{commit}"


def checkout_directory(source: CaseSource) -> str:
    """The store-relative checkout root for one repository source."""
    return checkout_root(source.repo, source.commit)


def suite_group(kind: str, repo: str) -> str:
    """A split group namespaced by suite, so the two suites never share one."""
    return f"{kind}:{family_key(repo)}"


def repository_source(repo: str, commit: str, repo_url: str) -> CaseSource:
    return CaseSource(
        kind="repository",
        root=checkout_root(repo, commit),
        fixture_revision="",
        repo=repo,
        commit=commit,
        repo_url=repo_url,
    )


def source_conformance_case(
    instance_id: str, source: CaseSource, spans: Sequence[GoldSpan]
) -> CaseSpec:
    """Anchor the request on the first annotated span, exactly as recorded."""
    anchor = spans[0]
    return CaseSpec(
        case_id=instance_id,
        source=source,
        request=InspectionRequest(
            target=RangeTarget(
                path=anchor.path,
                ranges=(
                    SourceSpan(
                        start_line=anchor.start_line, end_line=anchor.end_line
                    ),
                ),
            ),
            intent=Intent.EDIT,
        ),
        target_origin="supplied",
        judgment_basis="target_intent",
        split_group=suite_group(SOURCE_CONFORMANCE, source.repo),
    )


def context_selection_case(
    instance_id: str, source: CaseSource, patch: object
) -> CaseSpec | ExcludedRow:
    """Anchor on the changed symbol so competing context can be selected."""
    symbol = parse_changed_symbol(patch)
    if symbol is None:
        return ExcludedRow(
            instance_id, "patch names no changed symbol for context selection"
        )
    return CaseSpec(
        case_id=f"{instance_id}__selection",
        source=source,
        request=InspectionRequest(
            target=SymbolTarget(
                name=symbol.name, scopes=(package_scope(symbol.path),)
            ),
            intent=Intent.UNDERSTAND,
        ),
        target_origin="derived",
        judgment_basis="context_selection",
        split_group=suite_group(CONTEXT_SELECTION, source.repo),
    )


def _excluded_row(instance_id: str, reason: str) -> AdaptedRow:
    return AdaptedRow(instance_id, None, None, (ExcludedRow(instance_id, reason),))


def adapt_row(row: Mapping[str, object]) -> AdaptedRow:
    """Author both suite cases from one raw row, or exclude it with a reason."""
    instance_id = row.get("instance_id")
    if not isinstance(instance_id, str) or not _CASE_ID.fullmatch(instance_id):
        return _excluded_row(str(instance_id), "missing or invalid instance_id")
    language = row.get("language")
    if not isinstance(language, str) or language.lower() not in SUPPORTED_LANGUAGES:
        return _excluded_row(instance_id, f"unsupported language: {language!r}")
    commit = row.get("base_commit")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        return _excluded_row(instance_id, "base_commit must be a full git SHA")
    repo = row.get("repo")
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        return _excluded_row(instance_id, "repo must be owner/name")
    repo_url = row.get("repo_url")
    if not isinstance(repo_url, str) or not repo_url.startswith(
        ("https://", "file://", "/")
    ):
        return _excluded_row(instance_id, "repo_url must be https, file, or absolute")
    spans, error = parse_gold_spans(row.get("gold_context"))
    if error is not None:
        return _excluded_row(instance_id, error)
    source = repository_source(repo, commit, repo_url)
    selection = context_selection_case(instance_id, source, row.get("patch"))
    if isinstance(selection, ExcludedRow):
        return AdaptedRow(
            instance_id,
            source_conformance_case(instance_id, source, spans),
            None,
            (selection,),
        )
    return AdaptedRow(
        instance_id,
        source_conformance_case(instance_id, source, spans),
        selection,
    )


def import_rows(rows: Sequence[Mapping[str, object]]) -> ImportReport:
    """Adapt every row; ids are unique within each suite, never across them."""
    accepted: dict[str, list[CaseSpec]] = {kind: [] for kind in SUITE_KINDS}
    excluded: list[ExcludedRow] = []
    seen: dict[str, set[str]] = {kind: set() for kind in SUITE_KINDS}
    for row in rows:
        adapted = adapt_row(row)
        excluded.extend(adapted.excluded)
        cases = {
            SOURCE_CONFORMANCE: adapted.source_conformance,
            CONTEXT_SELECTION: adapted.context_selection,
        }
        for kind in SUITE_KINDS:
            case = cases[kind]
            if case is None:
                continue
            if case.case_id in seen[kind]:
                excluded.append(
                    ExcludedRow(
                        case.case_id, f"duplicate case id in {kind} sample"
                    )
                )
                continue
            seen[kind].add(case.case_id)
            accepted[kind].append(case)
    return ImportReport(
        source_conformance=tuple(accepted[SOURCE_CONFORMANCE]),
        context_selection=tuple(accepted[CONTEXT_SELECTION]),
        excluded=tuple(excluded),
    )


def assign_splits(cases: Sequence[CaseSpec]) -> dict[str, str]:
    """Sorted split groups assigned round-robin; no group ever crosses."""
    groups = sorted({case.split_group for case in cases})
    by_group = {
        group: SPLITS[index % len(SPLITS)] for index, group in enumerate(groups)
    }
    return {case.case_id: by_group[case.split_group] for case in cases}


def splits_document(cases: Sequence[CaseSpec]) -> dict[str, object]:
    assignments = assign_splits(cases)
    groups: dict[str, str] = {}
    for case in cases:
        groups.setdefault(case.split_group, assignments[case.case_id])
    return {
        "schema": SPLIT_SCHEMA,
        "rule": (
            "split groups sorted and assigned round-robin across "
            "development, validation, and holdout"
        ),
        "assignments": dict(sorted(assignments.items())),
        "groups": dict(sorted(groups.items())),
    }


def write_new_or_equal(path: Path, data: bytes) -> None:
    """Never overwrite a changed reviewed record."""
    if path.exists():
        if path.read_bytes() != data:
            raise ContractError(f"refusing to overwrite a changed record: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_cases(cases: Sequence[CaseSpec], directory: Path) -> tuple[Path, ...]:
    written: list[Path] = []
    for case in cases:
        path = directory / f"{case.case_id}.json"
        write_new_or_equal(path, encode_case_spec(case))
        written.append(path)
    return tuple(written)


def write_splits(cases: Sequence[CaseSpec], path: Path) -> Path:
    write_new_or_equal(path, (canonical_json(splits_document(cases)) + "\n").encode())
    return path


def load_case_files(directory: Path) -> tuple[CaseSpec, ...]:
    """Every single-case record in one directory, case-id ordered."""
    cases: list[CaseSpec] = []
    for path in sorted(directory.glob("*.json")):
        suite = decode_case_suite(path.read_bytes())
        if len(suite.cases) != 1:
            raise ContractError(f"case record must hold one case: {path}")
        cases.append(suite.cases[0])
    if not cases:
        raise ContractError(f"no case records found in {directory}")
    return tuple(cases)


def ensure_checkout(
    source: CaseSource, store: CaptureStore, *, url: str | None = None
) -> str:
    """Clone one pinned source at its exact commit; never run project code."""
    if source.kind != "repository":
        raise ContractError("only repository sources have checkouts")
    if not source.repo:
        raise ContractError("repository source has no repo")
    if url is None:
        url = source.repo_url
    if not url or not url.startswith(("https://", "file://", "/")):
        raise ContractError(f"unsupported checkout URL for {source.repo}: {url!r}")
    destination = store.root / checkout_directory(source)
    if (destination / ".git").is_dir():
        return _verify_checkout(destination, source)
    destination.mkdir(parents=True, exist_ok=True)
    run_git(destination, "init", "--quiet")
    run_git(destination, "remote", "add", "origin", url)
    run_git(
        destination,
        "fetch",
        "--quiet",
        "--depth",
        "1",
        "origin",
        source.commit,
        timeout=600,
    )
    run_git(destination, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    return _verify_checkout(destination, source)


def _verify_checkout(destination: Path, source: CaseSource) -> str:
    head = run_git(destination, "rev-parse", "HEAD").strip()
    if head != source.commit:
        raise RepositoryError(
            f"checkout {destination} is at {head[:12]}, expected "
            f"{source.commit[:12]}"
        )
    if run_git(destination, "status", "--porcelain").strip():
        raise RepositoryError(f"checkout is not clean: {destination}")
    return str(destination)
