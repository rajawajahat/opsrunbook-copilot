"""Unit tests for Step Functions collector (parsing / bounding)."""
from collectors.stepfunctions.sfn_client import (
    FailedExecution,
    _truncate,
    MAX_EXECUTIONS,
    MAX_ERROR_LENGTH,
    FAILED_STATUSES,
)


def test_truncate_none():
    assert _truncate(None, 100) is None


def test_truncate_short():
    assert _truncate("hello", 100) == "hello"


def test_truncate_long():
    s = "x" * 2000
    result = _truncate(s, 100)
    assert len(result) == 100 + len("...[truncated]")
    assert result.endswith("...[truncated]")


def test_failed_execution_dataclass():
    ex = FailedExecution(
        execution_arn="arn:aws:states:us-east-1:123:execution:sm:run-1",
        state_machine_arn="arn:aws:states:us-east-1:123:stateMachine:sm",
        name="run-1",
        status="FAILED",
        start_date="2026-02-17T10:00:00+00:00",
        stop_date="2026-02-17T10:01:00+00:00",
        error="SomeError",
        cause="Something went wrong",
        last_failed_state="ProcessOrder",
    )
    assert ex.status == "FAILED"
    assert ex.last_failed_state == "ProcessOrder"


def test_failed_statuses():
    assert "FAILED" in FAILED_STATUSES
    assert "TIMED_OUT" in FAILED_STATUSES
    assert "ABORTED" in FAILED_STATUSES


def test_max_constants():
    assert MAX_EXECUTIONS == 20
    assert MAX_ERROR_LENGTH == 1000
