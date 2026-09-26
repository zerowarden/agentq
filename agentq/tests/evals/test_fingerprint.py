"""Source fingerprints bind the dependencies that define experiment results."""

from pathlib import Path
from unittest.mock import patch

import pytest

from evals import fingerprint


@pytest.mark.parametrize("filename", ["models.py", "matrix.py"])
def test_metric_dependencies_invalidate_the_fingerprint(
    tmp_path: Path, filename: str
) -> None:
    package = tmp_path / "evals"
    package.mkdir()
    metrics = package / "metrics.py"
    metrics.write_text("METRIC = 1\n")
    dependency = package / filename
    dependency.write_text("VALUE = 1\n")
    with (
        patch.object(fingerprint, "__file__", str(package / "fingerprint.py")),
        patch("evals.metrics.__file__", str(metrics)),
    ):
        fingerprint.metrics_digest.cache_clear()
        first = fingerprint.metrics_digest()
        dependency.write_text("VALUE = 2\n")
        fingerprint.metrics_digest.cache_clear()
        second = fingerprint.metrics_digest()
    fingerprint.metrics_digest.cache_clear()
    assert first != second


def test_core_rendering_changes_invalidate_the_engine(tmp_path: Path) -> None:
    inspection = tmp_path / "inspection"
    inspection.mkdir()
    (inspection / "__init__.py").write_text("")
    core = tmp_path / "core"
    core.mkdir()
    source = core / "serialization.py"
    source.write_text("FORMAT = 1\n")
    with (
        patch("agentq.inspection.__file__", str(inspection / "__init__.py")),
        patch("agentq.core.__file__", str(core / "__init__.py")),
    ):
        fingerprint.decision_engine_digest.cache_clear()
        first = fingerprint.decision_engine_digest()
        source.write_text("FORMAT = 2\n")
        fingerprint.decision_engine_digest.cache_clear()
        second = fingerprint.decision_engine_digest()
    fingerprint.decision_engine_digest.cache_clear()
    assert first != second
