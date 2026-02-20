"""pr_fix_plan.v1 – LLM-generated fix plan for a PR review comment."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class ProposedEdit(BaseModel):
    file_path: str
    change_type: str = "edit"  # edit | create
    patch: str = ""
    instructions: str = ""
    rationale: str = ""


class PRFixPlanV1(BaseModel):
    schema_version: str = Field(default="pr_fix_plan.v1", frozen=True)
    delivery_id: str
    pr_number: int
    repo_full_name: str
    summary: str
    proposed_edits: list[ProposedEdit] = Field(default_factory=list)
    risk_level: str = "low"  # low | medium | high
    requires_human: bool = False
    model_trace: Optional[dict] = None
    created_at: Optional[str] = None
