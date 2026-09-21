from __future__ import annotations

import fnmatch
import json
import re
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .common import AgentQError, list_repo_files, run_cmd

DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)
SOURCE_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
TEST_RE = re.compile(
    r"(?:^|/)(?:__tests__|tests?|spec)(?:/|$)|(?:^|[._-])(?:test|spec)\.[^.]+$", re.I
)
CONFIG_RE = re.compile(
    r"(^|/)(?:vitest\.config\.|vite\.config\.|eslint\.config\.|package\.json$|"
    r"tsconfig[^/]*\.json$|turbo\.json$|nx\.json$|jest\.config\.)",
    re.I,
)
DOC_SUFFIXES = {".md", ".mdx", ".rst", ".adoc", ".txt"}
GLOBAL_BASENAMES = {
    "package.json",
    "pnpm-workspace.yaml",
    "pnpm-workspace.yml",
    "pnpm-lock.yaml",
    "yarn.lock",
    "package-lock.json",
    "bun.lock",
    "bun.lockb",
    "turbo.json",
    "nx.json",
    "tsconfig.json",
    "tsconfig.base.json",
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "vitest.workspace.ts",
    "vitest.workspace.js",
}
SCRIPT_ALIASES = {
    "test": ("test", "test:unit", "unit"),
    "typecheck": ("typecheck", "type-check", "check:types", "types"),
    "lint": ("lint", "lint:check"),
    "build": ("build", "compile"),
}


@dataclass(frozen=True)
class WorkspacePackage:
    key: str
    path: str
    name: str
    scripts: dict[str, str]
    dependencies: frozenset[str]
    dependency_kinds: dict[str, tuple[str, ...]]
    manifest: dict[str, Any]
    root: bool = False


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def package_manager(root: Path) -> str:
    if (
        (root / "pnpm-lock.yaml").exists()
        or (root / "pnpm-workspace.yaml").exists()
        or (root / "pnpm-workspace.yml").exists()
    ):
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lock").exists() or (root / "bun.lockb").exists():
        return "bun"
    return "npm"


def _strip_yaml_scalar(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0:1] in {"'", '"'} and value[-1:] == value[0:1]:
        value = value[1:-1]
    else:
        value = value.split(" #", 1)[0].strip()
    return value


def _pnpm_workspace_patterns(root: Path) -> list[str]:
    path = root / "pnpm-workspace.yaml"
    if not path.is_file():
        path = root / "pnpm-workspace.yml"
    if not path.is_file():
        return []
    patterns: list[str] = []
    active = False
    packages_indent = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if not active:
            if re.fullmatch(r"packages\s*:\s*", stripped):
                active = True
                packages_indent = indent
            continue
        if indent <= packages_indent and not stripped.startswith("-"):
            break
        match = re.match(r"^\s*-\s*(.+?)\s*$", raw)
        if match:
            value = _strip_yaml_scalar(match.group(1))
            if value:
                patterns.append(value)
    return patterns


def _package_json_workspace_patterns(root: Path) -> list[str]:
    obj = _read_json(root / "package.json")
    value = obj.get("workspaces")
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, str)]
    if isinstance(value, dict) and isinstance(value.get("packages"), list):
        return [str(item) for item in value["packages"] if isinstance(item, str)]
    return []


def workspace_patterns(root: Path) -> list[str]:
    patterns = _pnpm_workspace_patterns(root) or _package_json_workspace_patterns(root)
    # Preserve order but remove duplicates.
    return list(
        dict.fromkeys(
            pattern.strip().rstrip("/") for pattern in patterns if pattern.strip()
        )
    )


def _brace_expand(pattern: str) -> list[str]:
    match = re.search(r"\{([^{}]+)\}", pattern)
    if not match:
        return [pattern]
    out: list[str] = []
    for item in match.group(1).split(","):
        out.extend(
            _brace_expand(
                pattern[: match.start()] + item.strip() + pattern[match.end() :]
            )
        )
    return out


def matches_pattern(path: str, pattern: str) -> bool:
    path = path.strip("/") or "."
    pattern = pattern.strip().strip("/") or "."
    if pattern == ".":
        return path == "."
    if fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern):
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return False


def _allowed_by_patterns(path: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    positives: list[str] = []
    negatives: list[str] = []
    for original in patterns:
        negative = original.startswith("!")
        value = original[1:] if negative else original
        for expanded in _brace_expand(value):
            (negatives if negative else positives).append(expanded)
    included = (
        any(matches_pattern(path, pattern) for pattern in positives)
        if positives
        else True
    )
    excluded = any(matches_pattern(path, pattern) for pattern in negatives)
    return included and not excluded


def discover_workspace(root: Path) -> dict[str, WorkspacePackage]:
    patterns = workspace_patterns(root)
    manifests = [
        rel
        for rel in list_repo_files(root)
        if PurePosixPath(rel).name == "package.json"
    ]
    root_manifest = root / "package.json"
    if root_manifest.is_file() and "package.json" not in manifests:
        manifests.insert(0, "package.json")

    raw: list[tuple[str, dict[str, Any], bool]] = []
    for rel in sorted(set(manifests)):
        package_path = PurePosixPath(rel).parent.as_posix()
        package_path = "." if package_path == "." else package_path
        is_root = package_path == "."
        if not is_root and not _allowed_by_patterns(package_path, patterns):
            continue
        obj = _read_json(root / rel)
        if not obj:
            continue
        raw.append((package_path, obj, is_root))

    names = {
        str(obj.get("name")): path
        for path, obj, _ in raw
        if isinstance(obj.get("name"), str) and obj.get("name")
    }
    packages: dict[str, WorkspacePackage] = {}
    for path, obj, is_root in raw:
        name = str(
            obj.get("name") or (root.name if is_root else PurePosixPath(path).name)
        )
        scripts_obj = obj.get("scripts")
        scripts = {
            str(key): str(value)
            for key, value in (
                scripts_obj.items() if isinstance(scripts_obj, dict) else []
            )
            if isinstance(value, str)
        }
        local_deps: set[str] = set()
        kinds: dict[str, list[str]] = defaultdict(list)
        for field in DEPENDENCY_FIELDS:
            value = obj.get(field)
            if not isinstance(value, dict):
                continue
            for dep in value:
                dep_name = str(dep)
                if dep_name in names:
                    local_deps.add(dep_name)
                    kinds[dep_name].append(field)
        packages[path] = WorkspacePackage(
            key=path,
            path=path,
            name=name,
            scripts=scripts,
            dependencies=frozenset(local_deps),
            dependency_kinds={key: tuple(value) for key, value in kinds.items()},
            manifest=obj,
            root=is_root,
        )
    return packages


def workspace_graph(
    packages: dict[str, WorkspacePackage],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    by_name = {pkg.name: key for key, pkg in packages.items()}
    forward: dict[str, set[str]] = {key: set() for key in packages}
    reverse: dict[str, set[str]] = {key: set() for key in packages}
    for key, pkg in packages.items():
        for dependency_name in pkg.dependencies:
            target = by_name.get(dependency_name)
            if target is None or target == key:
                continue
            forward[key].add(target)
            reverse[target].add(key)
    return forward, reverse


def changed_files(root: Path, base: str | None = None) -> list[str]:
    changed: set[str] = set()
    if base:
        result = run_cmd(
            ["git", "diff", "--name-only", "-z", f"{base}...HEAD"], cwd=root, timeout=30
        )
        if result.returncode not in (0, 1):
            raise AgentQError(f"unable to diff base {base!r}")
        changed.update(item for item in result.stdout.split("\0") if item)
    result = run_cmd(
        ["git", "status", "--porcelain=v1", "-z"], cwd=root, timeout=30, check=True
    )
    records = [item for item in result.stdout.split("\0") if item]
    index = 0
    while index < len(records):
        record = records[index]
        if len(record) >= 4:
            status = record[:2]
            path = record[3:]
            if ("R" in status or "C" in status) and index + 1 < len(records):
                path = records[index + 1]
                index += 1
            changed.add(path)
        index += 1
    return sorted(changed)


def owner_for_file(path: str, packages: dict[str, WorkspacePackage]) -> str | None:
    normalized = path.strip("/")
    candidates = []
    for key in packages:
        if key == ".":
            candidates.append((0, key))
        elif normalized == key or normalized.startswith(key.rstrip("/") + "/"):
            candidates.append((len(PurePosixPath(key).parts), key))
    return max(candidates, default=(0, None))[1]


def transitive_dependents(
    start: Iterable[str], reverse: dict[str, set[str]], *, depth: int | None
) -> dict[str, int]:
    queue = deque((item, 0) for item in start)
    seen = set(start)
    distances: dict[str, int] = {}
    while queue:
        current, distance = queue.popleft()
        if depth is not None and distance >= depth:
            continue
        for dependent in sorted(reverse.get(current, set())):
            if dependent in seen:
                continue
            seen.add(dependent)
            distances[dependent] = distance + 1
            queue.append((dependent, distance + 1))
    return distances


def dependency_order(keys: Iterable[str], forward: dict[str, set[str]]) -> list[str]:
    selected = set(keys)
    permanent: set[str] = set()
    temporary: set[str] = set()
    ordered: list[str] = []

    def visit(node: str) -> None:
        if node in permanent:
            return
        if (
            node in temporary
        ):  # Preserve determinism in cycles; cycle reporting belongs to dependencies.
            return
        temporary.add(node)
        for dependency in sorted(forward.get(node, set())):
            if dependency in selected:
                visit(dependency)
        temporary.remove(node)
        permanent.add(node)
        ordered.append(node)

    for key in sorted(selected):
        visit(key)
    return ordered


def is_global_change(path: str) -> bool:
    normalized = path.strip("/")
    if "/" not in normalized and normalized in GLOBAL_BASENAMES:
        return True
    if normalized.startswith(".github/workflows/"):
        return True
    return bool(
        re.fullmatch(
            r"(?:tsconfig|eslint|vitest|vite|jest)[^/]*\.(?:json|js|cjs|mjs|ts)",
            normalized,
            re.I,
        )
    )


def is_docs_only(paths: list[str]) -> bool:
    if not paths:
        return False
    return all(
        Path(path).suffix.lower() in DOC_SUFFIXES
        or path.startswith("docs/")
        or PurePosixPath(path).name.lower() in {"readme", "license", "changelog"}
        for path in paths
    )


def is_public_contract_change(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = PurePosixPath(normalized).name
    return (
        name in {"package.json", "index.ts", "index.tsx", "index.js", "index.jsx"}
        or name.endswith(".d.ts")
        or "/contracts/" in f"/{normalized}"
        or "/types/" in f"/{normalized}"
        or "/public/" in f"/{normalized}"
        or "/exports/" in f"/{normalized}"
    )


def has_vitest(
    pkg: WorkspacePackage,
    root: Path,
    packages: dict[str, WorkspacePackage] | None = None,
) -> bool:
    dependency_names = set(pkg.manifest.get("dependencies") or {})
    dependency_names.update(pkg.manifest.get("devDependencies") or {})
    dependency_names.update(pkg.manifest.get("peerDependencies") or {})
    dependency_names.update(pkg.manifest.get("optionalDependencies") or {})
    if "vitest" in dependency_names or any(
        "vitest" in command for command in pkg.scripts.values()
    ):
        return True
    root_pkg = (packages or {}).get(".")
    if root_pkg and root_pkg is not pkg:
        root_deps = set(root_pkg.manifest.get("dependencies") or {}) | set(
            root_pkg.manifest.get("devDependencies") or {}
        )
        if "vitest" in root_deps:
            return True
    return (root / "node_modules" / ".bin" / "vitest").exists()


def find_script(pkg: WorkspacePackage, category: str) -> str | None:
    for candidate in SCRIPT_ALIASES.get(category, (category,)):
        if candidate in pkg.scripts:
            return candidate
    return None


def script_argv(manager: str, script: str) -> list[str]:
    if manager == "pnpm":
        return ["pnpm", "run", script]
    if manager == "yarn":
        return ["yarn", script]
    if manager == "bun":
        return ["bun", "run", script]
    return ["npm", "run", script]


def package_exec_argv(manager: str, argv: list[str]) -> list[str]:
    if manager == "pnpm":
        return ["pnpm", "exec", *argv]
    if manager == "yarn":
        return ["yarn", "exec", *argv]
    if manager == "bun":
        return ["bunx", *argv]
    return ["npx", "--no-install", *argv]


def workspace_summary(root: Path) -> dict[str, Any]:
    packages = discover_workspace(root)
    forward, reverse = workspace_graph(packages)
    return {
        "manager": package_manager(root),
        "patterns": workspace_patterns(root),
        "packages": len(packages),
        "edges": sum(len(value) for value in forward.values()),
        "roots": [pkg.name for pkg in packages.values() if pkg.root],
        "leaf_packages": sorted(
            pkg.name for key, pkg in packages.items() if not reverse.get(key)
        ),
    }
