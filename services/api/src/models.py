"""
API-layer request/response models for Iteration 2.

Extends the v1 contract with optional hints for metrics and Step Functions
while keeping packages/contracts/ untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.incident_event_v1 import TimeWindowV1


# ---------------------------------------------------------------------------
# Extended Hints (superset of IncidentHintsV1)
# ---------------------------------------------------------------------------

class MetricQueryHint(BaseModel):
    namespace: str
    metric_name: str
    dimensions: dict[str, str] = Field(default_factory=dict)
    period: int = Field(default=300, ge=60, description="Period in seconds (min 60)")
    stat: str = Field(default="Average", description="CloudWatch statistic: Average, Sum, Maximum, Minimum, p99, …")


class ExtendedHints(BaseModel):
    """Backward-compatible hints: log_groups only for iter-1, all three for iter-2."""
    log_groups: list[str] = Field(default_factory=list)
    metric_queries: list[MetricQueryHint] = Field(default_factory=list)
    state_machine_arns: list[str] = Field(default_factory=list)

    @field_validator("log_groups")
    @classmethod
    def clean_log_groups(cls, v: list[str]) -> list[str]:
        return [x.strip() for x in v if x and x.strip()]

    @model_validator(mode="after")
    def at_least_one_hint(self) -> "ExtendedHints":
        if not self.log_groups and not self.metric_queries and not self.state_machine_arns:
            raise ValueError("hints must contain at least one of: log_groups, metric_queries, state_machine_arns")
        return self


# ---------------------------------------------------------------------------
# Request model (mirrors IncidentEventV1 but with ExtendedHints)
# ---------------------------------------------------------------------------

class CreateIncidentRequest(BaseModel):
    schema_version: Literal["incident_event.v1"] = "incident_event.v1"
    event_id: str = Field(min_length=8)
    incident_id: Optional[str] = None
    tenant_id: Optional[str] = None
    source: str = Field(default="manual")
    service: str = Field(min_length=1)
    environment: str = Field(default="dev")
    severity: Optional[Literal["info", "warning", "critical"]] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    time_window: TimeWindowV1
    hints: ExtendedHints


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class EvidenceRef(BaseModel):
    collector_type: str
    s3_bucket: str
    s3_key: str
    sha256: str
    byte_size: int
    truncated: bool = False


class EvidenceSummary(BaseModel):
    """Optional evidence summary in POST response (present when snapshot exists)."""
    bucket: str = ""
    key: str = ""
    sha256: str = ""
    byte_size: int = 0
    truncated: bool = False


class CreateIncidentResponse(BaseModel):
    ok: bool = True
    incident_id: str
    execution_arn: str
    collector_run_id: str
    snapshot_sk: Optional[str] = None
    evidence: Optional[EvidenceSummary] = None


class RunStatusResponse(BaseModel):
    ok: bool = True
    incident_id: str
    collector_run_id: str
    execution_arn: str
    status: str  # RUNNING, SUCCEEDED, FAILED, TIMED_OUT, ABORTED
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    error: Optional[str] = None
