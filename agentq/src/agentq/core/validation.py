"""Shared validation primitives for typed wire contracts.

Contracts are frozen dataclasses with explicit ``from_wire``/``to_wire``
functions. Validation happens where external JSON, argv, or persisted records
enter the process; dataclass annotations alone never validate.

This module is deliberately dependency-free so contract modules never import
command implementations or capabilities.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast

from .errors import ContractError

_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_TAG_RE = re.compile(r"\A[a-z][a-z0-9]*(?:[-_.][a-z0-9]+)*\Z")
_WINDOWS_DRIVE_RE = re.compile(r"\A[A-Za-z]:")


def canonical_json(payload: Any) -> str:
    """Canonical JSON text: sorted keys, compact separators, raw Unicode."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def canonical_digest(payload: Any, *, length: int | None = None) -> str:
    """SHA-256 over :func:`canonical_json`, optionally truncated for short ids."""
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return digest if length is None else digest[:length]


def as_dict(value: Any) -> dict[str, Any]:
    """A JSON-boundary object: non-object values become an empty object."""
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    """A JSON-boundary array: non-array values become an empty array."""
    return cast("list[Any]", value) if isinstance(value, list) else []


def list_field(value: Mapping[str, Any], key: str) -> list[Any]:
    """A JSON-boundary list field: absent or wrong-typed values are empty."""
    item = value.get(key)
    return cast("list[Any]", item) if isinstance(item, list) else []


def dict_field(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    """A JSON-boundary object field: absent or wrong-typed values are empty."""
    item = value.get(key)
    return cast("dict[str, Any]", item) if isinstance(item, dict) else {}


def require_mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{what} must be a JSON object")
    return cast("dict[str, Any]", value)


def is_instance_of(value: Any, expected: type[Any] | tuple[type[Any], ...]) -> bool:
    """Runtime type guard for values crossing a dynamic boundary."""
    return isinstance(value, expected)


def require_str(value: Any, what: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{what} must be a string")
    if not allow_empty and not value:
        raise ContractError(f"{what} must be a non-empty string")
    return value


def optional_str(value: Any, what: str, *, allow_empty: bool = False) -> str | None:
    if value is None:
        return None
    return require_str(value, what, allow_empty=allow_empty)


def require_bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{what} must be a boolean")
    return value


def require_int(
    value: Any, what: str, *, minimum: int | None = None, maximum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{what} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"{what} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ContractError(f"{what} must be <= {maximum}")
    return value


def optional_int(
    value: Any, what: str, *, minimum: int | None = None, maximum: int | None = None
) -> int | None:
    if value is None:
        return None
    return require_int(value, what, minimum=minimum, maximum=maximum)


def require_number(value: Any, what: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{what} must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ContractError(f"{what} must be >= {minimum}")
    return number


def optional_number(
    value: Any, what: str, *, minimum: float | None = None
) -> float | None:
    if value is None:
        return None
    return require_number(value, what, minimum=minimum)


def require_sha256(value: Any, what: str) -> str:
    text = require_str(value, what)
    if not _SHA256_RE.fullmatch(text):
        raise ContractError(f"{what} must be a lowercase hex sha256 digest")
    return text


def require_tag(value: Any, what: str) -> str:
    text = require_str(value, what)
    if not _TAG_RE.fullmatch(text):
        raise ContractError(f"{what} must be a lowercase tag")
    return text


def require_schema(value: Any, expected: str, what: str) -> str:
    text = require_str(value, what)
    if text != expected:
        raise ContractError(f"unsupported {what}: {text!r} (expected {expected!r})")
    return text


def require_relative_posix(value: Any, what: str, *, allow_root: bool = False) -> str:
    """Validate the wire form of a repository-relative path.

    The wire form is POSIX, relative, has no empty/``.``/``..`` components and
    no trailing slash; the repository root is exactly ``.`` where allowed.
    Confinement against the live filesystem stays in :mod:`paths`.
    """
    text = require_str(value, what)
    if "\x00" in text:
        raise ContractError(f"{what} contains a NUL byte")
    if "\\" in text:
        raise ContractError(f"{what} must use POSIX separators")
    if text.startswith("/") or _WINDOWS_DRIVE_RE.match(text):
        raise ContractError(f"{what} must be repository-relative, not absolute")
    if text == ".":
        if allow_root:
            return text
        raise ContractError(f"{what} must name an entry below the repository root")
    if text.endswith("/"):
        raise ContractError(f"{what} must not have a trailing slash")
    if any(part in {"", ".", ".."} for part in text.split("/")):
        raise ContractError(f"{what} must not contain empty, '.' or '..' components")
    return text


def reject_unknown_keys(
    payload: Mapping[str, Any], allowed: Sequence[str], what: str
) -> None:
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        raise ContractError(f"{what} contains unknown fields: {', '.join(unknown)}")


def require_unique_strings(values: Sequence[str], what: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ContractError(f"{what} contains a duplicate entry: {value}")
        seen.add(value)
