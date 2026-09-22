"""Lexical fallback provider backed by bounded repository search."""

from __future__ import annotations

from agentq.core import LEXICAL
from agentq.discovery import SearchRequest, SearchResult, search

from ..models import EvidencePage, NavigationRequest, SymbolEvidence


def lexical_evidence(result: SearchResult) -> SymbolEvidence:
    """Normalize a bounded search result into canonical symbol evidence.

    Lexical hits are not declaration candidates, so ``candidate_count`` stays
    zero; they surface as the fallback reference page instead.
    """
    return SymbolEvidence(
        provider=LexicalFallbackProvider.name,
        provenance=LEXICAL,
        coverage=result.coverage,
        references=EvidencePage(
            results=result.hits,
            shown=result.shown,
            total=result.total_matching_lines,
            truncated=result.truncated,
        ),
        payload=result,
    )


def lexical_payload(evidence: SymbolEvidence) -> SearchResult | None:
    """The adapter-private search payload behind normalized evidence."""
    payload = evidence.payload
    return payload if isinstance(payload, SearchResult) else None


class LexicalFallbackProvider:
    name = "lexical"
    provenance = LEXICAL

    def supports(self, request: NavigationRequest) -> bool:
        return True

    def _search(self, request: NavigationRequest) -> SearchResult:
        return search(
            SearchRequest(
                root=request.root,
                query=request.symbol,
                scopes=request.paths,
                mode="fixed",
                word=False,
                case="smart",
                limit=request.limit,
                per_file=8,
                context=request.context,
                max_chars=240,
                include_sensitive=False,
                view="auto",
                max_files=40,
            )
        )

    def inspect_symbol(
        self, request: NavigationRequest, *, include_references: bool
    ) -> SymbolEvidence:
        return lexical_evidence(self._search(request))
