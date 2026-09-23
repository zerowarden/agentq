"""Owning-manifest resolution for package ownership evidence."""

from __future__ import annotations

import json
from pathlib import Path

from agentq.core import dict_field, relpath
from agentq.core.languages import ECOSYSTEMS
from agentq.discovery import PackageManifest


def nearest_manifest(
    root: Path, target: Path, *, ecosystem: str | None = None
) -> PackageManifest | None:
    """Walk up from target to the repository root looking for a package manifest.

    ``ecosystem`` restricts the walk to one ecosystem's manifests, so a Python
    declaration in a polyglot repository resolves to ``pyproject.toml`` instead
    of whichever manifest happens to appear first in the ecosystem catalog.
    """
    profiles = tuple(
        profile
        for profile in ECOSYSTEMS
        if ecosystem is None or profile.id == ecosystem
    )
    current = target if target.is_dir() else target.parent
    while True:
        for profile in profiles:
            for name in profile.manifests:
                path = current / name
                if not path.exists():
                    continue
                relative = relpath(root, path)
                if profile.id != "node":
                    return PackageManifest(path=relative, kind=profile.manifest_kind)
                try:
                    obj = json.loads(path.read_text(encoding="utf-8"))
                    scripts = tuple(sorted((dict_field(obj, "scripts")).keys()))
                except Exception:
                    return PackageManifest(path=relative, kind=profile.manifest_kind)
                return PackageManifest(
                    path=relative,
                    kind=profile.manifest_kind,
                    name=obj.get("name"),
                    scripts=scripts,
                )
        if current == root:
            break
        current = current.parent
    return None
