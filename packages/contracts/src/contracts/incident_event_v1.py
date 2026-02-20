from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class TimeWindowV1(BaseModel):
    start: datetime
    end: datetime

    @field_validator("start", "end")
    @classmethod
    def require_tz(cls, v: datetime) -> datetime:
        # Force timezone-aware times so CloudWatch queries are unambiguous
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware (e.g., 2026-02-15T12:00:00Z)")
        return v

    @model_validator(mode="after")
    def validate_order(self) -> "TimeWindowV1":
        if self.end <= self.start:
            raise ValueError("time_window.end must be after time_window.start")
        return self


class IncidentHintsV1(BaseModel):
    # Iteration 1 requires CloudWatch log groups
    log_groups: list[str] = Field(min_length=1)

    @field_validator("log_groups")
    @classmethod
    def non_empty_names(cls, v: list[str]) -> list[str]:
        cleaned = [x.strip() for x in v if x and x.strip()]
        if not cleaned:
            raise ValueError("hints.log_groups must contain at least one non-empty log group name")
        return cleaned


class IncidentEventV1(BaseModel):
    """
    Public input contract (v1).
    This is what alerting systems (or any client) sends to the entry endpoint.
    """

    schema_version: Literal["incident_event.v1"] = "incident_event.v1"

    # Provided by caller for dedupe/idempotency (recommended)
    event_id: str = Field(min_length=8)

    # Optional: if not provided, API can generate an incident_id
    incident_id: Optional[str] = None

    tenant_id: Optional[str] = None

    source: str = Field(default="manual", description="e.g., cloudwatch, newrelic, datadog, manual")
    service: str = Field(min_length=1, description="logical service/app name")
    environment: str = Field(default="dev", description="dev/stage/prod etc.")

    severity: Optional[Literal["info", "warning", "critical"]] = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    time_window: TimeWindowV1

    hints: IncidentHintsV1
