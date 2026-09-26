"""Reproducible source fingerprints for frozen experiments.

A frozen manifest must identify the implementations that produced it, not only
the configuration values. These helpers hash the decision engine and shared
core sources, plus the evaluation Python source tree. The latter includes
derived metric properties, aggregation, and promotion rules, so changing any
of them starts a new experiment. Non-Python artifacts do not affect identity.
Digests are cached for one process, whose imported code must remain unchanged.
They are eval-only: the installed runtime never reads one.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from types import ModuleType

from agentq.core import canonical_digest, canonical_json


def source_tree_digest(root: Path) -> str:
    """Content digest of every Python file under one directory, path-ordered."""
    entries = [
        (
            path.relative_to(root).as_posix(),
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in sorted(root.rglob("*.py"))
    ]
    return hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def _module_path(module: ModuleType) -> Path:
    location = module.__file__
    if location is None:
        raise ValueError(f"module {module.__name__!r} has no source file")
    return Path(location)


@lru_cache(maxsize=1)
def decision_engine_digest() -> str:
    """Decision sources, including the shared contracts and serialization."""
    import agentq.core
    import agentq.inspection

    return canonical_digest({
        module.__name__: source_tree_digest(_module_path(module).parent)
        for module in (agentq.inspection, agentq.core)
    })


@lru_cache(maxsize=1)
def metrics_digest() -> str:
    """Evaluation source, including derived metrics and experiment rules."""
    return source_tree_digest(Path(__file__).parent)
