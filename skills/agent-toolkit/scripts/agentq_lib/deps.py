from __future__ import annotations

import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .common import AgentQError, compact_line, find_executable, run_cmd
from .workspace import discover_workspace

DEPENDENCY_FIELDS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")


def _node_graph(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    packages = discover_workspace(root)
    by_name = {pkg.name: pkg for pkg in packages.values()}
    nodes = [
        {
            "id": f"node:{pkg.name}",
            "ecosystem": "node",
            "name": pkg.name,
            "path": "package.json" if pkg.path == "." else f"{pkg.path}/package.json",
            "private": bool(pkg.manifest.get("private")),
        }
        for pkg in packages.values()
    ]
    edges = []
    for pkg in packages.values():
        for dependency_name, kinds in pkg.dependency_kinds.items():
            if dependency_name not in by_name:
                continue
            for kind in kinds:
                edges.append({
                    "from": f"node:{pkg.name}",
                    "to": f"node:{dependency_name}",
                    "kind": kind,
                })
    return nodes, edges


def _cargo_graph(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cargo = find_executable("cargo")
    if not cargo or not (root / "Cargo.toml").exists():
        return [], []
    result = run_cmd(
        [cargo, "metadata", "--format-version", "1", "--no-deps", "--offline"],
        cwd=root, timeout=90, env={"CARGO_NET_OFFLINE": "true"},
    )
    if result.returncode != 0:
        return [], []
    try:
        obj = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], []
    packages = obj.get("packages") or []
    workspace_members = set(obj.get("workspace_members") or [])
    local_by_name = {str(pkg.get("name")): pkg for pkg in packages if pkg.get("name") and pkg.get("id") in workspace_members}
    nodes = []
    edges = []
    for name, pkg in local_by_name.items():
        manifest = Path(str(pkg.get("manifest_path", "")))
        try:
            path = manifest.relative_to(root).as_posix()
        except ValueError:
            path = str(manifest)
        nodes.append({"id": f"cargo:{name}", "ecosystem": "cargo", "name": name, "path": path, "private": False})
        for dep in pkg.get("dependencies") or []:
            dep_name = str(dep.get("name", ""))
            if dep_name in local_by_name:
                kind = str(dep.get("kind") or "normal")
                edges.append({"from": f"cargo:{name}", "to": f"cargo:{dep_name}", "kind": kind})
    return nodes, edges


def _cycles(node_ids: list[str], adjacency: dict[str, list[str]], limit: int = 20) -> list[list[str]]:
    index = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in adjacency.get(node, []):
            if nxt not in indices:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], indices[nxt])
        if low[node] == indices[node]:
            component = []
            while stack:
                item = stack.pop()
                on_stack.remove(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1 or (component and component[0] in adjacency.get(component[0], [])):
                found.append(sorted(component))

    for node in node_ids:
        if node not in indices and len(found) < limit:
            visit(node)
    return found[:limit]


def _walk(start: str, graph: dict[str, list[str]], depth: int, limit: int) -> list[dict[str, Any]]:
    queue = deque([(start, 0)])
    seen = {start}
    out = []
    while queue and len(out) < limit:
        current, distance = queue.popleft()
        if distance >= depth:
            continue
        for nxt in sorted(graph.get(current, [])):
            if nxt in seen:
                continue
            seen.add(nxt)
            out.append({"id": nxt, "distance": distance + 1})
            queue.append((nxt, distance + 1))
            if len(out) >= limit:
                break
    return out


def dependencies_data(root: Path, *, target: str | None = None, depth: int = 2, limit: int = 100) -> dict[str, Any]:
    node_nodes, node_edges = _node_graph(root)
    cargo_nodes, cargo_edges = _cargo_graph(root)
    nodes = node_nodes + cargo_nodes
    edges = node_edges + cargo_edges
    by_id = {node["id"]: node for node in nodes}
    by_name = defaultdict(list)
    for node in nodes:
        by_name[node["name"]].append(node["id"])
        by_name[node["path"]].append(node["id"])
        by_name[str(Path(node["path"]).parent)].append(node["id"])
    forward: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        forward[edge["from"]].append(edge["to"])
        reverse[edge["to"]].append(edge["from"])
    indegree = Counter(edge["to"] for edge in edges)
    outdegree = Counter(edge["from"] for edge in edges)

    selected: list[str] = []
    if target:
        exact = [node["id"] for node in nodes if target in {node["id"], node["name"], node["path"], str(Path(node["path"]).parent)}]
        if not exact:
            lowered = target.lower()
            exact = [node["id"] for node in nodes if lowered in node["name"].lower() or lowered in node["path"].lower()]
        selected = exact[:10]
        if not selected:
            raise AgentQError(f"no workspace package matched: {target}")

    cycles = _cycles(list(by_id), forward)
    data: dict[str, Any] = {
        "repo_root": str(root), "nodes": len(nodes), "edges": len(edges),
        "ecosystems": dict(Counter(node["ecosystem"] for node in nodes)),
        "top_depended_on": [
            {**by_id[node_id], "direct_dependents": count}
            for node_id, count in indegree.most_common(15)
        ],
        "top_dependencies": [
            {**by_id[node_id], "direct_dependencies": count}
            for node_id, count in outdegree.most_common(15)
        ],
        "cycles": [[by_id[item]["name"] for item in cycle] for cycle in cycles],
        "evidence_quality": "manifest-level local workspace edges; source imports and runtime loading are not inferred",
    }
    if target:
        targets = []
        for node_id in selected:
            deps = _walk(node_id, forward, depth, limit)
            dependents = _walk(node_id, reverse, depth, limit)
            targets.append({
                **by_id[node_id],
                "dependencies": [{**by_id[item["id"]], "distance": item["distance"]} for item in deps],
                "dependents": [{**by_id[item["id"]], "distance": item["distance"]} for item in dependents],
                "dependencies_truncated": len(deps) >= limit,
                "dependents_truncated": len(dependents) >= limit,
            })
        data["target"] = target
        data["matches"] = targets
    else:
        data["packages"] = nodes[:limit]
        data["packages_truncated"] = len(nodes) > limit
    return data


def render_dependencies(data: dict[str, Any]) -> str:
    lines = [
        f"workspace dependencies: {data['nodes']} packages, {data['edges']} local edges {data['ecosystems']}",
        f"evidence: {data['evidence_quality']}",
    ]
    for match in data.get("matches", []):
        lines.append(f"\n{match['name']} [{match['ecosystem']}] — {match['path']}")
        lines.append(f"  dependencies ({len(match['dependencies'])}):")
        lines.extend(f"    d={item['distance']} {item['name']} — {item['path']}" for item in match["dependencies"])
        lines.append(f"  dependents ({len(match['dependents'])}):")
        lines.extend(f"    d={item['distance']} {item['name']} — {item['path']}" for item in match["dependents"])
    if not data.get("matches"):
        if data["top_depended_on"]:
            lines.append("\nmost depended-on packages:")
            lines.extend(f"  {item['direct_dependents']:>3} {item['name']} — {item['path']}" for item in data["top_depended_on"])
        if data["top_dependencies"]:
            lines.append("\npackages with most local dependencies:")
            lines.extend(f"  {item['direct_dependencies']:>3} {item['name']} — {item['path']}" for item in data["top_dependencies"])
    if data["cycles"]:
        lines.append("\ncycles:")
        lines.extend("  " + " -> ".join(cycle + [cycle[0]]) for cycle in data["cycles"])
    return "\n".join(lines)
