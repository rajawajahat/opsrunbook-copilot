"""
Lambda handler: CloudWatch Logs Insights collector.

Input from Step Functions:
{
  "incident_id": "inc-...",
  "collector_run_id": "...",
  "log_groups": ["/aws/lambda/..."],
  "time_window": {"start": "ISO", "end": "ISO"},
  "evidence_bucket": "bucket-name",
  "event_bus_name": "bus-name"
}

Returns evidence_ref dict or {"skipped": true}.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

import boto3

MAX_ROWS = 100
MAX_BYTES = 200_000
MAX_POLL_SECONDS = 30
EVENT_SOURCE = "opsrunbook-copilot"

logs_client = boto3.client("logs")
s3_client = boto3.client("s3")
events_client = boto3.client("events")

RECENT_ERRORS_QUERY = (
    "fields @timestamp, @message, @logStream\n"
    "| filter @message like /ERROR|Error|Exception|Traceback/\n"
    "| sort @timestamp desc\n"
    "| limit 50"
)
TOP_ERRORS_QUERY = (
    "fields @timestamp, @message\n"
    "| filter @message like /ERROR|Error|Exception|Traceback/\n"
    "| stats count() as cnt by @message\n"
    "| sort cnt desc\n"
    "| limit 20"
)

# Redaction patterns
_REDACT_PATTERNS = [
    (re.compile(r"(?i)\bAuthorization:\s*Bearer\s+[A-Za-z0-9\-\._~\+/]+=*\b"), "Authorization: Bearer [REDACTED]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-\._~\+/]+=*\b"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|token|access[_-]?token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9\-\._~\+/=]{8,}['\"]?"), r"\1=[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    (re.compile(r"(?i)\baws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{16,}['\"]?"), "aws_secret_access_key=[REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?[^'\"\s]{6,}['\"]?"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)\b(postgres|mysql|mongodb|redis)://[^ \n\r\t]+"), r"\1://[REDACTED]"),
]


def _redact(text):
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _redact_obj(obj):
    if obj is None:
        return None
    if isinstance(obj, str):
        return _redact(obj)
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    return obj


def _run_query(log_groups, query, start_epoch, end_epoch, limit):
    resp = logs_client.start_query(
        logGroupNames=log_groups,
        startTime=start_epoch,
        endTime=end_epoch,
        queryString=query,
        limit=limit,
    )
    qid = resp["queryId"]
    deadline = time.time() + MAX_POLL_SECONDS
    while time.time() < deadline:
        r = logs_client.get_query_results(queryId=qid)
        status = r.get("status", "Unknown")
        if status in ("Complete", "Failed", "Cancelled", "Timeout"):
            rows = []
            for row in r.get("results", []):
                item = {}
                for cell in row:
                    f = cell.get("field")
                    if f:
                        item[f] = cell.get("value")
                rows.append(item)
            return {"status": status, "rows": rows, "stats": r.get("statistics", {})}
        time.sleep(1)
    return {"status": "ClientTimeout", "rows": [], "stats": {}}


def _to_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def lambda_handler(event, context):
    incident_id = event["incident_id"]
    collector_run_id = event["collector_run_id"]
    log_groups = event.get("log_groups", [])
    evidence_bucket = event["evidence_bucket"]
    event_bus = event.get("event_bus_name", "")
    tw = event["time_window"]

    if not log_groups:
        return {
            "collector_type": "logs",
            "incident_id": incident_id,
            "collector_run_id": collector_run_id,
            "skipped": True,
            "evidence_ref": None,
            "error": None,
            "cause": None,
        }

    start_dt = datetime.fromisoformat(tw["start"].replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(tw["end"].replace("Z", "+00:00"))
    start_epoch = int(start_dt.timestamp())
    end_epoch = int(end_dt.timestamp())

    sections = []
    r1 = _run_query(log_groups, RECENT_ERRORS_QUERY, start_epoch, end_epoch, 50)
    sections.append({"name": "recent_errors", **r1})
    r2 = _run_query(log_groups, TOP_ERRORS_QUERY, start_epoch, end_epoch, 20)
    sections.append({"name": "top_errors", **r2})

    payload = {
        "schema": "evidence.v1",
        "collector_type": "logs",
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "time_window": tw,
        "redaction": {"enabled": False},
        "log_groups": log_groups,
        "sections": sections,
    }

    redacted = _redact_obj(payload)

    # Enforce budget
    truncated = False
    for sec in redacted.get("sections", []):
        rows = sec.get("rows", [])
        if len(rows) > MAX_ROWS:
            sec["rows"] = rows[:MAX_ROWS]
            truncated = True

    body = _to_bytes(redacted)
    if len(body) > MAX_BYTES:
        redacted["sections"] = [{"name": s.get("name", "?"), "note": "Dropped due to size budget"} for s in redacted.get("sections", [])]
        truncated = True
        body = _to_bytes(redacted)

    sha = hashlib.sha256(body).hexdigest()
    key = f"evidence/{incident_id}/{collector_run_id}/logs.json"

    s3_client.put_object(Bucket=evidence_bucket, Key=key, Body=body, ContentType="application/json")

    evidence_ref = {
        "collector_type": "logs",
        "s3_bucket": evidence_bucket,
        "s3_key": key,
        "sha256": sha,
        "byte_size": len(body),
        "truncated": truncated,
    }

    if event_bus:
        _emit_event(event_bus, incident_id, collector_run_id, "logs", evidence_ref, tw, event.get("service", ""))

    return {
        "collector_type": "logs",
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "skipped": False,
        "evidence_ref": evidence_ref,
        "error": None,
        "cause": None,
    }


def _emit_event(bus, incident_id, run_id, collector_type, evidence_ref, tw, service):
    try:
        events_client.put_events(Entries=[{
            "Source": EVENT_SOURCE,
            "DetailType": "evidence.collected",
            "Detail": json.dumps({
                "incident_id": incident_id,
                "collector_run_id": run_id,
                "collector_type": collector_type,
                "evidence_ref": evidence_ref,
                "time_window": tw,
                "service": service,
                "emitted_at": datetime.now(timezone.utc).isoformat(),
            }, default=str),
            "EventBusName": bus,
        }])
    except Exception as e:
        print(f"[WARN] EventBridge emit failed: {e}")
