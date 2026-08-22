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

from .common import AgentQError, cache_dir, compact_line, redact_text

DIAGNOSTIC_RE = re.compile(
    r"(?i)(^|\b)(fail(?:ed|ure)?|error|exception|assertion|panic|fatal|TS\d{4}|E\d{3,4}|warning:|✗|×)(\b|:)"
)
LOCATION_RE = re.compile(r"(?:^|\s)([^\s:]+\.(?:ts|tsx|js|jsx|py|rs|go|java|kt|sql|sh)):(\d+)(?::(\d+))?")
PRIVATE_KEY_BEGIN_RE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
PRIVATE_KEY_END_RE = re.compile(r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def _private_temp_log(root: Path, label: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", label).strip("-") or "command"
    fd, name = tempfile.mkstemp(prefix=f"{safe}-", suffix=".log", dir=cache_dir(root))
    os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    os.close(fd)
    return Path(name)


def run_compact(
    root: Path,
    command: list[str],
    *,
    cwd: str | None = None,
    timeout: int = 900,
    label: str = "command",
    max_diagnostics: int = 60,
    tail_lines: int = 40,
    offline: bool = False,
) -> dict[str, Any]:
    """Run argv without a shell and stream only redacted text to a private local log."""
    if not command:
        raise AgentQError("a command is required after --")
    working = (root / cwd).resolve() if cwd else root
    if working != root and root not in working.parents:
        raise AgentQError(f"working directory is outside repository root: {working}")
    if not working.is_dir():
        raise AgentQError(f"working directory does not exist: {working}")

    env = os.environ.copy()
    runtime_cache = cache_dir(root) / "xdg"
    runtime_cache.mkdir(parents=True, exist_ok=True)
    try:
        runtime_cache.chmod(0o700)
    except OSError:
        pass
    env.update({
        "XDG_CACHE_HOME": str(runtime_cache),
        "NO_COLOR": "1", "FORCE_COLOR": "0", "CLICOLOR": "0", "TERM": "dumb",
        "CI": env.get("CI", "1"), "PAGER": "cat", "GIT_PAGER": "cat",
        "npm_config_update_notifier": "false", "npm_config_fund": "false",
        "npm_config_audit": "false", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    })
    if offline:
        env.update({
            "npm_config_offline": "true", "PNPM_OFFLINE": "true", "CARGO_NET_OFFLINE": "true",
            "PIP_NO_INDEX": "1",
        })

    final_log = _private_temp_log(root, label)
    diagnostics: list[str] = []
    diagnostics_seen = 0
    tail: deque[str] = deque(maxlen=tail_lines)
    output_lines = 0
    output_chars = 0
    diagnostics_keys: set[str] = set()
    reader_error: list[BaseException] = []
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
        in_private_key = False
        try:
            with final_log.open("w", encoding="utf-8", errors="replace") as destination:
                try:
                    os.chmod(final_log, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
                for line in proc.stdout:
                    output_lines += 1
                    if PRIVATE_KEY_BEGIN_RE.search(line):
                        in_private_key = True
                        redacted = "[REDACTED_PRIVATE_KEY_BLOCK]\n"
                    elif in_private_key:
                        if PRIVATE_KEY_END_RE.search(line):
                            in_private_key = False
                        continue
                    else:
                        redacted = redact_text(line)
                    destination.write(redacted)
                    output_chars += len(redacted)
                    clean = compact_line(redacted, 360)
                    tail.append(clean)
                    if DIAGNOSTIC_RE.search(clean) or LOCATION_RE.search(clean):
                        diagnostics_seen += 1
                        key = clean.strip()
                        if key and key not in diagnostics_keys and len(diagnostics) < max_diagnostics:
                            diagnostics_keys.add(key)
                            diagnostics.append(clean)
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

    return {
        "repo_root": str(root), "command": [redact_text(item) for item in command], "cwd": str(working),
        "exit_code": 124 if timed_out else proc.returncode, "timed_out": timed_out,
        "duration_seconds": round(duration, 3), "output_lines": output_lines, "output_chars": output_chars,
        "diagnostics": diagnostics, "diagnostics_truncated": diagnostics_seen > len(diagnostics),
        "tail": list(tail), "log": str(final_log), "log_mode": "0600 local redacted file",
    }


def render_run(data: dict[str, Any]) -> str:
    status = "TIMEOUT" if data["timed_out"] else "PASS" if data["exit_code"] == 0 else "FAIL"
    lines = [
        f"{status}: {' '.join(data['command'])}",
        f"exit={data['exit_code']} duration={data['duration_seconds']}s output_lines={data['output_lines']} output_chars={data['output_chars']}",
        f"full local log: {data['log']} ({data['log_mode']})",
    ]
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
