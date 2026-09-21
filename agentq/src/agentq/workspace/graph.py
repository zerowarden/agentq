"""Dependency graph construction and traversal over typed packages."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from .models import Package


@dataclass(frozen=True)
class DependencyGraph:
    """Local package edges in both directions, keyed by package path."""

    forward: Mapping[str, frozenset[str]]
    reverse: Mapping[str, frozenset[str]]
    by_name: Mapping[str, str]

    @classmethod
    def from_packages(cls, packages: Mapping[str, Package]) -> DependencyGraph:
        by_name = {package.name: key for key, package in packages.items()}
        forward: dict[str, set[str]] = {key: set() for key in packages}
        reverse: dict[str, set[str]] = {key: set() for key in packages}
        for key, package in packages.items():
            for dependency_name in package.dependencies:
                target = by_name.get(dependency_name)
                if target is None or target == key:
                    continue
                forward[key].add(target)
                reverse[target].add(key)
        return cls(
            forward={key: frozenset(value) for key, value in forward.items()},
            reverse={key: frozenset(value) for key, value in reverse.items()},
            by_name=by_name,
        )

    def edges(self) -> int:
        return sum(len(value) for value in self.forward.values())

    def dependents(
        self, start: Iterable[str], *, depth: int | None
    ) -> dict[str, int]:
        """Breadth-first dependent distances from ``start``, bounded by depth."""
        queue = deque((item, 0) for item in start)
        seen = set(start)
        distances: dict[str, int] = {}
        while queue:
            current, distance = queue.popleft()
            if depth is not None and distance >= depth:
                continue
            for dependent in sorted(self.reverse.get(current, frozenset())):
                if dependent in seen:
                    continue
                seen.add(dependent)
                distances[dependent] = distance + 1
                queue.append((dependent, distance + 1))
        return distances

    def dependency_order(self, keys: Iterable[str]) -> tuple[str, ...]:
        """Selected keys ordered so dependencies are visited before dependents."""
        selected = set(keys)
        permanent: set[str] = set()
        temporary: set[str] = set()
        ordered: list[str] = []

        def visit(node: str) -> None:
            if node in permanent:
                return
            if node in temporary:
                # Preserve determinism in cycles; cycle reporting belongs to dependencies.
                return
            temporary.add(node)
            for dependency in sorted(self.forward.get(node, frozenset())):
                if dependency in selected:
                    visit(dependency)
            temporary.remove(node)
            permanent.add(node)
            ordered.append(node)

        for key in sorted(selected):
            visit(key)
        return tuple(ordered)


def owner_for_file(path: str, packages: Mapping[str, Package]) -> str | None:
    """Deepest package directory containing ``path``, or ``None`` when unowned."""
    normalized = path.strip("/")
    candidates: list[tuple[int, str]] = []
    for key in packages:
        if key == ".":
            candidates.append((0, key))
        elif normalized == key or normalized.startswith(key.rstrip("/") + "/"):
            candidates.append((len(PurePosixPath(key).parts), key))
    if not candidates:
        return None
    return max(candidates)[1]
