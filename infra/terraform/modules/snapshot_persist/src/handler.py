"""
Lambda: persist aggregated snapshot after orchestrator completes.

Input from Step Functions:
{
  "context": {
    "incident_id": "inc-...",
    "collector_run_id": "...",
    "service": "...",
    "environment": "...",
    "time_window": {"start": "ISO", "end": "ISO"},
    "evidence_bucket": "bucket-name"
  },
  "results": [
    { "collector_type": "logs", "skipped": false, "evidence_ref": {...}, ... },
    ...
  ]
}

Writes:
- S3: evidence/<incident_id>/<collector_run_id>.json (single aggregated evidence payload)
- DynamoDB: pk=INCIDENT#<incident_id>, sk=SNAPSHOT#<created_at_iso>#<collector_run_id>
"""
import hashlib
import json
import os
from datetime import datetime, timezone

import boto3

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")


def _to_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")


def lambda_handler(event, context):
    ctx = event["context"]
    results = event.get("results", [])

    incident_id = ctx["incident_id"]
    collector_run_id = ctx["collector_run_id"]
    service = ctx.get("service", "")
    environment = ctx.get("environment", "dev")
    time_window = ctx.get("time_window", {})
    evidence_bucket = ctx["evidence_bucket"]
    snapshots_table = os.environ["SNAPSHOTS_TABLE"]

    created_at = datetime.now(timezone.utc).isoformat()

    # Build single aggregated evidence payload (refs + summary, no raw content)
    def _is_truncated_or_error(r):
        if not isinstance(r, dict):
            return False
        ref = r.get("evidence_ref") or {}
        return ref.get("truncated") or r.get("error")

    truncated = any(_is_truncated_or_error(r) for r in results)
    evidence_payload = {
        "schema": "evidence_snapshot.v1",
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "created_at": created_at,
        "service": service,
        "environment": environment,
        "time_window": time_window,
        "redaction": {"enabled": False},
        "collectors": [
            {
                "collector_type": r.get("collector_type", "unknown"),
                "skipped": r.get("skipped", True),
                "evidence_ref": r.get("evidence_ref"),
                "error": r.get("error"),
                "cause": r.get("cause"),
            }
            for r in results
            if isinstance(r, dict)
        ],
        "truncated": truncated,
    }

    body = _to_bytes(evidence_payload)
    sha256 = hashlib.sha256(body).hexdigest()
    byte_size = len(body)
    evidence_key = f"evidence/{incident_id}/{collector_run_id}.json"

    s3.put_object(
        Bucket=evidence_bucket,
        Key=evidence_key,
        Body=body,
        ContentType="application/json",
    )

    pk = f"INCIDENT#{incident_id}"
    sk = f"SNAPSHOT#{created_at}#{collector_run_id}"

    table = dynamodb.Table(snapshots_table)
    table.put_item(
        Item={
            "pk": pk,
            "sk": sk,
            "incident_id": incident_id,
            "created_at": created_at,
            "collector_run_id": collector_run_id,
            "evidence_bucket": evidence_bucket,
            "evidence_key": evidence_key,
            "evidence_sha256": sha256,
            "evidence_byte_size": byte_size,
            "truncated": truncated,
        }
    )

    # Emit evidence.snapshot.persisted event (best-effort)
    event_bus_name = os.environ.get("EVENT_BUS_NAME", "")
    if event_bus_name:
        try:
            events = boto3.client("events")
            events.put_events(Entries=[{
                "Source": "opsrunbook-copilot",
                "DetailType": "evidence.snapshot.persisted",
                "Detail": json.dumps({
                    "incident_id": incident_id,
                    "collector_run_id": collector_run_id,
                    "evidence_bucket": evidence_bucket,
                    "evidence_key": evidence_key,
                    "evidence_sha256": sha256,
                    "created_at": created_at,
                    "service": service,
                    "environment": environment,
                    "time_window": time_window,
                }, default=str),
                "EventBusName": event_bus_name,
            }])
        except Exception as e:
            print(json.dumps({"msg": "event_emit_failed", "error": str(e)[:300]}))

    return {
        "ok": True,
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "snapshot_sk": sk,
        "evidence_bucket": evidence_bucket,
        "evidence_key": evidence_key,
        "evidence_sha256": sha256,
        "evidence_byte_size": byte_size,
        "truncated": truncated,
    }
