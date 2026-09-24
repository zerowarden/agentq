"""Create a small, isolated, standard-library-only repository fixture.

Run from the inner agentq/ project:
    uv run --locked python scripts/create_m2_seed_repository.py

Does not overwrite changed files, install packages, or edit existing Git history.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

FILES: dict[str, str] = {
    "pyproject.toml": (
        '[project]\nname = "agentq-orders-fixture"\n'
        'version = "0.0.0"\nrequires-python = ">=3.10"\n'
    ),
    ".gitignore": "__pycache__/\n*.pyc\n",
    "orders.py": '''from dataclasses import dataclass


@dataclass(frozen=True)
class Order:
    order_id: int
    active: bool = True


def list_orders(orders: list[Order], limit: int | None = None) -> list[Order]:
    """Return active orders in input order, optionally limited."""
    if limit is not None and limit < 0:
        raise ValueError("limit must be nonnegative")
    active = [order for order in orders if order.active]
    return active if limit is None else active[:limit]
''',
    "report.py": '''from orders import Order, list_orders


def report_order_ids(orders: list[Order]) -> list[int]:
    return [order.order_id for order in list_orders(orders)]
''',
    "legacy.py": '''def list_orders() -> list[str]:
    """Unrelated declaration with the same name, used as a resolution decoy."""
    return ["legacy"]
''',
    "tests/__init__.py": "",
    "tests/test_orders.py": '''import unittest

from orders import Order, list_orders
from report import report_order_ids


class OrdersTests(unittest.TestCase):
    def test_filters_inactive_and_preserves_order(self):
        values = [Order(2), Order(1, active=False), Order(3)]
        self.assertEqual(list_orders(values), [Order(2), Order(3)])

    def test_zero_limit_returns_empty_list(self):
        self.assertEqual(list_orders([Order(1)], limit=0), [])

    def test_negative_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            list_orders([Order(1)], limit=-1)

    def test_consumer_uses_order_objects(self):
        self.assertEqual(report_order_ids([Order(7)]), [7])


if __name__ == "__main__":
    unittest.main()
''',
}


def write_unchanged_or_new(path: Path, text: str) -> None:
    content = text.encode("utf-8")
    if path.exists():
        if not path.is_file() or path.read_bytes() != content:
            raise RuntimeError(f"Refusing to overwrite changed fixture: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def main() -> None:
    if shutil.which("git") is None:
        raise RuntimeError("Git must be installed to isolate the fixture checkout.")
    project = Path(__file__).resolve().parents[1]
    fixture = project / "evals/fixtures/repositories/orders_python/repository"
    workspace = project.parent / ".agentq-eval/worktrees/orders-python"
    for relative_path, text in FILES.items():
        write_unchanged_or_new(fixture / relative_path, text)
        write_unchanged_or_new(workspace / relative_path, text)

    # A nested Git root prevents agentq from treating its own outer repo as input.
    env = os.environ.copy()
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_AUTHOR_DATE": "2026-09-23T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2026-09-23T00:00:00+00:00",
    })
    if not (workspace / ".git").exists():
        subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
        subprocess.run(["git", "-C", str(workspace), "add", "--", *FILES], check=True)
        subprocess.run([
            "git", "-C", str(workspace),
            "-c", "user.name=Agentq Fixture",
            "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null",
            "commit", "--quiet", "-m", "Seed orders fixture v1",
        ], check=True, env=env)

    actual_root = subprocess.check_output(
        ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"], text=True
    ).strip()
    if Path(actual_root).resolve() != workspace.resolve():
        raise RuntimeError("Fixture does not have its own Git root.")
    status = subprocess.check_output(
        ["git", "-C", str(workspace), "status", "--porcelain"], text=True
    )
    if status:
        raise RuntimeError("Fixture checkout is not clean; use a fresh directory.")

    subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=workspace, env=env, check=True,
    )
    line = next(
        i for i, value in enumerate(FILES["orders.py"].splitlines(), 1)
        if value.startswith("def list_orders(")
    )
    command = [
        sys.executable, "-m", "agentq", "inspect", "orders.py",
        "--line", str(line), "--column", "5", "--intent", "edit", "--format", "json",
    ]
    print(f"Tracked fixture source: {fixture}")
    print(f"Isolated working repository: {workspace}")
    print("Run the following in Bash after installing agentq with uv sync:")
    print("cd " + shlex.quote(str(workspace)))
    print(shlex.join(command))
    print("Expected: target resolved; bounded bundle or explicit capability gaps.")
    print("The four passing tests validate fixture code, not agentq evidence quality.")


if __name__ == "__main__":
    main()
