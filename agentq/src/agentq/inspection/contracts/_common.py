"""Shared validation for inspection contract records."""

from __future__ import annotations

from agentq.core import (
    ContractError,
    is_instance_of,
    require_relative_posix,
    require_unique_strings,
)


def validate_scopes(scopes: tuple[str, ...], what: str) -> None:
    if not is_instance_of(scopes, tuple) or not all(
        is_instance_of(item, str) for item in scopes
    ):
        raise ContractError(f"{what} must be a tuple of strings")
    for scope in scopes:
        require_relative_posix(scope, f"{what} entry", allow_root=True)
    require_unique_strings(scopes, what)
