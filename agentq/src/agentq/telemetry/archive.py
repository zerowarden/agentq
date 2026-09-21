"""Telemetry archival and persistence administration: rotation of the hot
log into the archive, systemd timer/service installation, storage
reporting, and reset.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agentq.core import AgentQError, repo_id, secure_dir
from agentq.tasking import current_task_state

from .storage import (
    _append_jsonl_many_unlocked,
    _iter_jsonl,
    _telemetry_lock,
    archive_file,
    hot_dir,
    hot_file,
)

ARCHIVE_SERVICE = "agentq-archive.service"
ARCHIVE_TIMER = "agentq-archive.timer"
DEFAULT_ARCHIVE_INTERVAL = "5min"


def systemd_user_dir() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config / "systemd" / "user"


def _systemctl_path() -> str | None:
    override = os.environ.get("AGENTQ_SYSTEMCTL")
    return override or shutil.which("systemctl")


def _systemctl_status(unit: str, action: str) -> str:
    executable = _systemctl_path()
    if not executable:
        return "unavailable"
    try:
        result = subprocess.run(
            [executable, "--user", action, unit],
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    text = (result.stdout or result.stderr).strip().splitlines()
    if text:
        return text[-1].strip()
    return "yes" if result.returncode == 0 else "no"


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _agentq_executable() -> str:
    candidate = shutil.which("agentq")
    if candidate:
        return str(Path(candidate).absolute())
    return str(Path(sys.argv[0]).absolute())


def _valid_systemd_interval(value: str) -> str:
    normalized = value.strip()
    if not re.fullmatch(
        r"[1-9][0-9]*(?:s|sec|secs|min|mins|m|h|hr|hrs)", normalized, re.IGNORECASE
    ):
        raise AgentQError("persistence interval must look like 30s, 5min, or 1h")
    return normalized


def install_persistence(*, interval: str = DEFAULT_ARCHIVE_INTERVAL) -> dict[str, Any]:
    interval = _valid_systemd_interval(interval)
    # The install command is intended for a normal user shell. Persist the
    # current hot set immediately so enabling the timer never leaves existing
    # telemetry waiting for its first scheduled run.
    initial_archive = archive_hot_events()
    systemctl = _systemctl_path()
    if not systemctl:
        raise AgentQError(
            "systemctl is unavailable; automatic telemetry persistence requires a systemd user session"
        )

    unit_dir = secure_dir(systemd_user_dir())
    service = unit_dir / ARCHIVE_SERVICE
    timer = unit_dir / ARCHIVE_TIMER
    executable = _agentq_executable()

    environment_lines: list[str] = []
    for key in ("AGENTQ_TELEMETRY_HOT", "AGENTQ_TELEMETRY_STATE"):
        value = os.environ.get(key)
        if value:
            environment_lines.append(f"Environment={_systemd_quote(f'{key}={value}')}")

    service_text = "\n".join(
        [
            "[Unit]",
            "Description=Persist agentq telemetry",
            "",
            "[Service]",
            "Type=oneshot",
            "UMask=0077",
            *environment_lines,
            f"ExecStart={_systemd_quote(executable)} stats --archive-only --all-repos",
            "StandardOutput=null",
            "StandardError=journal",
            "",
        ]
    )
    timer_text = "\n".join(
        [
            "[Unit]",
            "Description=Periodically persist agentq telemetry",
            "",
            "[Timer]",
            "OnBootSec=2min",
            f"OnUnitActiveSec={interval}",
            "AccuracySec=30s",
            "Persistent=true",
            f"Unit={ARCHIVE_SERVICE}",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )

    try:
        service.write_text(service_text, encoding="utf-8")
        timer.write_text(timer_text, encoding="utf-8")
        service.chmod(0o600)
        timer.chmod(0o600)
        # Out-of-scope subprocess use: operational systemd persistence commands,
        # not agent command lifecycles.
        subprocess.run([systemctl, "--user", "daemon-reload"], check=True, timeout=5)
        subprocess.run(
            [systemctl, "--user", "enable", "--now", ARCHIVE_TIMER],
            check=True,
            timeout=8,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise AgentQError(f"unable to install agentq archive timer: {exc}") from exc

    return {
        "action": "install-persistence",
        "service": str(service),
        "timer": str(timer),
        "interval": interval,
        "active": _systemctl_status(ARCHIVE_TIMER, "is-active"),
        "enabled": _systemctl_status(ARCHIVE_TIMER, "is-enabled"),
        "initial_archive": initial_archive,
    }


def remove_persistence() -> dict[str, Any]:
    systemctl = _systemctl_path()
    unit_dir = systemd_user_dir()
    service = unit_dir / ARCHIVE_SERVICE
    timer = unit_dir / ARCHIVE_TIMER
    if systemctl:
        subprocess.run(
            [systemctl, "--user", "disable", "--now", ARCHIVE_TIMER],
            check=False,
            timeout=8,
        )
    removed: list[str] = []
    for path in (timer, service):
        try:
            if path.exists():
                path.unlink()
                removed.append(str(path))
        except OSError as exc:
            raise AgentQError(f"unable to remove {path}: {exc}") from exc
    if systemctl:
        subprocess.run([systemctl, "--user", "daemon-reload"], check=False, timeout=5)
    return {"action": "remove-persistence", "removed": removed}


def _file_storage(path: Path, repo_id: str | None = None) -> dict[str, Any]:
    events = list(_iter_jsonl(path)) if path.exists() else []
    current = (
        sum(event.get("repo_id") == repo_id for event in events) if repo_id else None
    )
    try:
        info = path.stat()
        size = info.st_size
        modified = info.st_mtime
    except OSError:
        size = 0
        modified = None
    return {
        "path": str(path),
        "exists": path.exists(),
        "events": len(events),
        "current_repo_events": current,
        "bytes": int(size),
        "modified": modified,
    }


def storage_data(root: Path | None = None) -> dict[str, Any]:
    repository_id = repo_id(root) if root else None
    hot = _file_storage(hot_file(), repository_id)
    rotated = _file_storage(hot_file().with_suffix(".jsonl.1"), repository_id)
    persistent = _file_storage(archive_file(), repository_id)
    timer_path = systemd_user_dir() / ARCHIVE_TIMER
    interval = None
    if timer_path.exists():
        try:
            match = re.search(
                r"^OnUnitActiveSec=(.+)$",
                timer_path.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
            interval = match.group(1).strip() if match else None
        except OSError:
            pass
    return {
        "hot": hot,
        "hot_rotated": rotated,
        "persistent": persistent,
        "timer": {
            "unit": str(timer_path),
            "installed": timer_path.exists(),
            "active": (
                _systemctl_status(ARCHIVE_TIMER, "is-active")
                if timer_path.exists()
                else "not-installed"
            ),
            "enabled": (
                _systemctl_status(ARCHIVE_TIMER, "is-enabled")
                if timer_path.exists()
                else "not-installed"
            ),
            "interval": interval,
        },
    }


def _rewrite_excluding_repo(path: Path, repo_id: str) -> int:
    if not path.exists():
        return 0
    secure_dir(path.parent)
    temporary = path.parent / f".{path.name}.reset-{os.getpid()}-{secrets.token_hex(4)}"
    removed = 0
    try:
        with (
            path.open("r", encoding="utf-8", errors="replace") as source,
            temporary.open("w", encoding="utf-8") as target,
        ):
            for line in source:
                keep = True
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get("repo_id") == repo_id:
                        keep = False
                except json.JSONDecodeError:
                    pass
                if keep:
                    target.write(line)
                else:
                    removed += 1
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise AgentQError(f"unable to reset telemetry file {path}: {exc}") from exc
    return removed


def _active_task_count() -> int:
    tasks = hot_dir() / "tasks"
    try:
        return sum(
            1 for path in tasks.iterdir() if path.is_file() and path.suffix == ".json"
        )
    except OSError:
        return 0


def _reset_telemetry_unlocked(
    root: Path, *, all_repos: bool, hot_only: bool, force: bool
) -> dict[str, Any]:
    active = (
        _active_task_count() if all_repos else (1 if current_task_state(root) else 0)
    )
    if active and not force:
        scope = "one or more repositories" if all_repos else "this repository/worktree"
        raise AgentQError(
            f"an agentq task is active for {scope}; accept/abandon it first or pass --force"
        )

    repository_id = repo_id(root)
    persistent_removed = 0
    hot_removed = 0

    # Persistent state is handled first. Under Codex sandboxing this fails before
    # hot telemetry is modified, avoiding a misleading partial reset.
    if not hot_only:
        path = archive_file()
        if all_repos:
            persistent_removed = sum(1 for _ in _iter_jsonl(path))
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise AgentQError(
                    f"unable to reset persistent telemetry {path}: {exc}"
                ) from exc
        else:
            persistent_removed = _rewrite_excluding_repo(path, repository_id)

    for path in (hot_file().with_suffix(".jsonl.1"), hot_file()):
        if all_repos:
            hot_removed += sum(1 for _ in _iter_jsonl(path))
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise AgentQError(
                    f"unable to reset hot telemetry {path}: {exc}"
                ) from exc
        else:
            hot_removed += _rewrite_excluding_repo(path, repository_id)

    return {
        "action": "reset",
        "scope": "all-repositories" if all_repos else root.name,
        "hot_only": bool(hot_only),
        "hot_removed": hot_removed,
        "persistent_removed": persistent_removed,
        "active_tasks_preserved": active,
    }


def reset_telemetry(
    root: Path, *, all_repos: bool = False, hot_only: bool = False, force: bool = False
) -> dict[str, Any]:
    with _telemetry_lock():
        return _reset_telemetry_unlocked(
            root,
            all_repos=all_repos,
            hot_only=hot_only,
            force=force,
        )


def archive_hot_events() -> dict[str, Any]:
    with _telemetry_lock():
        hot = list(_iter_jsonl(hot_file())) + list(
            _iter_jsonl(hot_file().with_suffix(".jsonl.1"))
        )
        destination = archive_file()
        existing = {str(event["id"]) for event in _iter_jsonl(destination)}
        pending = []
        for event in hot:
            identity = str(event["id"])
            if identity in existing:
                continue
            existing.add(identity)
            pending.append(event)
        try:
            added = _append_jsonl_many_unlocked(destination, pending) if pending else 0
        except OSError as exc:
            raise AgentQError(
                f"unable to archive telemetry to {destination}; run 'agentq stats --archive' from a normal shell: {exc}"
            ) from exc
        return {
            "archive": str(destination),
            "hot_events": len(hot),
            "added": added,
            "total_archived": len(existing),
        }
