"""Package command construction and package-level tooling detection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import NodePackage, PackageManager

SCRIPT_ALIASES: Mapping[str, tuple[str, ...]] = {
    "test": ("test", "test:unit", "unit"),
    "typecheck": ("typecheck", "type-check", "check:types", "types"),
    "lint": ("lint", "lint:check"),
    "build": ("build", "compile"),
}


def find_script(package: NodePackage, category: str) -> str | None:
    """The declared script name for ``category``, honoring known aliases."""
    for candidate in SCRIPT_ALIASES.get(category, (category,)):
        if candidate in package.scripts:
            return candidate
    return None


def script_argv(manager: PackageManager, script: str) -> list[str]:
    if manager is PackageManager.PNPM:
        return ["pnpm", "run", script]
    if manager is PackageManager.YARN:
        return ["yarn", script]
    if manager is PackageManager.BUN:
        return ["bun", "run", script]
    return ["npm", "run", script]


def package_exec_argv(manager: PackageManager, argv: Sequence[str]) -> list[str]:
    if manager is PackageManager.PNPM:
        return ["pnpm", "exec", *argv]
    if manager is PackageManager.YARN:
        return ["yarn", "exec", *argv]
    if manager is PackageManager.BUN:
        return ["bunx", *argv]
    return ["npx", "--no-install", *argv]


def has_vitest(
    package: NodePackage, root: Path, root_package: NodePackage | None = None
) -> bool:
    """True when this package (or the root package) declares vitest tooling."""
    if "vitest" in package.declared_dependencies or any(
        "vitest" in command for command in package.scripts.values()
    ):
        return True
    if (
        root_package is not None
        and root_package is not package
        and "vitest" in root_package.runtime_dependencies
    ):
        return True
    return (root / "node_modules" / ".bin" / "vitest").exists()
