"""Declarative language and ecosystem metadata.

Boring facts only: display names, suffixes, manifests, and lockfiles. Semantic
interpretation (public API changes, test discovery, build commands) belongs to
the language/ecosystem provider, never to this catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LanguageProfile:
    """One language and the file suffixes that belong to it."""

    id: str
    display: str
    suffixes: tuple[str, ...]


@dataclass(frozen=True)
class EcosystemProfile:
    """One package ecosystem: its manifests, lockfiles, and manifest kind."""

    id: str
    manifest_kind: str
    manifests: tuple[str, ...]
    locks: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()


LANGUAGES: tuple[LanguageProfile, ...] = (
    LanguageProfile("typescript", "TypeScript", (".ts", ".mts", ".cts")),
    LanguageProfile("tsx", "TSX", (".tsx",)),
    LanguageProfile("javascript", "JavaScript", (".js", ".mjs", ".cjs")),
    LanguageProfile("jsx", "JSX", (".jsx",)),
    LanguageProfile("python", "Python", (".py",)),
    LanguageProfile("rust", "Rust", (".rs",)),
    LanguageProfile("go", "Go", (".go",)),
    LanguageProfile("java", "Java", (".java",)),
    LanguageProfile("kotlin", "Kotlin", (".kt", ".kts")),
    LanguageProfile("c", "C", (".c",)),
    LanguageProfile("cpp", "C/C++", (".h", ".cc", ".cpp", ".hpp")),
    LanguageProfile("csharp", "C#", (".cs",)),
    LanguageProfile("ruby", "Ruby", (".rb",)),
    LanguageProfile("php", "PHP", (".php",)),
    LanguageProfile("swift", "Swift", (".swift",)),
    LanguageProfile("sql", "SQL", (".sql",)),
    LanguageProfile("shell", "Shell", (".sh", ".bash", ".zsh")),
    LanguageProfile("fish", "Fish", (".fish",)),
    LanguageProfile("json", "JSON", (".json",)),
    LanguageProfile("yaml", "YAML", (".yaml", ".yml")),
    LanguageProfile("toml", "TOML", (".toml",)),
    LanguageProfile("markdown", "Markdown", (".md",)),
    LanguageProfile("mdx", "MDX", (".mdx",)),
    LanguageProfile("css", "CSS", (".css",)),
    LanguageProfile("scss", "SCSS", (".scss",)),
    LanguageProfile("html", "HTML", (".html",)),
    LanguageProfile("vue", "Vue", (".vue",)),
    LanguageProfile("svelte", "Svelte", (".svelte",)),
)

# Node first preserves the historical manifest precedence when several
# manifests share one directory.
ECOSYSTEMS: tuple[EcosystemProfile, ...] = (
    EcosystemProfile(
        "node",
        "npm",
        ("package.json",),
        ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb"),
        languages=("typescript", "tsx", "javascript", "jsx"),
    ),
    EcosystemProfile(
        "cargo", "cargo", ("Cargo.toml",), ("Cargo.lock",), languages=("rust",)
    ),
    EcosystemProfile(
        "python",
        "python",
        ("pyproject.toml",),
        ("uv.lock", "poetry.lock"),
        languages=("python",),
    ),
    EcosystemProfile("go", "go", ("go.mod",), ("go.sum",), languages=("go",)),
)

WORKSPACE_NAMES = frozenset({"pnpm-workspace.yaml", "pnpm-workspace.yml"})

_BY_SUFFIX: dict[str, LanguageProfile] = {
    suffix: profile for profile in LANGUAGES for suffix in profile.suffixes
}
_BY_ID: dict[str, LanguageProfile] = {profile.id: profile for profile in LANGUAGES}
_ECOSYSTEM_BY_LANGUAGE: dict[str, EcosystemProfile] = {
    language: profile for profile in ECOSYSTEMS for language in profile.languages
}
_MANIFEST_PROFILES: dict[str, EcosystemProfile] = {
    name.lower(): profile for profile in ECOSYSTEMS for name in profile.manifests
}

MANIFEST_NAMES = frozenset(name for profile in ECOSYSTEMS for name in profile.manifests)
LOCK_NAMES = frozenset(lock for profile in ECOSYSTEMS for lock in profile.locks)
MANIFEST_PATTERN = "|".join(re.escape(name) for name in sorted(MANIFEST_NAMES))


def language_id_for(path: str | Path) -> str | None:
    """The catalog id of the language owning ``path``, or ``None``."""
    profile = _BY_SUFFIX.get(Path(path).suffix.lower())
    return profile.id if profile is not None else None


def language_for(path: str | Path) -> str:
    """The display name of the language owning ``path``; ``Other`` when unknown."""
    profile = _BY_SUFFIX.get(Path(path).suffix.lower())
    return profile.display if profile is not None else "Other"


def suffixes_for(*language_ids: str) -> frozenset[str]:
    """Every suffix belonging to any of the given language ids."""
    return frozenset(
        suffix
        for language_id in language_ids
        for suffix in _BY_ID[language_id].suffixes
    )


def ecosystem_for_language(language_id: str | None) -> str | None:
    """The ecosystem id owning ``language_id`` files, if any.

    Ownership lookup needs provider context: in a polyglot repository a Python
    declaration must resolve to the Python ecosystem's manifest even when a
    package.json shares the same directory.
    """
    if language_id is None:
        return None
    profile = _ECOSYSTEM_BY_LANGUAGE.get(language_id)
    return profile.id if profile is not None else None


TS_JS_SUFFIXES = suffixes_for("typescript", "tsx", "javascript", "jsx")
