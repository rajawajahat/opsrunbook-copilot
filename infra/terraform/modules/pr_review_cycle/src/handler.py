"""
PR Review Cycle Lambda – Iteration 6.

Single handler dispatches to step-specific functions based on the
"step" field in the payload. Used by the pr_review_cycle state machine.
"""
import json
import os
import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import boto3

from github_ops import GitHubOps
from patcher import apply_patch_plan, PatchResult
from code_context import (
    build_code_context,
    build_code_context_from_text,
    extract_file_line_from_event,
    CodeContext,
    format_snippet,
)

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")

EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "")
INCIDENTS_TABLE = os.environ.get("INCIDENTS_TABLE", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_APP_SLUG = os.environ.get("GITHUB_APP_SLUG", "opsrunbook-copilot-bot")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "stub")
ALLOWED_PATHS = os.environ.get("GITHUB_ALLOWED_PATHS", ".opsrunbook/,src/,config/").split(",")
MAX_FILES_PER_EVENT = int(os.environ.get("MAX_FILES_PER_EVENT", "5"))
MAX_BYTES_PER_FILE = int(os.environ.get("MAX_BYTES_PER_FILE", "204800"))

SSM_GITHUB_TOKEN = os.environ.get("SSM_GITHUB_TOKEN", "/opsrunbook/dev/github/token")
SSM_GITHUB_APP_ID = os.environ.get("SSM_GITHUB_APP_ID", "/opsrunbook/dev/github/app_id")
SSM_GITHUB_APP_INSTALL_ID = os.environ.get("SSM_GITHUB_APP_INSTALL_ID", "/opsrunbook/dev/github/app_installation_id")
SSM_GITHUB_APP_PEM = os.environ.get("SSM_GITHUB_APP_PEM", "/opsrunbook/dev/github/app_private_key_pem")

_PLACEHOLDER = {"REPLACE_ME", "replace_me", "placeholder", ""}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_ssm(name: str, decrypt: bool = False) -> str | None:
    try:
        resp = ssm.get_parameter(Name=name, WithDecryption=decrypt)
        val = resp["Parameter"]["Value"]
        if not val:
            return val
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, str) else val
        except (json.JSONDecodeError, TypeError):
            return val
    except Exception:
        return None


def _real_or_none(val: str | None) -> str | None:
    if not val or val.strip() in _PLACEHOLDER:
        return None
    return val


def _normalize_pem(raw: str | None) -> str | None:
    import base64 as b64
    if not raw:
        return raw
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1]
    if not raw.startswith("-----BEGIN"):
        try:
            decoded = b64.b64decode(raw).decode("utf-8")
            if "-----BEGIN" in decoded:
                raw = decoded
        except Exception:
            pass
    if "\\n" in raw:
        raw = raw.replace("\\n", "\n")
    return raw


def _build_github_ops(installation_id: int | None = None) -> GitHubOps | None:
    if not GITHUB_OWNER:
        return None
    pat = _real_or_none(_get_ssm(SSM_GITHUB_TOKEN, decrypt=True))
    app_id = _real_or_none(_get_ssm(SSM_GITHUB_APP_ID))
    install_id = _real_or_none(_get_ssm(SSM_GITHUB_APP_INSTALL_ID))
    pem = _normalize_pem(_real_or_none(_get_ssm(SSM_GITHUB_APP_PEM, decrypt=True)))
    if installation_id:
        install_id = str(installation_id)
    if not pat and not (app_id and install_id and pem):
        return None
    return GitHubOps(
        owner=GITHUB_OWNER,
        pat=pat,
        app_id=app_id,
        installation_id=install_id,
        pem=pem,
    )


def _load_json_s3(bucket: str, key: str) -> dict:
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def _put_json_s3(bucket: str, key: str, data: dict) -> str:
    body = json.dumps(data, default=str, ensure_ascii=False, separators=(",", ":")).encode()
    sha = hashlib.sha256(body).hexdigest()
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return sha


# ── Step handlers ────────────────────────────────────────────────

def _step_load_pr_context(payload: dict) -> dict:
    event = payload["event"]
    repo_full = event["repo_full_name"]
    pr_number = event["pr_number"]
    installation_id = event.get("installation_id")

    parts = repo_full.split("/", 1)
    owner = parts[0] if len(parts) == 2 else GITHUB_OWNER
    repo = parts[1] if len(parts) == 2 else repo_full

    gh = _build_github_ops(installation_id)
    if not gh:
        raise RuntimeError("GitHub client not configured")

    pr_data = gh.get_pr(owner, repo, pr_number)
    pr_files = gh.get_pr_files(owner, repo, pr_number)

    head_ref = pr_data.get("head", {}).get("ref", "")

    pr_context = {
        "owner": owner,
        "repo": repo,
        "pr_number": pr_number,
        "title": pr_data.get("title", ""),
        "body": (pr_data.get("body") or "")[:4000],
        "state": pr_data.get("state", ""),
        "head_ref": head_ref,
        "head_sha": pr_data.get("head", {}).get("sha", ""),
        "base_ref": pr_data.get("base", {}).get("ref", ""),
        "labels": [l.get("name", "") for l in pr_data.get("labels", [])],
        "user_login": pr_data.get("user", {}).get("login", ""),
        "files": [
            {
                "filename": f.get("filename", ""),
                "status": f.get("status", ""),
                "patch": (f.get("patch") or "")[:3000],
            }
            for f in pr_files[:20]
        ],
        "code_contexts": [],
    }

    # Fetch code context for any file+line references in the event
    file_lines = extract_file_line_from_event(event)
    for fpath, line_num in file_lines[:3]:
        try:
            ctx = build_code_context(
                gh, owner, repo, fpath, head_ref, line_num, window=20,
            )
            pr_context["code_contexts"].append(ctx.to_dict())
        except Exception as e:
            print(json.dumps({
                "msg": "code_context_fetch_failed",
                "path": fpath, "line": line_num, "error": str(e)[:200],
            }))

    return {"event": event, "pr_context": pr_context}


def _step_guardrails_check(payload: dict) -> dict:
    event = payload["event"]
    pr_ctx = payload["pr_context"]

    # Only respond to PRs created by opsrunbook copilot
    pr_body = pr_ctx.get("body", "").lower()
    labels = [l.lower() for l in pr_ctx.get("labels", [])]
    user_login = pr_ctx.get("user_login", "").lower()

    is_ours = (
        "opsrunbook-copilot" in labels
        or "opsrunbook_copilot" in pr_body
        or GITHUB_APP_SLUG.lower() in user_login
        or user_login.endswith("[bot]")
    )

    if not is_ours:
        return {
            "event": event,
            "pr_context": pr_ctx,
            "guardrails": {"proceed": False, "reason": "PR not created by opsrunbook-copilot"},
        }

    # Skip if sender is the bot (avoid loops)
    sender = event.get("sender_login", "").lower()
    if sender.endswith("[bot]") or sender == GITHUB_APP_SLUG.lower():
        return {
            "event": event,
            "pr_context": pr_ctx,
            "guardrails": {"proceed": False, "reason": "sender is bot itself"},
        }

    # Stop phrase
    comment = event.get("comment_body", "").lower()
    if "/copilot stop" in comment:
        return {
            "event": event,
            "pr_context": pr_ctx,
            "guardrails": {"proceed": False, "reason": "stop command received"},
        }

    return {
        "event": event,
        "pr_context": pr_ctx,
        "guardrails": {"proceed": True, "reason": ""},
    }


def _step_build_review_packet(payload: dict) -> dict:
    event = payload["event"]
    pr_ctx = payload["pr_context"]
    delivery_id = event["delivery_id"]

    packet = {
        "schema_version": "pr_review_packet.v1",
        "delivery_id": delivery_id,
        "event": event,
        "pr_context": pr_ctx,
        "created_at": _now_iso(),
    }

    key = f"pr_review_packets/{pr_ctx['owner']}/{pr_ctx['repo']}/{delivery_id}.json"
    sha = _put_json_s3(EVIDENCE_BUCKET, key, packet)

    return {
        "event": event,
        "pr_context": pr_ctx,
        "review_packet_ref": {"s3_bucket": EVIDENCE_BUCKET, "s3_key": key, "sha256": sha},
    }


def _step_llm_plan_fix(payload: dict) -> dict:
    """Generate a fix plan using code context fetched in LoadPRContext."""
    event = payload["event"]
    pr_ctx = payload["pr_context"]
    comment = event.get("comment_body", "")
    code_contexts = pr_ctx.get("code_contexts", [])

    if LLM_PROVIDER == "stub":
        plan = _stub_plan_fix(event, pr_ctx, comment, code_contexts)
    else:
        plan = _stub_plan_fix(event, pr_ctx, comment, code_contexts)

    return {"event": event, "pr_context": pr_ctx, "fix_plan": plan}


def _stub_plan_fix(
    event: dict,
    pr_ctx: dict,
    comment: str,
    code_contexts: list[dict] | None = None,
) -> dict:
    """Deterministic stub that uses code context to produce targeted plans.

    When code_contexts are available the plan includes:
      - exact target file + line range to edit
      - the snippet as evidence
      - rationale linking the review comment to the code
      - a unified-diff patch when a simple textual fix is detected
    """
    delivery_id = event["delivery_id"]
    inline = event.get("inline_context") or {}
    code_contexts = code_contexts or []

    proposed_edits: list[dict] = []
    has_context = False

    # ── Path A: we have fetched code context ──────────────────────
    for ctx in code_contexts:
        has_context = True
        fpath = ctx["path"]
        target_line = ctx["target_line"]
        start_line = ctx["start_line"]
        end_line = ctx["end_line"]
        snippet = ctx["snippet"]
        file_sha = ctx.get("file_sha", "")

        # Try to extract a deterministic fix from the comment
        patch, instructions = _infer_fix_from_comment(comment, snippet, target_line)

        proposed_edits.append({
            "file_path": fpath,
            "change_type": "edit",
            "patch": patch,
            "instructions": instructions,
            "rationale": (
                f"Review comment on line {target_line}: \"{comment[:200]}\"\n"
                f"Code at lines {start_line}-{end_line}:\n{snippet[:1000]}"
            ),
            "target_line": target_line,
            "line_range": [start_line, end_line],
            "file_sha": file_sha,
        })

    # ── Path B: inline context but no fetched code_context ────────
    if not has_context and inline.get("path"):
        target_file = inline["path"]
        target_line = inline.get("line") or inline.get("original_line") or 1
        diff_hunk = inline.get("diff_hunk", "")

        patch, instructions = _infer_fix_from_comment(comment, diff_hunk, target_line)

        proposed_edits.append({
            "file_path": target_file,
            "change_type": "edit",
            "patch": patch,
            "instructions": instructions,
            "rationale": (
                f"Inline review comment on {target_file}:{target_line}: "
                f"\"{comment[:200]}\"\n"
                f"Diff hunk:\n{diff_hunk[:800]}"
            ),
            "target_line": target_line,
            "line_range": [max(1, target_line - 20), target_line + 20],
        })

    # ── Path C: bare file references in comment text ──────────────
    if not proposed_edits and comment:
        file_lines = extract_file_line_from_event(event)
        for fpath, line_num in file_lines[:3]:
            proposed_edits.append({
                "file_path": fpath,
                "change_type": "edit",
                "patch": "",
                "instructions": f"Address feedback at line {line_num}: {comment[:300]}",
                "rationale": f"File {fpath}:{line_num} referenced in comment",
                "target_line": line_num,
                "line_range": [max(1, line_num - 20), line_num + 20],
            })

    # Determine risk: low if we have context + a patch, otherwise medium/high
    has_patch = any(e.get("patch") for e in proposed_edits)
    if has_patch and has_context:
        risk = "low"
        requires_human = False
    elif has_context:
        risk = "medium"
        requires_human = True
    else:
        risk = "high" if not proposed_edits else "medium"
        requires_human = True

    return {
        "schema_version": "pr_fix_plan.v1",
        "delivery_id": delivery_id,
        "pr_number": pr_ctx.get("pr_number"),
        "repo_full_name": f"{pr_ctx.get('owner', '')}/{pr_ctx.get('repo', '')}",
        "summary": _build_plan_summary(proposed_edits, has_context, comment),
        "proposed_edits": proposed_edits,
        "risk_level": risk,
        "requires_human": requires_human,
        "model_trace": {
            "provider": "stub",
            "model": None,
            "code_contexts_used": len(code_contexts),
            "created_at": _now_iso(),
        },
        "created_at": _now_iso(),
    }


def _infer_fix_from_comment(comment: str, snippet: str, target_line: int) -> tuple[str, str]:
    """Try to produce a unified diff patch from common review comment patterns.

    Returns (patch, instructions). patch is empty string if no deterministic
    fix could be inferred.
    """
    comment_lower = comment.lower().strip()

    # Pattern: "replace X with Y" or "change X to Y"
    m = re.search(
        r'(?:replace|change)\s+[\'"](.+?)[\'"]\s+(?:with|to)\s+[\'"](.+?)[\'"]',
        comment, re.IGNORECASE,
    )
    if m:
        old_text, new_text = m.group(1), m.group(2)
        patch = _make_unified_diff(snippet, old_text, new_text, target_line)
        if patch:
            return patch, f'replace "{old_text}" with "{new_text}"'
        return "", f'replace "{old_text}" with "{new_text}"'

    # Pattern: "fix spelling of X" / "typo: X should be Y"
    m = re.search(
        r'(?:fix\s+spelling\s+(?:of\s+)?|typo:\s*)[\'"]?(\w+)[\'"]?\s+'
        r'(?:should\s+be|to|->|→)\s+[\'"]?(\w+)[\'"]?',
        comment, re.IGNORECASE,
    )
    if m:
        old_text, new_text = m.group(1), m.group(2)
        patch = _make_unified_diff(snippet, old_text, new_text, target_line)
        if patch:
            return patch, f'replace "{old_text}" with "{new_text}"'
        return "", f'replace "{old_text}" with "{new_text}"'

    # No deterministic fix detected
    return "", f"Address review feedback: {comment[:500]}"


def _make_unified_diff(
    snippet: str, old_text: str, new_text: str, target_line: int,
) -> str:
    """Produce a minimal unified diff hunk from a find-and-replace within a snippet.

    The snippet may have line-number prefixes (from format_snippet); we strip
    those before searching.
    """
    raw_lines = []
    for sline in snippet.split("\n"):
        # Strip "  N | " prefix if present
        idx = sline.find(" | ")
        if idx >= 0 and sline[:idx].strip().isdigit():
            raw_lines.append(sline[idx + 3:])
        else:
            raw_lines.append(sline)

    # Find the first line containing old_text
    match_idx = None
    for i, line in enumerate(raw_lines):
        if old_text in line:
            match_idx = i
            break

    if match_idx is None:
        return ""

    old_line = raw_lines[match_idx]
    new_line = old_line.replace(old_text, new_text, 1)

    # Compute the actual file line number
    # The snippet starts at some offset; target_line is roughly centred
    # We don't know exact start from snippet alone, but we can derive it
    # from the line-number prefixes if present
    first_num = None
    first_sline = snippet.split("\n")[0] if snippet else ""
    idx = first_sline.find(" | ")
    if idx >= 0:
        try:
            first_num = int(first_sline[:idx].strip())
        except ValueError:
            pass
    file_line = (first_num or max(1, target_line - 20)) + match_idx

    return (
        f"@@ -{file_line},1 +{file_line},1 @@\n"
        f"-{old_line}\n"
        f"+{new_line}"
    )


def _build_plan_summary(
    edits: list[dict], has_context: bool, comment: str,
) -> str:
    n = len(edits)
    files = ", ".join(e.get("file_path", "?") for e in edits[:3])
    has_patch = any(e.get("patch") for e in edits)

    if has_context and has_patch:
        return f"Context-grounded fix for {n} file(s) [{files}] with auto-generated patch"
    if has_context:
        return f"Code context extracted for {n} file(s) [{files}]; manual patch needed"
    if n:
        return f"{n} file(s) referenced in feedback [{files}]"
    return f"No file targets identified from comment: \"{comment[:80]}\""


def _step_apply_fix_safely(payload: dict) -> dict:
    event = payload["event"]
    pr_ctx = payload["pr_context"]
    fix_plan = payload["fix_plan"]

    if fix_plan.get("requires_human") or fix_plan.get("risk_level") == "high":
        return {
            "event": event,
            "pr_context": pr_ctx,
            "fix_plan": fix_plan,
            "apply_result": {
                "status": "deferred",
                "reason": "requires_human or high risk",
                "commit_sha": "",
                "updated_files": [],
            },
        }

    installation_id = event.get("installation_id")
    gh = _build_github_ops(installation_id)
    if not gh:
        return {
            "event": event,
            "pr_context": pr_ctx,
            "fix_plan": fix_plan,
            "apply_result": {"status": "failed", "reason": "github not configured", "commit_sha": "", "updated_files": []},
        }

    result = apply_patch_plan(
        gh=gh,
        owner=pr_ctx["owner"],
        repo=pr_ctx["repo"],
        branch=pr_ctx["head_ref"],
        plan=fix_plan,
        delivery_id=event["delivery_id"],
        allowed_paths=ALLOWED_PATHS,
        max_files=MAX_FILES_PER_EVENT,
        max_bytes=MAX_BYTES_PER_FILE,
    )

    return {
        "event": event,
        "pr_context": pr_ctx,
        "fix_plan": fix_plan,
        "apply_result": {
            "status": result.status,
            "reason": result.reason,
            "commit_sha": result.commit_sha,
            "updated_files": result.updated_files,
        },
    }


def _step_post_pr_comment(payload: dict) -> dict:
    event = payload["event"]
    pr_ctx = payload.get("pr_context", {})
    fix_plan = payload.get("fix_plan", {})
    apply_result = payload.get("apply_result", {})
    installation_id = event.get("installation_id")

    gh = _build_github_ops(installation_id)
    if not gh:
        return {
            "event": event,
            "apply_result": apply_result,
            "comment_result": {"status": "skipped", "reason": "github not configured"},
        }

    owner = pr_ctx.get("owner", "")
    repo = pr_ctx.get("repo", "")
    pr_number = event.get("pr_number")
    status = apply_result.get("status", "unknown")
    delivery_id = event.get("delivery_id", "")

    lines = [f"**OpsRunbook Copilot** — review response `{delivery_id[:12]}`", ""]

    if status == "success":
        lines.append(f"Applied fix in commit `{apply_result.get('commit_sha', '')[:12]}`")
        for f in apply_result.get("updated_files", []):
            lines.append(f"- `{f}`")
        lines.append("")
        lines.append("Please verify the changes and re-review.")
    elif status == "deferred":
        lines.append("This change requires human review. The fix plan has been recorded but no code was pushed.")
        summary = fix_plan.get("summary", "")
        if summary:
            lines.append(f"\n> {summary}")
        edits = fix_plan.get("proposed_edits", [])
        if edits:
            lines.append("\n**Files referenced:**")
            for e in edits[:5]:
                lines.append(f"- `{e.get('file_path', '')}`: {e.get('rationale', '')[:100]}")
    else:
        lines.append(f"Status: `{status}` — {apply_result.get('reason', '')}")

    lines.append(f"\n---\n_delivery: {delivery_id}_")
    body = "\n".join(lines)

    comment_resp = gh.create_pr_comment(owner, repo, pr_number, body)

    return {
        "event": event,
        "apply_result": apply_result,
        "comment_result": {
            "status": "posted",
            "comment_url": comment_resp.get("html_url", ""),
            "comment_id": comment_resp.get("id"),
        },
    }


def _step_persist_outcome(payload: dict) -> dict:
    event = payload.get("event", {})
    apply_result = payload.get("apply_result", {})
    comment_result = payload.get("comment_result") or {}
    delivery_id = event.get("delivery_id", "")

    if INCIDENTS_TABLE:
        table = dynamodb.Table(INCIDENTS_TABLE)
        created_at = _now_iso()
        action_id = uuid4().hex[:12]

        table.put_item(Item={
            "pk": f"WEBHOOK#PR_REVIEW#{event.get('repo_full_name', '')}#{event.get('pr_number', '')}",
            "sk": f"OUTCOME#{created_at}#{delivery_id}",
            "delivery_id": delivery_id,
            "action_type": "respond_to_pr_review",
            "status": apply_result.get("status", "unknown"),
            "commit_sha": apply_result.get("commit_sha", ""),
            "comment_url": comment_result.get("comment_url", ""),
            "created_at": created_at,
        })

    return {
        "ok": True,
        "delivery_id": delivery_id,
        "status": apply_result.get("status", "unknown"),
    }


# ── Dispatcher ───────────────────────────────────────────────────

_STEPS = {
    "load_pr_context": _step_load_pr_context,
    "guardrails_check": _step_guardrails_check,
    "build_review_packet": _step_build_review_packet,
    "llm_plan_fix": _step_llm_plan_fix,
    "apply_fix_safely": _step_apply_fix_safely,
    "post_pr_comment": _step_post_pr_comment,
    "persist_outcome": _step_persist_outcome,
}


def lambda_handler(event: dict, context: Any) -> dict:
    step = event.get("step", "")
    print(json.dumps({"msg": "pr_review_step", "step": step, "delivery_id": event.get("event", {}).get("delivery_id", "")}))

    handler_fn = _STEPS.get(step)
    if not handler_fn:
        raise ValueError(f"Unknown step: {step}")

    return handler_fn(event)
