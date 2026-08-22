from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .common import classify_path, compact_line, list_repo_files, relpath, run_cmd
from .search import files_data, search_data

SHARED_RISK_RE = re.compile(r"(^|/)(shared|common|foundation|platform|core|public|api|contracts?|types?|config|schema|migrations?|packages?)(/|$)", re.I)
PUBLIC_NAME_RE = re.compile(r"(^|/)(index\.[^.]+|package\.json|cargo\.toml|pyproject\.toml|.*\.d\.ts)$", re.I)


def _variants(target: str) -> list[str]:
    base = Path(target).name
    stem = Path(base).stem
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem).replace("-", " ").replace("_", " ").split()
    variants = {target, base, stem}
    if words:
        lower = [w.lower() for w in words]
        variants.update({"-".join(lower), "_".join(lower), "".join(lower), lower[0] + "".join(w.title() for w in lower[1:]), "".join(w.title() for w in lower)})
    ordered = [target] + [v for v in sorted(variants, key=lambda x: (-len(x), x)) if v and v != target]
    return ordered


def _nearest_manifest(root: Path, target: Path) -> dict[str, Any] | None:
    current = target if target.is_dir() else target.parent
    for directory in [current, *current.parents]:
        if directory == root.parent:
            break
        for name, kind in (("package.json", "npm"), ("Cargo.toml", "cargo"), ("pyproject.toml", "python")):
            path = directory / name
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
        if directory == root:
            break
    return None


def impact_data(root: Path, target: str, scopes: list[str], limit: int = 120) -> dict[str, Any]:
    target_path = root / target
    exists = target_path.exists()
    variants = _variants(target)
    primary = Path(target).stem or Path(target).name if exists else target
    refs = search_data(root, primary, scopes, mode="fixed", word=not exists, limit=limit, per_file=10)
    file_hits = files_data(root, Path(target).stem if exists else target, scopes, limit=40, include_sensitive=False)
    import_hits = []
    import_pattern = rf"(?:import|export|from|require|use|mod).*{re.escape(Path(target).stem if exists else target)}"
    try:
        imports = search_data(root, import_pattern, scopes, mode="regex", limit=50, per_file=5)
        import_hits = imports["hits"]
    except Exception:
        pass
    tests = [h for h in refs["hits"] if h["role"] == "test"]
    docs_config = [h for h in refs["hits"] if h["role"] in {"docs", "config"}]
    source_refs = [h for h in refs["hits"] if h["role"] == "source"]
    all_ref_files = refs.get("match_file_summary") or []
    package = _nearest_manifest(root, target_path if exists else root)
    unique_source_files = {str(h["path"]) for h in all_ref_files if h.get("role") == "source"}
    unique_import_files = {str(h["path"]) for h in (imports.get("match_file_summary") if 'imports' in locals() else []) or []}

    score = 0
    reasons: list[str] = []
    if len(unique_source_files) >= 20:
        score += 4; reasons.append("referenced by at least 20 source files")
    elif len(unique_source_files) >= 6:
        score += 2; reasons.append("referenced by multiple source files")
    elif unique_source_files:
        score += 1
    if len(unique_import_files) >= 10:
        score += 3; reasons.append("high import/export fan-out")
    elif unique_import_files:
        score += 1
    target_norm = target.replace("\\", "/")
    if SHARED_RISK_RE.search(target_norm) or PUBLIC_NAME_RE.search(target_norm):
        score += 3; reasons.append("target appears to be shared/public/config/schema surface")
    if docs_config:
        score += 1; reasons.append("document/config references exist")
    if not tests and (source_refs or import_hits):
        score += 1; reasons.append("no direct lexical test reference found")
    if not refs.get("scan_complete", True):
        score += 2; reasons.append("reference discovery reached scan safety cap")
    level = "high" if score >= 6 else "medium" if score >= 3 else "low"
    validation = []
    if tests:
        validation.append("run directly referenced tests")
    if package:
        validation.append(f"run relevant {package['kind']} package checks for {package.get('name') or package['path']}")
    if docs_config:
        validation.append("review docs/config/schema references")
    if level == "high":
        validation.append("run broader dependent-package or workspace verification")
    return {
        "repo_root": str(root), "target": target, "target_exists": exists, "variants": variants,
        "blast_radius": level, "score": score, "reasons": reasons,
        "package": package, "source_reference_files": len(unique_source_files),
        "import_reference_files": len(unique_import_files), "tests": tests[:25],
        "docs_config": docs_config[:25], "top_references": refs["hits"][:50],
        "filename_candidates": file_hits["files"][:20], "truncated": not refs.get("scan_complete", True),
        "validation": validation,
        "evidence_quality": "exact lexical matching-file counts plus bounded previews; use semantic overview for TypeScript/JavaScript symbol confirmation",
    }


def render_impact(data: dict[str, Any]) -> str:
    lines = [
        f"blast radius: {data['blast_radius'].upper()} (score {data['score']})",
        f"target: {data['target']}",
        f"evidence: {data['evidence_quality']}",
        f"source reference files: {data['source_reference_files']}; import-pattern files: {data['import_reference_files']}",
    ]
    if data.get("package"):
        p = data["package"]
        lines.append(f"owning manifest: {p['path']} [{p['kind']}{'; ' + p.get('name') if p.get('name') else ''}]")
    if data["reasons"]:
        lines.append("\nreasons:")
        lines.extend(f"  - {r}" for r in data["reasons"])
    if data["top_references"]:
        lines.append("\ntop references:")
        for h in data["top_references"][:25]:
            lines.append(f"  {h['path']}:{h['line']} [{h['kind']}; {h['role']}] {h['text']}")
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
    if data["truncated"]:
        lines.append("\nReference cap reached: treat the blast radius as a lower bound and narrow by package for a second pass.")
    return "\n".join(lines)
