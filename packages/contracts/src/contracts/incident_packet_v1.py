"""IncidentPacketV1 – structured analysis output (Iteration 3)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


class PacketEvidenceRef(BaseModel):
    collector_type: str
    s3_bucket: str
    s3_key: str
    sha256: str = ""
    byte_size: int = 0
    truncated: bool = False


class SnapshotRef(BaseModel):
    s3_bucket: str
    s3_key: str
    sha256: str = ""


class Finding(BaseModel):
    id: str
    summary: str
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[PacketEvidenceRef] = Field(default_factory=list)
    notes: Optional[str] = None

    @model_validator(mode="after")
    def high_confidence_needs_evidence(self) -> "Finding":
        if self.confidence > 0.6 and not self.evidence_refs:
            raise ValueError(
                f"Finding '{self.id}' has confidence {self.confidence} > 0.6 "
                "but no evidence_refs — high-confidence findings MUST cite evidence"
            )
        return self


class Hypothesis(BaseModel):
    summary: str
    confidence: float = Field(ge=0, le=1)
    evidence_refs: list[PacketEvidenceRef] = Field(default_factory=list)


class NextAction(BaseModel):
    summary: str
    commands: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    evidence_refs: list[PacketEvidenceRef] = Field(default_factory=list)


class SuspectedOwner(BaseModel):
    repo: str
    confidence: float = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list)


class ModelTrace(BaseModel):
    provider: str = "stub"
    model: Optional[str] = None
    prompt_version: str = "v1"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PacketHashes(BaseModel):
    sha256: str


class IncidentPacketV1(BaseModel):
    schema_version: Literal["incident_packet.v1"] = "incident_packet.v1"

    incident_id: str
    collector_run_id: str
    service: str
    environment: str = "dev"
    time_window: dict = Field(default_factory=dict)

    snapshot_ref: SnapshotRef

    findings: list[Finding] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)
    suspected_owners: list[SuspectedOwner] = Field(default_factory=list)
    limits: list[str] = Field(default_factory=list)

    model_trace: ModelTrace = Field(default_factory=ModelTrace)
    packet_hashes: Optional[PacketHashes] = None
    all_evidence_refs: list[PacketEvidenceRef] = Field(default_factory=list)
