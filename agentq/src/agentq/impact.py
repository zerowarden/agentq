from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agentq.core import (
    COMPLETE,
    HEURISTIC,
    SAMPLED,
    SCAN_CAP,
    relpath,
    resolve_repo_path,
    typed_coverage,
)
from agentq.discovery import (
    FilesRequest,
    SearchRequest,
    SearchResult,
    files,
    search,
)

SHARED_RISK_RE = re.compile(
    r"(^|/)(shared|common|foundation|platform|core|public|api|contracts?|types?|config|schema|migrations?|packages?)(/|$)",
    re.I,
)
PUBLIC_NAME_RE = re.compile(
    r"(^|/)(index\.[^.]+|package\.json|cargo\.toml|pyproject\.toml|.*\.d\.ts)$", re.I
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


def nearest_manifest(root: Path, target: Path) -> dict[str, Any] | None:
    """Walk up from target to the repository root looking for a package manifest."""
    current = target if target.is_dir() else target.parent
    while True:
        for name, kind in (
            ("package.json", "npm"),
            ("Cargo.toml", "cargo"),
            ("pyproject.toml", "python"),
        ):
            path = current / name
            if path.exists():
                item: dict[str, Any] = {"path": relpath(root, path), "kind": kind}
                if name == "package.json":
                    try:
                        obj = json.loads(path.read_text(encoding="utf-8"))
                        item["name"] = obj.get("name")
                        item["scripts"] = sorted((obj.get("scripts") or {}).keys())
                    except Exception:
                        pass
                return item
        if current == root:
            break
        current = current.parent
    return None


def impact_data(
    root: Path, target: str, scopes: list[str], limit: int = 120
) -> dict[str, Any]:
    target_repo = resolve_repo_path(root, target)
    target_path = target_repo.absolute
    exists = target_path.exists()
    variants = _variants(target)
    primary = Path(target).stem or Path(target).name if exists else target
    refs = search(
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
    filename_candidates = files(
        FilesRequest(
            root=root,
            query=Path(target).stem if exists else target,
            scopes=tuple(scopes),
            limit=40,
        )
    )
    imports: SearchResult | None = None
    import_pattern = rf"(?:import|export|from|require|use|mod).*{re.escape(Path(target).stem if exists else target)}"
    try:
        imports = search(
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
        imports = None
    import_hits = imports.hits if imports is not None else ()
    tests = [hit for hit in refs.hits if hit.role == "test"]
    docs_config = [hit for hit in refs.hits if hit.role in {"docs", "config"}]
    source_refs = [hit for hit in refs.hits if hit.role == "source"]
    package = nearest_manifest(root, target_path if exists else root)
    unique_source_files = {
        item.path for item in refs.match_file_summary if item.role == "source"
    }
    unique_import_files = (
        {item.path for item in imports.match_file_summary}
        if imports is not None
        else set()
    )
    scan_capped = not refs.scan_complete
    shared_surface = bool(
        SHARED_RISK_RE.search(target.replace("\\", "/"))
        or PUBLIC_NAME_RE.search(target.replace("\\", "/"))
    )

    observations = {
        "public_shared_surface": shared_surface,
        "lexical_source_fanout": len(unique_source_files),
        "import_pattern_fanout": len(unique_import_files),
        "direct_test_references": len(tests),
        "config_schema_references": len(docs_config),
        "owning_package": (
            (package.get("name") or package.get("path")) if package else None
        ),
        "scan_reached_cap": scan_capped,
    }

    # Manually weighted rules, not an empirically calibrated risk model.
    # The score stays internal; output reports observations and rules only.
    score = 0
    rules: list[str] = []
    if len(unique_source_files) >= 20:
        score += 4
        rules.append("referenced by at least 20 source files")
    elif len(unique_source_files) >= 6:
        score += 2
        rules.append("referenced by multiple source files")
    elif unique_source_files:
        score += 1
    if len(unique_import_files) >= 10:
        score += 3
        rules.append("high import/export fan-out")
    elif unique_import_files:
        score += 1
    if shared_surface:
        score += 3
        rules.append("target appears to be shared/public/config/schema surface")
    if docs_config:
        score += 1
        rules.append("document/config references exist")
    if not tests and (source_refs or import_hits):
        score += 1
        rules.append("no direct lexical test reference found")
    if scan_capped:
        score += 2
        rules.append("reference discovery reached scan safety cap")
    level = "high" if score >= 6 else "medium" if score >= 3 else "low"

    validation = []
    if tests:
        validation.append("run directly referenced tests")
    if package:
        validation.append(
            f"run relevant {package['kind']} package checks for {package.get('name') or package['path']}"
        )
    if docs_config:
        validation.append("review docs/config/schema references")
    if level == "high":
        validation.append("run broader dependent-package or workspace verification")

    return {
        "repo_root": str(root),
        "target": target,
        "target_exists": exists,
        "variants": variants,
        "observations": observations,
        "heuristic_summary": {"level": level, "calibrated": False, "rules": rules},
        "package": package,
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


def render_impact(data: dict[str, Any]) -> str:
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
