"""github_pr_review_event.v1 – normalized GitHub PR review event."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class InlineContext(BaseModel):
    path: str = ""
    position: Optional[int] = None
    original_position: Optional[int] = None
    line: Optional[int] = None
    original_line: Optional[int] = None
    side: str = ""
    diff_hunk: str = ""


class GitHubPRReviewEventV1(BaseModel):
    schema_version: str = Field(default="github_pr_review_event.v1", frozen=True)
    delivery_id: str
    event_type: str
    action: str = ""
    pr_number: Optional[int] = None
    repo_full_name: str
    installation_id: Optional[int] = None
    sender_login: str
    comment_body: str = ""
    comment_url: str = ""
    pr_url: str = ""
    inline_context: Optional[InlineContext] = None
    review_state: Optional[str] = None
    received_at: Optional[str] = None
