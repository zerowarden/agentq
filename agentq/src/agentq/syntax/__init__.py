"""Language syntax analysis shared below discovery and navigation.

Consumers import the package surface: ``from agentq.syntax import ...``.
"""

from __future__ import annotations

from .models import OutlineParseError, OutlineSymbol
from .python import (
    MAX_PARSE_ERRORS,
    class_signature,
    collect_python_files,
    definitions_from_tree,
    function_signature,
    matching_definitions,
    parse_python,
    python_coverage,
    reference_kind,
)

__all__ = [
    "MAX_PARSE_ERRORS",
    "OutlineParseError",
    "OutlineSymbol",
    "class_signature",
    "collect_python_files",
    "definitions_from_tree",
    "function_signature",
    "matching_definitions",
    "parse_python",
    "python_coverage",
    "reference_kind",
]
