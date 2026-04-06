"""
Coding Agent Lambda — triggered by EventBridge action.completed (create_github_pr).

Loads the incident packet from S3, runs the LangGraph coding agent to investigate
the source repo and propose code fixes, then creates a fix PR on GitHub.

Environment variables:
    EVIDENCE_BUCKET      — S3 bucket where packets are stored
    PACKETS_TABLE        — DynamoDB table with packet refs
    GROQ_API_KEY_SSM     — SSM path for Groq API key
    GITHUB_TOKEN_SSM     — SSM path for GitHub PAT
    GITHUB_OWNER         — GitHub org/user that owns target repos
    LLM_PROVIDER         — "groq" or "stub"
    LLM_MODEL            — Override default model
    EVENT_BUS_NAME       — EventBridge bus for emitting events
    AUTOMATION_ENABLED   — Kill switch
"""
import json
import os
import time
from typing import Any

import boto3

s3 = boto3.client("s3")
ssm = boto3.client("ssm")
events_client = boto3.client("events")
dynamodb = boto3.resource("dynamodb")

EVIDENCE_BUCKET = os.environ.get("EVIDENCE_BUCKET", "")
PACKETS_TABLE = os.environ.get("PACKETS_TABLE", "")
INCIDENTS_TABLE = os.environ.get("INCIDENTS_TABLE", "")
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq")
LLM_MODEL = os.environ.get("LLM_MODEL", "")
AUTOMATION_ENABLED = os.environ.get("AUTOMATION_ENABLED", "true").lower() in ("true", "1", "yes")
EVENT_SOURCE = "opsrunbook-copilot"

GROQ_API_KEY_SSM = os.environ.get("GROQ_API_KEY_SSM", "/opsrunbook/dev/groq/api_key")
GITHUB_TOKEN_SSM = os.environ.get("GITHUB_TOKEN_SSM", "/opsrunbook/dev/github/token")

_secret_cache: dict[str, str] = {}


def _log(msg: str, incident_id: str = "", **fields):
    entry = {"msg": msg, "incident_id": incident_id, **fields}
    print(json.dumps(entry, default=str))


def _read_ssm(path: str) -> str:
    if path in _secret_cache:
        return _secret_cache[path]
    try:
        resp = ssm.get_parameter(Name=path, WithDecryption=True)
        raw = resp["Parameter"]["Value"]
        try:
            val = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            val = raw
        _secret_cache[path] = val
        return val
    except Exception as e:
        _log("ssm_read_failed", path=path, error=str(e)[:200])
        return ""


def _load_packet(bucket: str, key: str) -> dict:
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def _get_llm():
    if LLM_PROVIDER == "stub":
        return None
    api_key = _read_ssm(GROQ_API_KEY_SSM)
    if not api_key:
        _log("groq_api_key_missing")
        return None
    from langchain_groq import ChatGroq
    return ChatGroq(
        model=LLM_MODEL or "meta-llama/llama-4-scout-17b-16e-instruct",
        api_key=api_key,
        temperature=0.2,
        max_retries=2,
    )


def _get_github(repo_name: str):
    from agent.github_tools import GitHubAPI
    token = _read_ssm(GITHUB_TOKEN_SSM)
    if not token:
        _log("github_token_missing")
        return None
    gh = GitHubAPI(token=token, owner=GITHUB_OWNER, repo=repo_name)
    gh.get_default_branch()
    return gh


def _resolve_target_repo(packet: dict) -> str | None:
    owners = packet.get("suspected_owners", [])
    for owner in owners:
        conf = owner.get("confidence", 0)
        repo = owner.get("repo", "")
        if repo and conf >= 0.7:
            return repo
    return None


def _emit_event(incident_id: str, status: str, details: dict):
    if not EVENT_BUS_NAME:
        return
    try:
        events_client.put_events(Entries=[{
            "Source": EVENT_SOURCE,
            "DetailType": "coding_agent.completed",
            "Detail": json.dumps({
                "incident_id": incident_id,
                "status": status,
                **details,
            }, default=str),
            "EventBusName": EVENT_BUS_NAME,
        }])
    except Exception as e:
        _log("event_emit_failed", incident_id, error=str(e)[:200])


def _apply_edits_and_create_pr(gh, result, packet: dict) -> dict:
    if not result.proposed_edits:
        return {"status": "no_edits", "edits": 0}

    incident_id = packet.get("incident_id", "unknown")
    branch_name = f"opsrunbook/fix-{incident_id}-{int(time.time())}"
    gh.create_branch(branch_name)

    applied = 0
    failed = 0
    for edit in result.proposed_edits:
        try:
            content = gh.read_file(edit.file_path)
            if content is None or edit.old_code not in content:
                _log("edit_validation_failed", incident_id,
                     file=edit.file_path, reason="old_code not found")
                failed += 1
                continue
            gh.apply_edit(
                branch=branch_name,
                file_path=edit.file_path,
                old_code=edit.old_code,
                new_code=edit.new_code,
                message=f"fix({incident_id}): {edit.rationale[:72]}",
            )
            applied += 1
        except Exception as e:
            _log("edit_apply_failed", incident_id,
                 file=edit.file_path, error=str(e)[:200])
            failed += 1

    if applied == 0:
        return {"status": "no_valid_edits", "edits": 0, "failed": failed}

    pr_body = (
        f"## Automated Fix by OpsRunbook Coding Agent\n\n"
        f"**Incident**: `{incident_id}`\n"
        f"**Service**: `{packet.get('service', 'unknown')}`\n"
        f"**Edits applied**: {applied} | **Failed**: {failed}\n\n"
        f"---\n\n### Agent Summary\n\n{result.summary}\n\n"
        f"---\n\n"
        f"> **This PR was generated automatically and requires human review before merging.**\n"
        f"> Agent used {result.iterations} iterations and {len(result.tool_calls)} tool calls."
    )

    pr = gh.create_pull_request(
        title=f"[OpsRunbook Agent] Fix for {incident_id}",
        body=pr_body,
        head=branch_name,
    )

    return {
        "status": "pr_created",
        "pr_url": pr.get("html_url", ""),
        "pr_number": pr.get("number", 0),
        "branch": branch_name,
        "edits_applied": applied,
        "edits_failed": failed,
    }


def lambda_handler(event: dict, context: Any) -> dict:
    detail = event.get("detail", event)
    incident_id = detail.get("incident_id", "")

    _log("coding_agent_start", incident_id,
         automation_enabled=AUTOMATION_ENABLED, provider=LLM_PROVIDER)

    if not AUTOMATION_ENABLED:
        _log("automation_disabled", incident_id)
        return {"ok": True, "status": "disabled"}

    if LLM_PROVIDER == "stub":
        _log("llm_stub_mode", incident_id)
        return {"ok": True, "status": "stub_mode"}

    packet_ref = detail.get("packet_ref", {})
    packet_bucket = packet_ref.get("s3_bucket", "")
    packet_key = packet_ref.get("s3_key", "")

    if not packet_bucket or not packet_key:
        _log("no_packet_ref", incident_id)
        return {"ok": False, "error": "no packet_ref"}

    try:
        packet = _load_packet(packet_bucket, packet_key)
    except Exception as e:
        _log("packet_load_failed", incident_id, error=str(e)[:200])
        return {"ok": False, "error": "packet_load_failed"}

    repo_name = _resolve_target_repo(packet)
    if not repo_name:
        _log("no_target_repo", incident_id)
        return {"ok": True, "status": "no_target_repo"}

    _log("target_repo_resolved", incident_id, repo=repo_name)

    try:
        llm = _get_llm()
        if llm is None:
            _log("llm_init_failed", incident_id)
            return {"ok": False, "error": "llm_init_failed"}

        gh = _get_github(repo_name)
        if gh is None:
            _log("github_init_failed", incident_id)
            return {"ok": False, "error": "github_init_failed"}

        from agent.agent import run_agent
        result = run_agent(llm=llm, packet=packet, gh=gh, verbose=False)

        _log("agent_completed", incident_id,
             edits=len(result.proposed_edits),
             iterations=result.iterations,
             tool_calls=len(result.tool_calls))

        pr_result = _apply_edits_and_create_pr(gh, result, packet)

        _log("coding_agent_done", incident_id, **pr_result)

        _emit_event(incident_id, pr_result["status"], {
            "repo": repo_name,
            "pr_url": pr_result.get("pr_url", ""),
            "edits_applied": pr_result.get("edits_applied", 0),
        })

        return {"ok": True, "incident_id": incident_id, **pr_result}

    except Exception as e:
        _log("coding_agent_error", incident_id, error=str(e)[:500])
        _emit_event(incident_id, "error", {"error": str(e)[:200]})
        return {"ok": False, "error": str(e)[:200]}
