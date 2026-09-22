from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

_PREFERRED_LISTS = (
    "items",
    "results",
    "hits",
    "files",
    "hunks",
    "steps",
    "matches",
    "candidates",
    "packages",
    "recent",
    "commands",
)


class RenderedText(str):
    prebudget_chars: int
    truncated: bool

    def __new__(
        cls,
        value: str,
        *,
        prebudget_chars: int | None = None,
        truncated: bool = False,
    ) -> RenderedText:
        instance = super().__new__(cls, value)
        instance.prebudget_chars = (
            len(value) if prebudget_chars is None else max(len(value), prebudget_chars)
        )
        instance.truncated = truncated
        return instance


def rendered_text(
    value: str,
    *,
    prebudget_chars: int | None = None,
    truncated: bool = False,
) -> RenderedText:
    return RenderedText(value, prebudget_chars=prebudget_chars, truncated=truncated)


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _pointer(path: str, key: Any) -> str:
    escaped = str(key).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}" if path else f"/{escaped}"


def _record_omission(omitted: dict[str, int], path: str, count: int = 1) -> None:
    key = path or "/"
    omitted[key] = omitted.get(key, 0) + max(1, count)


def _project(
    value: Any, path: str, budget: int, omitted: dict[str, int]
) -> tuple[Any, int] | None:
    if budget < 2:
        _record_omission(omitted, path)
        return None
    if isinstance(value, list):
        values = cast("list[Any]", value)
        projected_items: list[Any] = []
        used = 2
        encoded_items = [_encode(item) for item in values]
        for index, (item, encoded) in enumerate(
            zip(values, encoded_items, strict=True)
        ):
            cost = len(encoded) + (1 if projected_items else 0)
            if used + cost > budget:
                _record_omission(omitted, path, len(values) - index)
                break
            projected_items.append(item)
            used += cost
        return projected_items, used
    if isinstance(value, dict):
        mapping = cast("dict[str, Any]", value)
        projected: dict[str, Any] = {}
        used = 2
        ordinary = [
            key
            for key, item in mapping.items()
            if not isinstance(item, list) and key != "_agentq"
        ]
        list_keys = [
            key for key in _PREFERRED_LISTS if isinstance(mapping.get(key), list)
        ]
        list_keys.extend(
            key
            for key, item in mapping.items()
            if isinstance(item, list) and key not in list_keys
        )
        for key in [*ordinary, *list_keys]:
            key_text = _encode(str(key))
            fixed_cost = len(key_text) + 1 + (1 if projected else 0)
            child_path = _pointer(path, key)
            child = _project(
                mapping[key], child_path, budget - used - fixed_cost, omitted
            )
            if child is None:
                continue
            child_value, child_size = child
            if used + fixed_cost + child_size > budget:
                _record_omission(
                    omitted,
                    child_path,
                    len(mapping[key]) if isinstance(mapping[key], list) else 1,
                )
                continue
            projected[str(key)] = child_value
            used += fixed_cost + child_size
        return projected, used
    encoded = _encode(value)
    if len(encoded) <= budget:
        return value, len(encoded)
    _record_omission(omitted, path)
    return None


def _bounded_omissions(omitted: dict[str, int], limit: int = 10) -> dict[str, int]:
    items = list(omitted.items())
    bounded = dict(items[:limit])
    if len(items) > limit:
        bounded["/…"] = sum(count for _, count in items[limit:])
    return bounded


def project_json(data: dict[str, Any], budget: int) -> tuple[str, bool]:
    full = _encode(data)
    if budget <= 0 or len(full) <= budget:
        return full, False

    payload_budget = max(2, budget - 220)
    projected: dict[str, Any] = {}
    omitted: dict[str, int] = {}
    for _ in range(2):
        omitted = {}
        result = _project(data, "", payload_budget, omitted)
        candidate = result[0] if result is not None else None
        projected = (
            cast("dict[str, Any]", candidate) if isinstance(candidate, dict) else {}
        )
        projected["_agentq"] = {
            "truncated": True,
            "original_chars": len(full),
            "budget": budget,
            "omitted": _bounded_omissions(omitted),
        }
        visible = _encode(projected)
        if len(visible) <= budget:
            return visible, True
        payload_budget = max(2, payload_budget - (len(visible) - budget) - 16)

    minimal = {
        "_agentq": {
            "truncated": True,
            "original_chars": len(full),
            "budget": budget,
            "omitted": {"/": max(1, len(data))},
        }
    }
    visible = _encode(minimal)
    if len(visible) > budget:
        visible = _encode({"_agentq": {"truncated": True, "budget": budget}})
    return visible, True


def _omission_marker(omission: str, count: int) -> str:
    return omission.replace("{count}", str(max(1, count)))


def _fit_prefix(
    prefix: str, budget: int, separator: str, total_count: int, omission: str
) -> tuple[list[tuple[str, bool]], int, bool]:
    kept: list[tuple[str, bool]] = []
    used = 0
    if not prefix:
        return kept, used, False
    if len(prefix) <= budget:
        return [(prefix, False)], len(prefix), False
    for line in prefix.splitlines():
        cost = len(line) + (len(separator) if kept else 0)
        marker_cost = len(_omission_marker(omission, total_count + 1)) + len(separator)
        if used + cost + marker_cost > budget:
            break
        kept.append((line, False))
        used += cost
    return kept, used, True


def _fit_records(
    records: Iterable[str],
    kept: list[tuple[str, bool]],
    used: int,
    total_count: int,
    budget: int,
    separator: str,
    omission: str,
) -> tuple[list[tuple[str, bool]], int, int]:
    emitted = 0
    for raw_record in records:
        record = raw_record.rstrip()
        if not record:
            continue
        remaining_after = total_count - emitted - 1
        cost = len(record) + (len(separator) if kept else 0)
        reserve = (
            len(separator) + len(_omission_marker(omission, remaining_after))
            if remaining_after
            else 0
        )
        if used + cost + reserve > budget:
            break
        kept.append((record, True))
        used += cost
        emitted += 1
    return kept, used, emitted


def _apply_omission_marker(
    kept: list[tuple[str, bool]],
    used: int,
    omitted_count: int,
    prefix_truncated: bool,
    budget: int,
    separator: str,
    omission: str,
) -> list[tuple[str, bool]]:
    if not (omitted_count or prefix_truncated):
        return kept
    omitted_count += int(prefix_truncated)
    omission_marker = _omission_marker(omission, omitted_count)
    marker_cost = len(omission_marker) + (len(separator) if kept else 0)
    while kept and used + marker_cost > budget:
        removed, is_record = kept.pop()
        used -= len(removed) + (len(separator) if kept else 0)
        if is_record:
            omitted_count += 1
        omission_marker = _omission_marker(omission, omitted_count)
        marker_cost = len(omission_marker) + (len(separator) if kept else 0)
    if marker_cost <= budget:
        kept.append((omission_marker, False))
    return kept


def budget_text_records(
    prefix: str,
    records: Iterable[str],
    budget: int,
    *,
    separator: str = "\n",
    omission: str = "… {count} complete records omitted by render budget",
    total_count: int | None = None,
) -> tuple[RenderedText, bool]:
    prefix = prefix.rstrip()
    full_chars: int | None = None

    if total_count is None or budget <= 0:
        materialized = [record.rstrip() for record in records if record.rstrip()]
        total_count = len(materialized)
        full = separator.join(([prefix] if prefix else []) + materialized)
        full_chars = len(full)
        if budget <= 0 or len(full) <= budget:
            return rendered_text(full, prebudget_chars=full_chars), False
        records = materialized
    else:
        total_count = max(0, total_count)

    kept, used, prefix_truncated = _fit_prefix(
        prefix, budget, separator, total_count, omission
    )
    kept, used, emitted = _fit_records(
        records, kept, used, total_count, budget, separator, omission
    )
    omitted_count = max(0, total_count - emitted)
    truncated = bool(omitted_count or prefix_truncated)
    kept = _apply_omission_marker(
        kept, used, omitted_count, prefix_truncated, budget, separator, omission
    )
    visible = separator.join(item for item, _ in kept)
    return (
        rendered_text(visible, prebudget_chars=full_chars, truncated=truncated),
        truncated,
    )
