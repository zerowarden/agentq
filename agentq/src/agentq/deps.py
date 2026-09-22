from __future__ import annotations

from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from agentq.core import RESULT_LIMIT, SAMPLED, SYNTACTIC, AgentQError
from agentq.core import complete as complete_coverage
from agentq.core import coverage as coverage_block
from agentq.discovery import list_repo_files

from .workspace import (
    ProjectGraph,
    ProjectUnit,
    discover_cargo,
    discover_go,
    discover_node,
    discover_python,
)


def project_graph(root: Path) -> ProjectGraph:
    """The repository's node/python/cargo/go units and local edges."""
    repo_files = list_repo_files(root)
    return ProjectGraph.merge(
        discover_node(root).graph,
        discover_python(root, repo_files).graph,
        discover_cargo(root, repo_files).graph,
        discover_go(root, repo_files).graph,
    )


def _node(unit: ProjectUnit) -> dict[str, Any]:
    return {
        "id": str(unit.id),
        "ecosystem": unit.id.ecosystem,
        "name": unit.name,
        "path": unit.manifest or unit.id.path,
        "private": unit.private,
    }


def _walk(
    start: str, graph: dict[str, list[str]], depth: int, limit: int
) -> list[dict[str, Any]]:
    queue = deque([(start, 0)])
    seen = {start}
    out: list[dict[str, Any]] = []
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


def dependencies_data(
    root: Path, *, target: str | None = None, depth: int = 2, limit: int = 100
) -> dict[str, Any]:
    graph = project_graph(root)
    nodes = [_node(unit) for unit in graph.units.values()]
    edges = [
        {"from": str(edge.source), "to": str(edge.target), "kind": edge.kind}
        for edge in graph.edges
    ]
    by_id = {node["id"]: node for node in nodes}
    by_name: dict[str, list[str]] = defaultdict(list)
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
        exact = [
            node["id"]
            for node in nodes
            if target
            in {node["id"], node["name"], node["path"], str(Path(node["path"]).parent)}
        ]
        if not exact:
            lowered = target.lower()
            exact = [
                node["id"]
                for node in nodes
                if lowered in node["name"].lower() or lowered in node["path"].lower()
            ]
        selected = exact[:10]
        if not selected:
            raise AgentQError(f"no workspace package matched: {target}")

    cycles = [
        [by_id[str(unit_id)]["name"] for unit_id in component]
        for component in graph.cycles()
    ]
    data: dict[str, Any] = {
        "repo_root": str(root),
        "nodes": len(nodes),
        "edges": len(edges),
        "ecosystems": dict(Counter(node["ecosystem"] for node in nodes)),
        "ambiguous_names": list(graph.ambiguous_names()),
        "top_depended_on": [
            {**by_id[node_id], "direct_dependents": count}
            for node_id, count in indegree.most_common(15)
        ],
        "top_dependencies": [
            {**by_id[node_id], "direct_dependencies": count}
            for node_id, count in outdegree.most_common(15)
        ],
        "cycles": cycles,
        "evidence_quality": (
            "manifest-level local workspace edges across node/python/cargo/go; "
            "source imports and runtime loading are not inferred"
        ),
    }
    if target:
        targets: list[dict[str, Any]] = []
        for node_id in selected:
            deps = _walk(node_id, forward, depth, limit)
            dependents = _walk(node_id, reverse, depth, limit)
            targets.append(
                {
                    **by_id[node_id],
                    "dependencies": [
                        {**by_id[item["id"]], "distance": item["distance"]}
                        for item in deps
                    ],
                    "dependents": [
                        {**by_id[item["id"]], "distance": item["distance"]}
                        for item in dependents
                    ],
                    "dependencies_truncated": len(deps) >= limit,
                    "dependents_truncated": len(dependents) >= limit,
                }
            )
        data["target"] = target
        data["matches"] = targets
        data["coverage"] = (
            coverage_block(SAMPLED, RESULT_LIMIT)
            if any(
                item["dependencies_truncated"] or item["dependents_truncated"]
                for item in targets
            )
            else complete_coverage()
        )
    else:
        data["packages"] = nodes[:limit]
        data["packages_truncated"] = len(nodes) > limit
        data["coverage"] = (
            coverage_block(SAMPLED, RESULT_LIMIT)
            if data["packages_truncated"]
            else complete_coverage()
        )
    data["provenance"] = SYNTACTIC
    return data


def render_dependencies(data: dict[str, Any], *, budget: int = 0) -> str:
    lines = [
        f"workspace dependencies: {data['nodes']} packages, {data['edges']} local edges {data['ecosystems']}",
        f"evidence: {data['evidence_quality']}",
    ]
    if data.get("ambiguous_names"):
        lines.append("ambiguous names: " + ", ".join(data["ambiguous_names"]))
    for match in data.get("matches", []):
        lines.append(f"\n{match['name']} [{match['ecosystem']}] — {match['path']}")
        lines.append(f"  dependencies ({len(match['dependencies'])}):")
        lines.extend(
            f"    d={item['distance']} {item['name']} — {item['path']}"
            for item in match["dependencies"]
        )
        lines.append(f"  dependents ({len(match['dependents'])}):")
        lines.extend(
            f"    d={item['distance']} {item['name']} — {item['path']}"
            for item in match["dependents"]
        )
    if not data.get("matches"):
        if data["top_depended_on"]:
            lines.append("\nmost depended-on packages:")
            lines.extend(
                f"  {item['direct_dependents']:>3} {item['name']} — {item['path']}"
                for item in data["top_depended_on"]
            )
        if data["top_dependencies"]:
            lines.append("\npackages with most local dependencies:")
            lines.extend(
                f"  {item['direct_dependencies']:>3} {item['name']} — {item['path']}"
                for item in data["top_dependencies"]
            )
    if data["cycles"]:
        lines.append("\ncycles:")
        lines.extend("  " + " -> ".join(cycle + [cycle[0]]) for cycle in data["cycles"])
    return "\n".join(lines)
