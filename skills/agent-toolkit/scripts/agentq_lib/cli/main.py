"""agentq CLI entry point: dispatch, telemetry recording, and error handling."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentq_lib.common import AgentQCancelled, AgentQError, repo_root
from agentq_lib.contracts._base import ContractError
from agentq_lib.emission import DispatchResult

from .commands import execute
from .parser import build_parser, expansion_controls
from .types import Outcome

_ERROR_FORMATS = frozenset({"text", "json", "compact-json"})
_JSON_ERROR_FORMATS = frozenset({"json", "compact-json"})
_STATS_ADMIN_FLAGS = (
    "archive_only",
    "storage",
    "install_persistence",
    "remove_persistence",
)


@dataclass(frozen=True)
class _OutcomeFacts:
    """Telemetry-relevant facts extracted from one dispatched outcome."""

    exit_code: int
    data: Mapping[str, Any] | None
    visible_chars: int
    prebudget_chars: int
    render_budget_truncated: bool
    source_cap_truncated: bool
    output_view: str
    output_attribution: Mapping[str, int] | None
    receipt_id: str | None
    receipt_status: str | None
    delivered_fragments: int
    receipt_error: str | None


def _outcome_facts(outcome: Outcome) -> _OutcomeFacts:
    match outcome:
        case DispatchResult():
            render = outcome.render
            receipt = outcome.receipt
            return _OutcomeFacts(
                exit_code=outcome.exit_code,
                data=(
                    outcome.telemetry_data
                    if outcome.telemetry_data is not None
                    else outcome.data
                ),
                visible_chars=render.visible_chars,
                prebudget_chars=render.prebudget_chars or 0,
                render_budget_truncated=outcome.render_budget_truncated,
                source_cap_truncated=outcome.source_cap_truncated,
                output_view=outcome.output_view,
                output_attribution=outcome.output_attribution,
                receipt_id=receipt.receipt_id if receipt is not None else None,
                receipt_status=(
                    receipt.transport_status.value if receipt is not None else None
                ),
                delivered_fragments=(
                    len(receipt.fragments) if receipt is not None else 0
                ),
                receipt_error=outcome.receipt_error,
            )
        case int():
            return _OutcomeFacts(
                exit_code=outcome,
                data=None,
                visible_chars=0,
                prebudget_chars=0,
                render_budget_truncated=False,
                source_cap_truncated=False,
                output_view="default",
                output_attribution=None,
                receipt_id=None,
                receipt_status=None,
                delivered_fragments=0,
                receipt_error=None,
            )
        case _:
            raise AgentQError(f"unsupported command outcome: {type(outcome).__name__}")


def _stats_admin_action(args: argparse.Namespace) -> bool:
    if getattr(args, "command", None) != "stats":
        return False
    if any(bool(getattr(args, flag, False)) for flag in _STATS_ADMIN_FLAGS):
        return True
    return bool(getattr(args, "reset", False) and getattr(args, "all_repos", False))


def _resolve_root(args: argparse.Namespace) -> Path:
    if _stats_admin_action(args):
        return Path(args.repo).expanduser().resolve()
    return repo_root(args.repo)


def _execute_with_cancellation(args: argparse.Namespace, root: Path) -> Outcome:
    """Install CLI signal handlers so supervised children are cancelled cleanly."""
    from agentq_lib.process import CancellationToken, set_active_cancellation

    token = CancellationToken()
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: Any) -> None:
        token.cancel(signum)
        if token.in_use:
            return
        prior = previous.get(signum)
        if callable(prior):
            prior(signum, frame)
        elif signum == signal.SIGINT:
            raise KeyboardInterrupt
        else:
            # Restore the default disposition and re-raise: without this the
            # handler would silently swallow SIGTERM outside supervised windows.
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.signal(signum, handler)
    set_active_cancellation(token)
    try:
        return execute(args, root)
    finally:
        set_active_cancellation(None)
        for signum, prior in previous.items():
            signal.signal(signum, prior)


def _record_event() -> Callable[..., None] | None:
    """Return the telemetry recorder, or None when telemetry is disabled.

    Checking the flag here keeps telemetry imports off the disabled fast path;
    the flag itself has one definition (agentq_lib.runtime.telemetry_enabled).
    """
    from agentq_lib.runtime import telemetry_enabled

    if not telemetry_enabled():
        return None
    from agentq_lib.telemetry import record_event

    return record_event


def _requested_error_format(args: argparse.Namespace | None, argv: list[str]) -> str:
    """Best-effort output format for an error raised before or during parsing."""
    if args is not None:
        value = str(getattr(args, "format", ""))
        if value in _ERROR_FORMATS:
            return value
    inline = next(
        (item.split("=", 1)[1] for item in argv if item.startswith("--format=")),
        None,
    )
    if inline in _JSON_ERROR_FORMATS:
        return inline
    if "--format" in argv:
        index = argv.index("--format")
        separate = argv[index + 1] if index + 1 < len(argv) else None
        if separate in _JSON_ERROR_FORMATS:
            return separate
    return "text"


def _error_command(args: argparse.Namespace | None, argv: list[str]) -> str:
    if args is not None:
        return str(getattr(args, "command", "unknown"))
    return argv[0] if argv else "unknown"


def _repeat_requested(args: argparse.Namespace | None, argv: list[str]) -> bool:
    if args is not None:
        return bool(getattr(args, "repeat", False))
    return "--repeat" in argv


def _report_error(
    exc: Exception,
    *,
    start: float,
    args: argparse.Namespace | None,
    root: Path | None,
    argv: list[str],
) -> int:
    if root is None:
        try:
            root = repo_root(".")
        except Exception:
            root = None
    error_format = _requested_error_format(args, argv)
    error_data = {"error": str(exc), "type": "AgentQError"}
    error_visible = (
        json.dumps(error_data, ensure_ascii=False, indent=2)
        if error_format in _JSON_ERROR_FORMATS
        else f"agentq: {exc}"
    )
    error_command = _error_command(args, argv)
    record = _record_event() if root is not None else None
    if record is not None:
        from agentq_lib.output_attribution import attribute_output

        record(
            root,
            command=error_command,
            duration_ms=round((time.perf_counter() - start) * 1000),
            tool_status="error",
            agentq_exit_code=2,
            visible_chars=len(error_visible),
            prebudget_chars=len(error_visible),
            error_type=type(exc).__name__,
            error_message=str(exc),
            invocation=argv,
            expansion_controls=expansion_controls(args) if args else None,
            output_format=error_format,
            output_view="default",
            output_attribution=attribute_output(
                error_command,
                error_data,
                error_visible,
                output_format=error_format,
            ),
            repeat_requested=_repeat_requested(args, argv),
        )
    print(error_visible, file=sys.stderr)
    return 2


def main() -> int:
    start = time.perf_counter()
    root: Path | None = None
    args: argparse.Namespace | None = None
    argv = sys.argv[1:]
    try:
        parser = build_parser()
        args = parser.parse_args()
        root = _resolve_root(args)
        outcome = _execute_with_cancellation(args, root)
        facts = _outcome_facts(outcome)
        record = _record_event()
        if record is not None:
            record(
                root,
                command=args.command,
                duration_ms=round((time.perf_counter() - start) * 1000),
                tool_status="ok",
                agentq_exit_code=facts.exit_code,
                visible_chars=facts.visible_chars,
                prebudget_chars=facts.prebudget_chars,
                truncated=facts.render_budget_truncated,
                render_budget_truncated=facts.render_budget_truncated,
                source_cap_truncated=facts.source_cap_truncated,
                data=facts.data,
                invocation=argv,
                expansion_controls=expansion_controls(args),
                output_format=str(args.format),
                output_view=facts.output_view,
                output_attribution=facts.output_attribution,
                repeat_requested=bool(getattr(args, "repeat", False)),
                receipt_id=facts.receipt_id,
                receipt_status=facts.receipt_status,
                delivered_fragments=facts.delivered_fragments,
                receipt_error=facts.receipt_error,
            )
        return facts.exit_code
    except AgentQCancelled as exc:
        print("agentq: interrupted", file=sys.stderr)
        return exc.exit_code
    except (AgentQError, ContractError) as exc:
        return _report_error(exc, start=start, args=args, root=root, argv=argv)
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 141
    except KeyboardInterrupt:
        print("agentq: interrupted", file=sys.stderr)
        return 130
