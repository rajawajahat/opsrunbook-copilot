"""
Analyzer Lambda – Iteration 3.

Triggered by EventBridge event: evidence.snapshot.persisted
Loads evidence snapshot manifest + collector evidence objects from S3,
runs stub analysis, produces IncidentPacketV1, persists to S3 + DynamoDB,
emits incident.analyzed event.
"""
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
events_client = boto3.client("events")

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "stub")
PACKETS_TABLE = os.environ["PACKETS_TABLE"]
EVENT_BUS_NAME = os.environ.get("EVENT_BUS_NAME", "")
EVENT_SOURCE = "opsrunbook-copilot"

RESOURCE_REPO_MAP: dict[str, str] = {}
try:
    _map_path = os.path.join(os.path.dirname(__file__), "resource_repo_map.json")
    if os.path.exists(_map_path):
        with open(_map_path) as f:
            RESOURCE_REPO_MAP = json.load(f)
except Exception:
    pass


def _to_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str
    ).encode("utf-8")


def _load_json(bucket: str, key: str) -> dict:
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Stub analyzer
# ---------------------------------------------------------------------------

def _make_evidence_ref(collector: dict) -> Optional[dict]:
    ref = collector.get("evidence_ref")
    if not ref or not isinstance(ref, dict) or not ref.get("s3_key"):
        return None
    return {
        "collector_type": ref.get("collector_type", collector.get("collector_type", "unknown")),
        "s3_bucket": ref.get("s3_bucket", ""),
        "s3_key": ref.get("s3_key", ""),
        "sha256": ref.get("sha256", ""),
        "byte_size": ref.get("byte_size", 0),
        "truncated": ref.get("truncated", False),
    }


def _analyze_logs(evidence: dict, eref: dict) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    findings, hypotheses, actions, limits = [], [], [], []
    sections = evidence.get("sections", [])
    error_messages = []
    for sec in sections:
        if sec.get("name") == "recent_errors":
            for row in sec.get("rows", [])[:10]:
                msg = row.get("@message", "")
                if msg:
                    error_messages.append(msg[:300])

    if error_messages:
        top = error_messages[:5]
        findings.append({
            "id": "logs-errors-found",
            "summary": f"Found {len(error_messages)} recent error(s) in logs. Top: {top[0][:120]}",
            "confidence": 0.8,
            "evidence_refs": [eref],
            "notes": f"Total errors sampled: {len(error_messages)}",
        })
        hypotheses.append({
            "summary": "Application is throwing runtime errors — check recent deployments or config changes.",
            "confidence": 0.5,
            "evidence_refs": [eref],
        })
        actions.append({
            "summary": "Inspect full error logs in CloudWatch Logs Insights",
            "commands": [
                "fields @timestamp, @message | filter @message like /ERROR|Exception/ | sort @timestamp desc | limit 50"
            ],
            "links": [],
            "evidence_refs": [eref],
        })
    else:
        limits.append("No errors found in log evidence; logs may be empty or filtered.")

    return findings, hypotheses, actions, limits


def _analyze_metrics(evidence: dict, eref: dict) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    findings, hypotheses, actions, limits = [], [], [], []
    series = evidence.get("series", [])
    sections = evidence.get("sections", [])
    if sections:
        for sec in sections:
            series.extend(sec.get("series", []))

    if series:
        findings.append({
            "id": "metrics-collected",
            "summary": f"Collected {len(series)} metric series. Stub mode — no anomaly detection.",
            "confidence": 0.4,
            "evidence_refs": [eref],
        })
        actions.append({
            "summary": "Review CloudWatch metrics dashboard for anomalies manually",
            "links": ["https://console.aws.amazon.com/cloudwatch/home#metricsV2"],
            "evidence_refs": [eref],
        })
    else:
        limits.append("Metrics evidence present but no series data found.")

    return findings, hypotheses, actions, limits


def _analyze_stepfn(evidence: dict, eref: dict) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    findings, hypotheses, actions, limits = [], [], [], []
    sections = evidence.get("sections", [])

    for sec in sections:
        if sec.get("name") == "orchestrator_execution":
            status = sec.get("status")
            # RUNNING is expected — the stepfn collector runs *inside* the
            # orchestrator, so it always sees its own execution as RUNNING.
            # Only flag genuinely terminal failure statuses.
            if status in ("FAILED", "TIMED_OUT", "ABORTED"):
                findings.append({
                    "id": "stepfn-orchestrator-failed",
                    "summary": f"Orchestrator execution status: {status}. Error: {(sec.get('error') or 'N/A')[:200]}",
                    "confidence": 0.9,
                    "evidence_refs": [eref],
                })
            last_state = sec.get("last_failed_state")
            if last_state:
                hypotheses.append({
                    "summary": f"Failure in state '{last_state}' — check that Lambda's logs and IAM permissions.",
                    "confidence": 0.5,
                    "evidence_refs": [eref],
                })

        if sec.get("name") == "failed_executions":
            execs = sec.get("executions", [])
            if execs:
                latest = execs[0]
                findings.append({
                    "id": "stepfn-failed-executions",
                    "summary": f"Found {len(execs)} failed execution(s). Latest: {latest.get('name','')} status={latest.get('status','')}",
                    "confidence": 0.8,
                    "evidence_refs": [eref],
                })
                arn = latest.get("execution_arn", "")
                if arn:
                    region = arn.split(":")[3] if len(arn.split(":")) > 3 else "us-east-1"
                    actions.append({
                        "summary": "Inspect latest failed Step Functions execution in console",
                        "links": [f"https://{region}.console.aws.amazon.com/states/home?region={region}#/executions/details/{arn}"],
                        "evidence_refs": [eref],
                    })

    if not sections:
        limits.append("Step Functions evidence has no sections.")

    return findings, hypotheses, actions, limits


# ---------------------------------------------------------------------------
# Repo candidates resolver
# ---------------------------------------------------------------------------

def _resolve_repo_candidates(manifest: dict, evidence_objects: dict) -> list[dict]:
    resource_names: set[str] = set()

    # Extract from manifest service
    svc = manifest.get("service", "")
    if svc:
        resource_names.add(svc)

    # Extract from collector evidence
    for _ctype, evidence in evidence_objects.items():
        for lg in evidence.get("log_groups", []):
            # /aws/lambda/<function_name>
            parts = lg.strip("/").split("/")
            if len(parts) >= 3:
                resource_names.add(parts[-1])
        for sec in evidence.get("sections", []):
            for arn_field in ("state_machine_arn", "execution_arn"):
                v = sec.get(arn_field, "")
                if v and ":" in v:
                    resource_names.add(v.split(":")[-1].split("/")[0])
            for sm in sec.get("state_machine_arns", []):
                if sm and ":" in sm:
                    resource_names.add(sm.split(":")[-1])
            for ex in sec.get("executions", []):
                for arn_field in ("execution_arn", "state_machine_arn"):
                    v = ex.get(arn_field, "")
                    if v and ":" in v:
                        resource_names.add(v.split(":")[-1].split("/")[0])

    candidates: dict[str, set[str]] = {}
    for rname in resource_names:
        rname_lower = rname.lower()
        for prefix, repo in RESOURCE_REPO_MAP.items():
            if prefix.lower() in rname_lower:
                candidates.setdefault(repo, set()).add(f"resource '{rname}' matches prefix '{prefix}'")

    owners = []
    for repo, reasons in candidates.items():
        owners.append({
            "repo": repo,
            "confidence": min(0.3 + 0.1 * len(reasons), 0.8),
            "reasons": sorted(reasons),
        })
    if not owners:
        owners.append({
            "repo": "unknown",
            "confidence": 0.1,
            "reasons": ["No resource-to-repo mapping matched"],
        })
    return owners


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    detail = event.get("detail", event)
    incident_id = detail["incident_id"]
    collector_run_id = detail["collector_run_id"]
    evidence_bucket = detail["evidence_bucket"]
    evidence_key = detail["evidence_key"]
    evidence_sha256 = detail.get("evidence_sha256", "")
    service = detail.get("service", "")
    environment = detail.get("environment", "dev")
    time_window = detail.get("time_window", {})

    print(json.dumps({"msg": "analyzer_start", "incident_id": incident_id, "collector_run_id": collector_run_id}))

    # Idempotency: check if packet already exists
    table = dynamodb.Table(PACKETS_TABLE)
    existing = table.query(
        KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
        FilterExpression="collector_run_id = :rid",
        ExpressionAttributeValues={
            ":pk": f"INCIDENT#{incident_id}",
            ":prefix": "PACKET#",
            ":rid": collector_run_id,
        },
        Limit=1,
    )
    if existing.get("Items"):
        print(json.dumps({"msg": "analyzer_idempotent_skip", "incident_id": incident_id}))
        return {"ok": True, "skipped": True, "incident_id": incident_id}

    # Load manifest
    manifest = _load_json(evidence_bucket, evidence_key)

    # Load each collector evidence
    collectors = manifest.get("collectors", [])
    evidence_objects: dict[str, dict] = {}
    all_evidence_refs: list[dict] = []
    for c in collectors:
        eref = _make_evidence_ref(c)
        if eref:
            all_evidence_refs.append(eref)
        ref = c.get("evidence_ref") or {}
        ctype = c.get("collector_type", "unknown")
        if ref.get("s3_key") and not c.get("skipped"):
            try:
                evidence_objects[ctype] = _load_json(ref["s3_bucket"], ref["s3_key"])
            except Exception as e:
                print(json.dumps({"msg": "evidence_load_error", "collector_type": ctype, "error": str(e)[:300]}))

    # Run stub analysis
    findings, hypotheses, next_actions, limits = [], [], [], []

    if "logs" in evidence_objects:
        logs_eref = next((r for r in all_evidence_refs if r.get("collector_type") == "logs"), None)
        if logs_eref:
            f, h, a, l = _analyze_logs(evidence_objects["logs"], logs_eref)
            findings.extend(f); hypotheses.extend(h); next_actions.extend(a); limits.extend(l)
    else:
        limits.append("Logs collector evidence not available or skipped.")

    if "metrics" in evidence_objects:
        met_eref = next((r for r in all_evidence_refs if r.get("collector_type") == "metrics"), None)
        if met_eref:
            f, h, a, l = _analyze_metrics(evidence_objects["metrics"], met_eref)
            findings.extend(f); hypotheses.extend(h); next_actions.extend(a); limits.extend(l)
    else:
        limits.append("Metrics collector evidence not available or skipped.")

    if "stepfn" in evidence_objects:
        sfn_eref = next((r for r in all_evidence_refs if r.get("collector_type") == "stepfn"), None)
        if sfn_eref:
            f, h, a, l = _analyze_stepfn(evidence_objects["stepfn"], sfn_eref)
            findings.extend(f); hypotheses.extend(h); next_actions.extend(a); limits.extend(l)
    else:
        limits.append("Step Functions collector evidence not available or skipped.")

    # Repo candidates
    suspected_owners = _resolve_repo_candidates(manifest, evidence_objects)

    # Build packet
    created_at = _now_iso()
    packet = {
        "schema_version": "incident_packet.v1",
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "service": service,
        "environment": environment,
        "time_window": time_window,
        "snapshot_ref": {
            "s3_bucket": evidence_bucket,
            "s3_key": evidence_key,
            "sha256": evidence_sha256,
        },
        "findings": findings,
        "hypotheses": hypotheses,
        "next_actions": next_actions,
        "suspected_owners": suspected_owners,
        "limits": limits,
        "model_trace": {
            "provider": LLM_PROVIDER,
            "model": None,
            "prompt_version": "v1",
            "created_at": created_at,
        },
        "all_evidence_refs": all_evidence_refs,
    }

    body = _to_bytes(packet)
    sha256 = hashlib.sha256(body).hexdigest()
    packet["packet_hashes"] = {"sha256": sha256}
    body = _to_bytes(packet)
    sha256 = hashlib.sha256(body).hexdigest()
    packet["packet_hashes"]["sha256"] = sha256
    body = _to_bytes(packet)

    packet_key = f"packets/{incident_id}/{collector_run_id}.json"
    s3.put_object(Bucket=evidence_bucket, Key=packet_key, Body=body, ContentType="application/json")

    # Persist metadata to DynamoDB
    sk = f"PACKET#{created_at}#{collector_run_id}"
    table.put_item(
        Item={
            "pk": f"INCIDENT#{incident_id}",
            "sk": sk,
            "incident_id": incident_id,
            "collector_run_id": collector_run_id,
            "created_at": created_at,
            "packet_bucket": evidence_bucket,
            "packet_key": packet_key,
            "packet_sha256": sha256,
            "packet_byte_size": len(body),
            "service": service,
            "environment": environment,
        }
    )

    # Emit incident.analyzed event
    if EVENT_BUS_NAME:
        try:
            events_client.put_events(Entries=[{
                "Source": EVENT_SOURCE,
                "DetailType": "incident.analyzed",
                "Detail": json.dumps({
                    "incident_id": incident_id,
                    "collector_run_id": collector_run_id,
                    "packet_hash": sha256,
                    "packet_ref": {
                        "s3_bucket": evidence_bucket,
                        "s3_key": packet_key,
                        "sha256": sha256,
                        "byte_size": len(body),
                    },
                    "snapshot_ref": {
                        "s3_bucket": evidence_bucket,
                        "s3_key": evidence_key,
                        "sha256": evidence_sha256,
                    },
                    "suspected_owners": suspected_owners,
                    "top_findings": [
                        {"id": f.get("id", ""), "summary": f.get("summary", "")[:200], "confidence": f.get("confidence", 0)}
                        for f in findings[:5]
                    ],
                    "emitted_at": created_at,
                    "created_at": created_at,
                    "service": service,
                    "environment": environment,
                }, default=str),
                "EventBusName": EVENT_BUS_NAME,
            }])
        except Exception as e:
            print(json.dumps({"msg": "eventbridge_emit_failed", "error": str(e)[:300]}))

    print(json.dumps({"msg": "analyzer_done", "incident_id": incident_id, "packet_key": packet_key}))

    return {
        "ok": True,
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "packet_key": packet_key,
        "packet_sha256": sha256,
    }
