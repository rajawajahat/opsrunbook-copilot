from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class SignatureCountV1(BaseModel):
    signature: str
    count: int = Field(ge=0)


class EvidenceItemV1(BaseModel):
    backend: Literal["cloudwatch_logs"] = "cloudwatch_logs"

    s3_bucket: str
    s3_key: str

    byte_size: int = Field(ge=0)
    sha256: str = Field(min_length=32)

    truncated: bool = False
    notes: Optional[str] = None


class EvidenceSnapshotV1(BaseModel):
    """
    Stored artifact for Iteration 1.
    The evidence content lives in S3. DynamoDB stores this metadata.
    """

    schema_version: Literal["evidence_snapshot.v1"] = "evidence_snapshot.v1"

    incident_id: str
    collector_run_id: str

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Useful for UI / auditing
    service: str
    environment: str
    time_window_start: datetime
    time_window_end: datetime

    top_signatures: list[SignatureCountV1] = Field(default_factory=list)
    evidence_items: list[EvidenceItemV1] = Field(default_factory=list)
