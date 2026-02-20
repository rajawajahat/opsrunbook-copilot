from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import boto3


@dataclass(frozen=True)
class QueryResult:
    query_id: str
    status: str
    rows: list[dict[str, Any]]
    stats: dict[str, Any]


class CloudWatchInsightsClient:
    """
    Wrapper over CloudWatch Logs Insights.

    Responsibilities:
    - Start a query over one or more log groups
    - Poll until completion (or timeout)
    - Normalize results into list[dict] rows
    """

    def __init__(self, region: str):
        self._client = boto3.client("logs", region_name=region)

    def start_query(
        self,
        *,
        log_groups: list[str],
        query: str,
        start_time: datetime,
        end_time: datetime,
        limit: int = 100,
    ) -> str:
        # CloudWatch expects epoch seconds
        start_epoch = int(start_time.timestamp())
        end_epoch = int(end_time.timestamp())

        resp = self._client.start_query(
            logGroupNames=log_groups,
            startTime=start_epoch,
            endTime=end_epoch,
            queryString=query,
            limit=limit,
        )
        return resp["queryId"]

    def wait_for_results(
        self,
        *,
        query_id: str,
        timeout_seconds: int = 30,
        poll_interval_seconds: float = 1.0,
    ) -> QueryResult:
        deadline = time.time() + timeout_seconds
        last_resp: dict[str, Any] | None = None

        while time.time() < deadline:
            resp = self._client.get_query_results(queryId=query_id)
            last_resp = resp
            status = resp.get("status", "Unknown")

            if status in ("Complete", "Failed", "Cancelled", "Timeout"):
                return QueryResult(
                    query_id=query_id,
                    status=status,
                    rows=self._normalize_rows(resp.get("results", [])),
                    stats=resp.get("statistics", {}),
                )

            time.sleep(poll_interval_seconds)

        # Timeout from our client side
        if last_resp is None:
            last_resp = self._client.get_query_results(queryId=query_id)

        return QueryResult(
            query_id=query_id,
            status="ClientTimeout",
            rows=self._normalize_rows(last_resp.get("results", [])),
            stats=last_resp.get("statistics", {}),
        )

    @staticmethod
    def _normalize_rows(results: list[list[dict[str, str]]]) -> list[dict[str, Any]]:
        """
        CloudWatch returns rows like:
          [[{"field":"@timestamp","value":"..."}, {"field":"@message","value":"..."}], ...]
        Convert to:
          [{"@timestamp":"...", "@message":"..."}, ...]
        """
        out: list[dict[str, Any]] = []
        for row in results:
            item: dict[str, Any] = {}
            for cell in row:
                field = cell.get("field")
                if field:
                    item[field] = cell.get("value")
            out.append(item)
        return out
