"""Unit tests for CloudWatch Metrics collector (parsing / bounding)."""
from datetime import datetime, timedelta, timezone

from collectors.cloudwatch.metrics_client import (
    MetricQuery,
    _auto_period,
    _compute_summary,
    MAX_METRIC_DATA_POINTS,
    MAX_METRIC_QUERIES,
)


def test_auto_period_short_window():
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=5)
    p = _auto_period(start, end)
    assert p == 60


def test_auto_period_long_window():
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    p = _auto_period(start, end)
    assert p >= 300


def test_compute_summary_empty():
    s = _compute_summary([])
    assert s["count"] == 0
    assert s["min"] is None


def test_compute_summary_values():
    s = _compute_summary([1.0, 2.0, 3.0, 10.0])
    assert s["min"] == 1.0
    assert s["max"] == 10.0
    assert s["avg"] == 4.0
    assert s["count"] == 4


def test_metric_query_defaults():
    q = MetricQuery(namespace="AWS/Lambda", metric_name="Errors")
    assert q.period == 300
    assert q.stat == "Average"
    assert q.dimensions == {}


def test_max_constants():
    assert MAX_METRIC_DATA_POINTS == 500
    assert MAX_METRIC_QUERIES == 20
