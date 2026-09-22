"""Navigation providers: Python, TypeScript, and lexical fallback.

This is the adapter layer: it owns the concrete payload classes and exposes
only canonical :class:`~agentq.navigation.models.SymbolEvidence` plus
provider-dispatched wire projection and rendering to the orchestration layer.
"""

from __future__ import annotations

from agentq.core import RenderedText, rendered_text
from agentq.discovery import SearchResult, render_search

from ..models import (
    TS_NAV_TYPES,
    NavigationPayload,
    PythonOverview,
    SymbolEvidence,
)
from .lexical import (
    LexicalFallbackProvider,
    lexical_evidence,
    lexical_payload,
)
from .python import (
    PythonProvider,
    python_evidence,
    python_payload,
    python_symbol_overview,
    render_python_overview,
)
from .typescript import (
    TypeScriptProvider,
    render_ts_nav,
    ts_nav,
    ts_nav_from_payload,
    typescript_evidence,
    typescript_payload,
)


def navigation_payload(evidence: SymbolEvidence) -> NavigationPayload | None:
    """The concrete provider payload behind normalized evidence."""
    if evidence.provider == TypeScriptProvider.name:
        return typescript_payload(evidence)
    if evidence.provider == PythonProvider.name:
        return python_payload(evidence)
    if evidence.provider == LexicalFallbackProvider.name:
        return lexical_payload(evidence)
    return None


def render_navigation(
    payload: NavigationPayload | None, *, budget: int = 0
) -> RenderedText:
    """Render one provider payload through its owning renderer."""
    if isinstance(payload, TS_NAV_TYPES):
        return render_ts_nav(payload, budget=budget)
    if isinstance(payload, PythonOverview):
        return render_python_overview(payload, budget=budget)
    if isinstance(payload, SearchResult):
        rendered = render_search(payload, budget=budget)
        return (
            rendered
            if isinstance(rendered, RenderedText)
            else rendered_text(str(rendered))
        )
    return rendered_text("")


__all__ = [
    "LexicalFallbackProvider",
    "PythonProvider",
    "TypeScriptProvider",
    "lexical_evidence",
    "lexical_payload",
    "navigation_payload",
    "python_evidence",
    "python_payload",
    "python_symbol_overview",
    "render_navigation",
    "render_python_overview",
    "render_ts_nav",
    "ts_nav",
    "ts_nav_from_payload",
    "typescript_evidence",
    "typescript_payload",
]
