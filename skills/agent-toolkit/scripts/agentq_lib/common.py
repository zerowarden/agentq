from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

VERSION = "1.4.0"

DEFAULT_SKIP_PARTS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build",
    "target", "coverage", ".next", ".nuxt", ".svelte-kit", ".turbo",
    ".parcel-cache", ".cache", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".venv", "venv", ".tox",
}

SENSITIVE_NAMES = {
    ".env", ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
    "secrets.json", "secrets.yaml", "secrets.yml", "id_rsa", "id_ed25519",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
SENSITIVE_PARTS = {".ssh", ".aws", ".gnupg", "secrets", "credentials"}
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")

DEFAULT_RG_EXCLUDES = [
    "!.git/**", "!.hg/**", "!.svn/**", "!node_modules/**", "!vendor/**",
    "!dist/**", "!build/**", "!target/**", "!coverage/**", "!.next/**",
    "!.nuxt/**", "!.svelte-kit/**", "!.turbo/**", "!.parcel-cache/**",
    "!.cache/**", "!__pycache__/**", "!.mypy_cache/**", "!.pytest_cache/**",
    "!.ruff_cache/**", "!.venv/**", "!venv/**", "!.tox/**",
]
SENSITIVE_RG_EXCLUDES = [
    "!.env", "!*.pem", "!*.key", "!*.p12", "!*.pfx",
    "!*.jks", "!*.keystore", "!**/.ssh/**", "!**/.aws/**", "!**/.gnupg/**",
    "!**/credentials/**", "!**/secrets/**", "!**/id_rsa", "!**/id_ed25519",
]
SENSITIVE_RG_REINCLUDES: list[str] = []

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.S), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{24,}\b"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{20,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|service[_-]?role[_-]?key|private[_-]?key)\b(\s*[:=]\s*)([^\s,;\]}]{6,})"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(https?://[^:/\s]+:)([^@/\s]+)(@)"), r"\1[REDACTED]\3"),
]

ROLE_PATTERNS = {
    "test": re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(?:^|[._-])(test|spec)\.[^.]+$", re.I),
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

LANG_BY_SUFFIX = {
    ".ts": "TypeScript", ".tsx": "TSX", ".mts": "TypeScript", ".cts": "TypeScript",
    ".js": "JavaScript", ".jsx": "JSX", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".py": "Python", ".rs": "Rust", ".go": "Go", ".java": "Java",
    ".kt": "Kotlin", ".kts": "Kotlin", ".c": "C", ".h": "C/C++",
    ".cc": "C++", ".cpp": "C++", ".hpp": "C++", ".cs": "C#",
    ".rb": "Ruby", ".php": "PHP", ".swift": "Swift", ".sql": "SQL",
    ".sh": "Shell", ".bash": "Shell", ".zsh": "Shell", ".fish": "Fish",
    ".json": "JSON", ".yaml": "YAML", ".yml": "YAML", ".toml": "TOML",
    ".md": "Markdown", ".mdx": "MDX", ".css": "CSS", ".scss": "SCSS",
    ".html": "HTML", ".vue": "Vue", ".svelte": "Svelte",
}


class AgentQError(RuntimeError):
    pass


@dataclass
class Completed:
    args: Sequence[str]
    returncode: int
    stdout: str
    stderr: str


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def redact_text(text: str) -> str:
    result = text
    for pattern, replacement in SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def compact_line(text: str, max_chars: int = 240) -> str:
    text = strip_ansi(text).replace("\r", "").rstrip("\n")
    text = redact_text(text)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 15)] + " …[truncated]"


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


def scope_match(path: str, scopes: list[str]) -> bool:
    if not scopes or scopes == ["."]:
        return True
    normalized = path.replace(os.sep, "/")
    for scope in scopes:
        value = scope.replace(os.sep, "/")
        while value.startswith("./"):
            value = value[2:]
        value = value.rstrip("/")
        if value in {"", "."} or normalized == value or normalized.startswith(value + "/"):
            return True
    return False


def find_executable(name: str) -> str | None:
    aliases = {
        "fd": ["fd", "fdfind"],
        "bat": ["bat", "batcat"],
        "ast-grep": ["ast-grep"],
        "ctags": ["ctags"],
        "difft": ["difft"],
    }
    for candidate in aliases.get(name, [name]):
        found = shutil.which(candidate)
        if found:
            return found
    if name == "ast-grep":
        sg = shutil.which("sg")
        if sg:
            probe = subprocess.run([sg, "--version"], text=True, capture_output=True)
            if "ast-grep" in (probe.stdout + probe.stderr).lower():
                return sg
    return None


def tool_version(executable: str) -> str:
    for flags in (["--version"], ["-V"], ["version"]):
        try:
            result = subprocess.run([executable, *flags], text=True, capture_output=True, timeout=5)
        except Exception:
            continue
        text = strip_ansi((result.stdout or result.stderr).strip())
        if text:
            return compact_line(text.splitlines()[0], 160)
    return "installed"


def run_cmd(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    timeout: float | None = 60,
    check: bool = False,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> Completed:
    merged = os.environ.copy()
    merged.update({"NO_COLOR": "1", "CLICOLOR": "0", "TERM": "dumb", "PAGER": "cat", "GIT_PAGER": "cat"})
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            list(args), cwd=str(cwd) if cwd else None, text=True, input=input_text,
            capture_output=True, timeout=timeout, env=merged,
        )
    except FileNotFoundError as exc:
        raise AgentQError(f"required command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentQError(f"command timed out after {timeout}s: {' '.join(args)}") from exc
    completed = Completed(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and proc.returncode != 0:
        detail = compact_line(proc.stderr or proc.stdout or "command failed", 500)
        raise AgentQError(f"command failed ({proc.returncode}): {' '.join(args)}\n{detail}")
    return completed


def repo_root(start: str | Path = ".") -> Path:
    base = Path(start).expanduser().resolve()
    if base.is_file():
        base = base.parent
    result = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=base, timeout=10)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return base


def ensure_within(root: Path, path: Path, *, allow_outside: bool = False) -> Path:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        resolved = root / resolved
    resolved = resolved.resolve()
    if not allow_outside and resolved != root and root not in resolved.parents:
        raise AgentQError(f"path is outside repository root: {resolved}")
    return resolved


def relpath(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def list_repo_files(root: Path, *, include_untracked: bool = True) -> list[str]:
    if (root / ".git").exists() or run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, timeout=5).returncode == 0:
        args = ["git", "ls-files", "-z", "--cached"]
        if include_untracked:
            args += ["--others", "--exclude-standard"]
        result = run_cmd(args, cwd=root, timeout=30, check=True)
        return sorted({item for item in result.stdout.split("\0") if item and not is_skipped(item)})
    rg = find_executable("rg")
    if rg:
        args = [rg, "--files", "--hidden"]
        for glob in DEFAULT_RG_EXCLUDES + SENSITIVE_RG_EXCLUDES:
            args += ["--glob", glob]
        result = run_cmd(args, cwd=root, timeout=30)
        if result.returncode in (0, 1):
            return sorted({line for line in result.stdout.splitlines() if line and not is_skipped(line)})
    files: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and not is_skipped(path.relative_to(root).as_posix()):
            files.append(path.relative_to(root).as_posix())
    return sorted(files)


def is_skipped(path: str | Path) -> bool:
    p = Path(path)
    return any(part in DEFAULT_SKIP_PARTS for part in p.parts)


def _writable_runtime_dir(base: Path, digest: str) -> Path | None:
    target = base / digest
    try:
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
        fd, probe = tempfile.mkstemp(prefix=".write-probe-", dir=target)
        os.close(fd)
        Path(probe).unlink(missing_ok=True)
        return target
    except OSError:
        return None


def cache_dir(root: Path) -> Path:
    """Return a private sandbox-friendly runtime directory.

    AGENTQ_CACHE_HOME is the only persistent override. By default, agentq uses
    the process temp directory because coding-agent sandboxes commonly deny
    writes to ~/.cache even when normal UNIX permissions would allow them.
    A repository-local directory is a last-resort fallback.
    """
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    override = os.environ.get("AGENTQ_CACHE_HOME")
    if override:
        candidates = [Path(override).expanduser()]
    else:
        uid = os.getuid() if hasattr(os, "getuid") else "user"
        candidates = [
            Path(tempfile.gettempdir()) / f"agentq-{uid}",
            root / ".agentq-tmp",
        ]

    attempted: list[str] = []
    for base in candidates:
        attempted.append(str(base))
        target = _writable_runtime_dir(base, digest)
        if target is not None:
            return target

    raise AgentQError("no writable runtime directory available; tried: " + ", ".join(attempted))


def write_private_log(root: Path, label: str, text: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label).strip("-") or "log"
    fd, name = tempfile.mkstemp(prefix=f"{safe}-", suffix=".log", dir=cache_dir(root))
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
    except Exception:
        os.close(fd)
        raise
    return Path(name)


def bound_output(text: str, budget: int) -> tuple[str, bool]:
    if budget <= 0 or len(text) <= budget:
        return text, False
    marker = f"\n… [complete records omitted at {budget} chars; narrow the command scope]"
    cutoff = max(0, budget - len(marker))
    head = text[:cutoff]
    newline = head.rfind("\n")
    head = head[:newline] if newline >= 0 else ""
    return (head.rstrip() + marker if head else marker.lstrip()), True


def add_rg_excludes(args: list[str], *, include_sensitive: bool = False) -> None:
    for pattern in DEFAULT_RG_EXCLUDES:
        args += ["--glob", pattern]
    if not include_sensitive:
        for pattern in SENSITIVE_RG_EXCLUDES:
            args += ["--glob", pattern]
        for pattern in SENSITIVE_RG_REINCLUDES:
            args += ["--glob", pattern]


def language_for(path: str | Path) -> str:
    return LANG_BY_SUFFIX.get(Path(path).suffix.lower(), "Other")


def parse_json_lines(text: str) -> Iterable[Any]:
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def human_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB"]
    current = float(value)
    for unit in units:
        if current < 1024 or unit == units[-1]:
            return f"{current:.1f} {unit}" if unit != "B" else f"{int(current)} B"
        current /= 1024
    return f"{value} B"


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
