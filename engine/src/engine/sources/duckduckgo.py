from __future__ import annotations

from engine.circuit_breaker import CircuitBreaker
from engine.models import SearchQuery
from engine.search_backend import DuckDuckGoSearchBackend

from .targeted_search import TargetedSearchSource, TargetedSearchStats

DuckDuckGoStats = TargetedSearchStats


class DuckDuckGoSource(TargetedSearchSource):
    """Backward-compatible zero-key targeted search source."""

    def __init__(
        self,
        http,
        queries: list[str | SearchQuery],
        breaker: CircuitBreaker | None = None,
    ) -> None:
        super().__init__(
            DuckDuckGoSearchBackend(http),
            queries,
            breaker=breaker,
            source_label="duckduckgo",
        )
