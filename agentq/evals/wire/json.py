"""Strict JSON primitives shared by the wire codecs.

Every decoder under :mod:`evals.wire` reads through these helpers: duplicate
keys and non-finite numbers are rejected, unknown fields are errors, and every
value is validated before a contract record is constructed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import TypeVar, cast

from agentq.core import ContractError

_T = TypeVar("_T", bound=Enum)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ContractError(f"non-finite JSON number: {name}")


def decode_json(text: str, *, what: str) -> object:
    """Parse one strict JSON document: no duplicate keys, no non-finite values."""
    try:
        return json.loads(
            text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except ContractError:
        raise
    except ValueError as exc:
        raise ContractError(f"{what} is not valid JSON: {exc}") from exc


def as_mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError(f"{what} must be a JSON object")
    return cast("dict[str, object]", value)


def as_list(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise ContractError(f"{what} must be a JSON array")
    return cast("list[object]", value)


def exact_keys(
    fields: Mapping[str, object], allowed: set[str], what: str
) -> None:
    extra = sorted(set(fields) - allowed)
    if extra:
        raise ContractError(f"{what} has unknown fields: {', '.join(extra)}")


def object_fields(
    value: object,
    what: str,
    allowed: set[str],
    *,
    optional: Iterable[str] = (),
) -> dict[str, object]:
    """Validate an object's keys before decoding, with explicit legacy defaults."""
    mapping = as_mapping(value, what)
    exact_keys(mapping, allowed, what)
    missing = sorted(allowed - set(optional) - mapping.keys())
    if missing:
        raise ContractError(f"{what} is missing fields: {', '.join(missing)}")
    return mapping


def read_str(value: object, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ContractError(f"{what} must be a string")
    return value


def optional_str(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(f"{what} must be a string or null")
    return value


def read_int(value: object, what: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{what} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{what} must be >= {minimum}")
    return value


def optional_int(value: object, what: str, *, minimum: int | None = None) -> int | None:
    if value is None:
        return None
    return read_int(value, what, minimum=minimum)


def read_number(value: object, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{what} must be a number")
    return float(value)


def read_bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{what} must be a boolean")
    return value


def read_enum(enum_type: type[_T], value: object, what: str) -> _T:
    if not isinstance(value, str):
        raise ContractError(f"{what} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ContractError(f"unsupported {what}: {value!r}") from exc


def read_strings(value: object, what: str) -> tuple[str, ...]:
    items = as_list(value, what)
    return tuple(
        read_str(item, f"{what}[{index}]", allow_empty=True)
        for index, item in enumerate(items)
    )


def reject_duplicates(values: Iterable[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ContractError(f"{what} contains a duplicate id: {value!r}")
        seen.add(value)


def read_json_file(path: Path, *, what: str) -> object:
    """Read one strict JSON document from disk with consistent boundary errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"{what} is unreadable: {path}") from exc
    return decode_json(text, what=what)
