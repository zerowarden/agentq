"""Import pinned external authoring rows into reviewed-able case records.

This is the only module that reads evaluation-only authoring material
(problem statements, patches, gold spans). It validates each row strictly,
authors one CaseSpec per accepted row, excludes malformed rows with explicit
reasons, and assigns grouped splits so repositories, forks, and task families
cannot cross development, validation, and holdout boundaries. It never applies
a patch and never copies task text into a case record.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentq.core import AgentQError, ContractError, canonical_json
from agentq.execution import run_cmd
from agentq.inspection.contracts import (
    InspectionRequest,
    Intent,
    RangeTarget,
    SourceSpan,
)

from .codec import decode_case_suite, decode_json, encode_case_spec
from .models import CaseSource, CaseSpec
from .repository import RepositoryError
from .store import CaptureStore

SUPPORTED_LANGUAGES = frozenset({"python", "typescript"})
SPLITS = ("development", "validation", "holdout")
SPLIT_SCHEMA = "agentq.eval.splits/v1"
CHECKOUT_ROOT = "checkouts"
GOLD_CONTEXT_FILE_KEYS = ("file", "path")

_CASE_ID = re.compile(r"[A-Za-z0-9_.-]+")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ExcludedRow:
    """One row that could not become a case, with a visible reason."""

    instance_id: str
    reason: str


@dataclass(frozen=True)
class ImportReport:
    cases: tuple[CaseSpec, ...]
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


def parse_gold_spans(
    value: object,
) -> tuple[tuple[tuple[str, int, int], ...], str | None]:
    """Decode gold_context into validated (file, start_line, end_line) spans."""
    if isinstance(value, str):
        try:
            decoded = decode_json(value, what="gold_context")
        except ContractError as exc:
            return (), f"gold_context is not valid JSON: {exc}"
    else:
        decoded = value
    if not isinstance(decoded, list) or not decoded:
        return (), "gold_context must be a non-empty JSON array"
    spans: list[tuple[str, int, int]] = []
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
        spans.append((path, start, end))
    return tuple(spans), None


def family_key(repo: str) -> str:
    """Repo name without owner, so forks stay in one split group."""
    return repo.rsplit("/", 1)[-1]


def checkout_directory(source: CaseSource) -> str:
    """The store-relative checkout root for one repository source."""
    key = source.repo.replace("/", "__")
    return f"{CHECKOUT_ROOT}/{key}/{source.commit}"


def adapt_row(row: Mapping[str, object]) -> CaseSpec | ExcludedRow:
    """Author one case from one raw row, or exclude it with a reason."""
    instance_id = row.get("instance_id")
    if not isinstance(instance_id, str) or not _CASE_ID.fullmatch(instance_id):
        return ExcludedRow(str(instance_id), "missing or invalid instance_id")
    language = row.get("language")
    if not isinstance(language, str) or language.lower() not in SUPPORTED_LANGUAGES:
        return ExcludedRow(instance_id, f"unsupported language: {language!r}")
    commit = row.get("base_commit")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        return ExcludedRow(instance_id, "base_commit must be a full git SHA")
    repo = row.get("repo")
    if not isinstance(repo, str) or not _REPO.fullmatch(repo):
        return ExcludedRow(instance_id, "repo must be owner/name")
    repo_url = row.get("repo_url")
    if not isinstance(repo_url, str) or not repo_url.startswith(
        ("https://", "file://", "/")
    ):
        return ExcludedRow(instance_id, "repo_url must be https, file, or absolute")
    spans, error = parse_gold_spans(row.get("gold_context"))
    if error is not None:
        return ExcludedRow(instance_id, error)
    path, start_line, end_line = spans[0]
    source = CaseSource(
        kind="repository",
        root=f"{CHECKOUT_ROOT}/{repo.replace('/', '__')}/{commit}",
        fixture_revision="",
        repo=repo,
        commit=commit,
        repo_url=repo_url,
    )
    request = InspectionRequest(
        target=RangeTarget(
            path=path, ranges=(SourceSpan(start_line=start_line, end_line=end_line),)
        ),
        intent=Intent.EDIT,
    )
    return CaseSpec(
        case_id=instance_id,
        source=source,
        request=request,
        target_origin="supplied",
        judgment_basis="target_intent",
        split_group=f"family:{family_key(repo)}",
    )


def import_rows(rows: Sequence[Mapping[str, object]]) -> ImportReport:
    """Adapt every row; accepted cases are unique by id and by family task."""
    cases: list[CaseSpec] = []
    excluded: list[ExcludedRow] = []
    seen: set[str] = set()
    for row in rows:
        adapted = adapt_row(row)
        if isinstance(adapted, ExcludedRow):
            excluded.append(adapted)
            continue
        if adapted.case_id in seen:
            excluded.append(
                ExcludedRow(adapted.case_id, "duplicate case id in sample")
            )
            continue
        seen.add(adapted.case_id)
        cases.append(adapted)
    return ImportReport(cases=tuple(cases), excluded=tuple(excluded))


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
    _git(destination, "init", "--quiet")
    _git(destination, "remote", "add", "origin", url)
    _git(destination, "fetch", "--quiet", "--depth", "1", "origin", source.commit)
    _git(destination, "checkout", "--quiet", "--detach", "FETCH_HEAD")
    return _verify_checkout(destination, source)


def _verify_checkout(destination: Path, source: CaseSource) -> str:
    head = _git(destination, "rev-parse", "HEAD").strip()
    if head != source.commit:
        raise RepositoryError(
            f"checkout {destination} is at {head[:12]}, expected "
            f"{source.commit[:12]}"
        )
    if _git(destination, "status", "--porcelain").strip():
        raise RepositoryError(f"checkout is not clean: {destination}")
    return str(destination)


def _git(cwd: Path, *args: str) -> str:
    try:
        result = run_cmd(["git", *args], cwd=cwd, timeout=600)
    except (AgentQError, OSError) as exc:
        raise RepositoryError(f"git could not run in {cwd}: {exc}") from exc
    if result.returncode != 0:
        raise RepositoryError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
        )
    return result.stdout
