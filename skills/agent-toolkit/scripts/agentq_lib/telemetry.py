from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .common import AgentQError, bound_output, human_bytes
from .context_cache import context_cache_enabled, diff_payload, read_ranges
from .output_attribution import OUTPUT_ATTRIBUTION_KEYS, attribution_total, empty_attribution
from .runtime import env_enabled, repo_id, secure_dir, stable_id, telemetry_hot_dir, thread_id
from .tasking import current_task_id, current_task_state

try:
    import fcntl
except ImportError:
    fcntl = None

SCHEMA = 6
ACCEPTED_SCHEMAS = {1, 2, 3, 4, 5, 6}
MAX_EVENT_BYTES = 4096
MAX_HOT_BYTES = 10 * 1024 * 1024
ARCHIVE_SERVICE = "agentq-archive.service"
ARCHIVE_TIMER = "agentq-archive.timer"
DEFAULT_ARCHIVE_INTERVAL = "5min"
KNOWN_FAILURE_OPTION_CATEGORIES = {
    "--include-source": "source-inclusion",
}
MEASURED_COMMANDS = {"read", "search", "git-diff", "outline", "inspect"}
EXPANSION_OPTIONS = {
    "--budget": "budget",
    "--limit": "limit",
    "--max-results": "limit",
    "--max-lines": "max_lines",
    "--max-files": "max_files",
    "--max-hunks": "max_hunks",
    "--max-chars": "max_chars",
    "--scan-cap": "scan_cap",
    "--per-file": "samples_per_file",
    "--samples-per-file": "samples_per_file",
}

def telemetry_enabled() -> bool:
    return env_enabled("AGENTQ_TELEMETRY")


def hot_dir() -> Path:
    return telemetry_hot_dir()


def hot_file() -> Path:
    return hot_dir() / "events.jsonl"


@contextmanager
def _telemetry_lock() -> Iterable[None]:
    path = secure_dir(hot_dir()) / "telemetry.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, stat.S_IRUSR | stat.S_IWUSR)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def archive_file() -> Path:
    override = os.environ.get("AGENTQ_TELEMETRY_STATE")
    if override:
        path = Path(override).expanduser()
        return path if path.suffix else path / "events.jsonl"
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state / "agentq" / "events.jsonl"


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
            text=True, capture_output=True, timeout=3, check=False,
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
    if not re.fullmatch(r"[1-9][0-9]*(?:s|sec|secs|min|mins|m|h|hr|hrs)", normalized, re.IGNORECASE):
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
        raise AgentQError("systemctl is unavailable; automatic telemetry persistence requires a systemd user session")

    unit_dir = secure_dir(systemd_user_dir())
    service = unit_dir / ARCHIVE_SERVICE
    timer = unit_dir / ARCHIVE_TIMER
    executable = _agentq_executable()

    environment_lines: list[str] = []
    for key in ("AGENTQ_TELEMETRY_HOT", "AGENTQ_TELEMETRY_STATE"):
        value = os.environ.get(key)
        if value:
            environment_lines.append(f"Environment={_systemd_quote(f'{key}={value}')}" )

    service_text = "\n".join([
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
    ])
    timer_text = "\n".join([
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
    ])

    try:
        service.write_text(service_text, encoding="utf-8")
        timer.write_text(timer_text, encoding="utf-8")
        service.chmod(0o600)
        timer.chmod(0o600)
        subprocess.run([systemctl, "--user", "daemon-reload"], check=True, timeout=5)
        subprocess.run([systemctl, "--user", "enable", "--now", ARCHIVE_TIMER], check=True, timeout=8)
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
        subprocess.run([systemctl, "--user", "disable", "--now", ARCHIVE_TIMER], check=False, timeout=8)
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
    current = sum(event.get("repo_id") == repo_id for event in events) if repo_id else None
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
            match = re.search(r"^OnUnitActiveSec=(.+)$", timer_path.read_text(encoding="utf-8"), re.MULTILINE)
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
            "active": _systemctl_status(ARCHIVE_TIMER, "is-active") if timer_path.exists() else "not-installed",
            "enabled": _systemctl_status(ARCHIVE_TIMER, "is-enabled") if timer_path.exists() else "not-installed",
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
        with path.open("r", encoding="utf-8", errors="replace") as source, temporary.open("w", encoding="utf-8") as target:
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
        return sum(1 for path in tasks.iterdir() if path.is_file() and path.suffix == ".json")
    except OSError:
        return 0


def _reset_telemetry_unlocked(root: Path, *, all_repos: bool, hot_only: bool, force: bool) -> dict[str, Any]:
    active = _active_task_count() if all_repos else (1 if current_task_state(root) else 0)
    if active and not force:
        scope = "one or more repositories" if all_repos else "this repository/worktree"
        raise AgentQError(f"an agentq task is active for {scope}; accept/abandon it first or pass --force")

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
                raise AgentQError(f"unable to reset persistent telemetry {path}: {exc}") from exc
        else:
            persistent_removed = _rewrite_excluding_repo(path, repository_id)

    for path in (hot_file().with_suffix(".jsonl.1"), hot_file()):
        if all_repos:
            hot_removed += sum(1 for _ in _iter_jsonl(path))
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise AgentQError(f"unable to reset hot telemetry {path}: {exc}") from exc
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


def reset_telemetry(root: Path, *, all_repos: bool = False, hot_only: bool = False, force: bool = False) -> dict[str, Any]:
    with _telemetry_lock():
        return _reset_telemetry_unlocked(
            root, all_repos=all_repos, hot_only=hot_only, force=force,
        )


def render_storage(data: dict[str, Any]) -> str:
    def short_path(value: str) -> str:
        home = str(Path.home())
        return "~" + value[len(home):] if value.startswith(home + os.sep) else value

    def age_label(value: float | None) -> str:
        if value is None:
            return ""
        seconds = max(0, int(time.time() - value))
        if seconds < 60:
            return f"{seconds}s ago"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        return f"{seconds // 86400}d ago"

    lines = ["agentq telemetry storage"]
    for label, key in (("hot", "hot"), ("rotated", "hot_rotated"), ("persistent", "persistent")):
        item = data[key]
        current = item.get("current_repo_events")
        suffix = f", {current} current-repo" if current is not None else ""
        updated = age_label(item.get("modified"))
        updated_suffix = f", updated {updated}" if updated else ""
        lines.append(f"{label:<10} {human_bytes(item['bytes']):>9}, {item['events']} events{suffix}{updated_suffix}, {short_path(item['path'])}")
    timer = data["timer"]
    if timer["installed"]:
        timer_bits = [timer["active"], timer["enabled"]]
        if timer.get("interval"):
            timer_bits.append(str(timer["interval"]))
        lines.append(f"timer      {', '.join(timer_bits)}")
    else:
        lines.append("timer      not installed")
        lines.append("install    agentq stats --install-persistence")
    return "\n".join(lines)


def render_persistence(data: dict[str, Any]) -> str:
    if data.get("action") == "install-persistence":
        return f"archive timer installed: {data['interval']}, {data['active']}, {data['enabled']}"
    return f"archive timer removed: {len(data.get('removed', []))} unit file(s)"


def render_reset(data: dict[str, Any]) -> str:
    total = int(data.get("hot_removed", 0)) + int(data.get("persistent_removed", 0))
    scope = data.get("scope", "current repository")
    detail = "hot only" if data.get("hot_only") else "hot + persistent"
    return f"telemetry reset: {scope}, {total} events removed, {detail}"


def render_archive(data: dict[str, Any]) -> str:
    return f"telemetry archived: +{data.get('added', 0)}, {data.get('total_archived', 0)} persistent"


def _rotate_hot(path: Path) -> None:
    try:
        if path.stat().st_size < MAX_HOT_BYTES:
            return
    except OSError:
        return
    backup = path.with_suffix(".jsonl.1")
    try:
        backup.unlink(missing_ok=True)
        path.replace(backup)
        backup.chmod(0o600)
    except OSError:
        pass


def _event_payload(event: dict[str, Any]) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_EVENT_BYTES:
        event = {key: value for key, value in event.items() if key not in {"metrics", "repo_name"}}
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return (payload + "\n").encode("utf-8")


def _append_jsonl_many_unlocked(path: Path, events: Iterable[dict[str, Any]]) -> int:
    secure_dir(path.parent)
    _rotate_hot(path) if path == hot_file() else None
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, stat.S_IRUSR | stat.S_IWUSR)
    count = 0
    with os.fdopen(fd, "ab") as handle:
        buffer = bytearray()
        for event in events:
            buffer.extend(_event_payload(event))
            count += 1
            if len(buffer) >= 1024 * 1024:
                handle.write(buffer)
                buffer.clear()
        if buffer:
            handle.write(buffer)
    return count


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    with _telemetry_lock():
        _append_jsonl_many_unlocked(path, [event])


@lru_cache(maxsize=8)
def _fingerprint_key_at(path_value: str) -> bytes:
    path = Path(path_value)
    try:
        if path.exists():
            return path.read_bytes()
        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError:
            return path.read_bytes()
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key
    except OSError:
        # Ephemeral fallback preserves privacy if the telemetry directory is read-only.
        return hashlib.sha256(f"agentq:{os.getpid()}".encode()).digest()


def _fingerprint_key() -> bytes:
    path = secure_dir(hot_dir()) / "fingerprint.key"
    return _fingerprint_key_at(str(path))


def _fingerprint(value: str) -> str:
    return hmac.new(_fingerprint_key(), value.encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:20]


def _query_shape(value: str) -> str:
    if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", value):
        return "identifier"
    if any(ch in value for ch in "|()[]{}.*+?\\"):
        return "regex-like"
    return "literal"


def _error_category(message: str | None) -> str | None:
    if not message:
        return None
    text = message.lower()
    if "does not exist" in text or "not found" in text:
        return "not-found"
    if "outside repository" in text or "outside the repository" in text:
        return "outside-repository"
    if (
        "invalid arguments" in text or "requires" in text or "provide " in text
        or "cannot be combined" in text or "accepts one query" in text
    ):
        return "invalid-arguments"
    if "typescript" in text and ("project" in text or "tsconfig" in text):
        return "typescript-project-unavailable"
    if "unavailable" in text or "missing" in text:
        return "missing-dependency"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "parse" in text or "json" in text:
        return "parse-error"
    return "other"


def _error_signature(command: str, message: str | None) -> str | None:
    """Return a privacy-safe, aggregation-friendly failure signature."""
    if not message:
        return None
    text = message.strip()
    option = re.search(r"unrecognized (?:arguments?:\s+|option\s+)(--[A-Za-z0-9][A-Za-z0-9-]*)", text)
    if option:
        value = option.group(1)
        category = KNOWN_FAILURE_OPTION_CATEGORIES.get(value)
        identity = category or f"hmac-{_fingerprint('error-option' + chr(0) + value)}"
        return f"invalid-option:{command}:{identity}"
    if "unrecognized arguments:" in text:
        return f"unexpected-positional:{command}"
    if "search accepts one QUERY" in text:
        return "unexpected-positional:search"
    choice = re.search(r"invalid choice(?::\s+|\s+)['\"]?([A-Za-z0-9_-]{1,32})", text)
    if choice:
        identity = _fingerprint("error-choice" + chr(0) + choice.group(1))
        return f"invalid-choice:{command}:hmac-{identity}"
    if "cannot be combined" in text:
        return f"invalid-combination:{command}"
    if "does not exist" in text.lower() or "not found" in text.lower():
        return f"not-found:{command}:path"
    category = _error_category(message)
    return f"{category}:{command}" if category else None


def _recovery_hint(message: str | None) -> str | None:
    if not message:
        return None
    if "search accepts one QUERY" in message:
        return "search-path-form"
    if "did you mean:" in message:
        return "missing-path-suggestions"
    if "did you mean " in message:
        return "nearest-alternative"
    if "use --line N or --lines START:END" in message:
        return "source-preview-form"
    return None


def _compatibility_alias(command: str, invocation: list[str] | None) -> str | None:
    if not invocation:
        return None
    option_aliases = {
        ("files", "--max-results"): "files-max-results",
        ("search", "--max-results"): "search-max-results",
        ("search", "--samples-per-file"): "search-samples-per-file",
        ("inspect", "--max-results"): "inspect-max-results",
        ("git-diff", "--stat"): "git-diff-stat",
    }
    for item in invocation:
        option = item.partition("=")[0]
        alias = option_aliases.get((command, option))
        if alias:
            return alias
    if invocation[0] == "verified-changed":
        return "verified-changed-command"
    if command == "task" and len(invocation) > 1:
        task_aliases = {
            "start": "task-start", "current": "task-current", "done": "task-done",
            "drop": "task-drop", "cancel": "task-cancel",
        }
        value_options = {"--repo", "--format", "--budget"}
        index = 1
        while index < len(invocation):
            item = invocation[index]
            option = item.partition("=")[0]
            if option in value_options:
                index += 1 if "=" in item else 2
                continue
            if item.startswith("-"):
                index += 1
                continue
            return task_aliases.get(item)
        return None
    return None


def _metric_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, list):
        return len(value)
    return 0


def _records_measurement(records: Any) -> dict[str, int]:
    if not isinstance(records, list):
        return {"candidate_chars": 0, "candidate_lines": 0}
    return {
        "candidate_chars": sum(
            len(item) if isinstance(item, str) else len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            for item in records
        ),
        "candidate_lines": len(records),
    }


def _command_measurement(command: str, data: dict[str, Any]) -> dict[str, Any]:
    if command not in MEASURED_COMMANDS:
        return {}
    if isinstance(data.get("candidate_chars"), (int, float)):
        return {
            "candidate_chars": max(0, int(data["candidate_chars"])),
            "candidate_lines": max(0, _metric_int(data, "candidate_lines")),
            "candidate_measured": True,
        }
    if command == "outline":
        measured = _records_measurement(data.get("lines") or data.get("symbols"))
    elif command == "git-diff":
        patch = data.get("patch")
        if isinstance(patch, str):
            measured = {"candidate_chars": len(patch), "candidate_lines": len(patch.splitlines())}
        else:
            measured = _records_measurement(data.get("hunks") or data.get("files"))
    elif command == "inspect":
        kind = data.get("kind")
        nested = {
            "source-windows": ("read", "source"),
            "lexical": ("search", "search"),
            "file": ("outline", "outline"),
            "directory": ("outline", "outline"),
        }.get(str(kind))
        if nested and isinstance(data.get(nested[1]), dict):
            measured = _command_measurement(nested[0], data[nested[1]])
        elif kind == "python" and isinstance(data.get("python"), dict):
            python = data["python"]
            references = python.get("references") if isinstance(python.get("references"), dict) else {}
            measured = _records_measurement([*(python.get("candidates") or []), *(references.get("results") or [])])
        elif kind == "semantic" and isinstance(data.get("semantic"), dict):
            semantic = data["semantic"]
            measured = _records_measurement([*(semantic.get("candidates") or []), *(semantic.get("results") or [])])
        else:
            measured = _records_measurement([])
    else:
        measured = _records_measurement([])
    measured["candidate_measured"] = True
    return measured


def _invocation_profile(invocation: list[str] | None) -> tuple[str | None, dict[str, int]]:
    if not invocation:
        return None, {}
    normalized: list[str] = []
    controls: dict[str, int] = {}
    index = 0
    while index < len(invocation):
        item = invocation[index]
        if item == "--":
            normalized.extend(invocation[index:])
            break
        option, separator, inline = item.partition("=")
        control = EXPANSION_OPTIONS.get(option)
        if control:
            value = inline if separator else invocation[index + 1] if index + 1 < len(invocation) else ""
            try:
                controls[control] = max(0, int(value))
            except ValueError:
                pass
            index += 1 if separator else 2
            continue
        if item == "--repeat":
            index += 1
            continue
        normalized.append(item)
        index += 1
    return _fingerprint("\0".join(normalized)), controls


def event_metrics(root: Path, command: str, data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    metrics: dict[str, Any] = {}
    metrics.update(_command_measurement(command, data))
    for key in (
        "shown", "total", "files", "matches", "changed_files", "changed_packages",
        "dependent_packages", "affected_packages", "planned_steps", "executed_steps",
        "passed_steps", "failed_steps", "output_lines", "output_chars", "raw_output_lines",
        "raw_output_chars", "findings", "nodes", "edges", "total_matching_lines",
        "matching_files", "shown_files",
    ):
        value = _metric_int(data, key)
        if value:
            metrics[key] = value
    if command in {"search", "inspect"}:
        query = data.get("query") if command == "search" else data.get("target")
        if isinstance(query, str):
            metrics["query_shape"] = _query_shape(query)
            metrics["query_fingerprint"] = _fingerprint(query)
        for source, target in (("coverage", "search_coverage"), ("query_intent", "query_intent"), ("view", "search_view")):
            if isinstance(data.get(source), str):
                metrics[target] = data[source]
        if bool(data.get("semantic_candidate")):
            metrics["semantic_candidate"] = True
        candidates = data.get("symbol_candidates")
        if isinstance(candidates, list):
            metrics["prefix_candidate_count"] = len(candidates)
        if command == "inspect":
            kind = data.get("kind")
            if isinstance(kind, str):
                metrics["inspect_kind"] = kind
            semantic = data.get("semantic") if isinstance(data.get("semantic"), dict) else {}
            if isinstance(semantic.get("action"), str):
                metrics["semantic_action"] = semantic["action"]
                metrics["semantic_source"] = "inspect"
            if isinstance(semantic.get("resolution_mode"), str):
                metrics["semantic_mode"] = semantic["resolution_mode"]
            source = data.get("source") if isinstance(data.get("source"), dict) else {}
            source_ranges = read_ranges(root, source)
            if source_ranges:
                metrics["inspect_source_ranges"] = source_ranges
                metrics["inspect_source_range_count"] = len(source_ranges)
    if command in {"verify-changed", "verify-task", "verify"}:
        for source, target in (
            ("status", "verification_status"),
            ("mode", "verification_mode"),
            ("dependents", "dependent_policy"),
            ("verification_scope", "verification_scope"),
        ):
            if isinstance(data.get(source), str):
                metrics[target] = data[source]
        metrics["verification_checks_measured"] = any(
            key in data for key in ("planned_steps", "executed_steps", "passed_steps", "failed_steps")
        )
        metrics["verification_files_measured"] = "changed_files" in data
        metrics["verification_packages_measured"] = any(
            key in data for key in ("changed_packages", "dependent_packages", "affected_packages")
        )
    if command == "run" and isinstance(data.get("exit_code"), int):
        metrics["child_exit_code"] = data["exit_code"]
        if bool(data.get("timed_out")):
            metrics["child_timed_out"] = True
        argv = data.get("command")
        if isinstance(argv, list):
            metrics["command_fingerprint"] = _fingerprint("\0".join(str(x) for x in argv))
        elif isinstance(argv, str):
            metrics["command_fingerprint"] = _fingerprint(argv)
    if command == "read":
        ranges = read_ranges(root, data)
        if ranges:
            metrics["read_ranges"] = ranges
            metrics["read_range_count"] = len(ranges)
            metrics["read_lines"] = sum(int(item["lines"]) for item in ranges)
            metrics["read_windowed"] = bool(data.get("windowed"))
            metrics["online_cache_measured"] = context_cache_enabled()
        overlap = data.get("read_overlap") if isinstance(data.get("read_overlap"), dict) else {}
        if overlap:
            metrics["same_context_overlap_lines"] = _metric_int(overlap, "overlap_lines")
        items = data.get("items") if isinstance(data.get("items"), list) else []
        if any(isinstance(item, dict) and item.get("suppressed") for item in items):
            metrics["exact_repeat_suppressed"] = True
    if command == "task":
        if isinstance(data.get("action"), str):
            metrics["task_action"] = data["action"]
        if isinstance(data.get("status"), str):
            metrics["task_status"] = data["status"]
        if isinstance(data.get("completed_task_id"), str):
            metrics["completed_task_id"] = data["completed_task_id"]
    if command == "ts-nav":
        if isinstance(data.get("action"), str):
            metrics["semantic_action"] = data["action"]
            metrics["semantic_source"] = "ts-nav"
        if isinstance(data.get("resolution_mode"), str):
            metrics["semantic_mode"] = data["resolution_mode"]
        if bool(data.get("ambiguous")):
            metrics["semantic_ambiguous"] = True
    if command == "git-diff":
        metrics["diff_fingerprint"] = _fingerprint(diff_payload(data))
    if bool(data.get("repeat_suppressed")):
        metrics["exact_repeat_suppressed"] = True
    if bool(data.get("continuation")):
        metrics["continuation_provided"] = True
    return metrics

def _subject_status(command: str, data: dict[str, Any] | None) -> tuple[str | None, int | None]:
    if not isinstance(data, dict):
        return None, None
    if command == "run" and isinstance(data.get("exit_code"), int):
        code = int(data["exit_code"])
        if data.get("timed_out"):
            return "timeout", code
        return ("passed" if code == 0 else "failed"), code
    if command in {"verify-changed", "verify-task", "verify"}:
        status = data.get("status")
        code = data.get("exit_code")
        return (str(status) if isinstance(status, str) else None, int(code) if isinstance(code, int) else None)
    return None, None


def record_event(
    root: Path,
    *,
    command: str,
    duration_ms: int,
    tool_status: str = "ok",
    agentq_exit_code: int = 0,
    visible_chars: int = 0,
    prebudget_chars: int = 0,
    truncated: bool = False,
    render_budget_truncated: bool = False,
    source_cap_truncated: bool = False,
    data: dict[str, Any] | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    invocation: list[str] | None = None,
    expansion_controls: dict[str, int] | None = None,
    output_format: str = "text",
    output_view: str = "default",
    output_attribution: dict[str, int] | None = None,
    repeat_requested: bool = False,
) -> None:
    """Append privacy-minimized local telemetry. Never raises into agent work."""
    if not telemetry_enabled() or command == "stats":
        return
    if command == "task" and isinstance(data, dict) and data.get("action") == "status":
        return
    try:
        canonical = "verify-changed" if command == "verified-changed" else command
        metrics = event_metrics(root, canonical, data)
        source_chars = int(metrics.get("candidate_chars") or metrics.get("raw_output_chars") or metrics.get("output_chars") or 0)
        source_lines = int(metrics.get("candidate_lines") or metrics.get("raw_output_lines") or metrics.get("output_lines") or 0)
        operation_fingerprint, inferred_controls = _invocation_profile(invocation)
        controls = expansion_controls if expansion_controls is not None else inferred_controls
        subject_status, subject_exit_code = _subject_status(canonical, data)
        attribution = {
            key: max(0, int((output_attribution or {}).get(key, 0) or 0))
            for key in OUTPUT_ATTRIBUTION_KEYS
        }
        attributed = attribution_total(attribution) == max(0, int(visible_chars))
        event: dict[str, Any] = {
            "schema": SCHEMA,
            "id": secrets.token_hex(8),
            "time": round(time.time(), 3),
            "repo_id": repo_id(root),
            "repo_name": root.name[:80],
            "thread_id": thread_id(),
            "task_id": (str(data.get("task_id")) if canonical == "task" and isinstance(data, dict) and data.get("task_id") else current_task_id(root)),
            "command": canonical,
            "tool_status": "ok" if tool_status == "ok" else "error",
            "agentq_exit_code": int(agentq_exit_code),
            "subject_status": subject_status,
            "subject_exit_code": subject_exit_code,
            "duration_ms": max(0, int(duration_ms)),
            "visible_chars": max(0, int(visible_chars)),
            "prebudget_chars": max(0, int(prebudget_chars)),
            "source_chars": max(0, source_chars),
            "source_lines": max(0, source_lines),
            "source_measured": bool(metrics.get("candidate_measured")) or source_chars > 0,
            "truncated": bool(truncated),
            "render_budget_truncated": bool(render_budget_truncated or truncated),
            "source_cap_truncated": bool(source_cap_truncated),
            "invocation_chars": sum(len(item) for item in invocation) + max(0, len(invocation) - 1) if invocation else 0,
            "invocation_fingerprint": _fingerprint("\0".join(invocation)) if invocation else None,
            "operation_fingerprint": operation_fingerprint,
            "expansion_controls": controls,
            "output_format": output_format if output_format in {"json", "compact-json", "text"} else "unknown",
            "output_view": output_view[:40] if output_view else "default",
            "output_attribution": attribution if attributed else empty_attribution(),
            "output_attributed": attributed,
            "repeat_requested": bool(repeat_requested),
            "metrics": metrics,
        }
        compatibility_alias = _compatibility_alias(canonical, invocation)
        if compatibility_alias:
            event["compatibility_alias"] = compatibility_alias
        if error_type:
            event["error_type"] = error_type[:80]
        category = _error_category(error_message)
        if category:
            event["error_category"] = category
        signature = _error_signature(canonical, error_message)
        if signature:
            event["error_signature"] = signature
        recovery_hint = _recovery_hint(error_message)
        if recovery_hint:
            event["recovery_hint"] = recovery_hint
        _append_jsonl(hot_file(), event)
    except Exception:
        return


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    schema = int(event.get("schema", 1))
    if schema == SCHEMA:
        normalized = dict(event)
        normalized.setdefault("source_schema", schema)
        normalized.setdefault("output_format", "unknown")
        normalized.setdefault("output_view", "default")
        normalized.setdefault("output_attribution", empty_attribution())
        normalized.setdefault("output_attributed", False)
        normalized.setdefault("repeat_requested", False)
        normalized.setdefault("render_budget_truncated", bool(normalized.get("truncated")))
        normalized.setdefault("source_cap_truncated", False)
        return normalized
    if schema in {2, 3, 4, 5}:
        converted = dict(event)
        converted["schema"] = SCHEMA
        converted["source_schema"] = schema
        converted.setdefault("task_id", None)
        converted.setdefault("invocation_chars", 0)
        converted.setdefault("invocation_fingerprint", None)
        converted.setdefault("output_format", "unknown")
        converted.setdefault("output_view", "default")
        converted.setdefault("output_attribution", empty_attribution())
        converted.setdefault("output_attributed", False)
        converted.setdefault("repeat_requested", False)
        converted.setdefault("render_budget_truncated", bool(converted.get("truncated")))
        converted.setdefault("source_cap_truncated", False)
        return converted

    # v1 telemetry treated child/verification failures as agentq failures. Recover
    # the distinction when metrics contain the child/verification result.
    command = str(event.get("command", "unknown"))
    metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
    subject_status: str | None = None
    subject_exit_code: int | None = None
    tool_status = "ok"

    if command == "run" and isinstance(metrics.get("child_exit_code"), int):
        subject_exit_code = int(metrics["child_exit_code"])
        subject_status = "passed" if subject_exit_code == 0 else "failed"
    elif command == "verify-changed" and isinstance(metrics.get("verification_status"), str):
        subject_status = str(metrics["verification_status"])
        subject_exit_code = 0 if bool(event.get("success")) else 1
    elif not bool(event.get("success", True)):
        tool_status = "error"

    converted = dict(event)
    converted.update({
        "schema": SCHEMA,
        "source_schema": schema,
        "thread_id": None,
        "task_id": None,
        "tool_status": tool_status,
        "agentq_exit_code": 0 if tool_status == "ok" else 2,
        "subject_status": subject_status,
        "subject_exit_code": subject_exit_code,
        "output_format": "unknown",
        "output_view": "default",
        "output_attribution": empty_attribution(),
        "output_attributed": False,
        "repeat_requested": False,
        "render_budget_truncated": bool(event.get("truncated")),
        "source_cap_truncated": False,
    })
    return converted


_DEFAULT_STATS_FIELDS = (
    "time", "repo_id", "repo_name", "thread_id", "task_id", "command", "source_schema",
    "tool_status", "subject_status", "duration_ms", "visible_chars",
    "prebudget_chars", "source_chars", "source_measured", "truncated",
    "render_budget_truncated", "source_cap_truncated",
    "invocation_chars", "error_category", "operation_fingerprint", "expansion_controls",
    "output_format", "output_view", "output_attribution", "output_attributed", "repeat_requested",
)
_DEFAULT_STATS_METRICS = {
    "read_ranges", "same_context_overlap_lines", "online_cache_measured", "exact_repeat_suppressed",
    "semantic_action", "semantic_source", "semantic_ambiguous", "task_action",
    "task_status", "completed_task_id", "verification_status", "verification_mode",
    "verification_scope", "changed_files", "changed_packages", "affected_packages",
    "planned_steps", "executed_steps", "passed_steps", "failed_steps",
    "verification_checks_measured", "verification_files_measured",
    "verification_packages_measured",
    "continuation_provided",
}


def _compact_stats_event(event: dict[str, Any]) -> dict[str, Any]:
    compact = {key: event[key] for key in _DEFAULT_STATS_FIELDS if key in event}
    metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
    selected_metrics = {key: metrics[key] for key in _DEFAULT_STATS_METRICS if key in metrics}
    if selected_metrics:
        compact["metrics"] = selected_metrics
    return compact


def _iter_jsonl(
    path: Path,
    *,
    cutoff: float | None = None,
    repository_id: str | None = None,
    operations: set[str] | None = None,
) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(event, dict)
                    and event.get("id")
                    and int(event.get("schema", 1)) in ACCEPTED_SCHEMAS
                ):
                    normalized = _normalize_event(event)
                    if cutoff is not None and float(normalized.get("time", 0)) < cutoff:
                        continue
                    if repository_id is not None and normalized.get("repo_id") != repository_id:
                        continue
                    if operations and normalized.get("command") not in operations and normalized.get("command") != "task":
                        continue
                    yield normalized
    except OSError:
        return


def load_events(
    *,
    cutoff: float | None = None,
    repository_id: str | None = None,
    operations: set[str] | None = None,
    compact: bool = False,
    ordered: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    archive = archive_file()
    paths = [archive, hot_file(), hot_file().with_suffix(".jsonl.1")]
    by_id: dict[str, dict[str, Any]] = {}
    compact_events: list[dict[str, Any]] = []
    compact_ids: set[str] = set()
    sources: dict[str, int] = {}
    for path in paths:
        count = 0
        for event in _iter_jsonl(
            path, cutoff=cutoff, repository_id=repository_id, operations=operations,
        ):
            identity = str(event["id"])
            if compact:
                if identity not in compact_ids:
                    compact_ids.add(identity)
                    compact_events.append(_compact_stats_event(event))
            else:
                by_id[identity] = event
            count += 1
        source_name = "archive" if path == archive else "hot"
        sources[source_name] = sources.get(source_name, 0) + count
    events = compact_events if compact else list(by_id.values())
    if ordered:
        events.sort(key=lambda item: float(item.get("time", 0)))
    return events, sources


def _interval_stats(
    intervals: list[tuple[int, int]],
) -> tuple[list[tuple[int, int]], int, int, list[tuple[int, int]]]:
    merged: list[tuple[int, int]] = []
    fully_covered: list[tuple[int, int]] = []
    total_lines = sum(end - start + 1 for start, end in intervals)
    for current_start, current_end in intervals:
        covered = sum(
            max(0, min(current_end, right) - max(current_start, left) + 1)
            for left, right in merged
        )
        if covered == current_end - current_start + 1:
            fully_covered.append((current_start, current_end))
        combined: list[tuple[int, int]] = []
        for left, right in sorted([*merged, (current_start, current_end)]):
            if not combined or left > combined[-1][1] + 1:
                combined.append((left, right))
            else:
                combined[-1] = (combined[-1][0], max(combined[-1][1], right))
        merged = combined
    unique_lines = sum(end - start + 1 for start, end in merged)
    return merged, max(0, total_lines - unique_lines), len(fully_covered), fully_covered


def _merge_intervals_once(intervals: list[tuple[int, int]]) -> tuple[list[tuple[int, int]], int, int]:
    merged, overlap, fully_redundant, _ = _interval_stats(intervals)
    return merged, overlap, fully_redundant


def _cross_context_overlap(contexts: dict[str, list[tuple[int, int]]]) -> tuple[int, int]:
    changes: dict[int, list[tuple[str, bool]]] = defaultdict(list)
    for context, intervals in contexts.items():
        for start, end in intervals:
            changes[start].append((context, True))
            changes[end + 1].append((context, False))
    active: set[str] = set()
    previous: int | None = None
    cross_task = cross_thread = 0
    for position in sorted(changes):
        if previous is not None and position > previous and len(active) > 1:
            width = position - previous
            if sum(context.startswith("task:") for context in active) > 1:
                cross_task += width
            else:
                cross_thread += width
        for context, entering in changes[position]:
            if entering:
                active.add(context)
            else:
                active.discard(context)
        previous = position
    return cross_task, cross_thread


def _fallback_session_contexts(
    events: list[dict[str, Any]],
    gap_seconds: int = 30 * 60,
) -> dict[int, str]:
    by_repo: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for index, event in enumerate(events):
        if event.get("thread_id"):
            continue
        by_repo[str(event.get("repo_id", ""))].append((float(event.get("time", 0)), index))
    contexts: dict[int, str] = {}
    for repository_id, entries in by_repo.items():
        previous: float | None = None
        session = 0
        for timestamp, index in sorted(entries):
            if previous is None or timestamp - previous > gap_seconds:
                session += 1
            contexts[index] = f"session:{repository_id}:{session}"
            previous = timestamp
    return contexts


def read_efficiency(events: list[dict[str, Any]], *, detailed: bool = False) -> dict[str, Any]:
    seen_files: set[str] = set()
    context_ranges: dict[tuple[str, str, str], list[tuple[int, int]]] = defaultdict(list)
    calls = total_lines = range_count = reread_ranges = tracked_events = online_overlap_lines = online_observed_calls = 0
    fallback_contexts = _fallback_session_contexts(events)

    for event_index, event in enumerate(events):
        if event.get("command") != "read":
            continue
        calls += 1
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        online_overlap_lines += int(metrics.get("same_context_overlap_lines", 0) or 0)
        online_observed_calls += metrics.get("online_cache_measured") is True or bool(metrics.get("same_context_overlap_lines"))
        ranges = metrics.get("read_ranges") if isinstance(metrics.get("read_ranges"), list) else []
        if ranges:
            tracked_events += 1
        task = str(event.get("task_id") or "")
        thread = str(event.get("thread_id") or "")
        context = (
            f"task:{task}" if task else f"thread:{thread}" if thread
            else fallback_contexts.get(event_index, f"session:{event_index}")
        )
        for item in ranges:
            if not isinstance(item, dict):
                continue
            file_id, version, start, end = item.get("file"), item.get("version"), item.get("start"), item.get("end")
            if not isinstance(file_id, str) or not isinstance(version, str) or not isinstance(start, int) or not isinstance(end, int) or end < start:
                continue
            range_count += 1
            line_count = end - start + 1
            total_lines += line_count
            if file_id in seen_files:
                reread_ranges += 1
            seen_files.add(file_id)
            context_ranges[(context, file_id, version)].append((start, end))

    overlap_lines = fully_redundant = 0
    context_contributors: dict[str, dict[str, int]] = defaultdict(lambda: {"lines": 0, "ranges": 0})
    file_contributors: dict[str, dict[str, int]] = defaultdict(lambda: {"lines": 0, "ranges": 0})
    fully_covered_rows: list[dict[str, Any]] = []
    merged_contexts: dict[tuple[str, str], dict[str, list[tuple[int, int]]]] = defaultdict(dict)
    for (context, file_id, version), intervals in context_ranges.items():
        merged, overlap, redundant, covered_ranges = _interval_stats(intervals)
        overlap_lines += overlap
        fully_redundant += redundant
        merged_contexts[(file_id, version)][context] = merged
        if detailed and (overlap or redundant):
            context_contributors[context]["lines"] += overlap
            context_contributors[context]["ranges"] += redundant
            file_contributors[file_id]["lines"] += overlap
            file_contributors[file_id]["ranges"] += redundant
            fully_covered_rows.extend({
                "context": context,
                "file_id": file_id,
                "start": start,
                "end": end,
            } for start, end in covered_ranges)
    cross_task_overlap = cross_thread_overlap = 0
    for contexts in merged_contexts.values():
        task_overlap, thread_overlap = _cross_context_overlap(contexts)
        cross_task_overlap += task_overlap
        cross_thread_overlap += thread_overlap

    result = {
        "calls": calls,
        "tracked_calls": tracked_events,
        "ranges": range_count,
        "unique_files": len(seen_files),
        "reread_ranges": reread_ranges,
        "revisited_ranges": reread_ranges,
        "total_lines": total_lines,
        "unique_lines": max(0, total_lines - overlap_lines),
        "overlap_lines": overlap_lines,
        "overlap_percent": _percent(overlap_lines, total_lines),
        "same_context_overlap_lines": overlap_lines,
        "same_context_overlap_percent": _percent(overlap_lines, total_lines),
        "cross_task_overlap_lines": cross_task_overlap,
        "cross_thread_overlap_lines": cross_thread_overlap,
        "fully_redundant_ranges": fully_redundant,
        "online_cache_overlap_lines": online_overlap_lines,
        "online_cache_observed_calls": online_observed_calls,
        "online_cache_note": "scope=bounded_online_cache",
    }
    if detailed:
        result.update({
            "top_contexts": [
                {"context": context, **values}
                for context, values in sorted(
                    context_contributors.items(),
                    key=lambda item: (-item[1]["lines"], -item[1]["ranges"], item[0]),
                )[:10]
            ],
            "top_files": [
                {"file_id": file_id, **values}
                for file_id, values in sorted(
                    file_contributors.items(),
                    key=lambda item: (-item[1]["lines"], -item[1]["ranges"], item[0]),
                )[:10]
            ],
            "fully_covered_range_rows": sorted(
                fully_covered_rows,
                key=lambda item: (str(item["context"]), str(item["file_id"]), int(item["start"]), int(item["end"])),
            )[:10],
        })
    return result


def _resolve_read_file_labels(root: Path, reads: dict[str, Any]) -> None:
    """Resolve current repository paths for detailed display without persisting them."""
    rows = [
        *list(reads.get("top_files") or []),
        *list(reads.get("fully_covered_range_rows") or []),
    ]
    wanted = {str(row.get("file_id")) for row in rows if row.get("file_id")}
    labels: dict[str, str] = {}
    if wanted:
        repository_id = repo_id(root)
        visited = 0
        for directory, directories, filenames in os.walk(root):
            directories[:] = sorted(name for name in directories if name not in {".git", "node_modules"})
            for filename in sorted(filenames):
                path = Path(directory) / filename
                try:
                    path_text = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                file_id = stable_id(f"{repository_id}:{path_text}")
                if file_id in wanted:
                    labels[file_id] = path_text
                visited += 1
                if visited >= 5000 or len(labels) == len(wanted):
                    break
            if visited >= 5000 or len(labels) == len(wanted):
                break
    for row in rows:
        file_id = str(row.get("file_id", "unknown"))
        row["file"] = labels.get(file_id, f"file:{file_id[:8]}")

def archive_hot_events() -> dict[str, Any]:
    with _telemetry_lock():
        hot = list(_iter_jsonl(hot_file())) + list(_iter_jsonl(hot_file().with_suffix(".jsonl.1")))
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
        return {"archive": str(destination), "hot_events": len(hot), "added": added, "total_archived": len(existing)}


def _parse_since(value: str, now: float) -> float | None:
    normalized = value.strip().lower()
    if normalized in {"all", "0", "forever"}:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([mhdw])", normalized)
    if not match:
        raise AgentQError("--since must be 'all' or a duration such as 6h, 7d, or 4w")
    amount = float(match.group(1))
    unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    return now - amount * unit


def _gap_session_count(events: list[dict[str, Any]], gap_seconds: int = 30 * 60) -> int:
    return len(set(_fallback_session_contexts(events, gap_seconds).values()))


def _percent(numerator: int | float, denominator: int | float) -> float | None:
    return round(100.0 * numerator / denominator, 1) if denominator else None


def _activity_step(span: float) -> int:
    if span <= 6 * 3600:
        return 15 * 60
    if span <= 24 * 3600:
        return 60 * 60
    if span <= 7 * 86400:
        return 6 * 3600
    if span <= 30 * 86400:
        return 86400
    if span <= 90 * 86400:
        return 3 * 86400
    return max(86400, math.ceil(span / 30 / 86400) * 86400)


def _activity_buckets(events: list[dict[str, Any]], start: float, end: float) -> list[dict[str, Any]]:
    span = max(1.0, end - start)
    step = _activity_step(span)
    count = max(1, math.ceil(span / step))
    buckets = [0] * count
    for event in events:
        ts = float(event.get("time", 0))
        if ts < start or ts > end:
            continue
        index = min(count - 1, max(0, int((ts - start) // step)))
        buckets[index] += 1
    return [
        {
            "start": start + index * step,
            "end": min(end, start + (index + 1) * step),
            "calls": value,
        }
        for index, value in enumerate(buckets)
    ]


def _measurement_result(
    total_calls: int,
    instrumented_calls: int,
    source: int,
    measured_visible: int,
    all_visible: int,
    budget_removed: int,
) -> dict[str, Any]:
    return {
        "instrumented_calls": instrumented_calls,
        "total_calls": total_calls,
        "instrumented_call_percent": _percent(instrumented_calls, total_calls),
        "candidate_chars": source,
        "candidate_visible_chars": measured_visible,
        "candidate_delta_chars": measured_visible - source,
        "measured_source_chars": source,
        "measured_visible_chars": measured_visible,
        "total_visible_chars": all_visible,
        "instrumented_visible_percent": _percent(measured_visible, all_visible),
        "avoided_chars": 0,
        "overhead_chars": 0,
        "reduction_percent": None,
        "budget_removed_chars": budget_removed,
    }


def _add_rendering_overhead(
    measurement: dict[str, Any],
    attributed_calls: int,
    attributed_visible: int,
    attribution: dict[str, int],
) -> None:
    evidence = max(0, int(attribution.get("unique_evidence_chars", 0)))
    components = {
        key: max(0, int(attribution.get(key, 0)))
        for key in (
            "duplicate_evidence_chars", "framing_chars", "serialization_chars", "advice_chars",
        )
    }
    overhead = sum(components.values())
    measurement.update({
        "rendering_overhead_calls": attributed_calls,
        "rendering_overhead_visible_chars": attributed_visible,
        "rendering_evidence_chars": evidence,
        "rendering_overhead_chars": overhead,
        "rendering_overhead_percent": _percent(overhead, attributed_visible) if attributed_calls else None,
        "rendering_overhead_components": components,
        # Compatibility alias. This no longer contains the candidate-to-visible delta.
        "overhead_chars": overhead,
    })


def _command_rows(events: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    for event in events:
        command = str(event.get("command", "unknown"))
        summary = summaries.setdefault(command, {
            "calls": 0,
            "tool_ok": 0,
            "subject_passes": 0,
            "subject_failures": 0,
            "durations": [],
            "total_ms": 0,
            "visible_chars": 0,
            "truncations": 0,
            "source_cap_truncations": 0,
            "invocation_chars": 0,
            "instrumented_calls": 0,
            "source_chars": 0,
            "measured_visible": 0,
            "budget_removed": 0,
            "attributed_calls": 0,
            "attributed_visible_chars": 0,
            "output_attribution": empty_attribution(),
        })
        visible = int(event.get("visible_chars", 0))
        source = int(event.get("source_chars", 0))
        duration = int(event.get("duration_ms", 0))
        summary["calls"] += 1
        summary["tool_ok"] += event.get("tool_status") == "ok"
        summary["subject_passes"] += event.get("subject_status") in {"passed", "clean", "skipped-docs"}
        summary["subject_failures"] += event.get("subject_status") in {"failed", "timeout", "partial", "unverified"}
        summary["durations"].append(duration)
        summary["total_ms"] += duration
        summary["visible_chars"] += visible
        summary["truncations"] += bool(event.get("render_budget_truncated", event.get("truncated")))
        summary["source_cap_truncations"] += bool(event.get("source_cap_truncated"))
        summary["invocation_chars"] += int(event.get("invocation_chars", 0))
        summary["budget_removed"] += max(0, int(event.get("prebudget_chars", 0)) - visible)
        attribution = event.get("output_attribution")
        if event.get("output_attributed") and attribution_total(attribution) == visible:
            summary["attributed_calls"] += 1
            summary["attributed_visible_chars"] += visible
            for key in OUTPUT_ATTRIBUTION_KEYS:
                summary["output_attribution"][key] += max(0, int(attribution.get(key, 0) or 0))
        if event.get("source_measured") or source > 0:
            summary["instrumented_calls"] += 1
            summary["source_chars"] += source
            summary["measured_visible"] += visible

    rows: list[dict[str, Any]] = []
    for command, summary in summaries.items():
        calls = int(summary["calls"])
        tool_ok = int(summary["tool_ok"])
        measurement = _measurement_result(
            calls,
            int(summary["instrumented_calls"]),
            int(summary["source_chars"]),
            int(summary["measured_visible"]),
            int(summary["visible_chars"]),
            int(summary["budget_removed"]),
        )
        measurement.update({
            "attributed_calls": int(summary["attributed_calls"]),
            "attributed_call_percent": _percent(int(summary["attributed_calls"]), calls),
            "attributed_visible_chars": int(summary["attributed_visible_chars"]),
            "output_attribution": dict(summary["output_attribution"]),
        })
        if command in MEASURED_COMMANDS:
            _add_rendering_overhead(
                measurement,
                int(summary["attributed_calls"]),
                int(summary["attributed_visible_chars"]),
                summary["output_attribution"],
            )
        else:
            _add_rendering_overhead(measurement, 0, 0, empty_attribution())
        rows.append({
            "command": command,
            "calls": calls,
            "tool_ok": tool_ok,
            "tool_errors": calls - tool_ok,
            "subject_passes": int(summary["subject_passes"]),
            "subject_failures": int(summary["subject_failures"]),
            "median_ms": int(median(summary["durations"])),
            "total_ms": int(summary["total_ms"]),
            "visible_chars": int(summary["visible_chars"]),
            "truncations": int(summary["truncations"]),
            "source_cap_truncations": int(summary["source_cap_truncations"]),
            **measurement,
            # v1.2.0 JSON aliases; semantics now explicitly mean agentq/tool health.
            "successes": tool_ok,
            "failures": calls - tool_ok,
            "success_rate": _percent(tool_ok, calls),
            "source_chars": measurement["measured_source_chars"],
            "suppressed_chars": None,
        })
    calls = sum(int(summary["calls"]) for summary in summaries.values())
    measurement = _measurement_result(
        calls,
        sum(int(summary["instrumented_calls"]) for summary in summaries.values()),
        sum(int(summary["source_chars"]) for summary in summaries.values()),
        sum(int(summary["measured_visible"]) for summary in summaries.values()),
        sum(int(summary["visible_chars"]) for summary in summaries.values()),
        sum(int(summary["budget_removed"]) for summary in summaries.values()),
    )
    attributed_calls = sum(int(summary["attributed_calls"]) for summary in summaries.values())
    aggregate_attribution = empty_attribution()
    rendering_attribution = empty_attribution()
    rendering_calls = 0
    rendering_visible = 0
    for command, summary in summaries.items():
        for key in OUTPUT_ATTRIBUTION_KEYS:
            aggregate_attribution[key] += int(summary["output_attribution"][key])
        if command in MEASURED_COMMANDS:
            rendering_calls += int(summary["attributed_calls"])
            rendering_visible += int(summary["attributed_visible_chars"])
            for key in OUTPUT_ATTRIBUTION_KEYS:
                rendering_attribution[key] += int(summary["output_attribution"][key])
    measurement.update({
        "attributed_calls": attributed_calls,
        "attributed_call_percent": _percent(attributed_calls, calls),
        "attributed_visible_chars": sum(int(summary["attributed_visible_chars"]) for summary in summaries.values()),
        "output_attribution": aggregate_attribution,
    })
    _add_rendering_overhead(
        measurement,
        rendering_calls,
        rendering_visible,
        rendering_attribution,
    )
    aggregate = {
        "measurement": measurement,
        "tool_ok": sum(int(summary["tool_ok"]) for summary in summaries.values()),
        "duration_ms": sum(int(summary["total_ms"]) for summary in summaries.values()),
        "truncations": sum(int(summary["truncations"]) for summary in summaries.values()),
        "source_cap_truncations": sum(int(summary["source_cap_truncations"]) for summary in summaries.values()),
        "invocation_chars": sum(int(summary["invocation_chars"]) for summary in summaries.values()),
    }
    return sorted(rows, key=lambda row: (-int(row["calls"]), str(row["command"]))), aggregate


def _output_profile_rows(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    profiles: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in events:
        identity = (
            str(event.get("command", "unknown")),
            str(event.get("output_format", "unknown")),
            str(event.get("output_view", "default")),
        )
        profile = profiles.setdefault(identity, {
            "calls": 0,
            "visible_chars": 0,
            "instrumented_calls": 0,
            "source_chars": 0,
            "measured_visible": 0,
            "attributed_calls": 0,
            "attributed_visible_chars": 0,
            "output_attribution": empty_attribution(),
        })
        visible = max(0, int(event.get("visible_chars", 0)))
        source = max(0, int(event.get("source_chars", 0)))
        profile["calls"] += 1
        profile["visible_chars"] += visible
        if event.get("source_measured") or source > 0:
            profile["instrumented_calls"] += 1
            profile["source_chars"] += source
            profile["measured_visible"] += visible
        attribution = event.get("output_attribution")
        if event.get("output_attributed") and attribution_total(attribution) == visible:
            profile["attributed_calls"] += 1
            profile["attributed_visible_chars"] += visible
            for key in OUTPUT_ATTRIBUTION_KEYS:
                profile["output_attribution"][key] += max(0, int(attribution.get(key, 0) or 0))

    rows: list[dict[str, Any]] = []
    for (command, output_format, view), profile in profiles.items():
        calls = int(profile["calls"])
        measurement = _measurement_result(
            calls,
            int(profile["instrumented_calls"]),
            int(profile["source_chars"]),
            int(profile["measured_visible"]),
            int(profile["visible_chars"]),
            0,
        )
        if command in MEASURED_COMMANDS:
            _add_rendering_overhead(
                measurement,
                int(profile["attributed_calls"]),
                int(profile["attributed_visible_chars"]),
                profile["output_attribution"],
            )
        else:
            _add_rendering_overhead(measurement, 0, 0, empty_attribution())
        rows.append({
            "command": command,
            "format": output_format,
            "view": view,
            "calls": calls,
            "visible_chars": int(profile["visible_chars"]),
            "attributed_calls": int(profile["attributed_calls"]),
            "attributed_call_percent": _percent(int(profile["attributed_calls"]), calls),
            "attributed_visible_chars": int(profile["attributed_visible_chars"]),
            "output_attribution": dict(profile["output_attribution"]),
            **measurement,
        })
    return sorted(rows, key=lambda row: (
        -int(row["visible_chars"]), str(row["command"]), str(row["format"]), str(row["view"]),
    ))


def _search_format_usage(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    formats = Counter(
        str(event.get("output_format", "unknown"))
        for event in events
        if event.get("command") == "search"
    )
    structured = formats.get("json", 0) + formats.get("compact-json", 0)
    return {
        "calls": sum(formats.values()),
        "formats": dict(sorted(formats.items())),
        "compact_json_calls": formats.get("compact-json", 0),
        "legacy_json_calls": formats.get("json", 0),
        "unknown_calls": formats.get("unknown", 0),
        "compact_structured_percent": _percent(formats.get("compact-json", 0), structured),
        "legacy_structured_percent": _percent(formats.get("json", 0), structured),
    }


_COHORT_WINDOW_SECONDS = 7 * 86400
_COHORT_MIN_COVERAGE_PERCENT = 95.0
_COHORT_FORMATS = {"text", "json", "compact-json"}


def _cohort_exclusion(event: dict[str, Any]) -> str | None:
    try:
        source_schema = int(event.get("source_schema", event.get("schema", 1)))
    except (TypeError, ValueError):
        source_schema = -1
    if source_schema != SCHEMA:
        return "incompatible_schema"
    if str(event.get("output_format", "unknown")) not in _COHORT_FORMATS:
        return "unknown_format"
    if str(event.get("output_view", "unknown")) in {"", "unknown"}:
        return "unknown_view"
    if not isinstance(event.get("operation_fingerprint"), str) or not event["operation_fingerprint"]:
        return "missing_operation_fingerprint"
    return None


def _cohort_window(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    eligible: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for event in events:
        reason = _cohort_exclusion(event)
        if reason:
            excluded[reason] += 1
        else:
            eligible.append(event)
    total = len(eligible) + sum(excluded.values())
    return {
        "calls": total,
        "eligible_calls": len(eligible),
        "eligible_percent": _percent(len(eligible), total),
        "excluded": dict(sorted(excluded.items())),
        "events": eligible,
    }


def _cohort_comparison(events: Iterable[dict[str, Any]], now: float) -> dict[str, Any]:
    current_start = now - _COHORT_WINDOW_SECONDS
    previous_start = current_start - _COHORT_WINDOW_SECONDS
    values = [event for event in events if event.get("command") != "task"]
    current = _cohort_window(
        event for event in values if current_start <= float(event.get("time", 0)) <= now
    )
    previous = _cohort_window(
        event for event in values if previous_start <= float(event.get("time", 0)) < current_start
    )

    def identity(event: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(event.get("command", "unknown")),
            str(event["output_format"]),
            str(event["output_view"]),
            str(event["operation_fingerprint"]),
        )

    current_by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    previous_by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in current.pop("events"):
        current_by_identity[identity(event)].append(event)
    for event in previous.pop("events"):
        previous_by_identity[identity(event)].append(event)
    matched = set(current_by_identity) & set(previous_by_identity)

    profiles: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in matched:
        profile_key = item[:3]
        profile = profiles.setdefault(profile_key, {
            "fingerprints": 0,
            "current": [],
            "previous": [],
        })
        profile["fingerprints"] += 1
        profile["current"].extend(current_by_identity[item])
        profile["previous"].extend(previous_by_identity[item])

    def profile_window(items: list[dict[str, Any]]) -> dict[str, Any]:
        visible = [max(0, int(item.get("visible_chars", 0))) for item in items]
        failures = sum(item.get("tool_status") != "ok" for item in items)
        return {
            "calls": len(items),
            "visible_chars": sum(visible),
            "visible_chars_per_call": round(sum(visible) / len(visible), 1) if visible else None,
            "visible_chars_distribution": _distribution(visible),
            "tool_errors": failures,
            "tool_failure_percent": _percent(failures, len(items)),
        }

    rows: list[dict[str, Any]] = []
    matched_current: list[dict[str, Any]] = []
    matched_previous: list[dict[str, Any]] = []
    for (command, output_format, view), profile in profiles.items():
        current_items = profile["current"]
        previous_items = profile["previous"]
        matched_current.extend(current_items)
        matched_previous.extend(previous_items)
        current_profile = profile_window(current_items)
        previous_profile = profile_window(previous_items)
        rows.append({
            "command": command,
            "format": output_format,
            "view": view,
            "matched_fingerprints": int(profile["fingerprints"]),
            "current": current_profile,
            "previous": previous_profile,
            "visible_reduction_percent": _percent(
                float(previous_profile["visible_chars_per_call"] or 0)
                - float(current_profile["visible_chars_per_call"] or 0),
                float(previous_profile["visible_chars_per_call"] or 0),
            ),
        })

    current_matched_percent = _percent(len(matched_current), int(current["calls"]))
    previous_matched_percent = _percent(len(matched_previous), int(previous["calls"]))
    claim_eligible = bool(matched) and all(
        value is not None and value >= _COHORT_MIN_COVERAGE_PERCENT
        for value in (current_matched_percent, previous_matched_percent)
    )
    current_summary = profile_window(matched_current)
    previous_summary = profile_window(matched_previous)
    reduction = None
    if claim_eligible:
        reduction = _percent(
            float(previous_summary["visible_chars_per_call"] or 0)
            - float(current_summary["visible_chars_per_call"] or 0),
            float(previous_summary["visible_chars_per_call"] or 0),
        )
    for row in rows:
        row["claim_eligible"] = claim_eligible
        if not claim_eligible:
            row["visible_reduction_percent"] = None
    return {
        "window_seconds": _COHORT_WINDOW_SECONDS,
        "minimum_coverage_percent": _COHORT_MIN_COVERAGE_PERCENT,
        "current": {"start": current_start, "end": now, **current},
        "previous": {"start": previous_start, "end": current_start, **previous},
        "matched_fingerprints": len(matched),
        "matched_current_calls": len(matched_current),
        "matched_previous_calls": len(matched_previous),
        "matched_current_percent": current_matched_percent,
        "matched_previous_percent": previous_matched_percent,
        "claim_eligible": claim_eligible,
        "visible_reduction_percent": reduction,
        "current_matched": current_summary,
        "previous_matched": previous_summary,
        "rows": sorted(
            rows,
            key=lambda row: (-int(row["current"]["calls"]), str(row["command"]), str(row["format"]), str(row["view"])),
        )[:20],
        "note": "matched=schema+operation+format+view+private_operation_fingerprint",
    }


def _retry_behavior(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    ordered = sorted(events, key=lambda event: float(event.get("time", 0)))
    previous_by_task: dict[str, dict[str, Any]] = {}
    result = {
        "truncated_calls": 0,
        "truncation_followups": 0,
        "expanded_budget_retries": 0,
        "error_calls": 0,
        "error_followups": 0,
        "same_command_error_retries": 0,
        "recovered_error_retries": 0,
        "compatibility_alias_calls": 0,
        "compatibility_alias_successes": 0,
        "hinted_error_calls": 0,
        "hinted_error_followups": 0,
        "hinted_recovered_retries": 0,
    }
    for event in ordered:
        budget_limited = bool(event.get("render_budget_truncated", event.get("truncated")))
        if budget_limited:
            result["truncated_calls"] += 1
        if event.get("tool_status") == "error":
            result["error_calls"] += 1
            if event.get("recovery_hint"):
                result["hinted_error_calls"] += 1
        if event.get("compatibility_alias"):
            result["compatibility_alias_calls"] += 1
            if event.get("tool_status") == "ok":
                result["compatibility_alias_successes"] += 1
        task_id = event.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        previous = previous_by_task.get(task_id)
        if previous:
            if previous.get("render_budget_truncated", previous.get("truncated")):
                result["truncation_followups"] += 1
                result["expanded_budget_retries"] += int(_is_expanded_retry(previous, event))
            if previous.get("tool_status") == "error":
                result["error_followups"] += 1
                if previous.get("recovery_hint"):
                    result["hinted_error_followups"] += 1
                if previous.get("command") == event.get("command"):
                    result["same_command_error_retries"] += 1
                    if event.get("tool_status") == "ok":
                        result["recovered_error_retries"] += 1
                        if previous.get("recovery_hint"):
                            result["hinted_recovered_retries"] += 1
        previous_by_task[task_id] = event
    return result


def _percentile(values: list[int | float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    if low == high:
        return ordered[low]
    return ordered[low] * (high - pos) + ordered[high] * (pos - low)


def _distribution(values: list[int | float]) -> dict[str, float | int | None]:
    return {
        "p50": round(_percentile(values, 0.50) or 0, 1) if values else None,
        "p90": round(_percentile(values, 0.90) or 0, 1) if values else None,
        "max": round(max(values), 1) if values else None,
    }


def _context_key(event: dict[str, Any]) -> str:
    task, thread = event.get("task_id"), event.get("thread_id")
    return f"task:{task}" if task else f"thread:{thread}" if thread else f"repo:{event.get('repo_id', '')}"


def _build_context_index(
    all_events: list[dict[str, Any]],
    selected_events: list[dict[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    selected_ids = {str(event.get("id")) for event in selected_events if event.get("id")}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in all_events:
        if event.get("command") == "task":
            continue
        grouped[_context_key(event)].append(event)
    for items in grouped.values():
        items.sort(key=lambda item: float(item.get("time", 0)))
    selected = {
        key: [event for event in items if str(event.get("id")) in selected_ids]
        for key, items in grouped.items()
    }
    return {"all": dict(grouped), "selected": selected}


def _transition_counts(
    contexts: dict[str, list[dict[str, Any]]],
    *,
    detailed: bool = False,
) -> list[dict[str, Any]]:

    def label(event: dict[str, Any]) -> str:
        command = str(event.get("command", "?"))
        if not detailed:
            return command
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        if command == "ts-nav" and metrics.get("semantic_action"):
            return f"ts-nav:{metrics['semantic_action']}"
        if command == "inspect" and metrics.get("semantic_action"):
            return f"inspect:{metrics['semantic_action']}"
        if command == "inspect" and metrics.get("inspect_kind"):
            return f"inspect:{metrics['inspect_kind']}"
        if command in {"verify", "verify-task", "verify-changed"} and metrics.get("verification_scope"):
            return f"verify:{metrics['verification_scope']}"
        return command

    counts: Counter[tuple[str, str]] = Counter()
    origins: Counter[str] = Counter()
    for items in contexts.values():
        for left, right in zip(items, items[1:]):
            if float(right.get("time", 0)) - float(left.get("time", 0)) > 30 * 60:
                continue
            source, target = label(left), label(right)
            counts[(source, target)] += 1
            origins[source] += 1
    rows = []
    for (source, target), count in counts.most_common(16):
        rows.append({
            "from": source,
            "to": target,
            "transition": f"{source} → {target}",
            "calls": count,
            "from_transitions": origins[source],
            "percent": _percent(count, origins[source]),
        })
    return rows


def operation_transitions(contexts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return _transition_counts(contexts, detailed=False)


def command_chains(contexts: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return _transition_counts(contexts, detailed=True)


def _ranges_overlap_or_adjacent(left: dict[str, Any], right: dict[str, Any]) -> tuple[bool, bool]:
    if left.get("file") != right.get("file") or left.get("version") != right.get("version"):
        return False, False
    ls, le = left.get("start"), left.get("end")
    rs, re_ = right.get("start"), right.get("end")
    if not all(isinstance(v, int) for v in (ls, le, rs, re_)):
        return True, False
    adjacent = int(rs) <= int(le) + 2 and int(re_) >= int(ls) - 2
    return True, adjacent


def read_chain_behavior(
    events: list[dict[str, Any]],
    contexts: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    reads = [event for event in events if event.get("command") == "read"]
    counts = [int((event.get("metrics") or {}).get("read_range_count", 0)) for event in reads]
    consecutive = same_file = adjacent = 0
    for items in contexts.values():
        for left, right in zip(items, items[1:]):
            if left.get("command") != "read" or right.get("command") != "read":
                continue
            if float(right.get("time", 0)) - float(left.get("time", 0)) > 30 * 60:
                continue
            consecutive += 1
            left_ranges = (left.get("metrics") or {}).get("read_ranges") or []
            right_ranges = (right.get("metrics") or {}).get("read_ranges") or []
            pair_same = pair_adjacent = False
            for lrange in left_ranges:
                for rrange in right_ranges:
                    if not isinstance(lrange, dict) or not isinstance(rrange, dict):
                        continue
                    same, near = _ranges_overlap_or_adjacent(lrange, rrange)
                    pair_same = pair_same or same
                    pair_adjacent = pair_adjacent or near
            same_file += int(pair_same)
            adjacent += int(pair_adjacent)
    return {
        "single_range_calls": sum(value == 1 for value in counts),
        "multi_range_calls": sum(value > 1 for value in counts),
        "untracked_calls": sum(value == 0 for value in counts),
        "windowed_calls": sum(bool((event.get("metrics") or {}).get("read_windowed")) for event in reads),
        "consecutive_read_pairs": consecutive,
        "same_file_consecutive_pairs": same_file,
        "adjacent_same_file_pairs": adjacent,
    }


def failure_breakdown(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_command: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_command[str(event.get("command", "unknown"))].append(event)
    rows: list[dict[str, Any]] = []
    signatures: Counter[str] = Counter()
    for command, items in by_command.items():
        failures = [item for item in items if item.get("tool_status") != "ok"]
        if not failures:
            continue
        categories = Counter(str(item.get("error_category") or "unknown") for item in failures)
        local_signatures = Counter(str(item.get("error_signature") or item.get("error_category") or "unknown") for item in failures)
        signatures.update(local_signatures)
        rows.append({
            "command": command,
            "errors": len(failures),
            "calls": len(items),
            "rate": _percent(len(failures), len(items)),
            "top_cause": categories.most_common(1)[0][0],
            "top_signature": local_signatures.most_common(1)[0][0],
        })
    rows.sort(key=lambda row: (-int(row["errors"]), -float(row.get("rate") or 0), str(row["command"])))
    return {
        "rows": rows,
        "signatures": [{"signature": signature, "errors": count} for signature, count in signatures.most_common(12)],
    }


VERIFICATION_COMMANDS = {"run", "verify", "verify-changed", "verify-task"}


def _is_expanded_retry(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if not previous.get("truncated") or previous.get("operation_fingerprint") != current.get("operation_fingerprint"):
        return False
    before = previous.get("expansion_controls") if isinstance(previous.get("expansion_controls"), dict) else {}
    after = current.get("expansion_controls") if isinstance(current.get("expansion_controls"), dict) else {}
    return any(
        isinstance(value, (int, float)) and int(value) > int(before.get(key, 0) or 0)
        for key, value in after.items()
    )


def _accepted_task_outcome(task_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(events, key=lambda item: float(item.get("time", 0)))
    commands = Counter(str(event.get("command", "unknown")) for event in ordered)
    visible = sum(int(event.get("visible_chars", 0)) for event in ordered)
    source = sum(int(event.get("source_chars", 0)) for event in ordered)
    overlap = exact_suppressions = expanded_retries = correction_calls = 0
    verification_calls = 0
    verification_result: str | None = None
    correction_open = False
    previous_by_operation: dict[str, dict[str, Any]] = {}
    for event in ordered:
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        overlap += int(metrics.get("same_context_overlap_lines", 0) or 0)
        exact_suppressions += int(bool(metrics.get("exact_repeat_suppressed")))
        fingerprint = event.get("operation_fingerprint")
        if isinstance(fingerprint, str):
            previous = previous_by_operation.get(fingerprint)
            if previous and _is_expanded_retry(previous, event):
                expanded_retries += 1
            previous_by_operation[fingerprint] = event

        if event.get("command") in VERIFICATION_COMMANDS:
            verification_calls += 1
            status = event.get("subject_status")
            if isinstance(status, str):
                verification_result = status
                if status in {"failed", "timeout", "partial", "unverified"}:
                    correction_open = True
                elif status == "passed":
                    correction_open = False
        elif correction_open:
            correction_calls += 1

    return {
        "task": task_id[-8:],
        "visible_chars": visible,
        "estimated_tokens": round(visible / 4),
        "candidate_chars": source,
        "calls": len(ordered),
        "calls_by_command": dict(sorted(commands.items())),
        "same_context_overlap_lines": overlap,
        "exact_repeat_suppressions": exact_suppressions,
        "expanded_retries": expanded_retries,
        "verification_calls": verification_calls,
        "verification_result": verification_result,
        "correction_calls": correction_calls,
    }


def task_efficiency(
    root: Path,
    all_events: list[dict[str, Any]],
    selected_events: list[dict[str, Any]],
    operation_events: list[dict[str, Any]],
    *,
    all_repos: bool,
    detailed: bool = False,
    contexts: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    task_events = [event for event in selected_events if event.get("command") == "task" and event.get("task_id")]
    starts: set[str] = set()
    accepted: set[str] = set()
    abandoned: set[str] = set()
    accepted_at: dict[str, float] = {}
    for event in task_events:
        task_id = str(event["task_id"])
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        action = metrics.get("task_action")
        if action == "begin":
            starts.add(task_id)
        elif action == "accept":
            accepted.add(task_id)
            accepted_at[task_id] = float(event.get("time", 0))
        elif action == "abandon":
            abandoned.add(task_id)
        elif action == "next":
            starts.add(task_id)
            completed = metrics.get("completed_task_id")
            if isinstance(completed, str):
                accepted.add(completed)
                accepted_at[completed] = float(event.get("time", 0))

    attributed_count = sum(bool(event.get("task_id")) for event in operation_events)
    if detailed and contexts is not None:
        accepted_operations = [
            event
            for task_id in accepted
            for event in contexts.get(f"task:{task_id}", [])
        ]
    else:
        accepted_operations = (
            event for event in all_events
            if event.get("command") != "task" and event.get("task_id") in accepted
        )
    accepted_count = len(accepted)
    accepted_calls = accepted_visible = accepted_overlap = accepted_suppressions = 0
    accepted_reads = accepted_runs = 0
    accepted_commands: Counter[str] = Counter()
    per_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in accepted_operations:
        accepted_calls += 1
        accepted_visible += int(event.get("visible_chars", 0))
        command = str(event.get("command", "unknown"))
        accepted_commands[command] += 1
        accepted_reads += command == "read"
        accepted_runs += command == "run"
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        accepted_overlap += int(metrics.get("same_context_overlap_lines", 0) or 0)
        accepted_suppressions += bool(metrics.get("exact_repeat_suppressed"))
        if detailed and event.get("task_id"):
            per_task[str(event["task_id"])].append(event)

    outcomes = (
        [
            _accepted_task_outcome(task_id, per_task.get(task_id, []))
            for task_id in sorted(accepted, key=lambda value: accepted_at.get(value, 0))
        ]
        if detailed else []
    )
    task_calls = [int(item["calls"]) for item in outcomes]
    task_visible = [int(item["visible_chars"]) for item in outcomes]
    task_reads = [int(item["calls_by_command"].get("read", 0)) for item in outcomes]
    task_searches = [int(item["calls_by_command"].get("search", 0)) for item in outcomes]
    task_runs = [int(item["calls_by_command"].get("run", 0)) for item in outcomes]
    verification_results = Counter(str(item["verification_result"] or "not-run") for item in outcomes)

    active_state = None if all_repos else current_task_state(root)
    active_age = None
    if active_state and isinstance(active_state.get("started_at"), (int, float)):
        active_age = max(0, round(time.time() - float(active_state["started_at"])))
    return {
        "started": len(starts),
        "accepted": accepted_count,
        "abandoned": len(abandoned),
        "active": 1 if active_state else 0,
        "active_age_seconds": active_age,
        "attributed_calls": attributed_count,
        "unattributed_calls": max(0, len(operation_events) - attributed_count),
        "attribution_percent": _percent(attributed_count, len(operation_events)),
        "accepted_operation_calls": accepted_calls,
        "accepted_visible_chars": accepted_visible,
        "visible_chars_per_accepted_task": round(accepted_visible / accepted_count) if accepted_count else None,
        "token_proxy_per_accepted_task": round(accepted_visible / 4 / accepted_count) if accepted_count else None,
        "calls_per_accepted_task": round(accepted_calls / accepted_count, 1) if accepted_count else None,
        "reads_per_accepted_task": round(accepted_reads / accepted_count, 1) if accepted_count else None,
        "runs_per_accepted_task": round(accepted_runs / accepted_count, 1) if accepted_count else None,
        "calls_by_command": dict(sorted(accepted_commands.items())),
        "same_context_overlap_lines": accepted_overlap,
        "exact_repeat_suppressions": accepted_suppressions,
        "expanded_retries": sum(int(item["expanded_retries"]) for item in outcomes),
        "correction_calls": sum(int(item["correction_calls"]) for item in outcomes),
        "verification_results": dict(sorted(verification_results.items())),
        "accepted_tasks": outcomes[-20:],
        "calls_distribution": _distribution(task_calls),
        "visible_chars_distribution": _distribution(task_visible),
        "token_proxy_distribution": _distribution([v / 4 for v in task_visible]),
        "reads_distribution": _distribution(task_reads),
        "searches_distribution": _distribution(task_searches),
        "runs_distribution": _distribution(task_runs),
        "note": "task=independently_acceptable_outcome",
    }


def _verification_stats(events: list[dict[str, Any]], *, detailed: bool) -> dict[str, Any]:
    def metrics(event: dict[str, Any]) -> dict[str, Any]:
        value = event.get("metrics")
        return value if isinstance(value, dict) else {}

    def measured(event: dict[str, Any], flag: str, legacy_keys: tuple[str, ...]) -> bool:
        values = metrics(event)
        return values.get(flag) is True or any(key in values for key in legacy_keys)

    check_keys = ("planned_steps", "executed_steps", "passed_steps", "failed_steps")
    file_keys = ("changed_files",)
    package_keys = ("changed_packages", "dependent_packages", "affected_packages")
    statuses = Counter(str(event.get("subject_status") or "unknown") for event in events)
    checks_measured = [event for event in events if measured(event, "verification_checks_measured", check_keys)]
    files_measured = [event for event in events if measured(event, "verification_files_measured", file_keys)]
    packages_measured = [event for event in events if measured(event, "verification_packages_measured", package_keys)]
    known_statuses = {"passed", "clean", "skipped-docs", "failed", "timeout", "partial", "unverified", "planned"}
    result = {
        "runs": len(events),
        "executed_runs": sum(event.get("subject_status") != "planned" for event in events),
        "passed": statuses.get("passed", 0) + statuses.get("clean", 0) + statuses.get("skipped-docs", 0),
        "failed": statuses.get("failed", 0) + statuses.get("timeout", 0),
        "partial": statuses.get("partial", 0) + statuses.get("unverified", 0),
        "planned": statuses.get("planned", 0),
        "dry_runs": statuses.get("planned", 0),
        "unknown": sum(count for status, count in statuses.items() if status not in known_statuses),
        "checks_executed": sum(int(metrics(event).get("executed_steps", 0)) for event in checks_measured),
        "checks_failed": sum(int(metrics(event).get("failed_steps", 0)) for event in checks_measured),
        "changed_files": sum(int(metrics(event).get("changed_files", 0)) for event in files_measured),
        "changed_packages": sum(int(metrics(event).get("changed_packages", 0)) for event in packages_measured),
        "affected_packages": sum(int(metrics(event).get("affected_packages", 0)) for event in packages_measured),
        "raw_output_chars": sum(int(metrics(event).get("raw_output_chars", 0)) for event in events),
        "checks_instrumented_runs": len(checks_measured),
        "files_instrumented_runs": len(files_measured),
        "packages_instrumented_runs": len(packages_measured),
        "checks_instrumented_percent": _percent(len(checks_measured), len(events)),
        "files_instrumented_percent": _percent(len(files_measured), len(events)),
        "packages_instrumented_percent": _percent(len(packages_measured), len(events)),
        "files_distribution": _distribution([]),
        "packages_distribution": _distribution([]),
        "checks_distribution": _distribution([]),
        "duration_ms_distribution": _distribution([]),
        "modes": [],
        "scopes": [],
        "scope_rows": [],
        "recent": [],
    }
    if not detailed:
        return result

    modes: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    scope_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        event_metrics = metrics(event)
        modes[str(event_metrics.get("verification_mode", "unknown"))] += 1
        scope = str(event_metrics.get("verification_scope", "worktree"))
        scopes[scope] += 1
        scope_groups[scope].append(event)

    scope_rows: list[dict[str, Any]] = []
    for scope, items in sorted(scope_groups.items()):
        local_statuses = Counter(str(item.get("subject_status") or "unknown") for item in items)
        local_files_measured = [item for item in items if measured(item, "verification_files_measured", file_keys)]
        local_packages_measured = [item for item in items if measured(item, "verification_packages_measured", package_keys)]
        local_checks_measured = [item for item in items if measured(item, "verification_checks_measured", check_keys)]
        files = [int(metrics(item).get("changed_files", 0)) for item in local_files_measured]
        packages = [int(metrics(item).get("affected_packages", 0)) for item in local_packages_measured]
        checks = [
            int(metrics(item).get(
                "planned_steps" if item.get("subject_status") == "planned" else "executed_steps", 0
            ))
            for item in local_checks_measured
        ]
        scope_rows.append({
            "scope": scope,
            "events": len(items),
            "executed": sum(item.get("subject_status") != "planned" for item in items),
            "dry_runs": local_statuses.get("planned", 0),
            "passed": local_statuses.get("passed", 0) + local_statuses.get("clean", 0) + local_statuses.get("skipped-docs", 0),
            "failed": local_statuses.get("failed", 0) + local_statuses.get("timeout", 0),
            "partial": local_statuses.get("partial", 0) + local_statuses.get("unverified", 0),
            "checks_instrumented_runs": len(local_checks_measured),
            "files_instrumented_runs": len(local_files_measured),
            "packages_instrumented_runs": len(local_packages_measured),
            "files_distribution": _distribution(files),
            "packages_distribution": _distribution(packages),
            "checks_distribution": _distribution(checks),
            "duration_ms_distribution": _distribution([int(item.get("duration_ms", 0)) for item in items]),
        })

    recent = []
    for event in reversed(events[-12:]):
        event_metrics = metrics(event)
        status = str(event.get("subject_status") or "unknown")
        recent.append({
            "time": float(event.get("time", 0)),
            "scope": str(event_metrics.get("verification_scope", "worktree")),
            "status": "dry-run" if status == "planned" else status,
            "mode": str(event_metrics.get("verification_mode", "unknown")),
            "files": int(event_metrics.get("changed_files", 0)) if measured(event, "verification_files_measured", file_keys) else None,
            "packages": int(event_metrics.get("affected_packages", 0)) if measured(event, "verification_packages_measured", package_keys) else None,
            "checks": int(event_metrics.get("planned_steps" if status == "planned" else "executed_steps", 0)) if measured(event, "verification_checks_measured", check_keys) else None,
            "failed_checks": int(event_metrics.get("failed_steps", 0)) if measured(event, "verification_checks_measured", check_keys) else None,
            "duration_ms": int(event.get("duration_ms", 0)),
        })

    result.update({
        "files_distribution": _distribution([int(metrics(event).get("changed_files", 0)) for event in files_measured]),
        "packages_distribution": _distribution([int(metrics(event).get("affected_packages", 0)) for event in packages_measured]),
        "checks_distribution": _distribution([int(metrics(event).get("executed_steps", 0)) for event in checks_measured if event.get("subject_status") != "planned"]),
        "duration_ms_distribution": _distribution([int(event.get("duration_ms", 0)) for event in events]),
        "modes": [{"mode": mode, "runs": count} for mode, count in modes.most_common()],
        "scopes": [{"scope": scope, "runs": count} for scope, count in scopes.most_common()],
        "scope_rows": scope_rows,
        "recent": recent,
    })
    return result


def _event_facets(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    thread_ids: set[str] = set()
    repositories: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    verification_events: list[dict[str, Any]] = []
    semantic_actions: Counter[str] = Counter()
    semantic_sources: Counter[str] = Counter()
    project_runs = project_passed = project_failed = project_timed_out = project_unknown = 0
    outline_calls = inspect_calls = semantic_calls = semantic_ambiguous = 0

    for event in events:
        command = str(event.get("command", "unknown"))
        thread_id = event.get("thread_id")
        if thread_id:
            thread_ids.add(str(thread_id))
        repositories[str(event.get("repo_name", "?"))] += 1
        if event.get("tool_status") != "ok":
            errors[str(event.get("error_category") or "unknown")] += 1
        if command == "run":
            project_runs += 1
            status = event.get("subject_status")
            project_passed += status == "passed"
            project_failed += status == "failed"
            project_timed_out += status == "timeout"
            project_unknown += status not in {"passed", "failed", "timeout"}
        if command in {"verify", "verify-changed", "verify-task"}:
            verification_events.append(event)
        outline_calls += command == "outline"
        inspect_calls += command == "inspect"
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        if metrics.get("semantic_action"):
            semantic_calls += 1
            semantic_actions[str(metrics.get("semantic_action", "unknown"))] += 1
            semantic_sources[str(metrics.get("semantic_source", command))] += 1
            semantic_ambiguous += bool(metrics.get("semantic_ambiguous"))

    return {
        "thread_ids": thread_ids,
        "repositories": repositories,
        "errors": errors,
        "verification_events": verification_events,
        "project_commands": {
            "runs": project_runs,
            "passed": project_passed,
            "failed": project_failed,
            "timed_out": project_timed_out,
            "unknown": project_unknown,
        },
        "navigation": {
            "outline_calls": outline_calls,
            "inspect_calls": inspect_calls,
            "semantic_calls": semantic_calls,
            "semantic_actions": dict(semantic_actions),
            "semantic_action_rows": [
                {"action": action, "calls": count, "percent": _percent(count, semantic_calls)}
                for action, count in semantic_actions.most_common()
            ],
            "semantic_sources": dict(semantic_sources),
            "semantic_ambiguous": semantic_ambiguous,
        },
    }


def stats_data(
    root: Path,
    *,
    since: str = "7d",
    recent: int = 0,
    detailed: bool = False,
    operations: list[str] | None = None,
    all_repos: bool = False,
    archive: bool = False,
) -> dict[str, Any]:
    archive_result = archive_hot_events() if archive else None
    now = time.time()
    cutoff = _parse_since(since, now)
    repository_id = repo_id(root)
    events, sources = load_events(
        cutoff=cutoff,
        repository_id=None if all_repos else repository_id,
        operations=set(operations) if operations else None,
        compact=not detailed and recent <= 0,
        ordered=True,
    )
    selected = events
    operation_events = [event for event in selected if event.get("command") != "task"]

    command_rows, aggregate = _command_rows(operation_events)
    measurement = aggregate["measurement"]
    visible_chars = int(measurement["total_visible_chars"])
    tool_ok = int(aggregate["tool_ok"])
    tool_errors = len(operation_events) - tool_ok
    facets = _event_facets(operation_events)
    thread_ids = facets["thread_ids"]
    fallback_sessions = _gap_session_count(operation_events)

    project_commands = facets["project_commands"]
    verification_events = facets["verification_events"]
    verification = _verification_stats(verification_events, detailed=detailed)

    recent_events = []
    if recent > 0:
        for event in reversed(operation_events[-recent:]):
            metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
            recent_events.append({
                "time": float(event.get("time", 0)),
                "repo": event.get("repo_name", "?"),
                "thread_id": event.get("thread_id"),
                "command": event.get("command", "unknown"),
                "tool_status": event.get("tool_status", "error"),
                "subject_status": event.get("subject_status"),
                "subject_exit_code": event.get("subject_exit_code"),
                "duration_ms": int(event.get("duration_ms", 0)),
                "visible_chars": int(event.get("visible_chars", 0)),
                "truncated": bool(event.get("truncated")),
                "metrics": metrics,
            })

    if cutoff is not None:
        window_start = cutoff
    elif operation_events:
        window_start = min(float(event.get("time", now)) for event in operation_events)
    else:
        window_start = now
    activity = _activity_buckets(operation_events, window_start, now)
    repos = facets["repositories"]
    reads = read_efficiency(operation_events, detailed=detailed)
    if detailed and not all_repos:
        _resolve_read_file_labels(root, reads)
    context_index = _build_context_index(events, operation_events) if detailed else None
    if context_index:
        reads.update(read_chain_behavior(operation_events, context_index["selected"]))
    navigation = facets["navigation"]
    tasks = task_efficiency(
        root, events, selected, operation_events,
        all_repos=all_repos,
        detailed=detailed,
        contexts=context_index["all"] if context_index else None,
    )
    transitions = operation_transitions(context_index["selected"]) if context_index else []
    chains = command_chains(context_index["selected"]) if context_index else []
    failures_detail = failure_breakdown(operation_events) if detailed else {"rows": [], "signatures": []}
    output_profiles = _output_profile_rows(operation_events) if detailed else []
    search_format_usage = _search_format_usage(operation_events)
    retry_behavior = _retry_behavior(operation_events) if detailed else _retry_behavior([])
    error_categories = facets["errors"]
    if since == "7d":
        cohort_events, _ = load_events(
            cutoff=now - 2 * _COHORT_WINDOW_SECONDS,
            repository_id=None if all_repos else repository_id,
            operations=set(operations) if operations else None,
            compact=True,
            ordered=False,
        )
        cohort_comparison = {"available": True, **_cohort_comparison(cohort_events, now)}
    else:
        cohort_comparison = {
            "available": False,
            "reason": "seven-day comparison requires --since 7d",
        }

    return {
        "schema": SCHEMA,
        "scope": "all repositories" if all_repos else root.name,
        "since": since,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window_start": window_start,
        "window_end": now,
        "events": len(operation_events),
        "threads": len(thread_ids),
        "fallback_sessions": fallback_sessions,
        "sessions": len(thread_ids) + fallback_sessions,
        "tool_ok": tool_ok,
        "tool_errors": tool_errors,
        "tool_reliability": _percent(tool_ok, len(operation_events)),
        # v1.2.0 JSON aliases; these now refer only to agentq/tool health.
        "successes": tool_ok,
        "failures": tool_errors,
        "success_rate": _percent(tool_ok, len(operation_events)),
        "duration_ms": int(aggregate["duration_ms"]),
        "visible_chars": visible_chars,
        "visible_token_proxy": round(visible_chars / 4),
        "truncations": int(aggregate["truncations"]),
        "source_cap_truncations": int(aggregate["source_cap_truncations"]),
        "invocation_chars": int(aggregate["invocation_chars"]),
        "measurement": measurement,
        "source_chars": measurement["measured_source_chars"],
        "suppressed_chars": None,
        "reduction_percent": measurement["reduction_percent"],
        "project_commands": project_commands,
        "commands": command_rows,
        "activity": activity,
        "recent": recent_events,
        "detailed": bool(detailed),
        "verification": verification,
        "reads": reads,
        "navigation": navigation,
        "tasks": tasks,
        "transitions": transitions,
        "command_chains": chains,
        "failures_detail": failures_detail,
        "output_profiles": output_profiles,
        "search_format_usage": search_format_usage,
        "cohort_comparison": cohort_comparison,
        "retry_behavior": retry_behavior,
        "error_categories": dict(error_categories),
        "repositories": [{"name": name, "events": count} for name, count in repos.most_common(10)],
        "sources": sources,
        "archive_result": archive_result,
        "measurement_note": "tokens=visible_chars/4; rendering_overhead=attributed_visible-unique_evidence; efficiency_change=matched_7d_cohort; candidate_delta=diagnostic_only; reread_scope=context+version",
    }


def _duration(value_ms: int) -> str:
    seconds = value_ms / 1000
    if seconds < 1:
        return f"{value_ms}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{seconds % 60:02.0f}s"


def _display_dt(timestamp: float, utc: bool) -> datetime:
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt if utc else dt.astimezone()


def _time_label(timestamp: float, utc: bool, with_date: bool = False) -> str:
    dt = _display_dt(timestamp, utc)
    return dt.strftime("%m-%d %H:%M" if with_date else "%H:%M:%S")


def _window_parts(data: dict[str, Any], utc: bool) -> tuple[str, str, str]:
    start = _display_dt(float(data["window_start"]), utc)
    end = _display_dt(float(data["window_end"]), utc)
    tz = "UTC" if utc else (end.tzname() or "local")
    start_text = f"{start:%Y-%m-%d %H:%M}"
    end_text = f"{end:%H:%M}" if start.date() == end.date() else f"{end:%Y-%m-%d %H:%M}"
    return start_text, end_text, tz


def _window_label(data: dict[str, Any], utc: bool) -> str:
    start, end, tz = _window_parts(data, utc)
    return f"{start} to {end} {tz}"


def _pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}%"


def _dist_label(dist: dict[str, Any] | None) -> str:
    dist = dist or {}
    if dist.get("p50") is None:
        return "-"
    return f"{dist.get('p50')}/{dist.get('p90')}/{dist.get('max')}"


def _rendering_overhead_label(measurement: dict[str, Any]) -> str:
    if not measurement.get("rendering_overhead_calls"):
        return "-"
    overhead = int(measurement.get("rendering_overhead_chars", 0))
    return f"{human_bytes(overhead)} ({_pct(measurement.get('rendering_overhead_percent'))} of baseline)"


def _context_display(value: str) -> str:
    kind, separator, identity = value.partition(":")
    if not separator:
        return value
    if kind in {"task", "thread"}:
        return f"{kind} {identity[-8:]}"
    return kind


def _presentation_row(
    label: str,
    value: str | None = None,
    *,
    style: str = "",
    segments: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    parts = segments if segments is not None else [(str(value or ""), style)]
    return {
        "label": label,
        "value": "".join(text for text, _ in parts),
        "segments": [{"text": text, "style": part_style} for text, part_style in parts],
    }


def stats_presentation_model(data: dict[str, Any], *, utc: bool = False) -> dict[str, Any]:
    """Build the shared semantic model consumed by the plain stats renderer."""
    project = data.get("project_commands") or {}
    measured = data.get("measurement") or {}
    reads = data.get("reads") or {}
    tasks = data.get("tasks") or {}
    verification = data.get("verification") or {}
    cohort = data.get("cohort_comparison") or {}
    search_formats = data.get("search_format_usage") or {}
    total_calls = int(data.get("events", 0))

    findings: list[dict[str, Any]] = []

    def finding(priority: int, severity: str, summary: str) -> None:
        findings.append({
            "priority": priority,
            "severity": severity,
            "summary": summary,
        })

    tool_errors = int(data.get("tool_errors", 0))
    if tool_errors:
        failure_rows = sorted(
            (row for row in data.get("commands", []) if int(row.get("tool_errors", 0))),
            key=lambda row: (-float(_percent(int(row["tool_errors"]), int(row["calls"])) or 0), str(row["command"])),
        )
        rates = ", ".join(
            f"{row['command']} {_pct(_percent(int(row['tool_errors']), int(row['calls'])))}"
            for row in failure_rows[:3]
        )
        finding(
            10, "WARN",
            f"Failures: {tool_errors}/{total_calls} ({_pct(_percent(tool_errors, total_calls))})"
            + (f", top: {rates}" if rates else ""),
        )

    project_failed = int(project.get("failed", 0)) + int(project.get("timed_out", 0))
    if project_failed:
        finding(
            20, "WARN",
            f"Project command failures: {project_failed}/{int(project.get('runs', 0))}",
        )
    if int(verification.get("failed", 0)) or int(verification.get("partial", 0)):
        finding(
            30, "WARN",
            f"Verification issues: {int(verification.get('failed', 0))} failed, {int(verification.get('partial', 0))} partial",
        )

    reread_lines = int(reads.get("same_context_overlap_lines", 0))
    tracked_lines = int(reads.get("total_lines", 0))
    if reread_lines:
        finding(
            40, "REVIEW",
            f"Same-task rereads: {reread_lines:,}/{tracked_lines:,} lines ({_pct(_percent(reread_lines, tracked_lines))}), "
            f"fully covered: {int(reads.get('fully_redundant_ranges', 0)):,}/{int(reads.get('ranges', 0)):,} ranges",
        )

    if int(data.get("truncations", 0)):
        finding(
            50, "INFO",
            f"Budget-limited outputs: {int(data['truncations']):,}",
        )
    rendering_overhead = int(measured.get("rendering_overhead_chars", 0))
    if measured.get("rendering_overhead_calls") and rendering_overhead > 0:
        finding(
            60, "INFO",
            f"Rendering overhead: {_rendering_overhead_label(measured)}",
        )
    findings.sort(key=lambda item: (int(item["priority"]), str(item["summary"])))

    reliability = _pct(data.get("tool_reliability")) if total_calls else "-"
    health_rows = [
        _presentation_row(
            "Agentq CLI",
            segments=[
                (str(int(data.get("tool_ok", 0))), "green"),
                (" succeeded, ", ""),
                (str(tool_errors), "red" if tool_errors else ""),
                (" failed, ", ""),
                (reliability, ""),
            ],
        )
    ]
    project_runs = int(project.get("runs", 0))
    if project_runs:
        unknown = int(project.get("unknown", max(0, project_runs - int(project.get("passed", 0)) - project_failed)))
        timed_out = int(project.get("timed_out", 0))
        project_segments = [
            (str(int(project.get("passed", 0))), "green"),
            (" passed, ", ""),
            (str(project_failed), "red" if project_failed else ""),
            (" failed, ", ""),
            (str(unknown), ""),
            (" unknown", ""),
        ]
        if timed_out:
            project_segments.extend([(", ", ""), (str(timed_out), "red"), (" timed out", "")])
        health_rows.append(_presentation_row(
            "Project commands",
            segments=project_segments,
        ))
    else:
        health_rows.append(_presentation_row("Project commands", "none"))

    verification_runs = int(verification.get("runs", 0))
    if verification_runs:
        verification_failed = int(verification.get("failed", 0))
        verification_partial = int(verification.get("partial", 0))
        verification_planned = int(verification.get("planned", 0))
        verify_segments = [
            (str(int(verification.get("executed_runs", 0))), ""),
            (" executed, ", ""),
            (str(int(verification.get("passed", 0))), "green"),
            (" passed, ", ""),
            (str(verification_failed), "red" if verification_failed else ""),
            (" failed, ", ""),
            (str(verification_partial), "yellow" if verification_partial else ""),
            (" partial, ", ""),
            (str(verification_planned), ""),
            (" planned", ""),
        ]
        if verification.get("unknown"):
            verify_segments.extend([(", ", ""), (str(int(verification["unknown"])), ""), (" unknown", "")])
        instrumented = int(verification.get("checks_instrumented_runs", 0))
        if not instrumented:
            verify_segments.extend([("; checks ", ""), ("-", "dim")])
        else:
            verify_segments.extend([
                ("; ", ""),
                (str(int(verification.get("checks_executed", 0))), ""),
                (" checks", ""),
                (f" ({instrumented}/{verification_runs} runs)", "dim"),
            ])
        health_rows.append(_presentation_row(
            "Verification",
            segments=verify_segments,
        ))
    else:
        health_rows.append(_presentation_row("Verification", "none"))

    candidate_calls = int(measured.get("instrumented_calls", 0))
    output_rows = [
        _presentation_row(
            "Delivered",
            f"{human_bytes(int(data.get('visible_chars', 0)))}, ~{_compact_int(int(data.get('visible_token_proxy', 0)))} tokens",
        ),
        _presentation_row(
            "Candidate measured",
            f"{candidate_calls} / {int(measured.get('total_calls', total_calls))} ({_pct(measured.get('instrumented_call_percent'))})",
        ),
        _presentation_row(
            "Output attribution",
            f"{int(measured.get('attributed_calls', 0))} / {int(measured.get('total_calls', total_calls))} ({_pct(measured.get('attributed_call_percent'))})",
        ),
        _presentation_row(
            "Rendering baseline",
            f"{int(measured.get('rendering_overhead_calls', 0))} / {int(measured.get('total_calls', total_calls))} calls",
        ),
        _presentation_row(
            "Unique evidence",
            human_bytes(int(measured.get("rendering_evidence_chars", 0))) if measured.get("rendering_overhead_calls") else "-",
        ),
        _presentation_row(
            "Rendering overhead",
            _rendering_overhead_label(measured),
        ),
        _presentation_row(
            "Budget-limited",
            f"{int(data.get('truncations', 0))} calls",
        ),
        _presentation_row(
            "Source-capped",
            f"{int(data.get('source_cap_truncations', 0))} calls",
        ),
    ]
    if cohort.get("available"):
        current_cohort = cohort.get("current") or {}
        previous_cohort = cohort.get("previous") or {}
        output_rows.append(_presentation_row(
            "Comparable cohort",
            f"{int(cohort.get('matched_current_calls', 0))}/{int(current_cohort.get('calls', 0))} current calls, "
            f"{int(cohort.get('matched_previous_calls', 0))}/{int(previous_cohort.get('calls', 0))} previous calls",
        ))
        output_rows.append(_presentation_row(
            "Cohort coverage",
            f"current {_pct(cohort.get('matched_current_percent'))}, previous {_pct(cohort.get('matched_previous_percent'))}",
        ))
        if cohort.get("claim_eligible"):
            efficiency_change = float(cohort.get("visible_reduction_percent") or 0)
            output_rows.append(_presentation_row(
                "Efficiency change",
                f"{abs(efficiency_change):.1f}% {'fewer' if efficiency_change >= 0 else 'more'} visible chars/call",
            ))
        else:
            output_rows.append(_presentation_row("Efficiency change", "-"))
    if int(search_formats.get("calls", 0)):
        formats = search_formats.get("formats") or {}
        output_rows.append(_presentation_row(
            "Search formats",
            f"compact {int(formats.get('compact-json', 0))}, legacy {int(formats.get('json', 0))}, "
            f"text {int(formats.get('text', 0))}, unknown {int(formats.get('unknown', 0))}",
        ))

    read_calls = int(reads.get("calls", 0))
    if read_calls:
        activity_value = f"{read_calls} calls, {int(reads.get('ranges', 0))} ranges, {int(reads.get('unique_files', 0))} files"
    else:
        activity_value = "none"
    if tracked_lines:
        reread_value = f"{reread_lines:,}/{tracked_lines:,} lines ({_pct(_percent(reread_lines, tracked_lines))})"
    else:
        reread_value = "-"
    read_ranges = int(reads.get("ranges", 0))
    fully_value = f"{int(reads.get('fully_redundant_ranges', 0)):,}/{read_ranges:,} ranges" if read_ranges else "-"
    online_overlap = int(reads.get("online_cache_overlap_lines", 0))
    online_observed_calls = int(reads.get("online_cache_observed_calls", 0))
    online_value = (
        f"{online_overlap:,} lines, {online_observed_calls}/{int(reads.get('tracked_calls', 0))} reads sampled"
        if online_observed_calls else "-"
    )
    reading_rows = [
        _presentation_row("Activity", activity_value),
        _presentation_row("Same-task reread", reread_value),
        _presentation_row("Fully covered", fully_value),
        _presentation_row("Online cache", online_value),
    ]

    accepted = int(tasks.get("accepted", 0))
    task_bits = [f"{accepted} accepted", f"{int(tasks.get('active', 0))} active"]
    if tasks.get("abandoned"):
        task_bits.append(f"{int(tasks['abandoned'])} abandoned")
    workflow_rows = [
        _presentation_row(
            "Tasks",
            ", ".join(task_bits) if any(tasks.get(key) for key in ("started", "accepted", "active", "abandoned")) else "none",
        ),
        _presentation_row("Attribution", _pct(tasks.get("attribution_percent")) if total_calls else "-"),
    ]
    if accepted:
        workflow_rows.append(_presentation_row(
            "Accepted-task avg.",
            f"{tasks.get('calls_per_accepted_task')} calls, ~{_compact_int(int(tasks.get('token_proxy_per_accepted_task', 0)))} tokens",
        ))
        calls_dist = tasks.get("calls_distribution") or {}
        output_dist = tasks.get("visible_chars_distribution") or {}
        if calls_dist.get("p50") is not None:
            workflow_rows.append(_presentation_row(
                "Accepted-task calls",
                f"p50 {calls_dist.get('p50')}, p90 {calls_dist.get('p90')}",
            ))
        if output_dist.get("p50") is not None:
            workflow_rows.append(_presentation_row(
                "Accepted-task output",
                f"p50 {human_bytes(int(output_dist['p50']))}, p90 {human_bytes(int(output_dist['p90']))}",
            ))
    else:
        workflow_rows.append(_presentation_row("Accepted-task avg.", "-"))

    operations = [{
        "operation": str(row.get("command", "unknown")),
        "calls": int(row.get("calls", 0)),
        "failures": int(row.get("tool_errors", 0)),
        "failure_rate": _pct(_percent(int(row.get("tool_errors", 0)), int(row.get("calls", 0)))),
        "p50": _duration(int(row.get("median_ms", 0))),
        "output": human_bytes(int(row.get("visible_chars", 0))),
        "overhead": _rendering_overhead_label(row),
        "overhead_chars": int(row.get("rendering_overhead_chars", 0)),
        "overhead_available": bool(row.get("rendering_overhead_calls")),
        "truncations": int(row.get("truncations", 0)),
        "source_cap_truncations": int(row.get("source_cap_truncations", 0)),
    } for row in data.get("commands", [])]

    detail_sections: list[dict[str, Any]] = []
    profile_rows = [
        row for row in (data.get("output_profiles") or [])
        if row.get("command") in MEASURED_COMMANDS and int(row.get("attributed_calls", 0))
    ]
    if profile_rows:
        attribution_rows = []
        for row in profile_rows[:16]:
            attribution = row.get("output_attribution") or {}
            parts = [
                f"evidence {human_bytes(int(attribution.get('unique_evidence_chars', 0)))}",
                f"duplicate {human_bytes(int(attribution.get('duplicate_evidence_chars', 0)))}",
                f"framing {human_bytes(int(attribution.get('framing_chars', 0)))}",
                f"serialization {human_bytes(int(attribution.get('serialization_chars', 0)))}",
                f"advice {human_bytes(int(attribution.get('advice_chars', 0)))}",
            ]
            attribution_rows.append(_presentation_row(
                f"{row['command']}[{row['format']}/{row['view']}]",
                f"{int(row['attributed_calls'])}/{int(row['calls'])} attributed, "
                f"{human_bytes(int(row.get('attributed_visible_chars', 0)))} classified, " + ", ".join(parts),
            ))
        detail_sections.append({"name": "Output attribution", "rows": attribution_rows})

    cohort_rows = cohort.get("rows") or []
    if cohort.get("available") and (
        cohort_rows
        or int((cohort.get("current") or {}).get("calls", 0))
        or int((cohort.get("previous") or {}).get("calls", 0))
    ):
        current_excluded = (cohort.get("current") or {}).get("excluded") or {}
        previous_excluded = (cohort.get("previous") or {}).get("excluded") or {}
        comparison_rows = [
            _presentation_row(
                "Current exclusions",
                ", ".join(f"{key} {value}" for key, value in current_excluded.items()) or "none",
            ),
            _presentation_row(
                "Previous exclusions",
                ", ".join(f"{key} {value}" for key, value in previous_excluded.items()) or "none",
            ),
        ]
        comparison_rows.extend(
            _presentation_row(
                f"{row['command']}[{row['format']}/{row['view']}]",
                f"current p50/p90 {_dist_label(row['current'].get('visible_chars_distribution'))}, "
                f"previous p50/p90 {_dist_label(row['previous'].get('visible_chars_distribution'))}, "
                f"matched fingerprints {int(row.get('matched_fingerprints', 0))}",
            )
            for row in cohort_rows
        )
        detail_sections.append({"name": "Comparable seven-day cohort", "rows": comparison_rows})

    retry = data.get("retry_behavior") or {}
    if data.get("detailed"):
        detail_sections.append({"name": "Retry behavior", "rows": [
            _presentation_row(
                "Budget",
                f"{int(retry.get('truncated_calls', 0))} limited, {int(retry.get('truncation_followups', 0))} immediate follow-ups, "
                f"{int(retry.get('expanded_budget_retries', 0))} expanded retries",
            ),
            _presentation_row(
                "Agentq errors",
                f"{int(retry.get('error_calls', 0))} failed, {int(retry.get('error_followups', 0))} immediate follow-ups, "
                f"{int(retry.get('same_command_error_retries', 0))} same-command, {int(retry.get('recovered_error_retries', 0))} recovered",
            ),
            _presentation_row(
                "Recovery",
                f"{int(retry.get('hinted_error_calls', 0))} hinted errors, {int(retry.get('hinted_error_followups', 0))} follow-ups, "
                f"{int(retry.get('hinted_recovered_retries', 0))} recovered; "
                f"{int(retry.get('compatibility_alias_successes', 0))}/{int(retry.get('compatibility_alias_calls', 0))} aliases accepted",
            ),
        ]})

    failure_rows = (data.get("failures_detail") or {}).get("rows") or []
    if failure_rows:
        detail_sections.append({"name": "Agentq failures", "rows": [
            _presentation_row(
                str(row["command"]),
                segments=[
                    (str(int(row["errors"])), "red"),
                    (f"/{int(row['calls'])} (", ""),
                    (_pct(row.get("rate")), "red"),
                    (f"), cause={row['top_cause']}, signature={row['top_signature']}", ""),
                ],
            )
            for row in failure_rows[:12]
        ]})

    overhead_rows = [row for row in operations if row["overhead_available"] and row["overhead_chars"]]
    limited_rows = [row for row in operations if row["truncations"]]
    source_capped_rows = [row for row in operations if row["source_cap_truncations"]]
    if overhead_rows or limited_rows or source_capped_rows:
        rows = [
            _presentation_row(row["operation"], f"{row['overhead']} rendering overhead, {row['calls']} calls")
            for row in sorted(overhead_rows, key=lambda item: (-item["overhead_chars"], item["operation"]))
        ]
        rows.extend(
            _presentation_row(f"{row['operation']} budget limits", f"{row['truncations']} calls")
            for row in sorted(limited_rows, key=lambda item: (-item["truncations"], item["operation"]))
        )
        rows.extend(
            _presentation_row(f"{row['operation']} source caps", f"{row['source_cap_truncations']} calls")
            for row in sorted(source_capped_rows, key=lambda item: (-item["source_cap_truncations"], item["operation"]))
        )
        detail_sections.append({"name": "Output contributors", "rows": rows})

    read_detail_rows: list[dict[str, Any]] = []
    if reads.get("calls"):
        read_detail_rows.extend([
            _presentation_row(
                "Read shapes",
                f"{int(reads.get('single_range_calls', 0))} single-range, {int(reads.get('multi_range_calls', 0))} multi-range, {int(reads.get('untracked_calls', 0))} untracked",
            ),
            _presentation_row(
                "Cross-context overlap",
                f"{int(reads.get('cross_task_overlap_lines', 0)):,} cross-task lines, {int(reads.get('cross_thread_overlap_lines', 0)):,} cross-thread lines",
            ),
        ])
        read_detail_rows.extend(
            _presentation_row(
                f"Rereads: {_context_display(str(row['context']))}",
                f"{int(row['lines']):,} lines, {int(row['ranges'])} fully covered ranges",
            )
            for row in reads.get("top_contexts", [])
        )
        read_detail_rows.extend(
            _presentation_row(
                f"Rereads: {row.get('file', 'file:' + str(row.get('file_id', 'unknown'))[:8])}",
                f"{int(row['lines']):,} lines, {int(row['ranges'])} fully covered ranges",
            )
            for row in reads.get("top_files", [])
        )
        read_detail_rows.extend(
            _presentation_row(
                "Fully covered range",
                f"{row.get('file', 'file:' + str(row.get('file_id', 'unknown'))[:8])}:{int(row['start'])}-{int(row['end'])}, "
                f"{_context_display(str(row['context']))}",
            )
            for row in reads.get("fully_covered_range_rows", [])
        )
    if read_detail_rows:
        detail_sections.append({"name": "Reading contributors", "rows": read_detail_rows})

    navigation = data.get("navigation") or {}
    if navigation.get("semantic_calls"):
        navigation_rows = [_presentation_row(
            "Semantic calls",
            f"{int(navigation['semantic_calls'])} calls, {int(navigation.get('semantic_ambiguous', 0))} ambiguous, sources: "
            + ", ".join(f"{name} {count}" for name, count in sorted((navigation.get("semantic_sources") or {}).items())),
        )]
        navigation_rows.extend(
            _presentation_row(str(row["action"]), f"{int(row['calls'])} calls, {_pct(row.get('percent'))}")
            for row in navigation.get("semantic_action_rows", [])
        )
        detail_sections.append({"name": "Semantic navigation", "rows": navigation_rows})

    chains = data.get("command_chains") or []
    if chains:
        detail_sections.append({"name": "Command chains", "rows": [
            _presentation_row(str(row["transition"]), f"{int(row['calls'])} calls, {_pct(row.get('percent'))} of {row['from']}")
            for row in chains[:10]
        ]})

    accepted_tasks = tasks.get("accepted_tasks") or []
    if accepted_tasks:
        detail_sections.append({"name": "Accepted-task outcomes", "rows": [
            _presentation_row(
                f"Task {item['task']}",
                f"{int(item['calls'])} calls, {human_bytes(int(item['visible_chars']))}, ~{_compact_int(int(item['estimated_tokens']))} tokens, "
                f"reread {int(item['same_context_overlap_lines'])}, verify {item.get('verification_result') or 'not-run'}",
            )
            for item in accepted_tasks
        ]})

    scope_rows = verification.get("scope_rows") or []
    if scope_rows:
        detail_sections.append({"name": "Verification scope", "rows": [
            _presentation_row(
                str(row["scope"]),
                segments=[
                    (str(int(row["events"])), ""),
                    (" runs, ", ""),
                    (str(int(row["passed"])), "green"),
                    (" passed, ", ""),
                    (str(int(row["failed"])), "red" if row.get("failed") else ""),
                    (" failed, ", ""),
                    (str(int(row.get("partial", 0))), "yellow" if row.get("partial") else ""),
                    (" partial, ", ""),
                    (str(int(row["dry_runs"])), ""),
                    (f" planned, checks {int(row.get('checks_instrumented_runs', 0))}/{int(row['events'])}, ", ""),
                    (f"files {_dist_label(row.get('files_distribution'))}, packages {_dist_label(row.get('packages_distribution'))}", ""),
                ],
            )
            for row in scope_rows
        ]})

    recent_rows = [
        _presentation_row(
            f"{_time_label(event['time'], utc)}: {event['command']}",
            f"Agentq {event.get('tool_status', 'error')}, result {event.get('subject_status') or '-'}, "
            f"{_duration(event['duration_ms'])}, {human_bytes(event['visible_chars'])}",
        )
        for event in data.get("recent", [])
    ]
    if recent_rows:
        detail_sections.append({"name": "Recent", "rows": recent_rows})

    return {
        "title": f"agentq stats: {data.get('scope', 'current repository')} [{data.get('since', 'selected window')}]",
        "window": _window_label(data, utc),
        "empty": not total_calls,
        "findings": findings,
        "sections": [
            {"name": "Health", "rows": health_rows},
            {"name": "Output", "rows": output_rows},
            {"name": "Reading", "rows": reading_rows},
            {"name": "Workflow", "rows": workflow_rows},
        ],
        "operations": operations,
        "detail_sections": detail_sections if data.get("detailed") else [],
    }


_ANSI_RESET = "\033[0m"
_ANSI_ORANGE = "\033[38;5;208m"
_ANSI_SECTION = "\033[1;38;5;208m"
_ANSI_STATUS = {"red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m"}


def _ansi(text: str, code: str, enabled: bool) -> str:
    return f"{code}{text}{_ANSI_RESET}" if enabled else text


def _render_segments(row: dict[str, Any], *, ansi: bool) -> str:
    if not ansi:
        return str(row["value"])
    parts = []
    for segment in row.get("segments", []):
        text = str(segment.get("text", ""))
        style = str(segment.get("style", ""))
        colour = next((code for name, code in _ANSI_STATUS.items() if name in style), "")
        parts.append(_ansi(text, colour, bool(colour)))
    return "".join(parts) if parts else str(row["value"])


def _render_stats_text(data: dict[str, Any], *, utc: bool, ansi: bool) -> str:
    model = stats_presentation_model(data, utc=utc)
    heading = lambda value: _ansi(str(value), _ANSI_ORANGE, ansi)
    section_heading = lambda value: _ansi(str(value), _ANSI_SECTION, ansi)
    lines = [heading(model["title"]), model["window"]]
    if model["empty"]:
        lines.extend(["", "No telemetry in this scope."])
        return "\n".join(lines)

    if data.get("detailed"):
        lines.extend(["", section_heading("Attention")])
        if not model["findings"]:
            lines.append("  none")
        for item in model["findings"]:
            lines.append(f"  {item['severity']:<7} {item['summary']}")

    for section in model["sections"]:
        lines.extend(["", section_heading(section["name"])])
        for row in section["rows"]:
            lines.append(f"  {row['label']:<22} {_render_segments(row, ansi=ansi)}")

    lines.extend(["", section_heading("Operations")])
    operations_header = f"{'Operation':<20} {'Calls':>6} {'Failures':>12} {'Failure rate':>12} {'p50':>8} {'Output':>10}  Overhead"
    lines.append("  " + heading(operations_header))
    for row in model["operations"][:18]:
        failures_raw = str(row["failures"])
        failure_rate_raw = str(row["failure_rate"])
        failures = failures_raw.rjust(12)
        failure_rate = failure_rate_raw.rjust(12)
        if ansi and row["failures"]:
            failures = " " * (12 - len(failures_raw)) + _ansi(failures_raw, _ANSI_STATUS["red"], True)
            failure_rate = " " * (12 - len(failure_rate_raw)) + _ansi(failure_rate_raw, _ANSI_STATUS["red"], True)
        lines.append(
            f"  {row['operation']:<20} {row['calls']:>6} {failures} {failure_rate} "
            f"{row['p50']:>8} {row['output']:>10}  {row['overhead']}"
        )

    for section in model["detail_sections"]:
        lines.extend(["", section_heading(section["name"])])
        for row in section["rows"]:
            lines.append(f"  {row['label']:<28} {_render_segments(row, ansi=ansi)}")

    if data.get("archive_result"):
        archived = data["archive_result"]
        lines.extend(["", f"archive: added {archived['added']} events; total persistent {archived['total_archived']}"])
    return "\n".join(lines)


def render_stats_plain(data: dict[str, Any], *, utc: bool = False) -> str:
    return _render_stats_text(data, utc=utc, ansi=False)


def render_stats_ansi(data: dict[str, Any], *, utc: bool = False) -> str:
    return _render_stats_text(data, utc=utc, ansi=True)


# Backwards-compatible public name used by older callers/tests.
def render_stats(data: dict[str, Any], *, color: str = "auto", utc: bool = False) -> str:
    return render_stats_ansi(data, utc=utc) if color == "always" else render_stats_plain(data, utc=utc)


def _compact_int(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k".rstrip("0").rstrip(".")
    return f"{value / 1_000_000:.1f}m".rstrip("0").rstrip(".")


def print_stats(data: dict[str, Any], *, color: str = "auto", plain: bool = False, utc: bool = False, budget: int = 12000) -> None:
    use_color = not plain and (color == "always" or (color == "auto" and sys.stdout.isatty()))
    plain_text = render_stats_plain(data, utc=utc)
    rendered = render_stats_ansi(data, utc=utc) if use_color else plain_text
    text, truncated = bound_output(rendered, budget)
    if use_color and truncated:
        text, _ = bound_output(plain_text, budget)
    print(text, flush=True)


def watch_stats(
    root: Path,
    *,
    interval: float,
    since: str,
    recent: int,
    detailed: bool,
    operations: list[str],
    all_repos: bool,
    color: str,
    plain: bool = False,
    utc: bool = False,
    budget: int = 12000,
) -> None:
    try:
        while True:
            data = stats_data(root, since=since, recent=recent, detailed=detailed, operations=operations, all_repos=all_repos)
            clear = "\033[2J\033[H" if sys.stdout.isatty() else ""
            if clear:
                print(clear, end="")
            render_budget = max(1, budget - len(clear)) if budget > 0 else budget
            print_stats(data, color=color, plain=plain, utc=utc, budget=render_budget)
            time.sleep(interval)
    except KeyboardInterrupt:
        return
