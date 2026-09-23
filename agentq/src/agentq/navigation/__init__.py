"""Low-level navigation primitives consumed by inspection adapters.

This package exposes only provider execution and payload decoding. Inspection
contracts, resolution, policy, and rendering live in :mod:`agentq.inspection`.
"""

from __future__ import annotations

from .models import (
    TS_NAV_TYPES,
    DeclarationSpan,
    PythonOverview,
    PythonReference,
    PythonReferenceSection,
    TypeScriptBatch,
    TypeScriptBatchRequest,
    TypeScriptCandidateSearch,
    TypeScriptDiscovery,
    TypeScriptLocation,
    TypeScriptMeta,
    TypeScriptNav,
    TypeScriptNavRequest,
    TypeScriptOperation,
    TypeScriptProbe,
    TypeScriptProject,
    TypeScriptRuntime,
)
from .providers import (
    python_symbol_overview,
    ts_nav,
    ts_nav_batch,
    ts_nav_from_payload,
    ts_nav_probe,
    typescript_batch_from_payload,
)

__all__ = [
    "DeclarationSpan",
    "PythonOverview",
    "PythonReference",
    "PythonReferenceSection",
    "TS_NAV_TYPES",
    "TypeScriptBatch",
    "TypeScriptBatchRequest",
    "TypeScriptCandidateSearch",
    "TypeScriptDiscovery",
    "TypeScriptLocation",
    "TypeScriptMeta",
    "TypeScriptNav",
    "TypeScriptNavRequest",
    "TypeScriptOperation",
    "TypeScriptProbe",
    "TypeScriptProject",
    "TypeScriptRuntime",
    "python_symbol_overview",
    "ts_nav",
    "ts_nav_batch",
    "ts_nav_from_payload",
    "ts_nav_probe",
    "typescript_batch_from_payload",
]
