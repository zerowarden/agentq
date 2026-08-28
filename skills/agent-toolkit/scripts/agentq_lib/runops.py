from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .common import AgentQError, cache_dir, compact_line, redact_text, truncate_line
from .redaction import StreamingRedactor

DIAGNOSTIC_RE = re.compile(
    r"(?i)(^|\b)(fail(?:ed|ure)?|error|exception|assertion|panic|fatal|TS\d{4}|E\d{3,4}|warning:|✗|×)(\b|:)"
)
LOCATION_RE = re.compile(r"(?:^|\s)([^\s:]+\.(?:ts|tsx|js|jsx|py|rs|go|java|kt|sql|sh)):(\d+)(?::(\d+))?")

# Decorative output controls and update-notifier noise only. Behavior-sensitive
# settings (CI, XDG_CACHE_HOME, TERM) are deliberately left to the process
# environment unless an explicit profile or flag says otherwise.
_PROFILE_ENV: dict[str, dict[str, str]] = {
    "transparent": {},
    "compact": {
        "NO_COLOR": "1", "FORCE_COLOR": "0", "CLICOLOR": "0",
        "PAGER": "cat", "GIT_PAGER": "cat",
        "npm_config_update_notifier": "false", "npm_config_fund": "false",
        "npm_config_audit": "false", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    },
}
_PROFILE_ENV["ci"] = {**_PROFILE_ENV["compact"], "CI": "1"}
_PROFILE_ENV["offline"] = {
    **_PROFILE_ENV["ci"],
    "npm_config_offline": "true", "PNPM_OFFLINE": "true",
    "CARGO_NET_OFFLINE": "true", "PIP_NO_INDEX": "1",
}

_LOG_TTL_SECONDS = 24 * 60 * 60
_LOG_QUOTA_BYTES = 64 * 1024 * 1024
_LOG_ACTIVE_GRACE_SECONDS = 60


def _private_temp_log(root: Path, label: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label).strip("-") or "command"
    fd, name = tempfile.mkstemp(prefix=f"{safe}-", suffix=".log", dir=cache_dir(root))
    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    os.close(fd)
    return Path(name)


def _cleanup_logs(directory: Path) -> None:
    """Enforce the log TTL and total-size quota; best-effort and concurrency-safe."""
    now = time.time()
    try:
        entries = [entry for entry in os.scandir(directory) if entry.name.endswith(".log") and entry.is_file()]
    except OSError:
        return
    survivors: list[tuple[float, int, str]] = []
    total = 0
    for entry in entries:
        try:
            metadata = entry.stat()
        except OSError:
            continue
        if now - metadata.st_mtime < _LOG_ACTIVE_GRACE_SECONDS:
            total += metadata.st_size  # never reap a log that may be in flight
            continue
        if metadata.st_mtime < now - _LOG_TTL_SECONDS:
            try:
                os.unlink(entry.path)
            except OSError:
                total += metadata.st_size
            continue
        survivors.append((metadata.st_mtime, metadata.st_size, entry.path))
        total += metadata.st_size
    if total <= _LOG_QUOTA_BYTES:
        return
    for _, size, path in sorted(survivors):
        if total <= _LOG_QUOTA_BYTES:
            break
        try:
            os.unlink(path)
            total -= size
        except OSError:
            continue


def run_compact(
    root: Path,
    command: list[str],
    *,
    cwd: str | None = None,
    timeout: int = 900,
    label: str = "command",
    max_diagnostics: int = 60,
    tail_lines: int = 40,
    profile: str = "compact",
    isolated_cache: bool = False,
    keep_log: bool = False,
) -> dict[str, Any]:
    """Run argv without a shell and stream only redacted text to a private local log."""
    if profile not in _PROFILE_ENV:
        raise AgentQError(f"unknown run profile: {profile}")
    if not command:
        raise AgentQError("a command is required after --")
    working = (root / cwd).resolve() if cwd else root
    if working != root and root not in working.parents:
        raise AgentQError(f"working directory is outside repository root: {working}")
    if not working.is_dir():
        raise AgentQError(f"working directory does not exist: {working}")

    env = os.environ.copy()
    if isolated_cache:
        runtime_cache = cache_dir(root) / "xdg"
        runtime_cache.mkdir(parents=True, exist_ok=True)
        try:
            runtime_cache.chmod(0o700)
        except OSError:
            pass
        env["XDG_CACHE_HOME"] = str(runtime_cache)
    env.update(_PROFILE_ENV[profile])

    final_log = _private_temp_log(root, label)
    diagnostics: list[str] = []
    diagnostics_seen = 0
    tail: deque[str] = deque(maxlen=tail_lines)
    output_lines = 0
    output_chars = 0
    diagnostics_keys: set[str] = set()
    reader_error: list[BaseException] = []
    redactor = StreamingRedactor()
    start = time.perf_counter()
    timed_out = False

    try:
        proc = subprocess.Popen(
            command,
            cwd=working,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError as exc:
        final_log.unlink(missing_ok=True)
        raise AgentQError(f"command not found: {command[0]}") from exc

    assert proc.stdout is not None

    def drain_output() -> None:
        nonlocal output_lines, output_chars, diagnostics_seen
        try:
            with final_log.open("w", encoding="utf-8", errors="replace") as destination:
                try:
                    os.chmod(final_log, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
                for line in proc.stdout:
                    output_lines += 1
                    redacted = redactor.feed(line)
                    if not redacted:
                        continue
                    destination.write(redacted)
                    output_chars += len(redacted)
                    clean = truncate_line(redacted, 360)
                    tail.append(clean)
                    if DIAGNOSTIC_RE.search(clean) or LOCATION_RE.search(clean):
                        diagnostics_seen += 1
                        key = clean.strip()
                        if key and key not in diagnostics_keys and len(diagnostics) < max_diagnostics:
                            diagnostics_keys.add(key)
                            diagnostics.append(clean)
                tail_rest = redactor.finish()
                if tail_rest:
                    destination.write(tail_rest)
                    output_chars += len(tail_rest)
        except BaseException as exc:  # propagate reader failures after process cleanup
            reader_error.append(exc)

    reader = threading.Thread(target=drain_output, name="agentq-output-redactor", daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    finally:
        reader.join(timeout=10)
        if reader.is_alive():
            try:
                proc.stdout.close()
            except OSError:
                pass
            reader.join(timeout=2)

    duration = time.perf_counter() - start
    if reader_error:
        final_log.unlink(missing_ok=True)
        raise AgentQError(f"failed while capturing command output: {reader_error[0]}")
    if reader.is_alive():
        final_log.unlink(missing_ok=True)
        raise AgentQError("command output reader did not terminate cleanly")

    exit_code = 124 if timed_out else proc.returncode
    # Diagnostics and the tail are already extracted above; a successful run
    # does not need its full output retained unless --keep-log is explicit.
    retained = keep_log or exit_code != 0
    if not retained:
        final_log.unlink(missing_ok=True)
    try:
        _cleanup_logs(cache_dir(root))
    except (OSError, AgentQError):
        pass

    data: dict[str, Any] = {
        "repo_root": str(root), "command": [redact_text(item) for item in command], "cwd": str(working),
        "profile": profile, "exit_code": exit_code, "timed_out": timed_out,
        "duration_seconds": round(duration, 3), "output_lines": output_lines, "output_chars": output_chars,
        "diagnostics": diagnostics, "diagnostics_truncated": diagnostics_seen > len(diagnostics),
        "tail": list(tail),
        "log": str(final_log) if retained else None,
        "log_retention": "retained" if retained else "deleted",
        "redaction": redactor.stats(),
    }
    if retained:
        data["log_mode"] = "0600 local redacted file"
    if profile == "offline":
        data["network_isolation"] = "advisory environment flags only; not a network sandbox"
    return data


def render_run(data: dict[str, Any]) -> str:
    status = "TIMEOUT" if data["timed_out"] else "PASS" if data["exit_code"] == 0 else "FAIL"
    lines = [
        f"{status}: {' '.join(data['command'])}",
        f"exit={data['exit_code']} profile={data.get('profile', 'compact')} duration={data['duration_seconds']}s output_lines={data['output_lines']} output_chars={data['output_chars']}",
    ]
    if data.get("log"):
        lines.append(f"full local log: {data['log']} ({data.get('log_mode', '0600 local redacted file')})")
    else:
        lines.append("log: deleted after summary extraction; pass --keep-log to retain")
    if data.get("network_isolation"):
        lines.append(f"network isolation: {data['network_isolation']}")
    if data["diagnostics"]:
        lines.append(f"\ndiagnostics ({len(data['diagnostics'])}{'+' if data['diagnostics_truncated'] else ''}):")
        lines.extend(f"  {line}" for line in data["diagnostics"])
    if data["exit_code"] != 0 and data["tail"]:
        lines.append("\noutput tail:")
        lines.extend(f"  {line}" for line in data["tail"])
    elif data["exit_code"] == 0 and data["tail"]:
        summary = [x for x in data["tail"] if x.strip()][-4:]
        if summary:
            lines.append("\ncompletion summary:")
            lines.extend(f"  {line}" for line in summary)
    return "\n".join(lines)
