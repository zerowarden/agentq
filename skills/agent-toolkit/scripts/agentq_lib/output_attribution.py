from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Hashable


OUTPUT_ATTRIBUTION_KEYS = (
    "unique_evidence_chars",
    "duplicate_evidence_chars",
    "framing_chars",
    "serialization_chars",
    "advice_chars",
)

_ADVICE_KEYS = {
    "advice", "continuation", "error", "hint", "next", "reason", "suggestion",
}
_ADVICE_PREFIXES = (
    "agentq:",
    "coverage sampled:",
    "continue:",
    "global read cap reached",
    "read overlap:",
    "sample scan cap reached",
    "[unchanged range already returned",
    "… window stopped",
    "… ",
)
_SAFE_VIEWS = {
    "default", "directory", "file", "hunks", "lexical", "matches", "patch",
    "python", "repeat-suppressed", "semantic", "snippets", "source-windows",
    "summary", "windowed", "ambiguous", "edit",
}


def empty_attribution() -> dict[str, int]:
    return {key: 0 for key in OUTPUT_ATTRIBUTION_KEYS}


def attribution_total(attribution: Any) -> int:
    if not isinstance(attribution, dict):
        return 0
    return sum(max(0, int(attribution.get(key, 0) or 0)) for key in OUTPUT_ATTRIBUTION_KEYS)


def output_view(command: str, data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return "default"
    if data.get("repeat_suppressed"):
        return "repeat-suppressed"
    value = data.get("effective_view")
    if not isinstance(value, str) and isinstance(data.get("summary"), dict):
        value = data["summary"].get("view")
    if not isinstance(value, str) and command == "inspect":
        value = data.get("kind")
    if isinstance(value, str) and value in _SAFE_VIEWS:
        return value
    if command == "read" and data.get("windowed"):
        return "windowed"
    if command == "git-diff":
        if isinstance(data.get("patch"), str):
            return "patch"
        if data.get("hunks"):
            return "hunks"
        return "summary"
    return "default"


def _evidence_identity(
    command: str,
    key: str | None,
    value: str,
    parent: dict[str, Any] | None,
    ancestors: tuple[str, ...],
    path_hint: str,
) -> Hashable | None:
    parent = parent or {}
    local_path = parent.get("path") or parent.get("file") or path_hint
    line = parent.get("line")
    if key == "text" and isinstance(line, int):
        return ("line", str(local_path), line)
    if key == "patch" and command in {"git-diff", "inspect"}:
        return ("patch",)
    if key in {"name", "signature"} and "symbols" in ancestors:
        return ("symbol", str(local_path), line, key)
    if key in {"preview", "signature"} and any(part in {"candidates", "results"} for part in ancestors):
        return ("semantic", str(local_path), line, key)
    if key is None and ancestors and ancestors[-1] == "lines" and command in {"outline", "inspect"}:
        return ("outline-line", str(local_path), value)
    return None


def _is_advice(key: str | None, ancestors: tuple[str, ...]) -> bool:
    return bool(key in _ADVICE_KEYS or any(part in _ADVICE_KEYS for part in ancestors))


def _json_attribution(command: str, value: Any) -> dict[str, int]:
    result = empty_attribution()
    seen: set[Hashable] = set()

    def walk(
        current: Any,
        ancestors: tuple[str, ...] = (),
        path_hint: str = "",
        parent: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> None:
        if isinstance(current, dict):
            result["serialization_chars"] += 2 + max(0, len(current) - 1)
            local_path = current.get("path") or current.get("file") or path_hint
            local_path = str(local_path) if isinstance(local_path, str) else path_hint
            for child_key, child in current.items():
                result["serialization_chars"] += len(json.dumps(str(child_key), ensure_ascii=False)) + 1
                walk(child, (*ancestors, str(child_key)), local_path, current, str(child_key))
            return
        if isinstance(current, list):
            result["serialization_chars"] += 2 + max(0, len(current) - 1)
            for child in current:
                walk(child, ancestors, path_hint, parent, None)
            return
        if isinstance(current, str):
            encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
            result["serialization_chars"] += len(encoded) - len(current)
            identity = _evidence_identity(command, key, current, parent, ancestors, path_hint)
            if identity is not None:
                category = "duplicate_evidence_chars" if identity in seen else "unique_evidence_chars"
                seen.add(identity)
            elif _is_advice(key, ancestors):
                category = "advice_chars"
            else:
                category = "framing_chars"
            result[category] += len(current)
            return
        result["framing_chars"] += len(json.dumps(current, ensure_ascii=False, separators=(",", ":")))

    walk(value)
    return result


def _evidence_fragments(command: str, data: dict[str, Any]) -> dict[str, list[Hashable]]:
    fragments: dict[str, list[Hashable]] = defaultdict(list)

    def walk(
        current: Any,
        ancestors: tuple[str, ...] = (),
        path_hint: str = "",
        parent: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> None:
        if isinstance(current, dict):
            local_path = current.get("path") or current.get("file") or path_hint
            local_path = str(local_path) if isinstance(local_path, str) else path_hint
            for child_key, child in current.items():
                walk(child, (*ancestors, str(child_key)), local_path, current, str(child_key))
            return
        if isinstance(current, list):
            for child in current:
                walk(child, ancestors, path_hint, parent, None)
            return
        if not isinstance(current, str) or len(current) < 2:
            return
        identity = _evidence_identity(command, key, current, parent, ancestors, path_hint)
        if identity is None:
            return
        values = current.splitlines() if "\n" in current else [current]
        for index, fragment in enumerate(values):
            if len(fragment) >= 2:
                candidate = (*identity, index) if len(values) > 1 else identity
                if candidate not in fragments[fragment]:
                    fragments[fragment].append(candidate)

    walk(data)
    return fragments


def _text_attribution(command: str, data: dict[str, Any], visible: str) -> dict[str, int]:
    result = empty_attribution()
    categories = [""] * len(visible)

    offset = 0
    for line in visible.splitlines(keepends=True):
        if line.strip().lower().startswith(_ADVICE_PREFIXES):
            for index in range(offset, offset + len(line)):
                categories[index] = "advice_chars"
        offset += len(line)

    fragments = _evidence_fragments(command, data)
    for fragment in sorted(fragments, key=lambda item: (-len(item), item)):
        identities = fragments[fragment]
        start = 0
        occurrence = 0
        while True:
            found = visible.find(fragment, start)
            if found < 0:
                break
            end = found + len(fragment)
            start = found + max(1, len(fragment))
            if any(categories[index] for index in range(found, end)):
                continue
            category = "unique_evidence_chars" if occurrence < len(identities) else "duplicate_evidence_chars"
            occurrence += 1
            for index in range(found, end):
                categories[index] = category

    for category in categories:
        result[category or "framing_chars"] += 1
    return result


def attribute_output(
    command: str,
    data: dict[str, Any] | None,
    visible: str,
    *,
    output_format: str,
) -> dict[str, int]:
    if not visible:
        return empty_attribution()
    payload = data if isinstance(data, dict) else {}
    if output_format in {"json", "compact-json"}:
        try:
            parsed = json.loads(visible)
        except (TypeError, json.JSONDecodeError):
            result = empty_attribution()
            result["framing_chars"] = len(visible)
            return result
        result = _json_attribution(command, parsed)
    else:
        result = _text_attribution(command, payload, visible)
    difference = len(visible) - attribution_total(result)
    result["framing_chars"] += difference
    return result
