"""Verification metrics analytics.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from . import _distribution, _percent


def _verification_stats(
    events: list[dict[str, Any]], *, detailed: bool
) -> dict[str, Any]:
    def metrics(event: dict[str, Any]) -> dict[str, Any]:
        value = event.get("metrics")
        return value if isinstance(value, dict) else {}

    def measured(
        event: dict[str, Any], flag: str, legacy_keys: tuple[str, ...]
    ) -> bool:
        values = metrics(event)
        return values.get(flag) is True or any(key in values for key in legacy_keys)

    check_keys = ("planned_steps", "executed_steps", "passed_steps", "failed_steps")
    file_keys = ("changed_files",)
    package_keys = ("changed_packages", "dependent_packages", "affected_packages")
    statuses = Counter(
        str(event.get("subject_status") or "unknown") for event in events
    )
    checks_measured = [
        event
        for event in events
        if measured(event, "verification_checks_measured", check_keys)
    ]
    files_measured = [
        event
        for event in events
        if measured(event, "verification_files_measured", file_keys)
    ]
    packages_measured = [
        event
        for event in events
        if measured(event, "verification_packages_measured", package_keys)
    ]
    known_statuses = {
        "passed",
        "clean",
        "skipped-docs",
        "failed",
        "timeout",
        "partial",
        "unverified",
        "planned",
    }
    result = {
        "runs": len(events),
        "executed_runs": sum(
            event.get("subject_status") != "planned" for event in events
        ),
        "passed": statuses.get("passed", 0)
        + statuses.get("clean", 0)
        + statuses.get("skipped-docs", 0),
        "failed": statuses.get("failed", 0) + statuses.get("timeout", 0),
        "partial": statuses.get("partial", 0) + statuses.get("unverified", 0),
        "planned": statuses.get("planned", 0),
        "dry_runs": statuses.get("planned", 0),
        "unknown": sum(
            count for status, count in statuses.items() if status not in known_statuses
        ),
        "checks_executed": sum(
            int(metrics(event).get("executed_steps", 0)) for event in checks_measured
        ),
        "checks_failed": sum(
            int(metrics(event).get("failed_steps", 0)) for event in checks_measured
        ),
        "changed_files": sum(
            int(metrics(event).get("changed_files", 0)) for event in files_measured
        ),
        "changed_packages": sum(
            int(metrics(event).get("changed_packages", 0))
            for event in packages_measured
        ),
        "affected_packages": sum(
            int(metrics(event).get("affected_packages", 0))
            for event in packages_measured
        ),
        "raw_output_chars": sum(
            int(metrics(event).get("raw_output_chars", 0)) for event in events
        ),
        "checks_instrumented_runs": len(checks_measured),
        "files_instrumented_runs": len(files_measured),
        "packages_instrumented_runs": len(packages_measured),
        "checks_instrumented_percent": _percent(len(checks_measured), len(events)),
        "files_instrumented_percent": _percent(len(files_measured), len(events)),
        "packages_instrumented_percent": _percent(len(packages_measured), len(events)),
        "files_distribution": _distribution([]),
        "packages_distribution": _distribution([]),
        "checks_distribution": _distribution([]),
        "duration_ms_distribution": _distribution([]),
        "modes": [],
        "scopes": [],
        "scope_rows": [],
        "recent": [],
    }
    if not detailed:
        return result

    modes: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    scope_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        event_metrics = metrics(event)
        modes[str(event_metrics.get("verification_mode", "unknown"))] += 1
        scope = str(event_metrics.get("verification_scope", "worktree"))
        scopes[scope] += 1
        scope_groups[scope].append(event)

    scope_rows: list[dict[str, Any]] = []
    for scope, items in sorted(scope_groups.items()):
        local_statuses = Counter(
            str(item.get("subject_status") or "unknown") for item in items
        )
        local_files_measured = [
            item
            for item in items
            if measured(item, "verification_files_measured", file_keys)
        ]
        local_packages_measured = [
            item
            for item in items
            if measured(item, "verification_packages_measured", package_keys)
        ]
        local_checks_measured = [
            item
            for item in items
            if measured(item, "verification_checks_measured", check_keys)
        ]
        files = [
            int(metrics(item).get("changed_files", 0)) for item in local_files_measured
        ]
        packages = [
            int(metrics(item).get("affected_packages", 0))
            for item in local_packages_measured
        ]
        checks = [
            int(
                metrics(item).get(
                    (
                        "planned_steps"
                        if item.get("subject_status") == "planned"
                        else "executed_steps"
                    ),
                    0,
                )
            )
            for item in local_checks_measured
        ]
        scope_rows.append(
            {
                "scope": scope,
                "events": len(items),
                "executed": sum(
                    item.get("subject_status") != "planned" for item in items
                ),
                "dry_runs": local_statuses.get("planned", 0),
                "passed": local_statuses.get("passed", 0)
                + local_statuses.get("clean", 0)
                + local_statuses.get("skipped-docs", 0),
                "failed": local_statuses.get("failed", 0)
                + local_statuses.get("timeout", 0),
                "partial": local_statuses.get("partial", 0)
                + local_statuses.get("unverified", 0),
                "checks_instrumented_runs": len(local_checks_measured),
                "files_instrumented_runs": len(local_files_measured),
                "packages_instrumented_runs": len(local_packages_measured),
                "files_distribution": _distribution(files),
                "packages_distribution": _distribution(packages),
                "checks_distribution": _distribution(checks),
                "duration_ms_distribution": _distribution(
                    [int(item.get("duration_ms", 0)) for item in items]
                ),
            }
        )

    recent = []
    for event in reversed(events[-12:]):
        event_metrics = metrics(event)
        status = str(event.get("subject_status") or "unknown")
        recent.append(
            {
                "time": float(event.get("time", 0)),
                "scope": str(event_metrics.get("verification_scope", "worktree")),
                "status": "dry-run" if status == "planned" else status,
                "mode": str(event_metrics.get("verification_mode", "unknown")),
                "files": (
                    int(event_metrics.get("changed_files", 0))
                    if measured(event, "verification_files_measured", file_keys)
                    else None
                ),
                "packages": (
                    int(event_metrics.get("affected_packages", 0))
                    if measured(event, "verification_packages_measured", package_keys)
                    else None
                ),
                "checks": (
                    int(
                        event_metrics.get(
                            (
                                "planned_steps"
                                if status == "planned"
                                else "executed_steps"
                            ),
                            0,
                        )
                    )
                    if measured(event, "verification_checks_measured", check_keys)
                    else None
                ),
                "failed_checks": (
                    int(event_metrics.get("failed_steps", 0))
                    if measured(event, "verification_checks_measured", check_keys)
                    else None
                ),
                "duration_ms": int(event.get("duration_ms", 0)),
            }
        )

    result.update(
        {
            "files_distribution": _distribution(
                [
                    int(metrics(event).get("changed_files", 0))
                    for event in files_measured
                ]
            ),
            "packages_distribution": _distribution(
                [
                    int(metrics(event).get("affected_packages", 0))
                    for event in packages_measured
                ]
            ),
            "checks_distribution": _distribution(
                [
                    int(metrics(event).get("executed_steps", 0))
                    for event in checks_measured
                    if event.get("subject_status") != "planned"
                ]
            ),
            "duration_ms_distribution": _distribution(
                [int(event.get("duration_ms", 0)) for event in events]
            ),
            "modes": [
                {"mode": mode, "runs": count} for mode, count in modes.most_common()
            ],
            "scopes": [
                {"scope": scope, "runs": count} for scope, count in scopes.most_common()
            ],
            "scope_rows": scope_rows,
            "recent": recent,
        }
    )
    return result
