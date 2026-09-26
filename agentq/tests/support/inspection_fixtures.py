"""Shared inspection test fixtures: file trees, contexts, and plans."""

from __future__ import annotations

from pathlib import Path

from agentq.inspection.contracts import (
    CollectionPlan,
    InspectionContext,
    RepositoryIdentity,
    SymbolTarget,
)


def write_source_file(root: Path, relative: str, text: str) -> None:
    """Write one source file, creating its parent directory."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def bare_context(root: Path) -> InspectionContext:
    """A context with no registry, for tests that supply the adapter later."""
    return InspectionContext(identity=RepositoryIdentity(root=root))


def collection_plan() -> CollectionPlan:
    """The minimal symbol collection plan shared by selection tests."""
    return CollectionPlan(
        profile="test-plan", request_id="req-1", target=SymbolTarget(name="target")
    )
