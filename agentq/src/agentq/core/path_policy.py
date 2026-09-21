"""Sensitive-path and path-role policy shared by repository capabilities.

The predicates here decide what agentq excludes by default and how a path is
classified for ranking. They are pure name/shape rules; filesystem confinement
lives in :mod:`agentq.core.paths`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SENSITIVE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "credentials",
    "credentials.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "id_rsa",
    "id_ed25519",
}

SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}

SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", "secrets", "credentials"}

SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")

ROLE_PATTERNS = {
    "test": re.compile(
        r"(^|/)(tests?|__tests__|spec)(/|$)|(?:^|[._-])(test|spec)\.[^.]+$", re.I
    ),
    "docs": re.compile(r"(^|/)(docs?|examples?)(/|$)|\.(md|mdx|rst|adoc|txt)$", re.I),
    "config": re.compile(
        r"(^|/)(\.github|config|configs|migrations|supabase)(/|$)|"
        r"(^|/)(package\.json|tsconfig[^/]*\.json|pyproject\.toml|cargo\.toml|"
        r"[^/]+\.config\.(?:[cm]?[jt]s|tsx?)|.*\.(?:ya?ml|toml|ini|cfg))$",
        re.I,
    ),
    "generated": re.compile(
        r"(^|/)(generated|dist|build|coverage|snapshots?|__snapshots__)(/|$)|"
        r"(?:\.generated|\.gen)\.(?:[cm]?[jt]sx?|py|rs|go)$|\.(?:min\.js|map|lock)$",
        re.I,
    ),
}


def is_sensitive_path(path: str | Path) -> bool:
    p = Path(path)
    name = p.name.lower()
    parts = {part.lower() for part in p.parts}
    if name in SENSITIVE_NAMES:
        return True
    if name.startswith(".env") and not name.endswith(SAFE_ENV_SUFFIXES):
        return True
    if p.suffix.lower() in SENSITIVE_SUFFIXES:
        return True
    return bool(parts & SENSITIVE_PARTS)


def classify_path(path: str | Path) -> str:
    value = str(path).replace(os.sep, "/")
    for role, pattern in ROLE_PATTERNS.items():
        if pattern.search(value):
            return role
    return "source"
