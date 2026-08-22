from __future__ import annotations

import json
from typing import Any, Iterable

_MISSING = object()
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
) -> tuple[Any, int] | object:
    if budget < 2:
        _record_omission(omitted, path)
        return _MISSING
    if isinstance(value, list):
        projected: list[Any] = []
        used = 2
        encoded_items = [_encode(item) for item in value]
        for index, (item, encoded) in enumerate(zip(value, encoded_items)):
            cost = len(encoded) + (1 if projected else 0)
            if used + cost > budget:
                _record_omission(omitted, path, len(value) - index)
                break
            projected.append(item)
            used += cost
        return projected, used
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        used = 2
        ordinary = [
            key
            for key, item in value.items()
            if not isinstance(item, list) and key != "_agentq"
        ]
        list_keys = [
            key for key in _PREFERRED_LISTS if isinstance(value.get(key), list)
        ]
        list_keys.extend(
            key
            for key, item in value.items()
            if isinstance(item, list) and key not in list_keys
        )
        for key in [*ordinary, *list_keys]:
            key_text = _encode(str(key))
            fixed_cost = len(key_text) + 1 + (1 if projected else 0)
            child_path = _pointer(path, key)
            child = _project(
                value[key], child_path, budget - used - fixed_cost, omitted
            )
            if child is _MISSING:
                continue
            child_value, child_size = child
            if used + fixed_cost + child_size > budget:
                _record_omission(
                    omitted,
                    child_path,
                    len(value[key]) if isinstance(value[key], list) else 1,
                )
                continue
            projected[str(key)] = child_value
            used += fixed_cost + child_size
        return projected, used
    encoded = _encode(value)
    if len(encoded) <= budget:
        return value, len(encoded)
    _record_omission(omitted, path)
    return _MISSING


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
        projected = (
            result[0] if result is not _MISSING and isinstance(result[0], dict) else {}
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

    def marker(count: int) -> str:
        return omission.replace("{count}", str(max(1, count)))

    if total_count is None or budget <= 0:
        materialized = [record.rstrip() for record in records if record.rstrip()]
        total_count = len(materialized)
        full = separator.join((([prefix] if prefix else []) + materialized))
        full_chars = len(full)
        if budget <= 0 or len(full) <= budget:
            return rendered_text(full, prebudget_chars=full_chars), False
        records = materialized
    else:
        total_count = max(0, total_count)

    kept: list[tuple[str, bool]] = []
    used = 0
    prefix_truncated = False
    if prefix:
        if len(prefix) <= budget:
            kept.append((prefix, False))
            used = len(prefix)
        else:
            prefix_truncated = True
            for line in prefix.splitlines():
                cost = len(line) + (len(separator) if kept else 0)
                marker_cost = len(marker(total_count + 1)) + len(separator)
                if used + cost + marker_cost > budget:
                    break
                kept.append((line, False))
                used += cost

    emitted = 0
    for raw_record in records:
        record = raw_record.rstrip()
        if not record:
            continue
        remaining_after = total_count - emitted - 1
        cost = len(record) + (len(separator) if kept else 0)
        reserve = (
            len(separator) + len(marker(remaining_after)) if remaining_after else 0
        )
        if used + cost + reserve > budget:
            break
        kept.append((record, True))
        used += cost
        emitted += 1

    omitted_count = max(0, total_count - emitted)
    truncated = bool(omitted_count or prefix_truncated)
    if truncated:
        omitted_count += int(prefix_truncated)
        omission_marker = marker(omitted_count)
        marker_cost = len(omission_marker) + (len(separator) if kept else 0)
        while kept and used + marker_cost > budget:
            removed, is_record = kept.pop()
            used -= len(removed) + (len(separator) if kept else 0)
            if is_record:
                omitted_count += 1
            omission_marker = marker(omitted_count)
            marker_cost = len(omission_marker) + (len(separator) if kept else 0)
        if marker_cost <= budget:
            kept.append((omission_marker, False))
    visible = separator.join(item for item, _ in kept)
    return (
        rendered_text(visible, prebudget_chars=full_chars, truncated=truncated),
        truncated,
    )
