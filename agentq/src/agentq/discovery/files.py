"""Repository file discovery: listing, root discovery, excludes, ranked lookup."""

from __future__ import annotations

from pathlib import Path

from agentq.core import (
    AgentQError,
    resolve_repo_path,
)
from agentq.execution import run_cmd
from agentq.tooling import find_executable

DEFAULT_SKIP_PARTS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    "coverage",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".turbo",
    ".parcel-cache",
    ".cache",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    ".tox",
}

DEFAULT_RG_EXCLUDES = [
    "!.git/**",
    "!.hg/**",
    "!.svn/**",
    "!node_modules/**",
    "!vendor/**",
    "!dist/**",
    "!build/**",
    "!target/**",
    "!coverage/**",
    "!.next/**",
    "!.nuxt/**",
    "!.svelte-kit/**",
    "!.turbo/**",
    "!.parcel-cache/**",
    "!.cache/**",
    "!__pycache__/**",
    "!.mypy_cache/**",
    "!.pytest_cache/**",
    "!.ruff_cache/**",
    "!.venv/**",
    "!venv/**",
    "!.tox/**",
]
SENSITIVE_RG_EXCLUDES = [
    "!.env",
    "!*.pem",
    "!*.key",
    "!*.p12",
    "!*.pfx",
    "!*.jks",
    "!*.keystore",
    "!**/.ssh/**",
    "!**/.aws/**",
    "!**/.gnupg/**",
    "!**/credentials/**",
    "!**/secrets/**",
    "!**/id_rsa",
    "!**/id_ed25519",
]
SENSITIVE_RG_REINCLUDES: list[str] = []


def is_skipped(path: str | Path) -> bool:
    p = Path(path)
    return any(part in DEFAULT_SKIP_PARTS for part in p.parts)


def list_repo_files(root: Path, *, include_untracked: bool = True) -> list[str]:
    if (root / ".git").exists() or run_cmd(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=root, timeout=5
    ).returncode == 0:
        args = ["git", "ls-files", "-z", "--cached"]
        if include_untracked:
            args += ["--others", "--exclude-standard"]
        result = run_cmd(args, cwd=root, timeout=30, check=True)
        return sorted(
            {
                item
                for item in result.stdout.split("\0")
                if item and not is_skipped(item)
            }
        )
    rg = find_executable("rg")
    if rg:
        args = [rg, "--files", "--hidden"]
        for glob in DEFAULT_RG_EXCLUDES + SENSITIVE_RG_EXCLUDES:
            args += ["--glob", glob]
        result = run_cmd(args, cwd=root, timeout=30)
        if result.returncode in (0, 1):
            return sorted(
                {
                    line
                    for line in result.stdout.splitlines()
                    if line and not is_skipped(line)
                }
            )
    files: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and not is_skipped(path.relative_to(root).as_posix()):
            files.append(path.relative_to(root).as_posix())
    return sorted(files)


def add_rg_excludes(args: list[str], *, include_sensitive: bool = False) -> None:
    for pattern in DEFAULT_RG_EXCLUDES:
        args += ["--glob", pattern]
    if not include_sensitive:
        for pattern in SENSITIVE_RG_EXCLUDES:
            args += ["--glob", pattern]
        for pattern in SENSITIVE_RG_REINCLUDES:
            args += ["--glob", pattern]


def repo_root(start: str | Path = ".") -> Path:
    base = Path(start).expanduser().resolve()
    if base.is_file():
        base = base.parent
    result = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=base, timeout=10)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return base


def validated_scopes(root: Path, scopes: list[str]) -> list[str]:
    """Confine and require every requested scope; suggestions stay helpful."""
    values = scopes or ["."]
    normalized: list[str] = []
    missing: list[str] = []
    for value in values:
        # One confinement implementation (core.paths); existence stays separate
        # so missing scopes keep their basename suggestions below.
        confined = resolve_repo_path(root, value)
        if not confined.absolute.exists():
            missing.append(value)
            continue
        normalized.append(confined.relative)
    if missing:
        joined = ", ".join(missing[:6]) + (" …" if len(missing) > 6 else "")
        suggestions: list[str] = []
        try:
            repo_files = list_repo_files(root)
            wanted = {Path(value).name for value in missing if Path(value).name}
            suggestions = [path for path in repo_files if Path(path).name in wanted][:4]
        except Exception:
            suggestions = []
        suffix = (
            f"; closest basename matches: {', '.join(suggestions)}"
            if suggestions
            else ""
        )
        raise AgentQError(f"search path does not exist: {joined}{suffix}")
    return normalized or ["."]
