"""Data models for the coding agent."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ProposedEdit(BaseModel):
    """A single code edit proposed by the agent."""
    file_path: str
    old_code: str = Field(description="Exact code to replace (copied from read_file output)")
    new_code: str = Field(description="Replacement code")
    rationale: str = Field(description="Why this change fixes the issue")


class AgentResult(BaseModel):
    """Final output of an agent run."""
    incident_id: str
    repo: str
    summary: str = Field(description="Agent's final summary of investigation and changes")
    proposed_edits: list[ProposedEdit] = Field(default_factory=list)
    iterations: int = 0
    tool_calls: list[dict] = Field(default_factory=list)
