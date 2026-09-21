#!/usr/bin/env python3
"""Selectable fixture process for supervisor tests; not collected by unittest."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn


def _write_pid(path: str | None, pid: int) -> None:
    if path:
        Path(path).write_text(str(pid), encoding="utf-8")


def _ok(args: argparse.Namespace) -> int:
    print("first line")
    print("second line")
    return args.exit_code


def _lines(args: argparse.Namespace) -> int:
    for index in range(args.count):
        print(f"line {index}")
    return args.exit_code


def _json_lines(args: argparse.Namespace) -> int:
    for index in range(args.count):
        print(json.dumps({"type": "match", "index": index}))
    return args.exit_code


def _stderr(args: argparse.Namespace) -> int:
    print("stdout line")
    print("stderr line", file=sys.stderr)
    return args.exit_code


def _heavy(args: argparse.Namespace) -> int:
    chunk = "x" * args.size
    for _ in range(args.count):
        sys.stdout.write(chunk + "\n")
        sys.stdout.flush()
        sys.stderr.write(chunk + "\n")
        sys.stderr.flush()
    return args.exit_code


def _giant_line(args: argparse.Namespace) -> int:
    sys.stdout.write("x" * args.size)
    sys.stdout.flush()
    return args.exit_code


def _child_hold(args: argparse.Namespace) -> int:
    child = subprocess.Popen([sys.executable, __file__, "hang"])
    _write_pid(args.child_pid_file, child.pid)
    print("leader exiting")
    return args.exit_code


def _sleep_forever(*_args: object) -> NoReturn:
    while True:
        time.sleep(0.05)


def _ignore_term(*_args: object) -> NoReturn:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _sleep_forever()


def _closed_ignore_term(*_args: object) -> NoReturn:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.close(1)
    os.close(2)
    _sleep_forever()


_MODES: dict[str, Callable[[argparse.Namespace], int]] = {
    "ok": _ok,
    "lines": _lines,
    "json": _json_lines,
    "stderr": _stderr,
    "hang": _sleep_forever,
    "ignore-term": _ignore_term,
    "closed-ignore-term": _closed_ignore_term,
    "heavy": _heavy,
    "giant-line": _giant_line,
    "child-hold": _child_hold,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode")
    parser.add_argument("--child-pid-file")
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--size", type=int, default=1024)
    args = parser.parse_args()
    handler = _MODES.get(args.mode)
    if handler is None:
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
