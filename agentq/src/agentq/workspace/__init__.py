"""Workspace capability: change sets and owning-manifest resolution."""

from __future__ import annotations

from .changes import (
    ChangeSet,
    changed_files,
    is_docs_only,
    is_global_change,
    is_public_contract_change,
)
from .discovery import nearest_manifest

__all__ = [
    "ChangeSet",
    "changed_files",
    "is_docs_only",
    "is_global_change",
    "is_public_contract_change",
    "nearest_manifest",
]
