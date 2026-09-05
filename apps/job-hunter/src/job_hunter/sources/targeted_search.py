from __future__ import annotations

from dataclasses import dataclass, field

from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.models import Job, SearchQuery
from job_hunter.search_backend import SearchBackend, SearchBudgetExhausted

from .base import logger


@dataclass(slots=True)
class TargetedSearchStats:
    planned_by_market: dict[str, int] = field(default_factory=dict)
    attempted_by_market: dict[str, int] = field(default_factory=dict)
    succeeded_by_market: dict[str, int] = field(default_factory=dict)
    results_by_market: dict[str, int] = field(default_factory=dict)


def _normalize_query(query: str | SearchQuery) -> SearchQuery:
    if isinstance(query, SearchQuery):
        return query
    return SearchQuery(text=query)


def _bump(counts: dict[str, int], key: str, amount: int = 1) -> None:
    counts[key] = counts.get(key, 0) + amount


class TargetedSearchSource:
    def __init__(
        self,
        backend: SearchBackend,
        queries: list[str | SearchQuery],
        breaker: CircuitBreaker | None = None,
        *,
        source_label: str | None = None,
    ) -> None:
        self._backend = backend
        self._queries = [_normalize_query(query) for query in queries]
        self._breaker = breaker
        self._source_label = source_label
        self.stats = TargetedSearchStats()
        for query in self._queries:
            _bump(self.stats.planned_by_market, query.market_id or "legacy")

    def discover(self) -> list[Job]:
        jobs: list[Job] = []
        for index, query in enumerate(self._queries):
            market_key = query.market_id or "legacy"
            if self._breaker is not None and self._breaker.is_open:
                if self._breaker.claim_open_notice():
                    logger.warning(
                        "targeted search circuit open; skipping %s remaining queries",
                        len(self._queries) - index,
                    )
                break

            _bump(self.stats.attempted_by_market, market_key)
            try:
                response = self._backend.search(query.text)
            except SearchBudgetExhausted:
                logger.info(
                    "targeted search budget exhausted; skipping %s remaining queries",
                    len(self._queries) - index,
                )
                break
            except Exception:
                logger.warning(
                    "targeted search query failed: market=%s query=%s",
                    market_key,
                    query.text,
                    exc_info=True,
                )
                if self._breaker is not None:
                    self._breaker.record_failure()
                continue

            if self._breaker is not None:
                self._breaker.record_success()
            _bump(self.stats.succeeded_by_market, market_key)
            _bump(self.stats.results_by_market, market_key, len(response.hits))

            source = self._source_label or f"search:{response.backend}"
            for hit in response.hits:
                jobs.append(
                    Job(
                        source=source,
                        title=hit.title,
                        url=hit.url,
                        market_hint=query.market_id,
                    )
                )
        return jobs
