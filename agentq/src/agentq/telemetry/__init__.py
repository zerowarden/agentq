"""Telemetry: observation, storage, analytics, and reporting.

Storage and event decoding live in :mod:`agentq.telemetry.storage`; event
construction in :mod:`agentq.telemetry.recorder`; archival administration in
:mod:`agentq.telemetry.archive`; individual analyses in
:mod:`agentq.telemetry.analytics`; presentation in
:mod:`agentq.telemetry.report`. This package re-exports the surface consumed
by the CLI adapter and the statistics tests.
"""

from __future__ import annotations

from .analytics.chains import _build_context_index
from .analytics.efficiency import _cohort_comparison, read_efficiency
from .analytics.verification import _verification_stats
from .archive import (
    archive_file,
    archive_hot_events,
    install_persistence,
    remove_persistence,
    reset_telemetry,
    storage_data,
)
from .models import ComparisonIdentity, TelemetryEvent
from .recorder import _fingerprint_key, _fingerprint_key_at, record_event
from .recording import (
    Observation,
    OutcomeFacts,
    outcome_facts,
    record_error,
    record_success,
)
from .report import (
    _event_facets,
    print_stats,
    render_archive,
    render_persistence,
    render_reset,
    render_stats,
    render_stats_ansi,
    render_stats_plain,
    render_storage,
    stats_data,
    stats_presentation_model,
    watch_stats,
)
from .storage import (
    ACCEPTED_SCHEMAS,
    SCHEMA,
    _append_jsonl_many_unlocked,
    _normalize_event,
    hot_file,
    load_events,
)

__all__ = [
    "ACCEPTED_SCHEMAS",
    "ComparisonIdentity",
    "Observation",
    "OutcomeFacts",
    "SCHEMA",
    "TelemetryEvent",
    "_append_jsonl_many_unlocked",
    "_build_context_index",
    "_cohort_comparison",
    "_event_facets",
    "_fingerprint_key",
    "_fingerprint_key_at",
    "_normalize_event",
    "_verification_stats",
    "archive_file",
    "archive_hot_events",
    "hot_file",
    "install_persistence",
    "load_events",
    "outcome_facts",
    "print_stats",
    "read_efficiency",
    "record_error",
    "record_event",
    "record_success",
    "remove_persistence",
    "render_archive",
    "render_persistence",
    "render_reset",
    "render_stats",
    "render_stats_ansi",
    "render_stats_plain",
    "render_storage",
    "reset_telemetry",
    "stats_data",
    "stats_presentation_model",
    "storage_data",
    "watch_stats",
]
