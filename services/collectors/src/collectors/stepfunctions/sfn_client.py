"""Step Functions failures collector."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import boto3


MAX_EXECUTIONS = 20
MAX_ERROR_LENGTH = 1000
MAX_HISTORY_EVENTS = 50
FAILED_STATUSES = {"FAILED", "TIMED_OUT", "ABORTED"}


@dataclass(frozen=True)
class FailedExecution:
    execution_arn: str
    state_machine_arn: str
    name: str
    status: str
    start_date: str
    stop_date: Optional[str]
    error: Optional[str]
    cause: Optional[str]
    last_failed_state: Optional[str]


@dataclass(frozen=True)
class StepFnResult:
    executions: list[FailedExecution]
    truncated: bool
    total_found: int


class StepFunctionsClient:
    """Collects failed Step Functions executions within a time window."""

    def __init__(self, region: str):
        self._client = boto3.client("stepfunctions", region_name=region)

    def get_failed_executions(
        self,
        *,
        state_machine_arns: list[str],
        time_window_start: datetime,
        time_window_end: datetime,
        max_executions: int = MAX_EXECUTIONS,
    ) -> StepFnResult:
        if not state_machine_arns:
            return StepFnResult(executions=[], truncated=False, total_found=0)

        all_failed: list[FailedExecution] = []
        overall_truncated = False

        for sm_arn in state_machine_arns:
            for status in FAILED_STATUSES:
                execs = self._list_executions(
                    state_machine_arn=sm_arn,
                    status=status,
                    time_window_start=time_window_start,
                    time_window_end=time_window_end,
                )
                all_failed.extend(execs)

        all_failed.sort(key=lambda e: e.start_date, reverse=True)
        total_found = len(all_failed)

        if len(all_failed) > max_executions:
            all_failed = all_failed[:max_executions]
            overall_truncated = True

        enriched = []
        for ex in all_failed:
            enriched.append(self._enrich_execution(ex))

        return StepFnResult(
            executions=enriched,
            truncated=overall_truncated,
            total_found=total_found,
        )

    def _list_executions(
        self,
        *,
        state_machine_arn: str,
        status: str,
        time_window_start: datetime,
        time_window_end: datetime,
    ) -> list[FailedExecution]:
        results: list[FailedExecution] = []
        next_token: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "stateMachineArn": state_machine_arn,
                "statusFilter": status,
                "maxResults": 100,
            }
            if next_token:
                kwargs["nextToken"] = next_token

            resp = self._client.list_executions(**kwargs)

            for ex in resp.get("executions", []):
                start = ex.get("startDate")
                stop = ex.get("stopDate")
                if start and start < time_window_start:
                    return results
                if start and start > time_window_end:
                    continue

                results.append(
                    FailedExecution(
                        execution_arn=ex["executionArn"],
                        state_machine_arn=state_machine_arn,
                        name=ex.get("name", ""),
                        status=ex.get("status", status),
                        start_date=start.isoformat() if isinstance(start, datetime) else str(start),
                        stop_date=stop.isoformat() if isinstance(stop, datetime) else (str(stop) if stop else None),
                        error=None,
                        cause=None,
                        last_failed_state=None,
                    )
                )

            next_token = resp.get("nextToken")
            if not next_token:
                break

        return results

    def _enrich_execution(self, ex: FailedExecution) -> FailedExecution:
        """Fetch error/cause from DescribeExecution + last failed state from history."""
        try:
            desc = self._client.describe_execution(executionArn=ex.execution_arn)
            error = _truncate(desc.get("error"), MAX_ERROR_LENGTH)
            cause = _truncate(desc.get("cause"), MAX_ERROR_LENGTH)
        except Exception:
            error = ex.error
            cause = ex.cause

        last_failed_state = self._get_last_failed_state(ex.execution_arn)

        return FailedExecution(
            execution_arn=ex.execution_arn,
            state_machine_arn=ex.state_machine_arn,
            name=ex.name,
            status=ex.status,
            start_date=ex.start_date,
            stop_date=ex.stop_date,
            error=error,
            cause=cause,
            last_failed_state=last_failed_state,
        )

    def _get_last_failed_state(self, execution_arn: str) -> Optional[str]:
        """Walk execution history (bounded) to find the last TaskFailed / ExecutionFailed state."""
        try:
            resp = self._client.get_execution_history(
                executionArn=execution_arn,
                maxResults=MAX_HISTORY_EVENTS,
                reverseOrder=True,
            )
            for evt in resp.get("events", []):
                etype = evt.get("type", "")
                if "Failed" in etype or "TimedOut" in etype or "Aborted" in etype:
                    details = (
                        evt.get("taskFailedEventDetails")
                        or evt.get("executionFailedEventDetails")
                        or evt.get("lambdaFunctionFailedEventDetails")
                        or {}
                    )
                    state_name = evt.get("previousEventId")
                    if "name" in details:
                        return details["name"]
                    # Try to find state name from stateEnteredEventDetails in nearby events
                    for prev in resp.get("events", []):
                        if prev.get("type") == "TaskStateEntered":
                            sd = prev.get("stateEnteredEventDetails", {})
                            if sd.get("name"):
                                return sd["name"]
                    return etype
        except Exception:
            pass
        return None


def _truncate(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...[truncated]"
