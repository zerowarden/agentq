"""Lexical fallback provider backed by bounded repository search."""

from __future__ import annotations

from agentq.core import LEXICAL
from agentq.discovery import SearchRequest, SearchResult, search

from ..models import NavigationPayload, NavigationRequest


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

    def locate(self, request: NavigationRequest) -> NavigationPayload:
        return self._search(request)

    def overview(self, request: NavigationRequest) -> NavigationPayload:
        return self._search(request)
