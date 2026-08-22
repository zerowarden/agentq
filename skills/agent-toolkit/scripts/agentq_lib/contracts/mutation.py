"""Typed mutation contracts: plans, approved byte changes, apply policy, outcomes.

The current production plan schema (``agentq.codemod-plan/v1``) is decoded
strictly so tampered or malformed persisted plans fail before any mutator runs.
A plan digest is integrity metadata, never proof of user authorization.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ._base import (
    ContractError,
    canonical_digest,
    optional_int,
    optional_str,
    reject_unknown_keys,
    require_bool,
    require_int,
    require_mapping,
    require_relative_posix,
    require_sha256,
    require_str,
)

MUTATION_PLAN_SCHEMA_V1 = "agentq.codemod-plan/v1"
MUTATION_OUTCOME_SCHEMA = "agentq.mutation-outcome/v1"
KNOWN_MUTATION_PLAN_SCHEMAS = frozenset({MUTATION_PLAN_SCHEMA_V1})


class Engine(str, Enum):
    AST_GREP = "ast-grep"
    PYTHON_RE = "python-re"
    FIXED = "fixed"


class MutationStatus(str, Enum):
    APPLIED = "applied"
    NOOP = "noop"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_PARTIAL = "rollback_partial"


@dataclass(frozen=True)
class ByteEdit:
    """One ordered, non-overlapping byte replacement in the original bytes."""

    start: int
    end: int
    replacement: str

    def __post_init__(self) -> None:
        require_int(self.start, "edit start", minimum=0)
        require_int(self.end, "edit end", minimum=0)
        if self.end < self.start:
            raise ContractError("edit end must be >= start")
        require_str(self.replacement, "edit replacement", allow_empty=True)

    def to_wire(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "replacement": self.replacement}


@dataclass(frozen=True)
class PlannedFile:
    path: str
    sha256: str | None = None
    matches: int = 0
    match_spans: tuple[tuple[int, int], ...] = ()
    edits: tuple[ByteEdit, ...] = ()
    postimage_sha256: str | None = None

    def __post_init__(self) -> None:
        require_relative_posix(self.path, "planned file path")
        if self.sha256 is not None:
            require_sha256(self.sha256, f"planned file preimage hash for {self.path}")
        if self.postimage_sha256 is not None:
            require_sha256(
                self.postimage_sha256, f"planned file postimage hash for {self.path}"
            )
        require_int(self.matches, "planned file match count", minimum=0)
        previous_end = -1
        for span in self.match_spans:
            if (
                not isinstance(span, tuple)
                or len(span) != 2
                or isinstance(span[0], bool)
                or isinstance(span[1], bool)
                or not isinstance(span[0], int)
                or not isinstance(span[1], int)
            ):
                raise ContractError(
                    f"planned file {self.path} has a malformed match span"
                )
            start, end = span
            if start < 0 or end < start:
                raise ContractError(
                    f"planned file {self.path} has an invalid match span"
                )
            if start < previous_end:
                raise ContractError(
                    f"planned file {self.path} has overlapping or unordered match spans"
                )
            previous_end = end
        previous_end = -1
        for edit in self.edits:
            if not isinstance(edit, ByteEdit):
                raise ContractError(
                    f"planned file {self.path} edits must be ByteEdit values"
                )
            if edit.start < previous_end:
                raise ContractError(
                    f"planned file {self.path} has overlapping or unordered edits"
                )
            previous_end = edit.end
        if self.edits and not self.postimage_sha256:
            raise ContractError(
                f"planned file {self.path} with exact edits requires a postimage hash"
            )

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "path": self.path,
            "sha256": self.sha256,
            "matches": self.matches,
        }
        if self.match_spans:
            wire["match_spans"] = [[start, end] for start, end in self.match_spans]
        if self.edits:
            wire["edits"] = [edit.to_wire() for edit in self.edits]
        if self.postimage_sha256 is not None:
            wire["postimage_sha256"] = self.postimage_sha256
        return wire


@dataclass(frozen=True)
class MutationPlan:
    schema: str
    plan_id: str
    engine: str
    pattern: str
    rewrite: str | None
    scopes: tuple[str, ...]
    files: tuple[PlannedFile, ...]
    language: str | None = None
    engine_version: str | None = None
    repo_id: str | None = None
    worktree_id: str | None = None
    applicable: bool = True

    def __post_init__(self) -> None:
        if self.schema not in KNOWN_MUTATION_PLAN_SCHEMAS:
            raise ContractError(f"unsupported mutation plan schema: {self.schema!r}")
        require_str(self.plan_id, "mutation plan id")
        if self.engine not in {item.value for item in Engine}:
            raise ContractError(f"unsupported mutation engine: {self.engine!r}")
        require_str(self.pattern, "mutation plan pattern")
        optional_str(self.rewrite, "mutation plan rewrite", allow_empty=True)
        if not self.scopes:
            raise ContractError("mutation plan must declare at least one scope")
        for scope in self.scopes:
            require_relative_posix(scope, "mutation plan scope", allow_root=True)
        if not isinstance(self.files, tuple) or not all(
            isinstance(item, PlannedFile) for item in self.files
        ):
            raise ContractError("mutation plan files must be a tuple of PlannedFile")
        paths = [item.path for item in self.files]
        if len(set(paths)) != len(paths):
            raise ContractError("mutation plan contains duplicate file paths")
        if self.engine == Engine.AST_GREP.value and not self.language:
            raise ContractError(
                "AST mutation plan has no language provenance; regenerate the plan"
            )
        optional_str(self.language, "mutation plan language")
        optional_str(self.engine_version, "mutation plan engine version")
        optional_str(self.repo_id, "mutation plan repo id")
        optional_str(self.worktree_id, "mutation plan worktree id")
        require_bool(self.applicable, "mutation plan applicable flag")

    def require_applicable(self) -> None:
        if not self.applicable or self.rewrite is None:
            raise ContractError(
                "mutation plan has no rewrite; regenerate the plan with --rewrite to make it applicable"
            )

    @property
    def exact(self) -> bool:
        return bool(self.files) and all(item.edits for item in self.files)

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema": self.schema,
            "engine": self.engine,
            "pattern": self.pattern,
            "rewrite": self.rewrite,
            "scopes": list(self.scopes),
            "files": [item.to_wire() for item in self.files],
            "plan_id": self.plan_id,
        }
        if self.language is not None:
            wire["language"] = self.language
        if self.engine_version is not None:
            wire["engine_version"] = self.engine_version
        if self.repo_id is not None:
            wire["repo_id"] = self.repo_id
        if self.worktree_id is not None:
            wire["worktree_id"] = self.worktree_id
        return wire

    @classmethod
    def from_wire(cls, value: Any, *, what: str = "mutation plan") -> MutationPlan:
        payload = require_mapping(value, what)
        reject_unknown_keys(
            payload,
            (
                "schema",
                "engine",
                "pattern",
                "rewrite",
                "scopes",
                "files",
                "plan_id",
                "language",
                "engine_version",
                "repo_id",
                "worktree_id",
            ),
            what,
        )
        schema = require_str(payload.get("schema"), f"{what}.schema")
        if schema not in KNOWN_MUTATION_PLAN_SCHEMAS:
            raise ContractError(
                f"unsupported mutation plan schema {schema!r}; regenerate the plan with this agentq version"
            )
        engine = require_str(payload.get("engine"), f"{what}.engine")
        if engine not in {item.value for item in Engine}:
            raise ContractError(f"{what}.engine is not a supported engine: {engine!r}")
        scopes = payload.get("scopes")
        if not isinstance(scopes, list) or not scopes:
            raise ContractError(f"{what}.scopes must be a non-empty array")
        files = payload.get("files")
        if not isinstance(files, list):
            raise ContractError(f"{what}.files must be an array")
        plan_id = require_str(payload.get("plan_id"), f"{what}.plan_id")
        digest = plan_digest(payload)
        if plan_id != digest:
            raise ContractError(
                f"{what} digest does not match its content; regenerate the plan"
            )
        decoded_files = tuple(
            _planned_file_from_wire(item, what, index)
            for index, item in enumerate(files)
        )
        rewrite_value = payload.get("rewrite")
        rewrite = (
            None
            if rewrite_value is None
            else require_str(rewrite_value, f"{what}.rewrite", allow_empty=True)
        )
        return cls(
            schema=schema,
            plan_id=plan_id,
            engine=engine,
            pattern=require_str(payload.get("pattern"), f"{what}.pattern"),
            rewrite=rewrite,
            scopes=tuple(
                require_str(scope, f"{what}.scopes entry") for scope in scopes
            ),
            files=decoded_files,
            language=optional_str(payload.get("language"), f"{what}.language"),
            engine_version=optional_str(
                payload.get("engine_version"), f"{what}.engine_version"
            ),
            repo_id=optional_str(payload.get("repo_id"), f"{what}.repo_id"),
            worktree_id=optional_str(payload.get("worktree_id"), f"{what}.worktree_id"),
            applicable=rewrite is not None,
        )


def _planned_file_from_wire(value: Any, what: str, index: int) -> PlannedFile:
    entry = require_mapping(value, f"{what}.files[{index}]")
    reject_unknown_keys(
        entry,
        ("path", "sha256", "matches", "match_spans", "edits", "postimage_sha256"),
        f"{what}.files[{index}]",
    )
    spans = entry.get("match_spans") or []
    if not isinstance(spans, list):
        raise ContractError(f"{what}.files[{index}].match_spans must be an array")
    decoded_spans = []
    for span in spans:
        if not isinstance(span, list) or len(span) != 2:
            raise ContractError(f"{what}.files[{index}] has a malformed match span")
        decoded_spans.append((span[0], span[1]))
    edits = entry.get("edits") or []
    if not isinstance(edits, list):
        raise ContractError(f"{what}.files[{index}].edits must be an array")
    decoded_edits = []
    for edit in edits:
        item = require_mapping(edit, f"{what}.files[{index}].edits entry")
        reject_unknown_keys(
            item, ("start", "end", "replacement"), f"{what}.files[{index}].edits entry"
        )
        decoded_edits.append(
            ByteEdit(
                start=require_int(
                    item.get("start"), f"{what}.files[{index}].edits.start", minimum=0
                ),
                end=require_int(
                    item.get("end"), f"{what}.files[{index}].edits.end", minimum=0
                ),
                replacement=require_str(
                    item.get("replacement"),
                    f"{what}.files[{index}].edits.replacement",
                    allow_empty=True,
                ),
            )
        )
    return PlannedFile(
        path=require_relative_posix(entry.get("path"), f"{what}.files[{index}].path"),
        sha256=(
            require_sha256(entry.get("sha256"), f"{what}.files[{index}].sha256")
            if entry.get("sha256") is not None
            else None
        ),
        matches=require_int(
            entry.get("matches", 0), f"{what}.files[{index}].matches", minimum=0
        ),
        match_spans=tuple(decoded_spans),
        edits=tuple(decoded_edits),
        postimage_sha256=(
            require_sha256(
                entry.get("postimage_sha256"), f"{what}.files[{index}].postimage_sha256"
            )
            if entry.get("postimage_sha256") is not None
            else None
        ),
    )


def plan_digest(payload: Mapping[str, Any]) -> str:
    """Canonical digest over every plan field except ``plan_id`` itself."""
    return canonical_digest(
        {key: value for key, value in payload.items() if key != "plan_id"}
    )


@dataclass(frozen=True)
class ApplyPolicy:
    """The one policy applied identically to fresh and loaded plans."""

    consent: bool
    max_files: int = 100
    expect_count: int | None = None
    include_sensitive: bool = False
    max_file_bytes: int | None = None
    max_plan_bytes: int | None = None

    def __post_init__(self) -> None:
        require_bool(self.consent, "apply policy consent")
        require_int(self.max_files, "apply policy max files", minimum=1)
        optional_int(self.expect_count, "apply policy expected count", minimum=0)
        require_bool(self.include_sensitive, "apply policy include sensitive")
        optional_int(self.max_file_bytes, "apply policy max file bytes", minimum=1)
        optional_int(self.max_plan_bytes, "apply policy max plan bytes", minimum=1)


@dataclass(frozen=True)
class ChangedFile:
    path: str
    replacements: int

    def __post_init__(self) -> None:
        require_relative_posix(self.path, "changed file path")
        require_int(self.replacements, "changed file replacement count", minimum=0)

    def to_wire(self) -> dict[str, Any]:
        return {"path": self.path, "replacements": self.replacements}


@dataclass(frozen=True)
class MutationOutcome:
    """Honest mutation result: applied/noop/rejected/rolled_back/rollback_partial."""

    status: MutationStatus
    engine: str
    plan_id: str | None = None
    reviewed_plan: bool = False
    changed: tuple[ChangedFile, ...] = ()
    match_count: int = 0
    remaining_matches: int | None = None
    policy_excluded: tuple[str, ...] = ()
    message: str = ""
    schema: str = MUTATION_OUTCOME_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.status, MutationStatus):
            raise ContractError("mutation outcome status must be a MutationStatus")
        if self.engine not in {item.value for item in Engine}:
            raise ContractError(f"unsupported mutation engine: {self.engine!r}")
        optional_str(self.plan_id, "mutation outcome plan id")
        require_bool(self.reviewed_plan, "mutation outcome reviewed flag")
        require_int(self.match_count, "mutation outcome match count", minimum=0)
        optional_int(
            self.remaining_matches, "mutation outcome remaining matches", minimum=0
        )
        require_str(self.message, "mutation outcome message", allow_empty=True)
        if (
            self.status is MutationStatus.APPLIED
            and not self.changed
            and not self.message
        ):
            raise ContractError("an applied outcome must report changes or a message")
        if self.status is MutationStatus.NOOP and self.changed:
            raise ContractError("a noop outcome cannot report changed files")

    @property
    def applied(self) -> bool:
        return self.status is MutationStatus.APPLIED

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "schema": self.schema,
            "mode": self.engine,
            "engine": self.engine,
            "plan_id": self.plan_id,
            "reviewed_plan": self.reviewed_plan,
            "applied": self.applied,
            "mutation_status": self.status.value,
            "changed": [item.to_wire() for item in self.changed],
            "changed_files": len(self.changed),
            "matches": self.match_count,
            "initial_matches": self.match_count,
            "remaining_matches": self.remaining_matches,
            "message": self.message,
        }
        if self.policy_excluded:
            wire["policy_excluded"] = list(self.policy_excluded)
        return wire
