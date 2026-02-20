"""
Actions-runner Lambda – v1 hardened.

Triggered by EventBridge event: incident.analyzed
Loads the packet from S3, generates an ActionPlan, executes Jira + Teams + GitHub PR
actions (or DRY_RUN stubs), persists results to DynamoDB, optionally emits
action.completed event.

v1 hardening:
 - Idempotency: deterministic keys per action; skip if already executed
 - PR confidence gate: skip create_github_pr when repo_confidence < 0.7
 - Kill switch: AUTOMATION_ENABLED=false → collect/analyze only, no execution
 - Structured logging: every log includes incident_id + correlation_id
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import boto3

from plan_generator import generate_action_plan, build_teams_body, build_pr_notes, build_pr_body
from jira_client import JiraClient, DryRunJiraClient
from teams_notifier import TeamsNotifier, DryRunTeamsNotifier
from github_client import GitHubClient, DryRunGitHubClient
from repo_resolver import resolve_repo, load_mapping_rules, RepoResolution

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")
events_client = boto3.client("events")

INCIDENTS_TABLE = os.environ["INCIDENTS_TABLE"]
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "")
DRY_RUN = os.environ.get("ACTIONS_DRY_RUN", "true").lower() in ("true", "1", "yes")
AUTOMATION_ENABLED = os.environ.get("AUTOMATION_ENABLED", "true").lower() in ("true", "1", "yes")
EVENT_SOURCE = "opsrunbook-copilot"

PR_CONFIDENCE_THRESHOLD = float(os.environ.get("PR_CONFIDENCE_THRESHOLD", "0.7"))

SSM_JIRA_BASE_URL = os.environ.get("SSM_JIRA_BASE_URL", "/opsrunbook/dev/jira/base_url")
SSM_JIRA_EMAIL = os.environ.get("SSM_JIRA_EMAIL", "/opsrunbook/dev/jira/email")
SSM_JIRA_API_TOKEN = os.environ.get("SSM_JIRA_API_TOKEN", "/opsrunbook/dev/jira/api_token")
SSM_JIRA_PROJECT_KEY = os.environ.get("SSM_JIRA_PROJECT_KEY", "/opsrunbook/dev/jira/project_key")
SSM_JIRA_ISSUE_TYPE = os.environ.get("SSM_JIRA_ISSUE_TYPE", "/opsrunbook/dev/jira/issue_type")
SSM_TEAMS_WEBHOOK = os.environ.get("SSM_TEAMS_WEBHOOK", "/opsrunbook/dev/teams/webhook_url")

ENABLE_GITHUB_PR = os.environ.get("ENABLE_GITHUB_PR_ACTION", "false").lower() in ("true", "1", "yes")
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")
GITHUB_DEFAULT_BRANCH = os.environ.get("GITHUB_DEFAULT_BRANCH", "main")
SSM_GITHUB_TOKEN = os.environ.get("SSM_GITHUB_TOKEN", "/opsrunbook/dev/github/token")
SSM_GITHUB_APP_ID = os.environ.get("SSM_GITHUB_APP_ID", "/opsrunbook/dev/github/app_id")
SSM_GITHUB_APP_INSTALL_ID = os.environ.get("SSM_GITHUB_APP_INSTALL_ID", "/opsrunbook/dev/github/app_installation_id")
SSM_GITHUB_APP_PEM = os.environ.get("SSM_GITHUB_APP_PEM", "/opsrunbook/dev/github/app_private_key_pem")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str, incident_id: str = "", correlation_id: str = "", **extra):
    entry = {"msg": msg, "incident_id": incident_id, "correlation_id": correlation_id}
    entry.update(extra)
    print(json.dumps(entry, default=str))


def _idempotency_key(incident_id: str, action_type: str, discriminator: str = "") -> str:
    raw = f"{incident_id}|{action_type}|{discriminator}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _load_json(bucket: str, key: str) -> dict:
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def _get_ssm_param(name: str, decrypt: bool = False) -> str | None:
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


def _build_jira_client() -> JiraClient | None:
    base_url = _get_ssm_param(SSM_JIRA_BASE_URL)
    email = _get_ssm_param(SSM_JIRA_EMAIL)
    token = _get_ssm_param(SSM_JIRA_API_TOKEN, decrypt=True)
    project = _get_ssm_param(SSM_JIRA_PROJECT_KEY)
    issue_type = _get_ssm_param(SSM_JIRA_ISSUE_TYPE) or "Bug"
    if not all([base_url, email, token, project]):
        return None
    return JiraClient(base_url, email, token, project, issue_type)


def _build_teams_notifier() -> TeamsNotifier | None:
    webhook = _get_ssm_param(SSM_TEAMS_WEBHOOK, decrypt=True)
    if not webhook:
        return None
    return TeamsNotifier(webhook)


_PLACEHOLDER = {"REPLACE_ME", "replace_me", "placeholder", ""}


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


def _build_github_client() -> GitHubClient | None:
    if not GITHUB_OWNER:
        return None
    pat = _real_or_none(_get_ssm_param(SSM_GITHUB_TOKEN, decrypt=True))
    app_id = _real_or_none(_get_ssm_param(SSM_GITHUB_APP_ID))
    install_id = _real_or_none(_get_ssm_param(SSM_GITHUB_APP_INSTALL_ID))
    pem = _normalize_pem(_real_or_none(_get_ssm_param(SSM_GITHUB_APP_PEM, decrypt=True)))
    if not pat and not (app_id and install_id and pem):
        return None
    return GitHubClient(
        GITHUB_OWNER,
        pat=pat,
        app_id=app_id,
        installation_id=install_id,
        pem=pem,
        default_branch_fallback=GITHUB_DEFAULT_BRANCH,
    )


# ── Idempotency helpers ──────────────────────────────────────────

def _find_existing_action(table, incident_id: str, action_type: str) -> dict | None:
    """Check if an action of this type already succeeded for this incident."""
    try:
        resp = table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": f"INCIDENT#{incident_id}",
                ":prefix": "ACTION#",
            },
        )
        for item in resp.get("Items", []):
            if item.get("action_type") == action_type and item.get("status") == "success":
                result = dict(item)
                for field in ("external_refs", "evidence_refs"):
                    if isinstance(result.get(field), str):
                        try:
                            result[field] = json.loads(result[field])
                        except (json.JSONDecodeError, TypeError):
                            pass
                return result
    except Exception:
        pass
    return None


def _persist_action_plan(table, incident_id: str, plan: dict) -> str:
    created_at = plan.get("created_at", _now_iso())
    sk = f"ACTIONPLAN#{created_at}"
    table.put_item(Item={
        "pk": f"INCIDENT#{incident_id}",
        "sk": sk,
        "incident_id": incident_id,
        "created_at": created_at,
        "plan": json.dumps(plan, default=str),
    })
    return sk


def _persist_action_result(table, incident_id: str, result: dict) -> str:
    created_at = result.get("created_at", _now_iso())
    action_id = result.get("action_id", uuid4().hex[:12])
    sk = f"ACTION#{created_at}#{action_id}"
    table.put_item(Item={
        "pk": f"INCIDENT#{incident_id}",
        "sk": sk,
        "incident_id": incident_id,
        "action_id": action_id,
        "action_type": result.get("action_type", ""),
        "status": result.get("status", ""),
        "created_at": created_at,
        "request_summary": result.get("request_summary", "")[:1000],
        "response_summary": result.get("response_summary", "")[:1000],
        "external_refs": json.dumps(result.get("external_refs", {}), default=str),
        "error": result.get("error"),
        "cause": result.get("cause"),
        "evidence_refs": json.dumps(result.get("evidence_refs", []), default=str),
    })
    return sk


def _update_latest_pointer(table, incident_id: str, plan_sk: str, action_sks: list[str]):
    table.put_item(Item={
        "pk": f"INCIDENT#{incident_id}",
        "sk": "ACTIONS#LATEST",
        "incident_id": incident_id,
        "latest_actionplan_sk": plan_sk,
        "latest_action_sks": json.dumps(action_sks),
        "updated_at": _now_iso(),
    })


def _emit_event(action_type: str, status: str, incident_id: str, external_refs: dict, correlation_id: str = ""):
    if not EVENT_BUS_NAME:
        return
    try:
        events_client.put_events(Entries=[{
            "Source": EVENT_SOURCE,
            "DetailType": "action.completed",
            "Detail": json.dumps({
                "incident_id": incident_id,
                "action_type": action_type,
                "status": status,
                "external_refs": external_refs,
                "correlation_id": correlation_id,
                "emitted_at": _now_iso(),
            }, default=str),
            "EventBusName": EVENT_BUS_NAME,
        }])
    except Exception as e:
        _log("event_emit_failed", incident_id, correlation_id, error=str(e)[:300])


# ── Main handler ─────────────────────────────────────────────────

def lambda_handler(event: dict, context: Any) -> dict:
    detail = event.get("detail", event)
    incident_id = detail.get("incident_id", "")
    collector_run_id = detail.get("collector_run_id", "")
    correlation_id = detail.get("correlation_id", collector_run_id or uuid4().hex[:12])

    _log("actions_runner_start", incident_id, correlation_id, dry_run=DRY_RUN, automation_enabled=AUTOMATION_ENABLED)

    # Kill switch
    if not AUTOMATION_ENABLED:
        _log("automation_disabled", incident_id, correlation_id)
        return {"ok": True, "incident_id": incident_id, "status": "automation_disabled"}

    packet_ref = detail.get("packet_ref", {})
    packet_bucket = packet_ref.get("s3_bucket", "")
    packet_key = packet_ref.get("s3_key", "")

    if not packet_bucket or not packet_key:
        _log("no_packet_ref", incident_id, correlation_id)
        return {"ok": False, "error": "no packet_ref in event"}

    try:
        packet = _load_json(packet_bucket, packet_key)
    except Exception as e:
        _log("packet_load_failed", incident_id, correlation_id, error=str(e)[:300])
        return {"ok": False, "error": f"packet load failed: {e}"}

    plan = generate_action_plan(packet, dry_run=DRY_RUN)

    table = dynamodb.Table(INCIDENTS_TABLE)
    plan_sk = _persist_action_plan(table, incident_id, plan)
    action_sks: list[str] = []
    results: list[dict] = []

    # ── Execute: Jira (idempotent) ────────────────────────────────
    existing_jira = _find_existing_action(table, incident_id, "create_jira_ticket")
    if existing_jira:
        _log("jira_idempotent_skip", incident_id, correlation_id)
        jira_result = existing_jira
    else:
        jira_result = _execute_jira(plan, packet, incident_id, correlation_id)
        sk = _persist_action_result(table, incident_id, jira_result)
        action_sks.append(sk)
        _emit_event("create_jira_ticket", jira_result["status"], incident_id,
                     jira_result.get("external_refs", {}), correlation_id)
    results.append(jira_result)

    # ── Execute: Teams (idempotent) ───────────────────────────────
    existing_teams = _find_existing_action(table, incident_id, "notify_teams")
    if existing_teams:
        _log("teams_idempotent_skip", incident_id, correlation_id)
        teams_result = existing_teams
    else:
        teams_result = _execute_teams(plan, packet, jira_result.get("external_refs", {}),
                                      incident_id, correlation_id)
        sk = _persist_action_result(table, incident_id, teams_result)
        action_sks.append(sk)
        _emit_event("notify_teams", teams_result["status"], incident_id,
                     teams_result.get("external_refs", {}), correlation_id)
    results.append(teams_result)

    # ── Execute: GitHub PR (idempotent + confidence gate) ─────────
    if ENABLE_GITHUB_PR:
        existing_pr = _find_existing_action(table, incident_id, "create_github_pr")
        if existing_pr:
            _log("github_pr_idempotent_skip", incident_id, correlation_id)
            gh_result = existing_pr
        else:
            gh_result = _execute_github_pr(plan, packet, jira_result.get("external_refs", {}),
                                           incident_id, correlation_id)
            sk = _persist_action_result(table, incident_id, gh_result)
            action_sks.append(sk)
            _emit_event("create_github_pr", gh_result["status"], incident_id,
                         gh_result.get("external_refs", {}), correlation_id)
        results.append(gh_result)

    _update_latest_pointer(table, incident_id, plan_sk, action_sks)

    statuses = {r["action_type"]: r["status"] for r in results}
    _log("actions_runner_done", incident_id, correlation_id, statuses=statuses)

    return {"ok": True, "incident_id": incident_id, "results": [r["status"] for r in results]}


# ── Action executors ─────────────────────────────────────────────

def _execute_jira(plan: dict, packet: dict, incident_id: str, correlation_id: str) -> dict:
    jira_action = next((a for a in plan.get("actions", []) if a["action_type"] == "create_jira_ticket"), None)
    if not jira_action:
        return _skipped_result("create_jira_ticket", "no jira action in plan", incident_id=incident_id)

    action_id = uuid4().hex[:12]
    created_at = _now_iso()

    if DRY_RUN:
        client = DryRunJiraClient()
    else:
        client = _build_jira_client()
        if client is None:
            return _skipped_result("create_jira_ticket", "jira_not_configured", action_id, created_at, incident_id)

    try:
        resp = client.create_issue(
            summary=jira_action["title"],
            description=jira_action.get("description_md", ""),
            priority=jira_action.get("priority", "P2"),
            labels=["opsrunbook-copilot", "auto-generated"],
        )
        _log("jira_created", incident_id, correlation_id, key=resp.get("issue_key", ""))
        return {
            "schema_version": "incident_action_result.v1",
            "incident_id": incident_id,
            "action_id": action_id,
            "action_type": "create_jira_ticket",
            "status": "success",
            "created_at": created_at,
            "request_summary": f"Created issue: {jira_action['title'][:200]}",
            "response_summary": f"key={resp.get('issue_key', '')}",
            "external_refs": {
                "jira_issue_key": resp.get("issue_key", ""),
                "jira_url": resp.get("url", ""),
            },
            "evidence_refs": jira_action.get("evidence_refs", []),
        }
    except Exception as e:
        _log("jira_failed", incident_id, correlation_id, error=str(e)[:300])
        return {
            "schema_version": "incident_action_result.v1",
            "incident_id": incident_id,
            "action_id": action_id,
            "action_type": "create_jira_ticket",
            "status": "failed",
            "created_at": created_at,
            "request_summary": f"Attempted: {jira_action['title'][:200]}",
            "response_summary": "",
            "external_refs": {},
            "error": str(e)[:500],
            "evidence_refs": jira_action.get("evidence_refs", []),
        }


def _execute_teams(plan: dict, packet: dict, jira_refs: dict, incident_id: str, correlation_id: str) -> dict:
    teams_action = next((a for a in plan.get("actions", []) if a["action_type"] == "notify_teams"), None)
    if not teams_action:
        return _skipped_result("notify_teams", "no teams action in plan", incident_id=incident_id)

    action_id = uuid4().hex[:12]
    created_at = _now_iso()

    if DRY_RUN:
        notifier = DryRunTeamsNotifier()
    else:
        notifier = _build_teams_notifier()
        if notifier is None:
            return _skipped_result("notify_teams", "teams_not_configured", action_id, created_at, incident_id)

    body_md = build_teams_body(packet, jira_refs if jira_refs.get("jira_issue_key") else None)
    links = []
    if jira_refs.get("jira_url"):
        links.append({"name": f"Jira {jira_refs['jira_issue_key']}", "url": jira_refs["jira_url"]})

    try:
        resp = notifier.send_message(
            title=teams_action["title"],
            body_md=body_md,
            links=links,
        )
        _log("teams_sent", incident_id, correlation_id)
        return {
            "schema_version": "incident_action_result.v1",
            "incident_id": incident_id,
            "action_id": action_id,
            "action_type": "notify_teams",
            "status": "success",
            "created_at": created_at,
            "request_summary": f"Sent teams notification: {teams_action['title'][:200]}",
            "response_summary": f"status={resp.get('status_code', '')}",
            "external_refs": {
                "teams_message_id": resp.get("message_id", ""),
            },
            "evidence_refs": teams_action.get("evidence_refs", []),
        }
    except Exception as e:
        _log("teams_failed", incident_id, correlation_id, error=str(e)[:300])
        return {
            "schema_version": "incident_action_result.v1",
            "incident_id": incident_id,
            "action_id": action_id,
            "action_type": "notify_teams",
            "status": "failed",
            "created_at": created_at,
            "request_summary": f"Attempted: {teams_action['title'][:200]}",
            "response_summary": "",
            "external_refs": {},
            "error": str(e)[:500],
            "evidence_refs": teams_action.get("evidence_refs", []),
        }


def _execute_github_pr(plan: dict, packet: dict, jira_refs: dict,
                       incident_id: str, correlation_id: str) -> dict:
    gh_action = next((a for a in plan.get("actions", []) if a["action_type"] == "create_github_pr"), None)
    if not gh_action:
        return _skipped_result("create_github_pr", "no github_pr action in plan", incident_id=incident_id)

    action_id = uuid4().hex[:12]
    created_at = _now_iso()
    collector_run_id = packet.get("collector_run_id", "")

    jira_key = jira_refs.get("jira_issue_key", "")
    jira_url = jira_refs.get("jira_url", "")
    if not jira_key:
        return {
            "schema_version": "incident_action_result.v1",
            "incident_id": incident_id,
            "action_id": action_id,
            "action_type": "create_github_pr",
            "status": "failed",
            "created_at": created_at,
            "request_summary": "No Jira key available for branch naming",
            "response_summary": "",
            "external_refs": {},
            "error": "missing jira_issue_key from prior create_jira_ticket result",
            "evidence_refs": gh_action.get("evidence_refs", []),
        }

    try:
        if DRY_RUN:
            client = DryRunGitHubClient(GITHUB_OWNER or "dry-run-owner")
        else:
            client = _build_github_client()
            if client is None:
                return _skipped_result("create_github_pr", "github_not_configured", action_id, created_at, incident_id)
    except Exception as e:
        return _skipped_result("create_github_pr", f"github_client_error: {e}", action_id, created_at, incident_id)

    # ── Deterministic repo resolution ─────────────────────────────
    resolution = resolve_repo(
        packet=packet,
        checker=client,
        owner=GITHUB_OWNER,
    )

    _log("repo_resolution", incident_id, correlation_id,
         repo=resolution.repo_full_name,
         confidence=resolution.confidence,
         verification=resolution.verification,
         reasons=resolution.reasons,
         trace_frames=len(resolution.trace_frames))

    # ── Confidence gate ───────────────────────────────────────────
    if not resolution.repo_full_name or resolution.confidence < PR_CONFIDENCE_THRESHOLD:
        reason = (
            f"skipped: repo_confidence={resolution.confidence:.2f} < threshold={PR_CONFIDENCE_THRESHOLD} "
            f"(repo={resolution.repo_full_name or 'none'}, verification={resolution.verification})"
        )
        _log("pr_confidence_gate_skip", incident_id, correlation_id,
             confidence=resolution.confidence, threshold=PR_CONFIDENCE_THRESHOLD)
        return {
            "schema_version": "incident_action_result.v1",
            "incident_id": incident_id,
            "action_id": action_id,
            "action_type": "create_github_pr",
            "status": "skipped",
            "created_at": created_at,
            "request_summary": reason,
            "response_summary": "",
            "external_refs": {"repo_resolution": resolution.to_dict()},
            "error": reason,
            "evidence_refs": gh_action.get("evidence_refs", []),
        }

    repo_full = resolution.repo_full_name
    repo = repo_full.split("/", 1)[1] if "/" in repo_full else repo_full

    branch_name = f"opsrunbook/{jira_key}"
    service = packet.get("service", "")
    environment = packet.get("environment", "dev")
    pr_title = f"{jira_key} [{environment}] {service}: incident {incident_id} – initial analysis"

    file_path = f".opsrunbook/pr-notes/{jira_key}.md"
    file_content = build_pr_notes(packet, {"jira_issue_key": jira_key, "jira_url": jira_url})
    pr_body_text = _build_deterministic_pr_body(packet, jira_key, jira_url, resolution)
    commit_msg = f"{jira_key}: add incident analysis notes for {incident_id}"

    try:
        refs = client.create_pr_with_notes(
            repo=repo,
            branch_name=branch_name,
            pr_title=pr_title,
            pr_body=pr_body_text,
            file_path=file_path,
            file_content=file_content,
            commit_message=commit_msg,
            collector_run_id=collector_run_id,
        )
        refs["repo_resolution"] = resolution.to_dict()
        _log("github_pr_created", incident_id, correlation_id,
             pr_url=refs.get("pr_url", ""), reused=refs.get("reused_pr", False))
        return {
            "schema_version": "incident_action_result.v1",
            "incident_id": incident_id,
            "action_id": action_id,
            "action_type": "create_github_pr",
            "status": "success",
            "created_at": created_at,
            "request_summary": f"{'Updated' if refs.get('reused_pr') else 'Created'} PR: {pr_title[:200]}",
            "response_summary": f"pr={refs.get('pr_url', '')} repo={repo_full} verification={resolution.verification}",
            "external_refs": refs,
            "evidence_refs": gh_action.get("evidence_refs", []),
        }
    except Exception as e:
        _log("github_pr_failed", incident_id, correlation_id, error=str(e)[:300])
        return {
            "schema_version": "incident_action_result.v1",
            "incident_id": incident_id,
            "action_id": action_id,
            "action_type": "create_github_pr",
            "status": "failed",
            "created_at": created_at,
            "request_summary": f"Attempted: {pr_title[:200]}",
            "response_summary": "",
            "external_refs": {"repo_resolution": resolution.to_dict()},
            "error": str(e)[:500],
            "evidence_refs": gh_action.get("evidence_refs", []),
        }


# ── Deterministic PR body template ───────────────────────────────

def _build_deterministic_pr_body(packet: dict, jira_key: str, jira_url: str,
                                 resolution: RepoResolution) -> str:
    """Fixed backend-driven PR body template. No LLM-generated content."""
    incident_id = packet.get("incident_id", "N/A")
    service = packet.get("service", "N/A")
    environment = packet.get("environment", "N/A")
    tw = packet.get("time_window", {})
    findings = packet.get("findings", [])
    erefs = packet.get("all_evidence_refs", [])

    lines = [
        "<!-- opsrunbook_copilot: true -->",
        f"## Incident `{incident_id}`",
        "",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| **Service** | {service} |",
        f"| **Environment** | {environment} |",
        f"| **Time Window** | {tw.get('start', 'N/A')} → {tw.get('end', 'N/A')} |",
        f"| **Jira** | [{jira_key}]({jira_url}) |",
        f"| **Repo Confidence** | {resolution.confidence:.0%} ({resolution.verification}) |",
        "",
    ]

    if findings:
        lines.append(f"### {len(findings)} Finding(s)")
        for f in findings[:5]:
            conf = f.get("confidence", 0)
            summary = f.get("summary", "")[:150]
            refs_count = len(f.get("evidence_refs", []))
            lines.append(f"- [{conf:.0%}] {summary} ({refs_count} evidence ref(s))")
        lines.append("")

    if erefs:
        lines.append(f"### Evidence Summary")
        lines.append(f"- **{len(erefs)}** evidence object(s) collected")
        collector_types = sorted(set(e.get("collector_type", "?") for e in erefs))
        lines.append(f"- Collector types: {', '.join(collector_types)}")
        total_bytes = sum(e.get("byte_size", 0) for e in erefs)
        lines.append(f"- Total evidence size: {total_bytes:,} bytes")
        lines.append("")

    lines.append("### Repo Resolution")
    lines.append(f"- **Repo**: `{resolution.repo_full_name}`")
    lines.append(f"- **Confidence**: {resolution.confidence:.0%}")
    lines.append(f"- **Verification**: {resolution.verification}")
    for reason in resolution.reasons:
        lines.append(f"- {reason}")
    if resolution.trace_frames:
        lines.append(f"- **Trace frames**: {len(resolution.trace_frames)} app frame(s)")
        for tf in resolution.trace_frames[:3]:
            lines.append(f"  - `{tf.get('normalized_path', '')}:{tf.get('line', '?')}`")
    lines.append("")

    lines.append("---")
    lines.append("*Auto-generated by opsrunbook-copilot. Human review required before merge.*")

    return "\n".join(lines)


def _skipped_result(action_type: str, error: str, action_id: str | None = None,
                    created_at: str | None = None, incident_id: str = "") -> dict:
    return {
        "schema_version": "incident_action_result.v1",
        "incident_id": incident_id,
        "action_id": action_id or uuid4().hex[:12],
        "action_type": action_type,
        "status": "skipped",
        "created_at": created_at or _now_iso(),
        "request_summary": "",
        "response_summary": "",
        "external_refs": {},
        "error": error,
        "evidence_refs": [],
    }
