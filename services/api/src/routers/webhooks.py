"""
GitHub webhook endpoint – Iteration 6.

POST /v1/webhooks/github  → receive GitHub App events, verify signature,
                             normalize, persist, and trigger PR review cycle.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import boto3
from fastapi import APIRouter, Header, HTTPException, Request, Response

from src.settings import load_settings
from src.stores.s3_store import S3EvidenceStore
from src.stores.webhook_dedupe_store import WebhookDedupeStore

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

_settings = None
_s3 = None
_dedupe = None
_sfn = None


def _init():
    global _settings, _s3, _dedupe, _sfn
    if _settings is None:
        _settings = load_settings()
        _s3 = S3EvidenceStore(region=_settings.aws_region)
        _dedupe = WebhookDedupeStore(_settings.incidents_table, _settings.aws_region)
        _sfn = boto3.client("stepfunctions", region_name=_settings.aws_region)

WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
PR_REVIEW_STATE_MACHINE_ARN = os.getenv("PR_REVIEW_STATE_MACHINE_ARN", "")
GITHUB_APP_SLUG = os.getenv("GITHUB_APP_SLUG", "opsrunbook-copilot-bot")

_SUPPORTED_EVENTS = {
    "issue_comment",
    "pull_request_review",
    "pull_request_review_comment",
    "pull_request",
}


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _normalize_event(event_type: str, delivery_id: str, body: dict) -> dict:
    """Produce github_pr_review_event.v1 from raw webhook payload."""
    action = body.get("action", "")
    sender = body.get("sender", {})
    repo = body.get("repository", {})
    installation = body.get("installation", {})

    # Extract PR number depending on event type
    pr_number = None
    pr_url = ""
    if event_type == "issue_comment":
        issue = body.get("issue", {})
        pr_number = issue.get("number")
        pr_url = issue.get("pull_request", {}).get("html_url", "") or issue.get("html_url", "")
    elif event_type in ("pull_request_review", "pull_request_review_comment"):
        pr = body.get("pull_request", {})
        pr_number = pr.get("number")
        pr_url = pr.get("html_url", "")
    elif event_type == "pull_request":
        pr = body.get("pull_request", {})
        pr_number = pr.get("number")
        pr_url = pr.get("html_url", "")

    # Comment body
    comment_body = ""
    comment_url = ""
    if event_type == "issue_comment":
        comment = body.get("comment", {})
        comment_body = comment.get("body", "")
        comment_url = comment.get("html_url", "")
    elif event_type == "pull_request_review_comment":
        comment = body.get("comment", {})
        comment_body = comment.get("body", "")
        comment_url = comment.get("html_url", "")
    elif event_type == "pull_request_review":
        review = body.get("review", {})
        comment_body = review.get("body", "") or ""
        comment_url = review.get("html_url", "")

    # Inline context for review comments
    inline_context = None
    if event_type == "pull_request_review_comment":
        comment = body.get("comment", {})
        inline_context = {
            "path": comment.get("path", ""),
            "position": comment.get("position"),
            "original_position": comment.get("original_position"),
            "line": comment.get("line"),
            "original_line": comment.get("original_line"),
            "side": comment.get("side", ""),
            "diff_hunk": (comment.get("diff_hunk") or "")[:2000],
        }

    # Review state
    review_state = None
    if event_type == "pull_request_review":
        review_state = body.get("review", {}).get("state", "")

    return {
        "schema_version": "github_pr_review_event.v1",
        "delivery_id": delivery_id,
        "event_type": event_type,
        "action": action,
        "pr_number": pr_number,
        "repo_full_name": repo.get("full_name", ""),
        "installation_id": installation.get("id"),
        "sender_login": sender.get("login", ""),
        "comment_body": comment_body[:4000],
        "comment_url": comment_url,
        "pr_url": pr_url,
        "inline_context": inline_context,
        "review_state": review_state,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/github", status_code=202)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str = Header(None, alias="x-hub-signature-256"),
    x_github_event: str = Header(None, alias="x-github-event"),
    x_github_delivery: str = Header(None, alias="x-github-delivery"),
):
    _init()
    raw_body = await request.body()

    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="webhook secret not configured")

    if not _verify_signature(raw_body, x_hub_signature_256 or "", WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="invalid signature")

    if not x_github_event or not x_github_delivery:
        raise HTTPException(status_code=400, detail="missing required GitHub headers")

    # Dedupe check
    if _dedupe.already_processed(x_github_delivery):
        return {"ok": True, "delivery_id": x_github_delivery, "status": "already_processed"}

    body = json.loads(raw_body)

    # Persist raw payload to S3
    repo_name = body.get("repository", {}).get("full_name", "unknown").replace("/", "_")
    s3_key = f"webhooks/github/{repo_name}/{x_github_delivery}.json"
    raw_meta = {
        "delivery_id": x_github_delivery,
        "event_type": x_github_event,
        "action": body.get("action", ""),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "repository": body.get("repository", {}).get("full_name", ""),
        "installation_id": body.get("installation", {}).get("id"),
        "sender_login": body.get("sender", {}).get("login", ""),
    }
    _s3.put_json(
        bucket=_settings.evidence_bucket,
        key=s3_key,
        payload={"metadata": raw_meta, "payload": body},
    )

    # Skip unsupported events early
    if x_github_event not in _SUPPORTED_EVENTS:
        _dedupe.mark_processed(x_github_delivery, outcome="skipped_unsupported_event")
        return {"ok": True, "delivery_id": x_github_delivery, "status": "skipped", "reason": "unsupported_event"}

    # Skip if not PR-related (issue_comment on non-PR issue)
    if x_github_event == "issue_comment" and not body.get("issue", {}).get("pull_request"):
        _dedupe.mark_processed(x_github_delivery, outcome="skipped_not_pr")
        return {"ok": True, "delivery_id": x_github_delivery, "status": "skipped", "reason": "not_a_pr"}

    # Normalize
    normalized = _normalize_event(x_github_event, x_github_delivery, body)

    # Self-event detection
    sender_login = normalized["sender_login"].lower()
    if sender_login.endswith("[bot]") or sender_login == GITHUB_APP_SLUG.lower():
        _dedupe.mark_processed(x_github_delivery, outcome="skipped_self_event")
        return {"ok": True, "delivery_id": x_github_delivery, "status": "skipped", "reason": "self_event"}

    # Command controls
    comment_lower = normalized["comment_body"].lower().strip()
    if "/copilot stop" in comment_lower:
        _dedupe.mark_processed(x_github_delivery, outcome="copilot_paused")
        _dedupe.set_pr_paused(
            normalized["repo_full_name"], normalized["pr_number"], paused=True
        )
        return {"ok": True, "delivery_id": x_github_delivery, "status": "paused"}

    if "/copilot resume" in comment_lower:
        _dedupe.set_pr_paused(
            normalized["repo_full_name"], normalized["pr_number"], paused=False
        )
        _dedupe.mark_processed(x_github_delivery, outcome="copilot_resumed")
        return {"ok": True, "delivery_id": x_github_delivery, "status": "resumed"}

    # Check if PR is paused
    if _dedupe.is_pr_paused(normalized["repo_full_name"], normalized["pr_number"]):
        _dedupe.mark_processed(x_github_delivery, outcome="skipped_paused")
        return {"ok": True, "delivery_id": x_github_delivery, "status": "skipped", "reason": "pr_paused"}

    # Trigger PR review cycle Step Function
    execution_arn = ""
    if PR_REVIEW_STATE_MACHINE_ARN:
        normalized["raw_payload_ref"] = {
            "s3_bucket": _settings.evidence_bucket,
            "s3_key": s3_key,
        }
        try:
            resp = _sfn.start_execution(
                stateMachineArn=PR_REVIEW_STATE_MACHINE_ARN,
                name=f"pr-review-{x_github_delivery}",
                input=json.dumps(normalized, default=str),
            )
            execution_arn = resp.get("executionArn", "")
        except Exception as e:
            if "ExecutionAlreadyExists" in str(e):
                pass
            else:
                raise

    _dedupe.mark_processed(x_github_delivery, outcome="dispatched")

    return {
        "ok": True,
        "delivery_id": x_github_delivery,
        "status": "accepted",
        "execution_arn": execution_arn,
    }
