"""Unit tests for API-layer models (Iteration 2)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.models import (
    CreateIncidentRequest,
    ExtendedHints,
    MetricQueryHint,
    EvidenceRef,
)


def test_extended_hints_log_groups_only():
    h = ExtendedHints(log_groups=["/aws/lambda/demo"])
    assert h.log_groups == ["/aws/lambda/demo"]
    assert h.metric_queries == []
    assert h.state_machine_arns == []


def test_extended_hints_metrics_only():
    h = ExtendedHints(
        metric_queries=[
            MetricQueryHint(namespace="AWS/Lambda", metric_name="Errors")
        ]
    )
    assert len(h.metric_queries) == 1
    assert h.log_groups == []


def test_extended_hints_requires_at_least_one():
    with pytest.raises(Exception):
        ExtendedHints()


def test_extended_hints_cleans_log_groups():
    h = ExtendedHints(log_groups=["  /aws/lambda/x  ", "  ", "/aws/lambda/y"])
    assert h.log_groups == ["/aws/lambda/x", "/aws/lambda/y"]


def test_metric_query_hint_defaults():
    mq = MetricQueryHint(namespace="AWS/Lambda", metric_name="Duration")
    assert mq.period == 300
    assert mq.stat == "Average"
    assert mq.dimensions == {}


def test_create_incident_request_valid():
    now = datetime.now(timezone.utc)
    req = CreateIncidentRequest(
        event_id="evt-12345678",
        service="demo",
        environment="dev",
        time_window={"start": now - timedelta(minutes=10), "end": now},
        hints={"log_groups": ["/aws/lambda/demo"]},
    )
    assert req.schema_version == "incident_event.v1"
    assert len(req.hints.log_groups) == 1


def test_create_incident_request_all_hints():
    now = datetime.now(timezone.utc)
    req = CreateIncidentRequest(
        event_id="evt-12345678",
        service="demo",
        time_window={"start": now - timedelta(minutes=10), "end": now},
        hints={
            "log_groups": ["/aws/lambda/demo"],
            "metric_queries": [
                {"namespace": "AWS/Lambda", "metric_name": "Errors"},
            ],
            "state_machine_arns": [
                "arn:aws:states:us-east-1:123456:stateMachine:my-sm",
            ],
        },
    )
    assert len(req.hints.log_groups) == 1
    assert len(req.hints.metric_queries) == 1
    assert len(req.hints.state_machine_arns) == 1


def test_evidence_ref():
    ref = EvidenceRef(
        collector_type="logs",
        s3_bucket="my-bucket",
        s3_key="evidence/inc-123/run-1/logs.json",
        sha256="a" * 64,
        byte_size=1234,
    )
    assert ref.truncated is False
