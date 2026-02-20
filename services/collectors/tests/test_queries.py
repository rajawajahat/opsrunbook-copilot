from collectors.cloudwatch.queries import TOP_ERRORS_QUERY, RECENT_ERRORS_QUERY


def test_queries_are_not_empty():
    assert "filter" in TOP_ERRORS_QUERY
    assert "limit" in TOP_ERRORS_QUERY
    assert "filter" in RECENT_ERRORS_QUERY
    assert "limit" in RECENT_ERRORS_QUERY
