import logging

from job_hunter.search_backend import (
    FallbackSearchBackend,
    SearchBudgetExhausted,
    SearchResponse,
)


class _StubBackend:
    def __init__(self, name, *, error=None):
        self.name = name
        self._error = error
        self.calls = 0

    def search(self, query):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return SearchResponse(hits=[], backend=self.name)


def test_budget_exhausted_fallback_log_names_source_and_target(caplog):
    primary = _StubBackend("brave", error=SearchBudgetExhausted("out of budget"))
    secondary = _StubBackend("duckduckgo")

    with caplog.at_level(logging.INFO):
        response = FallbackSearchBackend(primary, secondary).search("frontend london")

    assert response.backend == "duckduckgo"
    assert secondary.calls == 1
    assert (
        "targeted search budget exhausted; falling back: from=brave to=duckduckgo"
        in caplog.text
    )
    assert "backend=brave" not in caplog.text


def test_backend_failure_fallback_log_names_source_and_target(caplog):
    primary = _StubBackend("brave", error=RuntimeError("boom"))
    secondary = _StubBackend("duckduckgo")

    with caplog.at_level(logging.INFO):
        response = FallbackSearchBackend(primary, secondary).search("frontend london")

    assert response.backend == "duckduckgo"
    assert (
        "targeted search backend failed; falling back: from=brave to=duckduckgo"
        in caplog.text
    )
    assert "backend=brave" not in caplog.text
