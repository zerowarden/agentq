"""Construct miniature repositories and independent structural judgments.

The construction specifies use sites and nonreferences before providers run.
Providers only supply candidate evidence; missing facts remain empty witnesses.
These are native structural stress tests, not task-conditioned relevance labels.
"""

from dataclasses import asdict, dataclass
from pathlib import Path

from agentq.core import ContractError, canonical_json
from agentq.inspection.contracts import (
    Fidelity,
    InspectionRequest,
    Intent,
    ObservationKind,
    SymbolTarget,
)

from .annotations import AnnotatedSpan, covered_lines
from .codec import capture_digest
from .locking import SuiteBuilder
from .models import (
    CaseSource,
    CaseSpec,
    JudgmentFacet,
    JudgmentSet,
    JudgmentWitness,
    SuiteLock,
)
from .repository import run_git
from .repository_capture import capture_case
from .store import CaptureStore

FAMILIES = (
    "direct",
    "alias",
    "qualified",
    "shadowing",
    "same_name",
    "many_callers",
    "same_file",
    "test_domain",
    "long_source",
    "long_caller",
    "empty_tests",
    "unicode",
)


@dataclass(frozen=True)
class Construction:
    family: str
    seed: int
    intent: Intent

    def __post_init__(self) -> None:
        if self.family not in FAMILIES or self.seed < 0:
            raise ContractError("unknown structural construction")


@dataclass(frozen=True)
class ConstructedRepository:
    files: dict[str, str]
    symbol: str
    source: AnnotatedSpan
    uses: tuple[AnnotatedSpan, ...]
    negatives: tuple[AnnotatedSpan, ...]


def construct(spec: Construction) -> ConstructedRepository:
    name = f"calculate_{spec.seed}"
    body_size = 90 + spec.seed if spec.family == "long_source" else 2 + spec.seed % 4
    lines = [
        f"def {name}(value):",
        *[f"    value += {i}" for i in range(body_size)],
        "    return value",
    ]
    files = {"mod.py": "\n".join(lines) + "\n", ".gitignore": "__pycache__/\n"}
    count = 7 if spec.family == "many_callers" else 2
    uses = []
    for index in range(count):
        path = f"use_{index}.py"
        call = f"{name}({index})"
        imported = f"from mod import {name}"
        if spec.family == "alias":
            imported += " as imported_target"
            call = f"imported_target({index})"
        elif spec.family == "qualified":
            imported = "import mod"
            call = f"mod.{call}"
        prefix = [imported]
        if spec.family == "long_caller":
            prefix += ["# supporting context " + "x" * 60] * (30 + index)
        if spec.family == "unicode":
            prefix += ["# 中文と日本語: a supplied structural fixture"]
        files[path] = "\n".join([*prefix, f"result = {call}"]) + "\n"
        uses.append(AnnotatedSpan(path, len(prefix) + 1, len(prefix) + 1))
    if spec.family == "same_file":
        files["use_0.py"] += (
            "\n".join(f"other_{i} = {name}({i})" for i in range(6)) + "\n"
        )
        uses.extend(AnnotatedSpan("use_0.py", line, line) for line in range(3, 9))
    negatives = []
    if spec.family in ("shadowing", "same_name"):
        if spec.family == "shadowing":
            files["decoy.py"] = f"def unrelated({name}):\n    return {name}(0)\n"
        else:
            files["decoy.py"] = (
                f"def {name}(value):\n    return -value\n\nresult = {name}(1)\n"
            )
        negatives.append(
            AnnotatedSpan("decoy.py", 1, len(files["decoy.py"].splitlines()))
        )
    if spec.family != "empty_tests":
        files["tests/test_mod.py"] = (
            f"from mod import {name}\n\ndef test_value():\n    assert {name}(0) >= 0\n"
        )
        uses.append(AnnotatedSpan("tests/test_mod.py", 4, 4))
    return ConstructedRepository(
        files,
        name,
        AnnotatedSpan("mod.py", 1, len(lines)),
        tuple(uses),
        tuple(negatives),
    )


def materialize(spec: Construction, root: Path) -> ConstructedRepository:
    built = construct(spec)
    root.mkdir(parents=True, exist_ok=True)
    if (root / ".git").is_dir():
        for path, text in built.files.items():
            if (root / path).read_text() != text:
                raise ContractError(
                    "generated repository differs from its construction"
                )
        return built
    for path, text in built.files.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    run_git(root, "init", "--quiet")
    run_git(root, "add", "--", ".")
    run_git(
        root,
        "-c",
        "user.name=Agentq Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--quiet",
        "-m",
        "constructed structural fixture",
        env={
            "GIT_AUTHOR_DATE": "2026-09-26T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-09-26T00:00:00+00:00",
        },
    )
    return built


def build_generated(
    store: CaptureStore, *, seeds: int = 2, suite_id: str = "native-structural-v1"
) -> SuiteLock:
    builder = SuiteBuilder(store, suite_id, track="stress")
    specs = [
        Construction(family, seed, intent)
        for family in FAMILIES
        for seed in range(seeds)
        for intent in Intent
    ]
    attempts = []
    for spec in specs:
        root = store.root / "generated" / f"{spec.family}-{spec.seed}"
        built = materialize(spec, root)
        case_id = f"structural-{spec.family}-{spec.seed}-{spec.intent.value}"
        case = CaseSpec(
            case_id=case_id,
            source=CaseSource(root=str(root), fixture_revision="construction-v1"),
            request=InspectionRequest(
                target=SymbolTarget(name=built.symbol, scopes=("mod.py",)),
                intent=spec.intent,
                evidence_scopes=(".",),
            ),
            track="stress",
            repository_family="construction:" + spec.family,
            original_inst_id=case_id,
        )
        capture, attempt = capture_case(case, checkout=root)
        attempts.append(attempt)
        if capture is None:
            continue
        witnesses, facets = [], []
        observations = {
            item.observation_id: item for item in capture.decision.pool.observations
        }
        for index, location in enumerate((built.source, *built.uses)):
            ids = []
            for variant in capture.decision.pool.variants:
                span = variant.span
                if span is None or variant.source.path != location.path:
                    continue
                candidate = AnnotatedSpan(location.path, span.start_line, span.end_line)
                if (
                    covered_lines((location,), (candidate,))
                    != location.end_line - location.start_line + 1
                ):
                    continue
                if index == 0 and (
                    variant.fidelity is not Fidelity.EXACT
                    or observations[variant.observation_id].kind
                    is not ObservationKind.SOURCE_WINDOW
                ):
                    continue
                ids.append(variant.variant_id)
            witness = f"fact-{index}"
            witnesses.append(JudgmentWitness(witness, tuple(ids)))
            facets.append(
                JudgmentFacet(
                    witness,
                    True,
                    ((witness,),),
                    "structural fact from construction specification",
                )
            )
        irrelevant = tuple(
            v.variant_id
            for v in capture.decision.pool.variants
            if any(v.source.path == span.path for span in built.negatives)
        )
        judgment = JudgmentSet(
            case_id=case_id,
            capture_id=capture_digest(capture),
            basis="structural_truth",
            review_status="construction_spec",
            facets=tuple(facets),
            witnesses=tuple(witnesses),
            irrelevant_variant_ids=irrelevant,
        )
        builder.add(
            case_id,
            capture,
            judgment=judgment,
            repository_family=case.repository_family,
            original_inst_id=case_id,
        )
    store.write_attempts(suite_id, tuple(attempts))
    store.write_artifact(
        store.root / "generated" / "construction-specifications.json",
        canonical_json([asdict(spec) for spec in specs]),
    )
    return builder.write()
