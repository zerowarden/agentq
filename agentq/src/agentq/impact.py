from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentq.core import (
    COMPLETE,
    HEURISTIC,
    SAMPLED,
    SCAN_CAP,
    resolve_repo_path,
    typed_coverage,
)
from agentq.discovery import (
    FilesRequest,
    FilesResult,
    PackageManifest,
    SearchRequest,
    SearchResult,
    files,
    search,
)

from .core.languages import MANIFEST_PATTERN, ecosystem_for_language, language_id_for
from .workspace import nearest_manifest

SHARED_RISK_RE = re.compile(
    r"(^|/)(shared|common|foundation|platform|core|public|api|contracts?|types?|config|schema|migrations?|packages?)(/|$)",
    re.I,
)
PUBLIC_NAME_RE = re.compile(
    r"(^|/)(index\.[^.]+|" + MANIFEST_PATTERN + r"|.*\.d\.ts)$", re.I
)


def _variants(target: str) -> list[str]:
    base = Path(target).name
    stem = Path(base).stem
    words = (
        re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem)
        .replace("-", " ")
        .replace("_", " ")
        .split()
    )
    variants = {target, base, stem}
    if words:
        lower = [w.lower() for w in words]
        variants.update(
            {
                "-".join(lower),
                "_".join(lower),
                "".join(lower),
                lower[0] + "".join(w.title() for w in lower[1:]),
                "".join(w.title() for w in lower),
            }
        )
    ordered = [target] + [
        v for v in sorted(variants, key=lambda x: (-len(x), x)) if v and v != target
    ]
    return ordered


def _search_term(target: str, *, exists: bool) -> str:
    """Stem-based search term for existing targets, literal name otherwise."""
    return Path(target).stem if exists else target


def _reference_search(
    root: Path, target: str, scopes: list[str], limit: int, *, exists: bool
) -> SearchResult:
    primary = (Path(target).stem or Path(target).name) if exists else target
    return search(
        SearchRequest(
            root=root,
            query=primary,
            scopes=tuple(scopes),
            mode="fixed",
            word=not exists,
            limit=limit,
            per_file=10,
        )
    )


def _filename_search(
    root: Path, target: str, scopes: list[str], *, exists: bool
) -> FilesResult:
    return files(
        FilesRequest(
            root=root,
            query=_search_term(target, exists=exists),
            scopes=tuple(scopes),
            limit=40,
        )
    )


def _import_search(
    root: Path, target: str, scopes: list[str], *, exists: bool
) -> SearchResult | None:
    import_pattern = (
        rf"(?:import|export|from|require|use|mod).*"
        rf"{re.escape(_search_term(target, exists=exists))}"
    )
    try:
        return search(
            SearchRequest(
                root=root,
                query=import_pattern,
                scopes=tuple(scopes),
                mode="regex",
                limit=50,
                per_file=5,
            )
        )
    except Exception:
        return None


def _impact_observations(
    *,
    shared_surface: bool,
    source_fanout: int,
    import_fanout: int,
    test_references: int,
    config_references: int,
    owning_package: str | None,
    scan_capped: bool,
) -> dict[str, Any]:
    return {
        "public_shared_surface": shared_surface,
        "lexical_source_fanout": source_fanout,
        "import_pattern_fanout": import_fanout,
        "direct_test_references": test_references,
        "config_schema_references": config_references,
        "owning_package": owning_package,
        "scan_reached_cap": scan_capped,
    }


@dataclass(frozen=True)
class ImpactSignals:
    """Observable impact facts; risk is classified by explicit rules below."""

    shared_surface: bool
    source_fanout: int
    import_fanout: int
    has_docs_config: bool
    has_tests: bool
    has_reference_evidence: bool
    scan_capped: bool


@dataclass(frozen=True)
class ImpactAssessment:
    """Rule-based risk level and the reasons that produced it."""

    level: str
    reasons: tuple[str, ...]


_SHARED_REASON = "target appears to be shared/public/config/schema surface"


def assess_impact(signals: ImpactSignals) -> ImpactAssessment:
    """Classify impact with explicit rules, never an additive score.

    HIGH requires both a broad surface and substantial observed fan-out.
    MEDIUM covers a broad surface alone, multiple source dependents, or a
    complete-looking scan that was actually capped.
    """
    reasons: list[str] = []
    if signals.shared_surface:
        reasons.append(_SHARED_REASON)
    if signals.source_fanout >= 20:
        reasons.append("referenced by at least 20 source files")
    elif signals.source_fanout >= 6:
        reasons.append("referenced by multiple source files")
    if signals.import_fanout >= 10:
        reasons.append("high import/export fan-out")
    if signals.scan_capped:
        reasons.append("reference discovery reached scan safety cap")
    if signals.has_docs_config:
        reasons.append("document/config references exist")
    if not signals.has_tests and signals.has_reference_evidence:
        reasons.append("no direct lexical test reference found")

    substantial_fanout = signals.source_fanout >= 20 or signals.import_fanout >= 10
    high = (signals.shared_surface or signals.scan_capped) and substantial_fanout
    medium = (
        high
        or signals.shared_surface
        or signals.source_fanout >= 6
        or signals.import_fanout >= 10
        or (
            signals.scan_capped and bool(signals.source_fanout or signals.import_fanout)
        )
        or (
            bool(signals.source_fanout)
            and not signals.has_tests
            and signals.has_reference_evidence
        )
    )
    if high:
        level = "high"
    elif medium:
        level = "medium"
    else:
        level = "low"
    return ImpactAssessment(level=level, reasons=tuple(reasons))


def _validation_steps(
    *,
    has_tests: bool,
    package: PackageManifest | None,
    has_docs_config: bool,
    level: str,
) -> list[str]:
    validation: list[str] = []
    if has_tests:
        validation.append("run directly referenced tests")
    if package:
        validation.append(
            f"run relevant {package.kind} package checks for {package.name or package.path}"
        )
    if has_docs_config:
        validation.append("review docs/config/schema references")
    if level == "high":
        validation.append("run broader dependent-package or workspace verification")
    return validation


def impact_data(
    root: Path, target: str, scopes: list[str], limit: int = 120
) -> dict[str, Any]:
    target_repo = resolve_repo_path(root, target)
    target_path = target_repo.absolute
    exists = target_path.exists()
    variants = _variants(target)
    refs = _reference_search(root, target, scopes, limit, exists=exists)
    filename_candidates = _filename_search(root, target, scopes, exists=exists)
    imports = _import_search(root, target, scopes, exists=exists)
    import_hits = imports.hits if imports is not None else ()
    tests = [hit for hit in refs.hits if hit.role == "test"]
    docs_config = [hit for hit in refs.hits if hit.role in {"docs", "config"}]
    source_refs = [hit for hit in refs.hits if hit.role == "source"]
    package = nearest_manifest(
        root,
        target_path if exists else root,
        ecosystem=ecosystem_for_language(language_id_for(target)),
    )
    unique_source_files: set[str] = {
        item.path for item in refs.match_file_summary if item.role == "source"
    }
    unique_import_files: set[str] = (
        {item.path for item in imports.match_file_summary}
        if imports is not None
        else set()
    )
    scan_capped = not refs.scan_complete
    shared_surface = bool(
        SHARED_RISK_RE.search(target.replace("\\", "/"))
        or PUBLIC_NAME_RE.search(target.replace("\\", "/"))
    )
    observations = _impact_observations(
        shared_surface=shared_surface,
        source_fanout=len(unique_source_files),
        import_fanout=len(unique_import_files),
        test_references=len(tests),
        config_references=len(docs_config),
        owning_package=(package.name or package.path) if package else None,
        scan_capped=scan_capped,
    )
    assessment = assess_impact(
        ImpactSignals(
            shared_surface=shared_surface,
            source_fanout=len(unique_source_files),
            import_fanout=len(unique_import_files),
            has_docs_config=bool(docs_config),
            has_tests=bool(tests),
            has_reference_evidence=bool(source_refs or import_hits),
            scan_capped=scan_capped,
        )
    )
    validation = _validation_steps(
        has_tests=bool(tests),
        package=package,
        has_docs_config=bool(docs_config),
        level=assessment.level,
    )

    return {
        "repo_root": str(root),
        "target": target,
        "target_exists": exists,
        "variants": variants,
        "observations": observations,
        "heuristic_summary": {
            "level": assessment.level,
            "calibrated": False,
            "rules": list(assessment.reasons),
        },
        "package": package.to_wire() if package is not None else None,
        "tests": [hit.to_wire() for hit in tests[:25]],
        "docs_config": [hit.to_wire() for hit in docs_config[:25]],
        "top_references": [hit.to_wire() for hit in refs.hits[:50]],
        "filename_candidates": [
            entry.to_wire() for entry in filename_candidates.files[:20]
        ],
        "truncated": scan_capped,
        "validation": validation,
        "provenance": HEURISTIC,
        "coverage": (
            typed_coverage(SAMPLED, SCAN_CAP)
            if scan_capped
            else typed_coverage(COMPLETE)
        ).to_wire(),
    }


def render_impact(data: dict[str, Any], *, budget: int = 0) -> str:
    o = data["observations"]
    summary = data["heuristic_summary"]
    lines = [
        f"target: {data['target']}"
        + ("" if data["target_exists"] else " (name only; no such path)"),
        f"public/shared surface: {'yes' if o['public_shared_surface'] else 'no'}",
        f"lexical source fanout: {o['lexical_source_fanout']} files",
        f"import-pattern fanout: {o['import_pattern_fanout']} files (lexical pattern evidence, not semantic imports)",
        f"direct test references: {o['direct_test_references']}",
        f"config/schema references: {o['config_schema_references']}",
        f"owning package: {o['owning_package'] or 'unknown'}",
        f"scan: {'capped; treat counts as lower bounds' if o['scan_reached_cap'] else 'complete within scope'}",
        f"evidence: {data['provenance']} · coverage {data['coverage']['status']}",
        f"\nheuristic summary: {summary['level'].upper()} — uncalibrated rule-based estimate, not a statistical measure",
    ]
    lines.extend(f"  - {rule}" for rule in summary["rules"])
    if data["top_references"]:
        lines.append("\ntop references:")
        for h in data["top_references"][:25]:
            lines.append(
                f"  {h['path']}:{h['line']} [{h['kind']}; {h['role']}] {h['text']}"
            )
    if data["tests"]:
        lines.append("\nrelated tests:")
        for h in data["tests"][:15]:
            lines.append(f"  {h['path']}:{h['line']} {h['text']}")
    if data["docs_config"]:
        lines.append("\ndocs/config/schema references:")
        for h in data["docs_config"][:15]:
            lines.append(f"  {h['path']}:{h['line']} {h['text']}")
    if data["validation"]:
        lines.append("\nvalidation scope:")
        lines.extend(f"  - {x}" for x in data["validation"])
    return "\n".join(lines)
