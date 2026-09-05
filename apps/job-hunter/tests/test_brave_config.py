from job_hunter.sources import _brave_monthly_query_limit


def test_brave_monthly_query_limit_defaults_to_1000(monkeypatch):
    monkeypatch.delenv("BRAVE_MONTHLY_QUERY_LIMIT", raising=False)

    assert _brave_monthly_query_limit() == 1000
