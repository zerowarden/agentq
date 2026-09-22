"""Canonical project graph: ecosystem-qualified unit identity and edges.

A project unit is identified by ``(ecosystem, repository-relative path)``, never
by package name: different ecosystems may legitimately contain a unit with the
same name. Name lookup is therefore many-valued; :meth:`ProjectGraph.resolve`
only succeeds when a name is unambiguous.

The graph owns traversal (dependent distances, dependency order) and structural
analysis (strongly connected components, cycles), so no consumer re-implements
graph knowledge.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import PurePosixPath

from agentq.core import ContractError, require_str


@dataclass(frozen=True, order=True)
class UnitId:
    """Stable identity of one project unit: ecosystem plus path."""

    ecosystem: str
    path: str

    def __post_init__(self) -> None:
        require_str(self.ecosystem, "unit ecosystem")
        require_str(self.path, "unit path")

    def to_wire(self) -> dict[str, str]:
        return {"ecosystem": self.ecosystem, "path": self.path}

    def __str__(self) -> str:
        return f"{self.ecosystem}:{self.path}"


@dataclass(frozen=True)
class ProjectUnit:
    """One discovered unit: identity, display name, and manifest location."""

    id: UnitId
    name: str
    manifest: str | None = None
    root: bool = False
    private: bool = False

    def __post_init__(self) -> None:
        require_str(self.name, "project unit name")
        if not isinstance(self.id, UnitId):
            raise ContractError("project unit id must be a UnitId")


@dataclass(frozen=True)
class DependencyEdge:
    """One directed local dependency, qualified by its declaring field kind."""

    source: UnitId
    target: UnitId
    kind: str = "dependency"

    def __post_init__(self) -> None:
        if not isinstance(self.source, UnitId) or not isinstance(self.target, UnitId):
            raise ContractError("dependency edge endpoints must be UnitId values")
        require_str(self.kind, "dependency edge kind")


@dataclass(frozen=True)
class ProjectGraph:
    """Units keyed by :class:`UnitId` plus their directed local edges."""

    units: Mapping[UnitId, ProjectUnit] = field(
        default_factory=dict[UnitId, ProjectUnit]
    )
    edges: tuple[DependencyEdge, ...] = ()

    def __post_init__(self) -> None:
        for edge in self.edges:
            if edge.source not in self.units:
                raise ContractError(f"dependency edge source is unknown: {edge.source}")
            if edge.target not in self.units:
                raise ContractError(f"dependency edge target is unknown: {edge.target}")

    @classmethod
    def from_units(
        cls,
        units: Mapping[UnitId, ProjectUnit],
        edges: Iterable[DependencyEdge] = (),
    ) -> ProjectGraph:
        """Build a graph, dropping self-edges and duplicate edges."""
        seen: set[DependencyEdge] = set()
        kept: list[DependencyEdge] = []
        for edge in edges:
            if edge.source == edge.target or edge in seen:
                continue
            seen.add(edge)
            kept.append(edge)
        return cls(units=dict(units), edges=tuple(kept))

    @classmethod
    def merge(cls, *graphs: ProjectGraph) -> ProjectGraph:
        units: dict[UnitId, ProjectUnit] = {}
        edges: list[DependencyEdge] = []
        for graph in graphs:
            for unit_id, unit in graph.units.items():
                existing = units.get(unit_id)
                if existing is not None and existing.name != unit.name:
                    raise ContractError(
                        f"conflicting project units for {unit_id}: "
                        f"{existing.name!r} vs {unit.name!r}"
                    )
                units[unit_id] = unit
            edges.extend(graph.edges)
        return cls.from_units(units, edges)

    def units_named(self, name: str) -> tuple[UnitId, ...]:
        """Every unit carrying ``name``; empty when unknown."""
        return tuple(
            sorted(unit_id for unit_id, unit in self.units.items() if unit.name == name)
        )

    def resolve(self, name: str) -> UnitId | None:
        """The unique unit named ``name``, or ``None`` when unknown/ambiguous."""
        matches = self.units_named(name)
        return matches[0] if len(matches) == 1 else None

    def ambiguous_names(self) -> tuple[str, ...]:
        """Names shared by more than one unit, sorted."""
        counts: dict[str, int] = {}
        for unit in self.units.values():
            counts[unit.name] = counts.get(unit.name, 0) + 1
        return tuple(sorted(name for name, count in counts.items() if count > 1))

    @cached_property
    def _adjacency(
        self,
    ) -> tuple[
        Mapping[UnitId, tuple[UnitId, ...]], Mapping[UnitId, tuple[UnitId, ...]]
    ]:
        forward: dict[UnitId, set[UnitId]] = {unit_id: set() for unit_id in self.units}
        reverse: dict[UnitId, set[UnitId]] = {unit_id: set() for unit_id in self.units}
        for edge in self.edges:
            forward[edge.source].add(edge.target)
            reverse[edge.target].add(edge.source)
        return (
            {key: tuple(sorted(value)) for key, value in forward.items()},
            {key: tuple(sorted(value)) for key, value in reverse.items()},
        )

    def outgoing(self, unit: UnitId) -> tuple[UnitId, ...]:
        return self._adjacency[0].get(unit, ())

    def incoming(self, unit: UnitId) -> tuple[UnitId, ...]:
        return self._adjacency[1].get(unit, ())

    def dependencies(self, unit: UnitId) -> frozenset[UnitId]:
        return frozenset(self.outgoing(unit))

    def edge_count(self) -> int:
        return len(self.edges)

    def dependents(
        self, start: Iterable[UnitId], *, depth: int | None
    ) -> dict[UnitId, int]:
        """Breadth-first dependent distances from ``start``, bounded by depth."""
        queue = deque((item, 0) for item in start)
        seen = set(start)
        distances: dict[UnitId, int] = {}
        while queue:
            current, distance = queue.popleft()
            if depth is not None and distance >= depth:
                continue
            for dependent in self.incoming(current):
                if dependent in seen:
                    continue
                seen.add(dependent)
                distances[dependent] = distance + 1
                queue.append((dependent, distance + 1))
        return distances

    def dependency_order(self, keys: Iterable[UnitId]) -> tuple[UnitId, ...]:
        """Selected keys ordered so dependencies are visited before dependents."""
        selected = set(keys)
        permanent: set[UnitId] = set()
        temporary: set[UnitId] = set()
        ordered: list[UnitId] = []

        def visit(node: UnitId) -> None:
            if node in permanent or node in temporary:
                return
            temporary.add(node)
            for dependency in self.outgoing(node):
                if dependency in selected:
                    visit(dependency)
            temporary.remove(node)
            permanent.add(node)
            ordered.append(node)

        for key in sorted(selected):
            visit(key)
        return tuple(ordered)

    def strongly_connected_components(self) -> tuple[tuple[UnitId, ...], ...]:
        """Tarjan components with more than one unit, each sorted."""
        finder = _TarjanComponents(self)
        for node in sorted(self.units):
            if node not in finder.indices:
                finder.visit(node)
        return tuple(sorted(finder.found))

    def cycles(self) -> tuple[tuple[UnitId, ...], ...]:
        return self.strongly_connected_components()


class _TarjanComponents:
    """Tarjan strongly-connected components over a :class:`ProjectGraph`."""

    def __init__(self, graph: ProjectGraph) -> None:
        self.graph = graph
        self.index = 0
        self.indices: dict[UnitId, int] = {}
        self.low: dict[UnitId, int] = {}
        self.stack: list[UnitId] = []
        self.on_stack: set[UnitId] = set()
        self.found: list[tuple[UnitId, ...]] = []

    def visit(self, node: UnitId) -> None:
        self.indices[node] = self.low[node] = self.index
        self.index += 1
        self.stack.append(node)
        self.on_stack.add(node)
        for nxt in self.graph.outgoing(node):
            self._visit_neighbour(node, nxt)
        if self.low[node] != self.indices[node]:
            return
        component = self._pop_component(node)
        if len(component) > 1:
            self.found.append(tuple(sorted(component)))

    def _visit_neighbour(self, node: UnitId, nxt: UnitId) -> None:
        if nxt not in self.indices:
            self.visit(nxt)
            self.low[node] = min(self.low[node], self.low[nxt])
        elif nxt in self.on_stack:
            self.low[node] = min(self.low[node], self.indices[nxt])

    def _pop_component(self, node: UnitId) -> list[UnitId]:
        component: list[UnitId] = []
        while self.stack:
            item = self.stack.pop()
            self.on_stack.remove(item)
            component.append(item)
            if item == node:
                break
        return component


def owner_for_file(path: str, units: Mapping[UnitId, ProjectUnit]) -> UnitId | None:
    """Deepest unit directory containing ``path``, or ``None`` when unowned.

    Ties across ecosystems at the same depth are broken deterministically by
    ecosystem then path; callers needing per-ecosystem ownership should pass a
    single-ecosystem graph.
    """
    normalized = path.strip("/")
    candidates: list[tuple[int, UnitId]] = []
    for unit_id in units:
        if unit_id.path == ".":
            candidates.append((0, unit_id))
        elif normalized == unit_id.path or normalized.startswith(
            unit_id.path.rstrip("/") + "/"
        ):
            candidates.append((len(PurePosixPath(unit_id.path).parts), unit_id))
    if not candidates:
        return None
    return max(candidates)[1]
