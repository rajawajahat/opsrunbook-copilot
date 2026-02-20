"""CloudWatch Metrics collector using GetMetricData API."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import boto3


MAX_METRIC_DATA_POINTS = 500
MAX_METRIC_QUERIES = 20


@dataclass(frozen=True)
class MetricQuery:
    namespace: str
    metric_name: str
    dimensions: dict[str, str] = field(default_factory=dict)
    period: int = 300  # seconds
    stat: str = "Average"


@dataclass(frozen=True)
class MetricTimeSeries:
    query_id: str
    label: str
    timestamps: list[str]
    values: list[float]
    stat: str
    period: int
    point_count: int
    truncated: bool
    summary: dict[str, Any]


@dataclass(frozen=True)
class MetricsResult:
    series: list[MetricTimeSeries]
    truncated: bool


def _auto_period(start: datetime, end: datetime, desired_points: int = 300) -> int:
    """Pick a period that keeps data points under desired_points."""
    span_seconds = int((end - start).total_seconds())
    if span_seconds <= 0:
        return 60
    raw = span_seconds // desired_points
    # Round up to nearest valid CW period (60, 300, 3600, …)
    for p in [60, 300, 900, 3600, 21600, 86400]:
        if p >= raw:
            return p
    return 86400


class CloudWatchMetricsClient:
    """Wrapper over CloudWatch GetMetricData with bounded output."""

    def __init__(self, region: str):
        self._client = boto3.client("cloudwatch", region_name=region)

    def get_metric_data(
        self,
        *,
        queries: list[MetricQuery],
        start_time: datetime,
        end_time: datetime,
        max_points: int = MAX_METRIC_DATA_POINTS,
    ) -> MetricsResult:
        if not queries:
            return MetricsResult(series=[], truncated=False)

        bounded_queries = queries[:MAX_METRIC_QUERIES]
        truncated_queries = len(queries) > MAX_METRIC_QUERIES

        metric_data_queries = []
        for idx, q in enumerate(bounded_queries):
            qid = f"m{idx}"
            period = q.period if q.period >= 60 else _auto_period(start_time, end_time)
            dims = [{"Name": k, "Value": v} for k, v in q.dimensions.items()]
            metric_data_queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": q.namespace,
                            "MetricName": q.metric_name,
                            "Dimensions": dims,
                        },
                        "Period": period,
                        "Stat": q.stat,
                    },
                    "ReturnData": True,
                }
            )

        all_results: list[dict[str, Any]] = []
        next_token: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "MetricDataQueries": metric_data_queries,
                "StartTime": start_time,
                "EndTime": end_time,
            }
            if next_token:
                kwargs["NextToken"] = next_token

            resp = self._client.get_metric_data(**kwargs)
            all_results.extend(resp.get("MetricDataResults", []))
            next_token = resp.get("NextToken")
            if not next_token:
                break

        series_list: list[MetricTimeSeries] = []
        overall_truncated = truncated_queries

        for r in all_results:
            timestamps = [t.isoformat() if isinstance(t, datetime) else str(t) for t in r.get("Timestamps", [])]
            values = list(r.get("Values", []))

            series_truncated = len(values) > max_points
            if series_truncated:
                timestamps = timestamps[:max_points]
                values = values[:max_points]
                overall_truncated = True

            summary = _compute_summary(values)
            qid = r.get("Id", "")
            idx = int(qid[1:]) if qid.startswith("m") and qid[1:].isdigit() else 0
            orig_q = bounded_queries[idx] if idx < len(bounded_queries) else bounded_queries[0]

            series_list.append(
                MetricTimeSeries(
                    query_id=qid,
                    label=r.get("Label", orig_q.metric_name),
                    timestamps=timestamps,
                    values=values,
                    stat=orig_q.stat,
                    period=orig_q.period,
                    point_count=len(values),
                    truncated=series_truncated,
                    summary=summary,
                )
            )

        return MetricsResult(series=series_list, truncated=overall_truncated)


def _compute_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"min": None, "max": None, "avg": None, "count": 0}
    return {
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "avg": round(sum(values) / len(values), 6),
        "count": len(values),
    }
