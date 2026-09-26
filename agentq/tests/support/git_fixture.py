"""Shared git fixture setup for tests that need a pinned commit."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def commit_all(root: Path, message: str = "seed") -> str:
    """Initialize one git repository and commit its current tree."""
    if shutil.which("git") is None:
        pytest.skip("git is required to build a checkout")
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_AUTHOR_DATE": "2026-09-23T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-09-23T00:00:00+00:00",
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True, env=env
        )

    git("init", "--quiet", ".")
    git("add", "--", ".")
    git(
        "-c",
        "user.name=Agentq Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--quiet",
        "-m",
        message,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
