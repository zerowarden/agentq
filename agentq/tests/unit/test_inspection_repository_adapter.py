"""Repository adapter: source, outline, lexical mentions, ownership (parametrized)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentq.inspection.adapters.repository import RepositoryInspectionAdapter
from agentq.inspection.budgeting import AcquisitionLimits
from agentq.inspection.capabilities import CapabilityRegistry
from agentq.inspection.contracts import (
    Capability,
    CollectionStatus,
    EvidenceRequest,
    Fidelity,
    InspectionContext,
    ObservationKind,
    PathKind,
    PathTarget,
    RangeTarget,
    RepositoryIdentity,
    RepresentationKind,
    SourceSpan,
    SymbolTarget,
    make_declaration_candidate,
)

REPOSITORY_CAPABILITIES = frozenset(
    {
        Capability.READ_SOURCE,
        Capability.OUTLINE,
        Capability.LEXICAL_MENTIONS,
        Capability.OWNING_PACKAGE,
    }
)


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _context(root: Path) -> InspectionContext:
    return InspectionContext(identity=RepositoryIdentity(root=root))


def _request(
    capability: Capability,
    target,
    *,
    subject=None,
    domain: str | None = None,
    scope: tuple[str, ...] = (),
    limit: int = 40,
) -> EvidenceRequest:
    return EvidenceRequest(
        request_id=f"req-{capability.value}",
        capability=capability,
        target=target,
        subject=subject,
        domain=domain,
        scope=scope,
        limit=limit,
    )


def _range_request(
    capability: Capability, path: str, start: int, end: int, *, limit: int = 40
) -> EvidenceRequest:
    return _request(
        capability,
        RangeTarget(path=path, ranges=(SourceSpan(start_line=start, end_line=end),)),
        limit=limit,
    )


def test_requested_range_is_read_exactly(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "line one\nline two\nline three\n")
    result = RepositoryInspectionAdapter().acquire(
        _range_request(Capability.READ_SOURCE, "src/a.py", 1, 2),
        _context(tmp_path),
    )
    assert result.status is CollectionStatus.COMPLETED
    observation = result.observations[0]
    assert observation.kind is ObservationKind.SOURCE_WINDOW
    assert observation.payload.text == "line one\nline two"  # type: ignore[union-attr]
    assert not observation.payload.truncated  # type: ignore[union-attr]
    variant = result.variants[0]
    assert variant.representation is RepresentationKind.EXACT_SOURCE
    assert variant.fidelity is Fidelity.EXACT
    assert result.coverage.is_complete()
    assert observation.version_of("src/a.py") is not None


def test_read_limits_downgrade_fidelity_to_bounded(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "one\ntwo\nthree\nfour\n")
    result = RepositoryInspectionAdapter().acquire(
        _range_request(Capability.READ_SOURCE, "src/a.py", 1, 4, limit=2),
        _context(tmp_path),
    )
    assert result.variants[0].fidelity is Fidelity.BOUNDED
    assert not result.coverage.is_complete()
    assert any(item.code == "line_cap" for item in result.diagnostics)


def test_a_line_wider_than_the_limit_cannot_claim_exact_source(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "src/a.py", "x = '" + "a" * 400 + "'\nshort = 1\n")
    result = RepositoryInspectionAdapter().acquire(
        _range_request(Capability.READ_SOURCE, "src/a.py", 1, 2),
        _context(tmp_path),
    )
    assert result.observations[0].payload.truncated  # type: ignore[union-attr]
    assert "[truncated]" in result.variants[0].text
    assert result.variants[0].fidelity is Fidelity.BOUNDED
    assert not result.coverage.is_complete()
    assert any(item.code == "line_cap" for item in result.diagnostics)


def test_source_line_width_is_configurable(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "value = '" + "a" * 400 + "'\n")
    request = _range_request(Capability.READ_SOURCE, "src/a.py", 1, 1)
    narrow = RepositoryInspectionAdapter().acquire(request, _context(tmp_path))
    wide = RepositoryInspectionAdapter().acquire(
        request,
        InspectionContext(
            identity=RepositoryIdentity(root=tmp_path),
            limits=AcquisitionLimits(max_source_line_chars=500),
        ),
    )
    assert narrow.observations[0].payload.truncated  # type: ignore[union-attr]
    assert narrow.variants[0].fidelity is Fidelity.BOUNDED
    assert wide.observations[0].payload.truncated is False  # type: ignore[union-attr]
    assert wide.variants[0].fidelity is Fidelity.EXACT
    assert wide.coverage.is_complete()


def test_missing_file_is_a_failed_acquisition_through_the_registry(
    tmp_path: Path,
) -> None:
    registry = CapabilityRegistry((RepositoryInspectionAdapter(),))
    results = registry.acquire(
        _range_request(Capability.READ_SOURCE, "src/missing.py", 1, 1),
        _context(tmp_path),
    )
    assert len(results) == 1
    assert results[0].record.status is CollectionStatus.FAILED
    assert not results[0].record.coverage.is_complete()
    assert results[0].record.diagnostics


def test_outline_reports_file_structure(tmp_path: Path) -> None:
    _write(tmp_path, "src/a.py", "def alpha():\n    return 1\n")
    _write(tmp_path, "src/b.py", "class Beta:\n    pass\n")
    result = RepositoryInspectionAdapter().acquire(
        _request(
            Capability.OUTLINE,
            PathTarget(path="src", path_kind=PathKind.DIRECTORY),
        ),
        _context(tmp_path),
    )
    assert result.status is CollectionStatus.COMPLETED
    observation = result.observations[0]
    assert observation.kind is ObservationKind.OUTLINE
    names = {item.name for item in observation.payload.symbols}  # type: ignore[union-attr]
    assert names == {"alpha", "Beta"}
    assert "alpha" in result.variants[0].text
    assert result.coverage.is_complete()


def test_empty_file_outline_is_completed_explicit_evidence(tmp_path: Path) -> None:
    _write(tmp_path, "src/empty.py", "")
    result = RepositoryInspectionAdapter().acquire(
        _request(
            Capability.OUTLINE,
            PathTarget(path="src/empty.py", path_kind=PathKind.FILE),
        ),
        _context(tmp_path),
    )
    assert result.status is CollectionStatus.COMPLETED
    assert len(result.observations) == 1
    assert result.observations[0].payload.symbols == ()  # type: ignore[union-attr]
    assert result.coverage.is_complete()


def test_empty_directory_outline_is_completed_explicit_evidence(
    tmp_path: Path,
) -> None:
    (tmp_path / "empty").mkdir()
    result = RepositoryInspectionAdapter().acquire(
        _request(
            Capability.OUTLINE,
            PathTarget(path="empty", path_kind=PathKind.DIRECTORY),
        ),
        _context(tmp_path),
    )
    assert result.status is CollectionStatus.COMPLETED
    assert len(result.observations) == 1
    assert result.observations[0].payload.symbols == ()  # type: ignore[union-attr]
    assert result.coverage.is_complete()


def test_mentions_are_labeled_by_domain(tmp_path: Path) -> None:
    _write(tmp_path, "src/app.py", "value = list_orders()\n")
    _write(tmp_path, "tests/test_app.py", "def test_it():\n    list_orders()\n")
    adapter = RepositoryInspectionAdapter()
    context = _context(tmp_path)
    all_mentions = adapter.acquire(
        _request(
            Capability.LEXICAL_MENTIONS,
            SymbolTarget(name="list_orders"),
            scope=("src", "tests"),
        ),
        context,
    )
    test_mentions = adapter.acquire(
        _request(
            Capability.LEXICAL_MENTIONS,
            SymbolTarget(name="list_orders"),
            domain="test",
            scope=("src", "tests"),
        ),
        context,
    )
    assert all_mentions.observations
    assert all(
        item.kind is ObservationKind.LEXICAL_MENTION
        for item in all_mentions.observations
    )
    assert test_mentions.observations
    assert all(
        item.kind is ObservationKind.TEST_MENTION for item in test_mentions.observations
    )
    assert all(
        item.payload.domain == "test"  # type: ignore[union-attr]
        for item in test_mentions.observations
    )
    assert all(
        (item.source.path or "").startswith("tests/")
        for item in test_mentions.observations
    )
    assert test_mentions.coverage.domain == "lexical_test_mentions"


def test_owning_package_is_found_from_the_declaration_path(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "pyproject.toml",
        '[project]\nname = "orders"\nversion = "0.1.0"\n',
    )
    _write(tmp_path, "src/service.py", "def list_orders():\n    return 1\n")
    subject = make_declaration_candidate(
        provider="python",
        path="src/service.py",
        source_version="v1",
        kind="function",
        span=SourceSpan(start_line=1, end_line=2),
        signature="list_orders()",
    )
    result = RepositoryInspectionAdapter().acquire(
        _request(
            Capability.OWNING_PACKAGE,
            SymbolTarget(name="list_orders"),
            subject=subject,
        ),
        _context(tmp_path),
    )
    assert result.status is CollectionStatus.COMPLETED
    payload = result.observations[0].payload
    assert payload.path == "pyproject.toml"  # type: ignore[union-attr]
    assert payload.kind == "python"  # type: ignore[union-attr]


def test_missing_manifest_is_a_complete_empty_outcome(tmp_path: Path) -> None:
    _write(tmp_path, "src/service.py", "def list_orders():\n    return 1\n")
    result = RepositoryInspectionAdapter().acquire(
        _request(
            Capability.OWNING_PACKAGE,
            PathTarget(path="src/service.py", path_kind=PathKind.FILE),
        ),
        _context(tmp_path),
    )
    assert result.status is CollectionStatus.EMPTY
    assert result.coverage.is_complete()


@pytest.mark.parametrize(
    "capability,available",
    (
        pytest.param(Capability.LEXICAL_MENTIONS, False, id="lexical-needs-ripgrep"),
        pytest.param(Capability.READ_SOURCE, True, id="reads-need-nothing"),
        pytest.param(Capability.OUTLINE, True, id="outline-has-a-fallback"),
        pytest.param(Capability.OWNING_PACKAGE, True, id="ownership-is-local"),
    ),
)
def test_availability_without_ripgrep(capability, available) -> None:
    adapter = RepositoryInspectionAdapter(which=lambda name: None)
    result = adapter.availability(
        capability, SymbolTarget(name="x"), _context(Path("/repo"))
    )
    assert result.available is available
    if not available:
        assert "ripgrep" in (result.reason or "")


@pytest.mark.parametrize(
    "target",
    (
        pytest.param(SymbolTarget(name="x"), id="symbol"),
        pytest.param(PathTarget(path="src", path_kind=PathKind.DIRECTORY), id="path"),
        pytest.param(
            RangeTarget(
                path="src/a.py", ranges=(SourceSpan(start_line=1, end_line=2),)
            ),
            id="range",
        ),
    ),
)
def test_repository_adapter_applies_to_every_target(target) -> None:
    adapter = RepositoryInspectionAdapter()
    assert adapter.applicable(target, _context(Path("/repo")))
    assert adapter.capabilities() == REPOSITORY_CAPABILITIES
