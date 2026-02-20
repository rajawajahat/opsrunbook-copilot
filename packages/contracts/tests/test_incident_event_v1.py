from datetime import datetime, timezone, timedelta

import pytest
from contracts.incident_event_v1 import IncidentEventV1


def test_requires_timezone():
    start = datetime(2026, 2, 15, 10, 0, 0)  # naive
    end = datetime(2026, 2, 15, 10, 5, 0, tzinfo=timezone.utc)
    with pytest.raises(Exception):
        IncidentEventV1(
            event_id="evt-12345678",
            service="demo",
            environment="dev",
            time_window={"start": start, "end": end},
            hints={"log_groups": ["/aws/lambda/demo"]},
        )


def test_requires_log_groups():
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    end = datetime.now(timezone.utc)
    with pytest.raises(Exception):
        IncidentEventV1(
            event_id="evt-12345678",
            service="demo",
            environment="dev",
            time_window={"start": start, "end": end},
            hints={"log_groups": ["   ", ""]},
        )


def test_valid_event():
    start = datetime.now(timezone.utc) - timedelta(minutes=10)
    end = datetime.now(timezone.utc)
    evt = IncidentEventV1(
        event_id="evt-12345678",
        service="demo",
        environment="dev",
        time_window={"start": start, "end": end},
        hints={"log_groups": ["/aws/lambda/demo"]},
    )
    assert evt.hints.log_groups == ["/aws/lambda/demo"]
