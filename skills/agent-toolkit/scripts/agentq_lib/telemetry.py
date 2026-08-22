from __future__ import annotations

import hashlib
import hmac
import json
import locale
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .common import AgentQError, bound_output, human_bytes
from .tasking import current_task_id, current_task_state

SCHEMA = 4
ACCEPTED_SCHEMAS = {1, 2, 3, 4}
MAX_EVENT_BYTES = 4096
MAX_HOT_BYTES = 10 * 1024 * 1024
SPARKS = "▁▂▃▄▅▆▇█"
ARCHIVE_SERVICE = "agentq-archive.service"
ARCHIVE_TIMER = "agentq-archive.timer"
DEFAULT_ARCHIVE_INTERVAL = "5min"

NERD_OK = "󰄬"
NERD_WARN = "󰀪"
NERD_ERROR = "󰅖"
NERD_ACTIVE = "󰐊"
NERD_INFO = "󰋼"


def telemetry_enabled() -> bool:
    return os.environ.get("AGENTQ_TELEMETRY", "1").strip().lower() not in {"0", "false", "no", "off"}


def _secure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def hot_dir() -> Path:
    override = os.environ.get("AGENTQ_TELEMETRY_HOT")
    if override:
        return _secure_dir(Path(override).expanduser())
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    return _secure_dir(Path(tempfile.gettempdir()) / f"agentq-{uid}" / "_telemetry")


def hot_file() -> Path:
    return hot_dir() / "events.jsonl"


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

    unit_dir = _secure_dir(systemd_user_dir())
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
    repo_id = _repo_id(root) if root else None
    hot = _file_storage(hot_file(), repo_id)
    rotated = _file_storage(hot_file().with_suffix(".jsonl.1"), repo_id)
    persistent = _file_storage(archive_file(), repo_id)
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
    _secure_dir(path.parent)
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


def reset_telemetry(root: Path, *, all_repos: bool = False, hot_only: bool = False, force: bool = False) -> dict[str, Any]:
    active = _active_task_count() if all_repos else (1 if current_task_state(root) else 0)
    if active and not force:
        scope = "one or more repositories" if all_repos else "this repository/worktree"
        raise AgentQError(f"an agentq task is active for {scope}; accept/abandon it first or pass --force")

    repo_id = _repo_id(root)
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
            persistent_removed = _rewrite_excluding_repo(path, repo_id)

    for path in (hot_file().with_suffix(".jsonl.1"), hot_file()):
        if all_repos:
            hot_removed += sum(1 for _ in _iter_jsonl(path))
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise AgentQError(f"unable to reset hot telemetry {path}: {exc}") from exc
        else:
            hot_removed += _rewrite_excluding_repo(path, repo_id)

    return {
        "action": "reset",
        "scope": "all-repositories" if all_repos else root.name,
        "hot_only": bool(hot_only),
        "hot_removed": hot_removed,
        "persistent_removed": persistent_removed,
        "active_tasks_preserved": active,
    }


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
        suffix = f" · {current} current-repo" if current is not None else ""
        updated = age_label(item.get("modified"))
        updated_suffix = f" · updated {updated}" if updated else ""
        lines.append(f"{label:<10} {human_bytes(item['bytes']):>9} · {item['events']} events{suffix}{updated_suffix} · {short_path(item['path'])}")
    timer = data["timer"]
    if timer["installed"]:
        timer_bits = [timer["active"], timer["enabled"]]
        if timer.get("interval"):
            timer_bits.append(str(timer["interval"]))
        lines.append(f"timer      {' · '.join(timer_bits)}")
    else:
        lines.append("timer      not installed")
        lines.append("install    agentq stats --install-persistence")
    return "\n".join(lines)


def render_persistence(data: dict[str, Any]) -> str:
    if data.get("action") == "install-persistence":
        return f"archive timer installed · {data['interval']} · {data['active']} · {data['enabled']}"
    return f"archive timer removed · {len(data.get('removed', []))} unit file(s)"


def render_reset(data: dict[str, Any]) -> str:
    total = int(data.get("hot_removed", 0)) + int(data.get("persistent_removed", 0))
    scope = data.get("scope", "current repository")
    detail = "hot only" if data.get("hot_only") else "hot + persistent"
    return f"telemetry reset · {scope} · {total} events removed · {detail}"


def render_archive(data: dict[str, Any]) -> str:
    return f"telemetry archived · +{data.get('added', 0)} · {data.get('total_archived', 0)} persistent"


def _repo_id(root: Path) -> str:
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]


def _codex_thread_id() -> str | None:
    raw = os.environ.get("CODEX_THREAD_ID")
    if not raw:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


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


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    _secure_dir(path.parent)
    _rotate_hot(path) if path == hot_file() else None
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_EVENT_BYTES:
        event = {key: value for key, value in event.items() if key not in {"metrics", "repo_name"}}
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, stat.S_IRUSR | stat.S_IWUSR)
    try:
        os.write(fd, (payload + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def _fingerprint_key() -> bytes:
    path = hot_dir() / "fingerprint.key"
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
    if "invalid arguments" in text or "requires" in text or "provide " in text or "cannot be combined" in text:
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


def _metric_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, list):
        return len(value)
    return 0


def _read_path_id(root: Path, path_text: str) -> str:
    return hashlib.sha256(f"{_repo_id(root)}:{path_text}".encode("utf-8")).hexdigest()[:16]


def _read_version_id(path: Path) -> str:
    try:
        stat_result = path.stat()
        payload = f"{stat_result.st_size}:{stat_result.st_mtime_ns}"
    except OSError:
        payload = "unknown"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _read_ranges(root: Path, data: dict[str, Any] | None, *, limit: int = 24) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return []
    ranges: list[dict[str, Any]] = []
    for item_index, item in enumerate(data["items"]):
        if not isinstance(item, dict) or item.get("refused"):
            continue
        path_text = item.get("path")
        start = item.get("start")
        end = item.get("end")
        if not isinstance(path_text, str) or not isinstance(start, int) or not isinstance(end, int):
            continue
        path = Path(path_text)
        actual = path if path.is_absolute() else root / path
        ranges.append({
            "item_index": item_index,
            "file": _read_path_id(root, path_text),
            "version": str(item.get("version") or _read_version_id(actual)),
            "start": max(1, start),
            "end": max(start, end),
            "lines": max(0, end - start + 1),
        })
        if len(ranges) >= limit:
            break
    return ranges


def event_metrics(root: Path, command: str, data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    metrics: dict[str, Any] = {}
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
    if command in {"verify-changed", "verify-task"}:
        for source, target in (
            ("status", "verification_status"),
            ("mode", "verification_mode"),
            ("dependents", "dependent_policy"),
        ):
            if isinstance(data.get(source), str):
                metrics[target] = data[source]
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
        ranges = _read_ranges(root, data)
        if ranges:
            metrics["read_ranges"] = ranges
            metrics["read_range_count"] = len(ranges)
            metrics["read_lines"] = sum(int(item["lines"]) for item in ranges)
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
        if isinstance(data.get("resolution_mode"), str):
            metrics["semantic_mode"] = data["resolution_mode"]
        if bool(data.get("ambiguous")):
            metrics["semantic_ambiguous"] = True
    return metrics

def _subject_status(command: str, data: dict[str, Any] | None) -> tuple[str | None, int | None]:
    if not isinstance(data, dict):
        return None, None
    if command == "run" and isinstance(data.get("exit_code"), int):
        code = int(data["exit_code"])
        if data.get("timed_out"):
            return "timeout", code
        return ("passed" if code == 0 else "failed"), code
    if command in {"verify-changed", "verify-task"}:
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
    data: dict[str, Any] | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    invocation: list[str] | None = None,
) -> None:
    """Append privacy-minimized local telemetry. Never raises into agent work."""
    if not telemetry_enabled() or command == "stats":
        return
    if command == "task" and isinstance(data, dict) and data.get("action") == "status":
        return
    try:
        canonical = "verify-changed" if command == "verified-changed" else command
        metrics = event_metrics(root, canonical, data)
        source_chars = int(metrics.get("raw_output_chars") or metrics.get("output_chars") or 0)
        source_lines = int(metrics.get("raw_output_lines") or metrics.get("output_lines") or 0)
        subject_status, subject_exit_code = _subject_status(canonical, data)
        event: dict[str, Any] = {
            "schema": SCHEMA,
            "id": secrets.token_hex(8),
            "time": round(time.time(), 3),
            "repo_id": _repo_id(root),
            "repo_name": root.name[:80],
            "thread_id": _codex_thread_id(),
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
            "truncated": bool(truncated),
            "invocation_chars": sum(len(item) for item in invocation) + max(0, len(invocation) - 1) if invocation else 0,
            "invocation_fingerprint": _fingerprint("\0".join(invocation)) if invocation else None,
            "metrics": metrics,
        }
        if error_type:
            event["error_type"] = error_type[:80]
        category = _error_category(error_message)
        if category:
            event["error_category"] = category
        _append_jsonl(hot_file(), event)
    except Exception:
        return


def _normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    schema = int(event.get("schema", 1))
    if schema == SCHEMA:
        return event
    if schema in {2, 3}:
        converted = dict(event)
        converted["schema"] = SCHEMA
        converted.setdefault("task_id", None)
        converted.setdefault("invocation_chars", 0)
        converted.setdefault("invocation_fingerprint", None)
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
        "thread_id": None,
        "task_id": None,
        "tool_status": tool_status,
        "agentq_exit_code": 0 if tool_status == "ok" else 2,
        "subject_status": subject_status,
        "subject_exit_code": subject_exit_code,
    })
    return converted


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
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
                    yield _normalize_event(event)
    except OSError:
        return


def load_events() -> tuple[list[dict[str, Any]], dict[str, int]]:
    paths = [archive_file(), hot_file(), hot_file().with_suffix(".jsonl.1")]
    by_id: dict[str, dict[str, Any]] = {}
    sources: dict[str, int] = {}
    for path in paths:
        count = 0
        for event in _iter_jsonl(path):
            by_id[str(event["id"])] = event
            count += 1
        source_name = "archive" if path == archive_file() else "hot"
        sources[source_name] = sources.get(source_name, 0) + count
    return sorted(by_id.values(), key=lambda item: float(item.get("time", 0))), sources


def _range_overlap(intervals: list[tuple[int, int]], start: int, end: int) -> int:
    overlap = 0
    for existing_start, existing_end in intervals:
        left = max(start, existing_start)
        right = min(end, existing_end)
        if left <= right:
            overlap += right - left + 1
    return min(max(0, end - start + 1), overlap)


def _merge_interval(intervals: list[tuple[int, int]], start: int, end: int) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for current_start, current_end in sorted([*intervals, (start, end)]):
        if not merged or current_start > merged[-1][1] + 1:
            merged.append((current_start, current_end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], current_end))
    return merged


def read_efficiency(events: list[dict[str, Any]]) -> dict[str, Any]:
    reads = [event for event in events if event.get("command") == "read"]
    seen_files: set[str] = set()
    # Coverage is partitioned by active-context identity. Cross-task/thread reuse
    # is informative, but only same-context overlap is treated as redundancy.
    context_coverage: dict[tuple[str, str, str], list[tuple[int, int]]] = defaultdict(list)
    global_coverage: dict[tuple[str, str], list[tuple[int, int, str]]] = defaultdict(list)
    total_lines = overlap_lines = range_count = reread_ranges = fully_redundant = tracked_events = 0
    cross_task_overlap = cross_thread_overlap = 0

    for event in reads:
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        ranges = metrics.get("read_ranges") if isinstance(metrics.get("read_ranges"), list) else []
        if ranges:
            tracked_events += 1
        task = str(event.get("task_id") or "")
        thread = str(event.get("thread_id") or "")
        context = f"task:{task}" if task else f"thread:{thread}" if thread else "unattributed"
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
            key = (file_id, version)
            ckey = (context, file_id, version)
            prior = context_coverage[ckey]
            overlap = _range_overlap(prior, start, end)
            overlap_lines += overlap
            if overlap >= line_count and line_count:
                fully_redundant += 1
            context_coverage[ckey] = _merge_interval(prior, start, end)

            # Attribute overlap with other contexts separately, without counting it
            # as avoidable within-task transcript duplication.
            for ps, pe, pcontext in global_coverage[key]:
                if pcontext == context:
                    continue
                amount = _range_overlap([(ps, pe)], start, end)
                if not amount:
                    continue
                if task and pcontext.startswith("task:"):
                    cross_task_overlap += amount
                else:
                    cross_thread_overlap += amount
            global_coverage[key].append((start, end, context))

    return {
        "calls": len(reads),
        "tracked_calls": tracked_events,
        "ranges": range_count,
        "unique_files": len(seen_files),
        "reread_ranges": reread_ranges,
        "total_lines": total_lines,
        "unique_lines": max(0, total_lines - overlap_lines),
        "overlap_lines": overlap_lines,
        "overlap_percent": _percent(overlap_lines, total_lines),
        "same_context_overlap_lines": overlap_lines,
        "same_context_overlap_percent": _percent(overlap_lines, total_lines),
        "cross_task_overlap_lines": cross_task_overlap,
        "cross_thread_overlap_lines": cross_thread_overlap,
        "fully_redundant_ranges": fully_redundant,
    }

def read_overlap_advice(root: Path, data: dict[str, Any]) -> dict[str, Any] | None:
    current_ranges = _read_ranges(root, data)
    if not current_ranges:
        return None
    events, _ = load_events()
    repo = _repo_id(root)
    task_id = current_task_id(root)
    thread_id = _codex_thread_id()
    now = time.time()

    prior_events: list[dict[str, Any]] = []
    for event in events:
        if event.get("repo_id") != repo or event.get("command") != "read":
            continue
        if task_id:
            if event.get("task_id") != task_id:
                continue
        elif thread_id:
            if event.get("thread_id") != thread_id:
                continue
        elif now - float(event.get("time", 0)) > 30 * 60:
            continue
        prior_events.append(event)

    prior_coverage: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
    for event in prior_events:
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        for item in metrics.get("read_ranges") or []:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("file", "")), str(item.get("version", "")))
            start, end = item.get("start"), item.get("end")
            if key[0] and key[1] and isinstance(start, int) and isinstance(end, int):
                prior_coverage[key] = _merge_interval(prior_coverage[key], start, end)

    overlapping = 0
    overlap_lines = 0
    fully_covered = 0
    fully_covered_indices: list[int] = []
    total_lines = 0
    for item in current_ranges:
        key = (str(item["file"]), str(item["version"]))
        start, end = int(item["start"]), int(item["end"])
        lines = end - start + 1
        total_lines += lines
        overlap = _range_overlap(prior_coverage.get(key, []), start, end)
        if overlap:
            overlapping += 1
            overlap_lines += overlap
        if overlap >= lines and lines:
            fully_covered += 1
            if isinstance(item.get("item_index"), int):
                fully_covered_indices.append(int(item["item_index"]))

    if not overlapping:
        return None
    return {
        "overlapping_ranges": overlapping,
        "overlap_lines": overlap_lines,
        "overlap_percent": _percent(overlap_lines, total_lines),
        "fully_covered_ranges": fully_covered,
        "fully_covered_indices": fully_covered_indices,
        "scope": "task" if task_id else "thread" if thread_id else "recent-session",
    }


def archive_hot_events() -> dict[str, Any]:
    hot = list(_iter_jsonl(hot_file())) + list(_iter_jsonl(hot_file().with_suffix(".jsonl.1")))
    destination = archive_file()
    existing = {str(event["id"]) for event in _iter_jsonl(destination)}
    added = 0
    try:
        for event in hot:
            if str(event["id"]) in existing:
                continue
            _append_jsonl(destination, event)
            existing.add(str(event["id"]))
            added += 1
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
    sessions = 0
    previous: dict[str, float] = {}
    for event in events:
        if event.get("thread_id"):
            continue
        repo = str(event.get("repo_id", ""))
        timestamp = float(event.get("time", 0))
        if repo not in previous or timestamp - previous[repo] > gap_seconds:
            sessions += 1
        previous[repo] = timestamp
    return sessions


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


def _measurement(items: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [item for item in items if int(item.get("source_chars", 0)) > 0]
    source = sum(int(item.get("source_chars", 0)) for item in measured)
    visible = sum(int(item.get("visible_chars", 0)) for item in measured)
    all_visible = sum(int(item.get("visible_chars", 0)) for item in items)
    avoided = max(0, source - visible)
    overhead = max(0, visible - source)
    reduction = _percent(source - visible, source) if source else None
    budget_removed = sum(max(0, int(item.get("prebudget_chars", 0)) - int(item.get("visible_chars", 0))) for item in items)
    return {
        "instrumented_calls": len(measured),
        "total_calls": len(items),
        "instrumented_call_percent": _percent(len(measured), len(items)),
        "measured_source_chars": source,
        "measured_visible_chars": visible,
        "total_visible_chars": all_visible,
        "instrumented_visible_percent": _percent(visible, all_visible),
        "avoided_chars": avoided,
        "overhead_chars": overhead,
        "reduction_percent": reduction,
        "budget_removed_chars": budget_removed,
    }


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


def operation_transitions(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        task, thread = event.get("task_id"), event.get("thread_id")
        key = f"task:{task}" if task else f"thread:{thread}" if thread else f"repo:{event.get('repo_id','')}"
        grouped[key].append(event)
    counts: Counter[str] = Counter()
    for items in grouped.values():
        items.sort(key=lambda item: float(item.get("time", 0)))
        for left, right in zip(items, items[1:]):
            if float(right.get("time", 0)) - float(left.get("time", 0)) > 30 * 60:
                continue
            counts[f"{left.get('command','?')} → {right.get('command','?')}"] += 1
    return [{"transition": name, "calls": count} for name, count in counts.most_common(12)]

def task_efficiency(
    root: Path,
    all_events: list[dict[str, Any]],
    selected_events: list[dict[str, Any]],
    operation_events: list[dict[str, Any]],
    *,
    all_repos: bool,
) -> dict[str, Any]:
    task_events = [event for event in selected_events if event.get("command") == "task" and event.get("task_id")]
    starts: set[str] = set()
    accepted: set[str] = set()
    abandoned: set[str] = set()
    for event in task_events:
        task_id = str(event["task_id"])
        metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
        action = metrics.get("task_action")
        if action == "begin":
            starts.add(task_id)
        elif action == "accept":
            accepted.add(task_id)
        elif action == "abandon":
            abandoned.add(task_id)
        elif action == "next":
            starts.add(task_id)
            completed = metrics.get("completed_task_id")
            if isinstance(completed, str):
                accepted.add(completed)

    attributed = [event for event in operation_events if event.get("task_id")]
    accepted_operations = [
        event for event in all_events
        if event.get("command") != "task" and event.get("task_id") in accepted
    ]
    accepted_visible = sum(int(event.get("visible_chars", 0)) for event in accepted_operations)
    accepted_count = len(accepted)
    per_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in accepted_operations:
        if event.get("task_id"):
            per_task[str(event["task_id"])].append(event)
    task_calls = [len(items) for items in per_task.values()]
    task_visible = [sum(int(e.get("visible_chars", 0)) for e in items) for items in per_task.values()]
    task_reads = [sum(e.get("command") == "read" for e in items) for items in per_task.values()]
    task_searches = [sum(e.get("command") == "search" for e in items) for items in per_task.values()]
    task_runs = [sum(e.get("command") == "run" for e in items) for items in per_task.values()]

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
        "attributed_calls": len(attributed),
        "unattributed_calls": max(0, len(operation_events) - len(attributed)),
        "attribution_percent": _percent(len(attributed), len(operation_events)),
        "accepted_operation_calls": len(accepted_operations),
        "accepted_visible_chars": accepted_visible,
        "visible_chars_per_accepted_task": round(accepted_visible / accepted_count) if accepted_count else None,
        "token_proxy_per_accepted_task": round(accepted_visible / 4 / accepted_count) if accepted_count else None,
        "calls_per_accepted_task": round(len(accepted_operations) / accepted_count, 1) if accepted_count else None,
        "reads_per_accepted_task": round(sum(event.get("command") == "read" for event in accepted_operations) / accepted_count, 1) if accepted_count else None,
        "runs_per_accepted_task": round(sum(event.get("command") == "run" for event in accepted_operations) / accepted_count, 1) if accepted_count else None,
        "calls_distribution": _distribution(task_calls),
        "visible_chars_distribution": _distribution(task_visible),
        "token_proxy_distribution": _distribution([v / 4 for v in task_visible]),
        "reads_distribution": _distribution(task_reads),
        "searches_distribution": _distribution(task_searches),
        "runs_distribution": _distribution(task_runs),
        "note": (
            "A task is one independently acceptable outcome. One Codex thread may contain several sequential tasks; "
            "small corrections, debugging, and verification retries remain in the current task."
        ),
    }


def stats_data(
    root: Path,
    *,
    since: str = "7d",
    recent: int = 0,
    operations: list[str] | None = None,
    all_repos: bool = False,
    archive: bool = False,
) -> dict[str, Any]:
    archive_result = archive_hot_events() if archive else None
    events, sources = load_events()
    now = time.time()
    cutoff = _parse_since(since, now)
    repo_id = _repo_id(root)
    selected = [
        event for event in events
        if (cutoff is None or float(event.get("time", 0)) >= cutoff)
        and (all_repos or event.get("repo_id") == repo_id)
        and (not operations or event.get("command") in operations or event.get("command") == "task")
    ]
    operation_events = [event for event in selected if event.get("command") != "task"]

    commands: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in operation_events:
        commands[str(event.get("command", "unknown"))].append(event)

    command_rows: list[dict[str, Any]] = []
    for command, items in sorted(commands.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        measurement = _measurement(items)
        subject_passes = sum(item.get("subject_status") in {"passed", "clean", "skipped-docs"} for item in items)
        subject_failures = sum(item.get("subject_status") in {"failed", "timeout", "partial", "unverified"} for item in items)
        row_tool_ok = sum(item.get("tool_status") == "ok" for item in items)
        row_tool_errors = len(items) - row_tool_ok
        command_rows.append({
            "command": command,
            "calls": len(items),
            "tool_ok": row_tool_ok,
            "tool_errors": row_tool_errors,
            "subject_passes": subject_passes,
            "subject_failures": subject_failures,
            "median_ms": int(median(int(item.get("duration_ms", 0)) for item in items)),
            "total_ms": sum(int(item.get("duration_ms", 0)) for item in items),
            "visible_chars": sum(int(item.get("visible_chars", 0)) for item in items),
            "truncations": sum(bool(item.get("truncated")) for item in items),
            **measurement,
            # v1.2.0 JSON aliases; semantics now explicitly mean agentq/tool health.
            "successes": row_tool_ok,
            "failures": row_tool_errors,
            "success_rate": _percent(row_tool_ok, len(items)),
            "source_chars": measurement["measured_source_chars"],
            "suppressed_chars": measurement["avoided_chars"],
        })

    visible_chars = sum(int(event.get("visible_chars", 0)) for event in operation_events)
    measurement = _measurement(operation_events)
    tool_ok = sum(event.get("tool_status") == "ok" for event in operation_events)
    tool_errors = len(operation_events) - tool_ok
    thread_ids = {str(event["thread_id"]) for event in operation_events if event.get("thread_id")}
    fallback_sessions = _gap_session_count(operation_events)

    project_run_events = [event for event in operation_events if event.get("command") == "run"]
    project_commands = {
        "runs": len(project_run_events),
        "passed": sum(event.get("subject_status") == "passed" for event in project_run_events),
        "failed": sum(event.get("subject_status") == "failed" for event in project_run_events),
        "timed_out": sum(event.get("subject_status") == "timeout" for event in project_run_events),
    }

    verification_events = [event for event in operation_events if event.get("command") in {"verify-changed", "verify-task"}]
    verification_modes = Counter(
        str((event.get("metrics") or {}).get("verification_mode", "unknown"))
        for event in verification_events
    )
    verification_statuses = Counter(str(event.get("subject_status") or "unknown") for event in verification_events)
    verification = {
        "runs": len(verification_events),
        "passed": verification_statuses.get("passed", 0) + verification_statuses.get("clean", 0) + verification_statuses.get("skipped-docs", 0),
        "failed": verification_statuses.get("failed", 0),
        "partial": verification_statuses.get("partial", 0) + verification_statuses.get("unverified", 0),
        "planned": verification_statuses.get("planned", 0),
        "checks_executed": sum(int((event.get("metrics") or {}).get("executed_steps", 0)) for event in verification_events),
        "checks_failed": sum(int((event.get("metrics") or {}).get("failed_steps", 0)) for event in verification_events),
        "changed_files": sum(int((event.get("metrics") or {}).get("changed_files", 0)) for event in verification_events),
        "changed_packages": sum(int((event.get("metrics") or {}).get("changed_packages", 0)) for event in verification_events),
        "affected_packages": sum(int((event.get("metrics") or {}).get("affected_packages", 0)) for event in verification_events),
        "raw_output_chars": sum(int((event.get("metrics") or {}).get("raw_output_chars", 0)) for event in verification_events),
        "modes": [{"mode": mode, "runs": count} for mode, count in verification_modes.most_common()],
    }

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
        window_start = float(operation_events[0].get("time", now))
    else:
        window_start = now
    activity = _activity_buckets(operation_events, window_start, now)
    repos = Counter(str(event.get("repo_name", "?")) for event in operation_events)
    reads = read_efficiency(operation_events)
    semantic_events = [event for event in operation_events if event.get("command") == "ts-nav"]
    semantic_actions = Counter(
        str((event.get("metrics") or {}).get("semantic_action", "unknown"))
        for event in semantic_events
    )
    navigation = {
        "outline_calls": sum(event.get("command") == "outline" for event in operation_events),
        "semantic_calls": len(semantic_events),
        "semantic_actions": dict(semantic_actions),
        "semantic_ambiguous": sum(bool((event.get("metrics") or {}).get("semantic_ambiguous")) for event in semantic_events),
    }
    tasks = task_efficiency(root, events, selected, operation_events, all_repos=all_repos)
    transitions = operation_transitions(operation_events)
    error_categories = Counter(str(event.get("error_category") or "unknown") for event in operation_events if event.get("tool_status") != "ok")

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
        "duration_ms": sum(int(event.get("duration_ms", 0)) for event in operation_events),
        "visible_chars": visible_chars,
        "visible_token_proxy": round(visible_chars / 4),
        "truncations": sum(bool(event.get("truncated")) for event in operation_events),
        "invocation_chars": sum(int(event.get("invocation_chars", 0)) for event in operation_events),
        "measurement": measurement,
        "source_chars": measurement["measured_source_chars"],
        "suppressed_chars": measurement["avoided_chars"],
        "reduction_percent": measurement["reduction_percent"],
        "project_commands": project_commands,
        "commands": command_rows,
        "activity": activity,
        "recent": recent_events,
        "detailed": recent > 0,
        "verification": verification,
        "reads": reads,
        "navigation": navigation,
        "tasks": tasks,
        "transitions": transitions,
        "error_categories": dict(error_categories),
        "repositories": [{"name": name, "events": count} for name, count in repos.most_common(10)],
        "sources": sources,
        "archive_result": archive_result,
        "measurement_note": (
            "visible token proxy is visible characters divided by four; it is not provider token accounting. "
            "Measured reduction applies only to calls where agentq captured source command output; coverage is reported explicitly. "
            "Read redundancy is same-task/thread and content-version scoped; cross-task/thread reuse is reported separately. "
            "A task is one independently acceptable outcome, not one thread or prompt."
        ),
    }


def _supports_unicode() -> bool:
    encoding = sys.stdout.encoding or locale.getpreferredencoding(False) or ""
    return "UTF" in encoding.upper()


def _sparkline(values: list[int], width: int | None = None) -> str:
    if not values:
        return ""
    if width and len(values) > width:
        bucket = len(values) / width
        values = [sum(values[int(index * bucket): max(int((index + 1) * bucket), int(index * bucket) + 1)]) for index in range(width)]
    maximum = max(values)
    if maximum <= 0:
        return "·" * len(values)
    chars = SPARKS if _supports_unicode() else ".:-=+*#@"
    return "".join(chars[min(len(chars) - 1, round(value / maximum * (len(chars) - 1)))] for value in values)


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
        return "—"
    return f"{value:.1f}%"


def _measurement_label(measurement: dict[str, Any]) -> str:
    if not measurement.get("instrumented_calls"):
        return "—"
    overhead = int(measurement.get("overhead_chars", 0))
    if overhead:
        return f"+{human_bytes(overhead)} overhead"
    return _pct(measurement.get("reduction_percent"))


def _recent_marker(event: dict[str, Any]) -> str:
    if event.get("tool_status") != "ok":
        return "✗"
    if event.get("subject_status") in {"failed", "timeout", "partial", "unverified"}:
        return "◇"
    return "✓"


def render_stats_plain(data: dict[str, Any], *, utc: bool = False) -> str:
    width = max(78, min(112, shutil.get_terminal_size((96, 30)).columns))
    rule = "─" * width if _supports_unicode() else "-" * width
    reliability = _pct(data.get("tool_reliability"))
    lines = [
        "agentq activity",
        f"scope {data['scope']} · {data['since']} · {_window_label(data, utc)}",
        rule,
    ]
    if not data["events"]:
        lines.extend([
            "No telemetry events in this scope.",
            "Run normal agentq commands, then use: agentq stats --since 7d",
            "Install persistence from a normal shell with: agentq stats --install-persistence",
        ])
        return "\n".join(lines)

    project = data["project_commands"]
    measured = data["measurement"]
    reads = data.get("reads") or {}
    navigation = data.get("navigation") or {}
    tasks = data.get("tasks") or {}
    lines.extend([
        f"activity     {data['events']} calls · {data['sessions']} contexts · {data['threads']} Codex threads · agentq {reliability} · {data['tool_errors']} errors",
        f"project      {project['passed']} passed · {project['failed']} failed · {project['timed_out']} timeout",
        f"exposure     {human_bytes(data['visible_chars'])} visible · ~{data['visible_token_proxy']:,} token proxy · {data['truncations']} cuts",
        (
            f"measured     {human_bytes(measured['measured_source_chars'])} source → {human_bytes(measured['measured_visible_chars'])} visible · "
            f"{_pct(measured['reduction_percent'])} reduction · {measured['instrumented_calls']}/{measured['total_calls']} calls "
            f"({_pct(measured['instrumented_call_percent'])}) instrumented"
            if measured["instrumented_calls"]
            else "measured     — (no source-output measurement in this window)"
        ),
    ])

    if reads.get("calls"):
        tracked = int(reads.get("tracked_calls", 0))
        overlap = _pct(reads.get("overlap_percent")) if tracked else "—"
        lines.append(
            f"reads        {reads.get('calls', 0)} calls · {reads.get('unique_files', 0)} files · "
            f"{reads.get('reread_ranges', 0)} rereads · {overlap} overlap · {reads.get('fully_redundant_ranges', 0)} redundant · "
            f"nav {navigation.get('semantic_calls', 0)} semantic / {navigation.get('outline_calls', 0)} outline"
        )
    if tasks.get("started") or tasks.get("accepted") or tasks.get("abandoned") or tasks.get("active"):
        active = ""
        if tasks.get("active"):
            age = tasks.get("active_age_seconds")
            active = f" · 1 active" + (f" ({_duration(int(age) * 1000)})" if age is not None else "")
        per_task = (
            f" · ~{tasks['token_proxy_per_accepted_task']:,} tokens/accepted · {tasks['calls_per_accepted_task']} calls/accepted"
            if tasks.get("token_proxy_per_accepted_task") is not None
            else ""
        )
        lines.append(
            f"tasks        {tasks.get('accepted', 0)} accepted{active} · {tasks.get('abandoned', 0)} abandoned · "
            f"attributed {_pct(tasks.get('attribution_percent'))}{per_task}"
        )

    if data["events"] >= 10 and len(data["activity"]) > 1:
        values = [int(item["calls"]) for item in data["activity"]]
        lines.append(f"activity     {_sparkline(values, width=min(48, max(12, width - 24)))}")
    if data.get("detailed") and tasks.get("calls_distribution", {}).get("p50") is not None:
        d = tasks["calls_distribution"]
        lines.append(f"task calls   p50 {d['p50']} · p90 {d['p90']} · max {d['max']}")
    if data.get("detailed") and data.get("transitions"):
        lines.append("transitions  " + " · ".join(f"{x['transition']} {x['calls']}" for x in data["transitions"][:5]))

    lines.extend([rule, "operations"])
    header = f"  {'Operation':<18} {'Calls':>5} {'Tool':>8} {'Pass':>5} {'Fail':>5} {'Median':>7} {'Visible':>9} {'Saved':>9}"
    lines.append(header)
    for row in data["commands"][:16]:
        marker = "✗" if row["tool_errors"] else "◇" if row["subject_failures"] else "✓"
        tool = "ok" if not row["tool_errors"] else f"{row['tool_errors']} err"
        has_subject = bool(row["subject_passes"] or row["subject_failures"])
        passed = str(row["subject_passes"]) if has_subject else "—"
        failed = str(row["subject_failures"]) if has_subject else "—"
        saved = _measurement_label(row)
        lines.append(
            f"{marker} {row['command']:<18} {row['calls']:>5} {tool:>8} {passed:>5} {failed:>5} "
            f"{_duration(row['median_ms']):>7} {human_bytes(row['visible_chars']):>9} {saved:>9}"
        )

    verification = data["verification"]
    if verification["runs"]:
        lines.extend([
            rule,
            "verification",
            f"  {verification['runs']} runs · {verification['passed']} passed · {verification['failed']} failed · "
            f"{verification['partial']} partial · {verification['planned']} planned",
            f"  {verification['checks_executed']} checks · {verification['checks_failed']} failed checks · "
            f"{verification['changed_files']} changed files to {verification['affected_packages']} affected packages",
        ])

    if data.get("recent"):
        lines.extend([rule, "recent (--detailed)"])
        for event in data["recent"]:
            marker = _recent_marker(event)
            tool = "ok" if event.get("tool_status") == "ok" else "error"
            subject = str(event.get("subject_status") or "—")
            code = event.get("subject_exit_code")
            if code is not None:
                subject += f" ({code})"
            lines.append(
                f"  {marker} {_time_label(event['time'], utc)}  {event['command']:<18} "
                f"tool {tool:<5} · command {subject:<12} · {_duration(event['duration_ms']):>7} · {human_bytes(event['visible_chars']):>9}"
            )

    if data.get("archive_result"):
        archived = data["archive_result"]
        lines.extend([rule, f"archive: added {archived['added']} events; total persistent {archived['total_archived']}"])
    lines.extend([
        rule,
        "note: Tool is agentq health; Pass/Fail are wrapped command outcomes. "
        "~tokens is visible characters divided by four. Use --detailed for recent activity.",
    ])
    return "\n".join(lines)


# Backwards-compatible public name used by older callers/tests.
def render_stats(data: dict[str, Any], *, color: str = "auto", utc: bool = False) -> str:
    del color
    return render_stats_plain(data, utc=utc)


def rich_available() -> bool:
    try:
        import rich  # noqa: F401
        return True
    except ImportError:
        return False


def _compact_int(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 1_000_000:
        return f"{value / 1_000:.1f}k".rstrip("0").rstrip(".")
    return f"{value / 1_000_000:.1f}m".rstrip("0").rstrip(".")


def _rich_dashboard(data: dict[str, Any], *, utc: bool = False) -> Any:
    """Render a dense, Jest/Vite-style dashboard without full-width chrome."""
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    project = data["project_commands"]
    measured = data["measurement"]
    reads = data.get("reads") or {}
    navigation = data.get("navigation") or {}
    tasks = data.get("tasks") or {}

    def pair(line: Text, label: str, value: str, style: str = "bold") -> None:
        if len(line.plain):
            line.append("  ")
        line.append(label + " ", style="dim")
        line.append(value, style=style)

    def section(name: str) -> Text:
        return Text(name, style="bold cyan")

    header = Text()
    header.append("agentq", style="bold cyan")
    header.append(f"  {data['scope']}", style="bold")
    header.append(f"  {data['since']}", style="dim")

    start_label, end_label, tz = _window_parts(data, utc)
    window = Text()
    window.append(start_label, style="dim")
    window.append(" to ", style="dim bold")
    window.append(f"{end_label} {tz}", style="dim")

    summary1 = Text()
    pair(summary1, "calls", str(data["events"]))
    pair(summary1, "contexts", str(data["sessions"]))
    if data["threads"]:
        pair(summary1, "threads", str(data["threads"]))
    reliability = data.get("tool_reliability")
    reliability_style = "green" if reliability is not None and reliability >= 99 else "yellow"
    if reliability is not None and reliability < 95:
        reliability_style = "red"
    pair(summary1, "agentq", _pct(reliability), reliability_style)
    if data["tool_errors"]:
        pair(summary1, "errors", f"{NERD_ERROR} {data['tool_errors']}", "red bold")

    summary2 = Text()
    summary2.append(f"{NERD_OK} ", style="green bold")
    summary2.append(f"{project['passed']} passed", style="green")
    summary2.append("  ")
    fail_icon = NERD_ERROR if project["failed"] else NERD_OK
    fail_style = "red bold" if project["failed"] else "green"
    summary2.append(f"{fail_icon} ", style=fail_style)
    summary2.append(f"{project['failed']} failed", style="red" if project["failed"] else "green")
    if project["timed_out"]:
        summary2.append(f"  {NERD_WARN} {project['timed_out']} timeout", style="yellow")
    pair(summary2, "visible", human_bytes(data["visible_chars"]))
    pair(summary2, "~tokens", _compact_int(int(data["visible_token_proxy"])), "cyan")
    if data["truncations"]:
        pair(summary2, "cuts", str(data["truncations"]), "yellow")

    summary_lines: list[Any] = [header, window, Text(), summary1, summary2]
    if measured["instrumented_calls"]:
        summary3 = Text()
        pair(summary3, "measured", human_bytes(measured["measured_source_chars"]))
        if measured["overhead_chars"]:
            pair(summary3, "wrapper overhead", human_bytes(measured["overhead_chars"]), "yellow")
        else:
            pair(summary3, "saved", human_bytes(measured["avoided_chars"]), "green bold")
            pair(summary3, "reduction", _pct(measured["reduction_percent"]), "green")
        pair(summary3, "coverage", f"{measured['instrumented_calls']}/{measured['total_calls']} calls ({_pct(measured['instrumented_call_percent'])})", "dim")
        summary_lines.append(summary3)

    if reads.get("calls"):
        read_line = Text()
        pair(read_line, "reads", str(reads.get("calls", 0)))
        pair(read_line, "files", str(reads.get("unique_files", 0)))
        if reads.get("reread_ranges"):
            pair(read_line, "rereads", str(reads["reread_ranges"]), "yellow")
        tracked = int(reads.get("tracked_calls", 0))
        if tracked and reads.get("overlap_percent") is not None:
            overlap_value = float(reads["overlap_percent"])
            overlap_style = "green" if overlap_value < 10 else "yellow" if overlap_value < 30 else "red"
            pair(read_line, "overlap", _pct(overlap_value), overlap_style)
        if reads.get("fully_redundant_ranges"):
            pair(read_line, "redundant", str(reads["fully_redundant_ranges"]), "red")
        pair(
            read_line,
            "nav",
            f"{navigation.get('semantic_calls', 0)} semantic / {navigation.get('outline_calls', 0)} outline",
            "cyan" if navigation.get("semantic_calls") else "yellow",
        )
        summary_lines.append(read_line)

    if tasks.get("started") or tasks.get("accepted") or tasks.get("abandoned") or tasks.get("active"):
        task_line = Text()
        pair(task_line, "tasks", f"{tasks.get('accepted', 0)} accepted", "green" if tasks.get("accepted") else "dim")
        if tasks.get("active"):
            age = tasks.get("active_age_seconds")
            active_value = "1"
            if age is not None:
                active_value += f" ({_duration(int(age) * 1000)})"
            pair(task_line, "active", f"{NERD_ACTIVE} {active_value}", "cyan")
        if tasks.get("abandoned"):
            pair(task_line, "abandoned", str(tasks["abandoned"]), "yellow")
        if tasks.get("attribution_percent") is not None:
            pair(task_line, "attributed", _pct(tasks["attribution_percent"]))
        if tasks.get("token_proxy_per_accepted_task") is not None:
            pair(task_line, "~tokens/task", _compact_int(int(tasks["token_proxy_per_accepted_task"])), "cyan")
            pair(task_line, "calls/task", str(tasks["calls_per_accepted_task"]))
        summary_lines.append(task_line)

    operations = Table(
        box=None,
        expand=False,
        show_header=True,
        header_style="dim bold",
        padding=(0, 1),
        collapse_padding=True,
    )
    operations.add_column("", no_wrap=True)
    operations.add_column("Operation", no_wrap=True, max_width=18)
    operations.add_column("Calls", justify="right", no_wrap=True)
    operations.add_column("Tool", justify="right", no_wrap=True)
    operations.add_column("Pass", justify="right", no_wrap=True)
    operations.add_column("Fail", justify="right", no_wrap=True)
    operations.add_column("Median", justify="right", no_wrap=True)
    operations.add_column("Visible", justify="right", no_wrap=True)
    operations.add_column("Saved", justify="right", no_wrap=True)

    for row in data["commands"][:16]:
        if row["tool_errors"]:
            marker = Text(NERD_ERROR, style="red bold")
        elif row["subject_failures"]:
            marker = Text(NERD_WARN, style="yellow bold")
        else:
            marker = Text(NERD_OK, style="green bold")

        tool = Text("ok", style="green")
        if row["tool_errors"]:
            tool = Text(f"{row['tool_errors']} err", style="red bold")

        has_subject = bool(row["subject_passes"] or row["subject_failures"])
        passed = Text(str(row["subject_passes"]), style="green") if has_subject else Text("—", style="dim")
        failed = Text(str(row["subject_failures"]), style="red" if row["subject_failures"] else "dim") if has_subject else Text("—", style="dim")

        saved = Text("—", style="dim")
        if row["instrumented_calls"]:
            if row["overhead_chars"]:
                saved = Text(f"+{human_bytes(row['overhead_chars'])}", style="yellow")
            elif row["reduction_percent"] is not None:
                saved = Text(f"{row['reduction_percent']:.1f}%", style="green")

        operations.add_row(
            marker,
            row["command"],
            str(row["calls"]),
            tool,
            passed,
            failed,
            _duration(row["median_ms"]),
            human_bytes(row["visible_chars"]),
            saved,
        )

    sections: list[Any] = [*summary_lines, Text(), section("Operations"), operations]

    verification = data["verification"]
    if verification["runs"]:
        verify = Text()
        if verification["failed"]:
            verify.append(f"{NERD_ERROR} ", style="red bold")
        elif verification["partial"]:
            verify.append(f"{NERD_WARN} ", style="yellow bold")
        elif verification["passed"]:
            verify.append(f"{NERD_OK} ", style="green bold")
        else:
            verify.append(f"{NERD_INFO} ", style="cyan bold")
        verify.append(f"{verification['runs']} run" + ("s" if verification["runs"] != 1 else ""), style="bold")
        if verification["passed"]:
            verify.append(f"  {verification['passed']} passed", style="green")
        if verification["failed"]:
            verify.append(f"  {verification['failed']} failed", style="red")
        if verification["partial"]:
            verify.append(f"  {verification['partial']} partial", style="yellow")
        if verification["planned"]:
            verify.append(f"  {verification['planned']} planned", style="cyan")
        verify.append(f"  {verification['checks_executed']} checks  {verification['changed_files']} files ", style="dim")
        verify.append("to", style="dim bold")
        verify.append(f" {verification['affected_packages']} pkgs", style="dim")
        sections.extend([Text(), section("Verification"), verify])

    if data.get("recent"):
        recent_table = Table(box=None, expand=False, show_header=True, header_style="dim bold", padding=(0, 1), collapse_padding=True)
        recent_table.add_column("", no_wrap=True)
        recent_table.add_column("Time", style="dim", no_wrap=True)
        recent_table.add_column("Operation", no_wrap=True, max_width=18)
        recent_table.add_column("Tool", no_wrap=True)
        recent_table.add_column("Command", no_wrap=True)
        recent_table.add_column("Median", justify="right", style="dim", no_wrap=True)
        recent_table.add_column("Visible", justify="right", style="dim", no_wrap=True)

        for event in data["recent"]:
            if event.get("tool_status") != "ok":
                marker = Text(NERD_ERROR, style="red bold")
                tool = Text("error", style="red")
            elif event.get("subject_status") in {"failed", "timeout", "partial", "unverified"}:
                marker = Text(NERD_WARN, style="yellow bold")
                tool = Text("ok", style="green")
            else:
                marker = Text(NERD_OK, style="green bold")
                tool = Text("ok", style="green")
            command_status = Text(str(event.get("subject_status") or "—"), style="dim")
            if event.get("subject_status") == "passed":
                command_status.stylize("green")
            elif event.get("subject_status") == "failed":
                command_status.stylize("red")
            elif event.get("subject_status") in {"timeout", "partial", "unverified"}:
                command_status.stylize("yellow")
            recent_table.add_row(
                marker,
                _time_label(event["time"], utc),
                event["command"],
                tool,
                command_status,
                _duration(event["duration_ms"]),
                human_bytes(event["visible_chars"]),
            )
        sections.extend([Text(), section("Recent · detailed"), recent_table])

    footer = Text(
        "Tool = agentq health; Pass/Fail = wrapped command outcomes; ~tokens = visible chars / 4. "
        "Use --detailed for recent activity.",
        style="dim",
    )
    if data.get("archive_result"):
        archived = data["archive_result"]
        footer.append(f"  archive +{archived['added']} / {archived['total_archived']}", style="dim")
    sections.extend([Text(), footer])
    return Group(*sections)

def print_stats(data: dict[str, Any], *, color: str = "auto", plain: bool = False, utc: bool = False, budget: int = 12000) -> None:
    use_rich = not plain and sys.stdout.isatty() and rich_available()
    if not use_rich:
        text, _ = bound_output(render_stats_plain(data, utc=utc), budget)
        print(text)
        return
    from rich.console import Console
    no_color = color == "never"
    force_terminal = True if color == "always" else None
    Console(no_color=no_color, force_terminal=force_terminal).print(_rich_dashboard(data, utc=utc))


def watch_stats(
    root: Path,
    *,
    interval: float,
    since: str,
    recent: int,
    operations: list[str],
    all_repos: bool,
    color: str,
    plain: bool = False,
    utc: bool = False,
) -> None:
    use_rich = not plain and sys.stdout.isatty() and rich_available()
    try:
        if use_rich:
            from rich.console import Console
            from rich.live import Live
            console = Console(no_color=(color == "never"), force_terminal=True if color == "always" else None)
            first = stats_data(root, since=since, recent=recent, operations=operations, all_repos=all_repos)
            with Live(_rich_dashboard(first, utc=utc), console=console, refresh_per_second=max(1, min(10, int(1 / interval) if interval < 1 else 4)), screen=False) as live:
                while True:
                    time.sleep(interval)
                    data = stats_data(root, since=since, recent=recent, operations=operations, all_repos=all_repos)
                    live.update(_rich_dashboard(data, utc=utc), refresh=True)
        else:
            while True:
                data = stats_data(root, since=since, recent=recent, operations=operations, all_repos=all_repos)
                if sys.stdout.isatty():
                    print("\033[2J\033[H", end="")
                print(render_stats_plain(data, utc=utc), flush=True)
                time.sleep(interval)
    except KeyboardInterrupt:
        return
