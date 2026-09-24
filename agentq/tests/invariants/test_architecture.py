"""Architectural dependency invariants.

These tests make the target dependency shape executable:

* the CLI is a leaf adapter: no non-adapter module imports ``agentq.cli``
* core is foundational: ``agentq.core`` imports nothing outside ``agentq.core``
* persistence does not depend on the CLI
* capability functions do not accept ``argparse.Namespace``
* capability functions do not write to stdout/stderr

Inspection is static (AST); capability modules are never imported by the
tests themselves.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

import agentq

PACKAGE_DIR = Path(agentq.__file__).resolve().parent
ROOT = "agentq"
CLI_ROOT = "agentq.cli"
CORE_ROOT = "agentq.core"
PERSISTENCE_ROOTS = ("agentq.persistence",)
ENTRY_MODULE = "agentq.__main__"
_NAMESPACE_RE = re.compile(r"\bNamespace\b")
_WRITE_TARGETS = frozenset({"sys.stdout", "sys.stderr", "stdout", "stderr"})


def _iter_modules() -> list[tuple[str, Path, str]]:
    """Every production module as (module name, path, containing package)."""
    items: list[tuple[str, Path, str]] = []
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(PACKAGE_DIR)
        if relative.name == "__init__.py":
            parts = relative.parts[:-1]
            module = ROOT if not parts else f"{ROOT}.{'.'.join(parts)}"
            package = module
        else:
            module = f"{ROOT}.{'.'.join(relative.with_suffix('').parts)}"
            package = module.rsplit(".", 1)[0]
        items.append((module, path, package))
    return items


def _resolve_relative(package: str, level: int, module: str | None) -> str | None:
    parts = package.split(".")
    if level > len(parts):
        return None
    base = ".".join(parts[: len(parts) - level + 1])
    return f"{base}.{module}" if module else base


def _imports_from_source(source: str, package: str) -> set[str]:
    """First-party imports referenced by source, resolved to absolute paths."""
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(
                alias.name
                for alias in node.names
                if alias.name == ROOT or alias.name.startswith(f"{ROOT}.")
            )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(package, node.level, node.module)
            elif node.module and (
                node.module == ROOT or node.module.startswith(f"{ROOT}.")
            ):
                base = node.module
            else:
                base = None
            if base is None:
                continue
            imports.add(base)
            imports.update(
                f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
            )
    return imports


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}.")


def _is_adapter(module: str) -> bool:
    return module == ENTRY_MODULE or _under(module, CLI_ROOT)


def _namespace_parameters(source: str) -> list[str]:
    """Functions whose signature annotates a parameter as a Namespace."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            if argument.annotation is None:
                continue
            annotation = ast.unparse(argument.annotation)
            if _NAMESPACE_RE.search(annotation):
                found.append(f"{node.name}({argument.arg}: {annotation})")
    return found


def _write_calls(source: str) -> list[str]:
    """Direct print()/stdout/stderr writes in a module body."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id == "print":
            found.append("print")
        elif isinstance(function, ast.Attribute) and function.attr == "write":
            target = ast.unparse(function.value)
            if target in _WRITE_TARGETS:
                found.append(f"{target}.write")
    return found


def _purity_modules() -> list[tuple[str, Path, str]]:
    """Capability/foundational modules subject to signature and output rules."""
    return [item for item in _iter_modules() if not _is_adapter(item[0])]


class CliLeafTests(unittest.TestCase):
    def test_non_adapter_modules_do_not_import_cli(self) -> None:
        offenders: dict[str, list[str]] = {}
        for module, path, package in _iter_modules():
            if _is_adapter(module):
                continue
            hits = sorted(
                item
                for item in _imports_from_source(path.read_text(), package)
                if _under(item, CLI_ROOT)
            )
            if hits:
                offenders[module] = hits
        self.assertFalse(offenders, f"modules importing the CLI adapter: {offenders}")


class CoreBoundaryTests(unittest.TestCase):
    def test_core_modules_do_not_import_outside_core(self) -> None:
        offenders: dict[str, list[str]] = {}
        for module, path, package in _iter_modules():
            if not _under(module, CORE_ROOT):
                continue
            outside = sorted(
                item
                for item in _imports_from_source(path.read_text(), package)
                if not _under(item, CORE_ROOT)
            )
            if outside:
                offenders[module] = outside
        self.assertFalse(offenders, f"core modules importing non-core: {offenders}")


class PersistenceBoundaryTests(unittest.TestCase):
    def test_persistence_modules_do_not_import_cli(self) -> None:
        checked = 0
        for module, path, package in _iter_modules():
            if not any(_under(module, root) for root in PERSISTENCE_ROOTS):
                continue
            checked += 1
            hits = sorted(
                item
                for item in _imports_from_source(path.read_text(), package)
                if _under(item, CLI_ROOT)
            )
            self.assertFalse(hits, f"{module} imports the CLI adapter: {hits}")
        self.assertGreaterEqual(checked, 1, "no persistence modules found")


class CapabilitySignatureTests(unittest.TestCase):
    def test_capability_functions_do_not_accept_argparse_namespace(self) -> None:
        offenders: dict[str, list[str]] = {}
        for module, path, _package in _purity_modules():
            found = _namespace_parameters(path.read_text())
            if found:
                offenders[module] = found
        self.assertFalse(offenders, f"functions accepting Namespace: {offenders}")


class CapabilityOutputTests(unittest.TestCase):
    def test_capability_functions_do_not_write_stdout_or_stderr(self) -> None:
        offenders: dict[str, list[str]] = {}
        for module, path, _package in _purity_modules():
            found = _write_calls(path.read_text())
            if found:
                offenders[module] = found
        self.assertFalse(offenders, f"modules writing output: {offenders}")


DISCOVERY_ROOT = "agentq.discovery"
NAVIGATION_ROOT = "agentq.navigation"
SYNTAX_ROOT = "agentq.syntax"
DELIVERY_ROOT = "agentq.delivery"
INSPECTION_ROOT = "agentq.inspection"
INSPECTION_ADAPTERS_ROOT = "agentq.inspection.adapters"
INSPECTION_FORBIDDEN_ROOTS = (
    CLI_ROOT,
    "agentq.discovery",
    "agentq.workspace",
    "agentq.navigation",
    "agentq.delivery",
    "agentq.persistence",
    "agentq.execution",
)
INSPECTION_ADAPTER_FORBIDDEN_ROOTS = (
    CLI_ROOT,
    "agentq.execution",
    "agentq.persistence",
    "agentq.telemetry",
    "agentq.delivery",
)
TEXT_HELPERS = frozenset({"compact_line", "strip_ansi"})
CONCRETE_PAYLOAD_NAMES = frozenset(
    {
        "TypeScriptNav",
        "TypeScriptCandidateSearch",
        "TypeScriptSymbolOverview",
        "TypeScriptLocations",
        "TypeScriptLocation",
        "TypeScriptSection",
        "PythonOverview",
        "PythonReference",
        "PythonReferenceSection",
    }
)


class DependencyDirectionTests(unittest.TestCase):
    def test_discovery_does_not_import_navigation(self) -> None:
        offenders: dict[str, list[str]] = {}
        for module, path, package in _iter_modules():
            if not _under(module, DISCOVERY_ROOT):
                continue
            hits = sorted(
                item
                for item in _imports_from_source(path.read_text(), package)
                if _under(item, NAVIGATION_ROOT)
            )
            if hits:
                offenders[module] = hits
        self.assertFalse(offenders, f"discovery importing navigation: {offenders}")

    def test_syntax_sits_below_discovery_and_navigation(self) -> None:
        offenders: dict[str, list[str]] = {}
        for module, path, package in _iter_modules():
            if not _under(module, SYNTAX_ROOT):
                continue
            hits = sorted(
                item
                for item in _imports_from_source(path.read_text(), package)
                if _under(item, DISCOVERY_ROOT) or _under(item, NAVIGATION_ROOT)
            )
            if hits:
                offenders[module] = hits
        self.assertFalse(offenders, f"syntax importing capabilities: {offenders}")

    def test_capabilities_do_not_import_text_helpers_from_delivery(self) -> None:
        offenders: dict[str, list[str]] = {}
        for module, path, package in _iter_modules():
            if _is_adapter(module) or _under(module, DELIVERY_ROOT):
                continue
            hits = sorted(
                item
                for item in _imports_from_source(path.read_text(), package)
                if _under(item, DELIVERY_ROOT)
                and item.rsplit(".", 1)[-1] in TEXT_HELPERS
            )
            if hits:
                offenders[module] = hits
        self.assertFalse(
            offenders, f"modules importing text helpers from delivery: {offenders}"
        )


class NavigationProviderBoundaryTests(unittest.TestCase):
    def test_navigation_is_a_low_level_boundary(self) -> None:
        offenders: dict[str, list[str]] = {}
        for module, path, package in _iter_modules():
            if not _under(module, NAVIGATION_ROOT):
                continue
            hits = sorted(
                item
                for item in _imports_from_source(path.read_text(), package)
                if _under(item, CLI_ROOT) or _under(item, INSPECTION_ROOT)
            )
            if hits:
                offenders[module] = hits
        self.assertFalse(
            offenders, f"navigation importing CLI or inspection: {offenders}"
        )


class CoreOperationRegistryTests(unittest.TestCase):
    def test_core_does_not_enumerate_operations(self) -> None:
        source = (PACKAGE_DIR / "core/request.py").read_text()
        self.assertNotIn("KNOWN_OPERATIONS", source)


class InspectionBoundaryTests(unittest.TestCase):
    """The inspection domain depends on core and itself, nothing more."""

    def test_inspection_domain_imports_only_core_and_itself(self) -> None:
        offenders: dict[str, list[str]] = {}
        checked = 0
        for module, path, package in _iter_modules():
            if not _under(module, INSPECTION_ROOT):
                continue
            if _under(module, INSPECTION_ADAPTERS_ROOT):
                continue
            checked += 1
            outside = sorted(
                item
                for item in _imports_from_source(path.read_text(), package)
                if not _under(item, CORE_ROOT) and not _under(item, INSPECTION_ROOT)
            )
            if outside:
                offenders[module] = outside
        self.assertGreaterEqual(checked, 10, "inspection package modules not found")
        self.assertFalse(
            offenders, f"inspection domain importing outside its boundary: {offenders}"
        )

    def test_inspection_adapters_do_not_import_cli_or_execution(self) -> None:
        offenders: dict[str, list[str]] = {}
        checked = 0
        for module, path, package in _iter_modules():
            if not _under(module, INSPECTION_ADAPTERS_ROOT):
                continue
            checked += 1
            hits = sorted(
                item
                for item in _imports_from_source(path.read_text(), package)
                if any(
                    _under(item, root) for root in INSPECTION_ADAPTER_FORBIDDEN_ROOTS
                )
            )
            if hits:
                offenders[module] = hits
        self.assertGreaterEqual(checked, 1, "inspection adapters package not found")
        self.assertFalse(
            offenders, f"inspection adapters importing forbidden roots: {offenders}"
        )

    def test_service_never_imports_adapters_or_payloads(self) -> None:
        path = PACKAGE_DIR / "inspection/service.py"
        imports = _imports_from_source(path.read_text(), INSPECTION_ROOT)
        offenders = sorted(
            item
            for item in imports
            if _under(item, INSPECTION_ADAPTERS_ROOT)
            or item.rsplit(".", 1)[-1] in CONCRETE_PAYLOAD_NAMES
        )
        self.assertFalse(
            offenders, f"service importing concrete adapters/payloads: {offenders}"
        )

    def test_service_does_not_branch_on_language_names(self) -> None:
        source = (PACKAGE_DIR / "inspection/service.py").read_text().lower()
        for token in ("typescript", "javascript", "python", "tsx", "jsx"):
            self.assertNotIn(token, source)


class DetectorSanityTests(unittest.TestCase):
    """A broken architecture detector is dangerous; spot-check each one."""

    def test_detectors_recognize_representative_cases(self) -> None:
        with self.subTest(detector="first-party imports"):
            cli_import = _imports_from_source("from .cli.main import main", ROOT)
            self.assertTrue(any(_under(item, CLI_ROOT) for item in cli_import))
            core_relative = _imports_from_source(
                "from .evidence import Coverage", CORE_ROOT
            )
            self.assertFalse(
                [item for item in core_relative if not _under(item, CORE_ROOT)]
            )
            capability_leak = _imports_from_source(
                "from ..search import search_data", CORE_ROOT
            )
            self.assertTrue(
                [item for item in capability_leak if not _under(item, CORE_ROOT)]
            )

        with self.subTest(detector="namespace annotations"):
            found = _namespace_parameters(
                "def explicit(args: argparse.Namespace): ...\n"
                "def bare(args: Namespace): ...\n"
                "def plain(args): ...\n"
                "def annotated_ok(args: str): ...\n"
            )
            self.assertEqual(len(found), 2)

        with self.subTest(detector="stdout/stderr writes"):
            writes = _write_calls(
                "print('x')\nsys.stdout.write('x')\nlogger.write('x')\n"
            )
            self.assertEqual(writes, ["print", "sys.stdout.write"])
