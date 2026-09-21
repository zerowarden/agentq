from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .errors import AgentQError

_FALSE_VALUES = {"0", "false", "no", "off"}


def env_enabled(name: str, *, default: bool = True) -> bool:
    fallback = "1" if default else "0"
    return os.environ.get(name, fallback).strip().lower() not in _FALSE_VALUES


def secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def stable_id(value: str, *, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:length]


def repo_id(root: Path) -> str:
    return stable_id(str(root.resolve()))


def thread_id() -> str | None:
    raw = os.environ.get("CODEX_THREAD_ID")
    return stable_id(raw) if raw else None


def session_id() -> str | None:
    """Host session identity for repeat suppression, hashed before storage.

    Precedence: explicit AGENTQ_SESSION_ID, then a recognized host thread ID.
    """
    raw = os.environ.get("AGENTQ_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")
    return stable_id(raw) if raw else None


def telemetry_enabled() -> bool:
    return env_enabled("AGENTQ_TELEMETRY")


def default_runtime_root() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return Path(tempfile.gettempdir()) / f"agentq-{uid}"


def telemetry_hot_dir() -> Path:
    override = os.environ.get("AGENTQ_TELEMETRY_HOT")
    return (
        Path(override).expanduser()
        if override
        else default_runtime_root() / "_telemetry"
    )


def context_cache_dir() -> Path:
    override = os.environ.get("AGENTQ_CONTEXT_CACHE_HOME")
    if override:
        return Path(override).expanduser()
    return telemetry_hot_dir().parent / "_context"


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

    raise AgentQError(
        "no writable runtime directory available; tried: " + ", ".join(attempted)
    )
