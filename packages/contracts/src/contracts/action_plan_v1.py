"""ActionPlan + ActionResult schemas – Iteration 4."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PlannedAction(BaseModel):
    action_type: Literal["create_jira_ticket", "notify_teams"]
    priority: Literal["P0", "P1", "P2"] = "P2"
    title: str
    description_md: str = ""
    evidence_refs: list[dict] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    dry_run: bool = True


class ActionPlanV1(BaseModel):
    schema_version: Literal["incident_action_plan.v1"] = "incident_action_plan.v1"
    incident_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    environment: str = "dev"
    service: str = ""
    suspected_owners: list[dict] = Field(default_factory=list)
    actions: list[PlannedAction] = Field(default_factory=list)


class ActionResultV1(BaseModel):
    schema_version: Literal["incident_action_result.v1"] = "incident_action_result.v1"
    incident_id: str
    action_id: str
    action_type: str
    status: Literal["success", "failed", "skipped"] = "success"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    request_summary: str = ""
    response_summary: str = ""
    external_refs: dict = Field(default_factory=dict)
    error: Optional[str] = None
    cause: Optional[str] = None
    evidence_refs: list[dict] = Field(default_factory=list)
