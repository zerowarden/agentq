#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RESOURCE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])((?:scripts|references|assets)/[A-Za-z0-9_./-]+)"
)
FORBIDDEN_PARTS = {
    "__pycache__",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    ".venv",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".swp"}


def parse_frontmatter(path: Path) -> tuple[dict[str, str], str]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening YAML frontmatter marker")
    try:
        end = next(
            index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError("missing closing YAML frontmatter marker") from exc
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        data[key.strip()] = value
    return data, text


def _agentq_examples(text: str) -> list[str]:
    commands: list[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if pending:
            pending += " " + stripped
        elif stripped.startswith("agentq "):
            pending = stripped
        else:
            continue
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        commands.append(pending)
        pending = ""
    if pending:
        commands.append(pending)
    return commands


def _validate_agentq_examples(skills_root: Path, skills: list[Path]) -> list[str]:
    errors: list[str] = []
    scripts_dir = skills_root / "agent-toolkit" / "scripts"
    module_path = scripts_dir / "agentq.py"
    if not module_path.is_file():
        return ["cannot validate agentq examples: agentq.py missing"]
    sys.path.insert(0, str(scripts_dir))
    try:
        spec = importlib.util.spec_from_file_location(
            "agentq_validate_module", module_path
        )
        if spec is None or spec.loader is None:
            return ["cannot validate agentq examples: module loader unavailable"]
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        parser = module.build_parser()
        for skill in skills:
            text = (skill / "SKILL.md").read_text(encoding="utf-8")
            for command in _agentq_examples(text):
                try:
                    argv = shlex.split(command, comments=True)
                except ValueError as exc:
                    errors.append(
                        f"{skill.name}: malformed agentq example {command!r}: {exc}"
                    )
                    continue
                if len(argv) < 2 or any("<command>" in token for token in argv):
                    continue
                try:
                    parser.parse_args(argv[1:])
                except (module.AgentQError, SystemExit) as exc:
                    if isinstance(exc, SystemExit) and exc.code == 0:
                        continue
                    errors.append(
                        f"{skill.name}: invalid agentq example {command!r}: {exc}"
                    )
    finally:
        try:
            sys.path.remove(str(scripts_dir))
        except ValueError:
            pass
    return errors


def main() -> int:
    skills_root = Path(__file__).resolve().parents[2]
    errors: list[str] = []
    warnings: list[str] = []
    skills = []

    for child in sorted(skills_root.iterdir()):
        if not child.is_dir():
            warnings.append(f"unexpected top-level file: {child.name}")
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.is_file():
            errors.append(f"{child.name}: missing SKILL.md")
            continue
        skills.append(child)
        try:
            frontmatter, text = parse_frontmatter(skill_file)
        except ValueError as exc:
            errors.append(f"{child.name}: {exc}")
            continue
        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")
        if name != child.name:
            errors.append(f"{child.name}: frontmatter name is {name!r}")
        if not NAME_RE.fullmatch(name):
            errors.append(f"{child.name}: invalid skill name")
        if not description:
            errors.append(f"{child.name}: missing description")
        elif len(description) > 1024:
            errors.append(f"{child.name}: description exceeds 1024 characters")
        if len(text.splitlines()) > 500:
            errors.append(f"{child.name}: SKILL.md exceeds 500 lines")
        if len(text.split()) > 5500:
            errors.append(
                f"{child.name}: SKILL.md is too large for progressive disclosure"
            )
        for resource in RESOURCE_RE.findall(text):
            candidate = child / resource.rstrip(".,;:)")
            if not candidate.exists():
                errors.append(
                    f"{child.name}: referenced resource does not exist: {resource}"
                )

    for path in skills_root.rglob("*"):
        rel = path.relative_to(skills_root)
        if any(part in FORBIDDEN_PARTS for part in rel.parts):
            errors.append(f"forbidden generated/cache path: {rel}")
        if path.is_file() and path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden generated file: {rel}")
        if path.is_symlink():
            warnings.append(f"symlink present; verify portability: {rel}")

    python_files = sorted(skills_root.rglob("*.py"))
    if python_files:
        with tempfile.TemporaryDirectory(prefix="skill-pycache-") as cache:
            env = os.environ.copy()
            env["PYTHONPYCACHEPREFIX"] = cache
            # Out-of-scope subprocess use: repository validation probes, not
            # agent command lifecycles.
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", *map(str, python_files)],
                text=True,
                capture_output=True,
                env=env,
            )
            if result.returncode != 0:
                errors.append(
                    "Python compilation failed: "
                    + (result.stderr or result.stdout).strip()
                )

    shell_files = []
    for path in skills_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            first = path.open("r", encoding="utf-8", errors="ignore").readline()
        except OSError:
            continue
        if path.suffix == ".sh" or "bash" in first:
            shell_files.append(path)
        if (
            path.parent.name == "scripts"
            and path.suffix not in {".py", ".md", ".ts"}
            and os.access(path, os.X_OK) is False
        ):
            errors.append(f"non-executable script: {path.relative_to(skills_root)}")
    for path in shell_files:
        result = subprocess.run(
            ["bash", "-n", str(path)], text=True, capture_output=True
        )
        if result.returncode != 0:
            errors.append(
                f"shell syntax failed: {path.relative_to(skills_root)}: {(result.stderr or result.stdout).strip()}"
            )

    agentq = skills_root / "agent-toolkit" / "scripts" / "agentq"
    if not agentq.is_file() or not os.access(agentq, os.X_OK):
        errors.append("agent-toolkit/scripts/agentq is missing or not executable")
    else:
        result = subprocess.run(
            [str(agentq), "--version"], text=True, capture_output=True
        )
        if result.returncode != 0:
            errors.append("agentq --version failed")
        else:
            errors.extend(_validate_agentq_examples(skills_root, skills))

    print(f"skills: {len(skills)}")
    print(f"python files: {len(python_files)}")
    print(f"shell files: {len(shell_files)}")
    for warning in warnings:
        print(f"WARN: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    print("validation: " + ("PASS" if not errors else "FAIL"))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
