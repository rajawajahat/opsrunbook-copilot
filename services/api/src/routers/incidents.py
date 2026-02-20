"""
Incident endpoints – v1 hardened.

POST /v1/incidents       → start async orchestration via Step Functions
GET  /v1/incidents/{id}/runs/{run_id} → poll execution status + evidence refs
GET  /v1/incidents/{id}  → latest snapshot (kept from iter-1)
GET  /v1/incidents/{id}/evidence → latest evidence payload (kept from iter-1)
GET  /v1/incidents/{id}/meta     → incident metadata (kept from iter-1)
GET  /v1/incidents/{id}/snapshot/latest → latest snapshot record
GET  /v1/incidents/{id}/packet/latest → latest IncidentPacket (iter-3)
GET  /v1/incidents/{id}/packet/{run_id} → packet for specific run (iter-3)
GET  /v1/incidents/{id}/actions/latest → latest ActionPlan + results (iter-4)
GET  /v1/incidents/{id}/actions        → list action results (iter-4)
POST /v1/incidents/{id}/replay         → replay harness (v1 hardening)
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import boto3
from fastapi import APIRouter, HTTPException

from src.models import (
    CreateIncidentRequest,
    CreateIncidentResponse,
    EvidenceRef,
    RunStatusResponse,
)
from src.evidence.time_window import clamp_time_window
from src.settings import load_settings
from src.stores.dynamo_store import (
    DynamoStore,
    IncidentRecord,
    SnapshotRecord,
    make_snapshot_sk,
)
from src.stores.s3_store import S3EvidenceStore
from src.stores.snapshots_store import SnapshotsStore
from src.stores.packets_store import PacketsStore
from src.stores.actions_store import ActionsStore

router = APIRouter(prefix="/v1/incidents", tags=["incidents"])
_settings = load_settings()
snapshots = SnapshotsStore(_settings.snapshots_table, _settings.aws_region)
packets_store = PacketsStore(_settings.packets_table, _settings.aws_region) if _settings.packets_table else None
actions_store = ActionsStore(_settings.incidents_table, _settings.aws_region)
s3 = S3EvidenceStore(region=_settings.aws_region)


def _parse_dt(v):
    """Parse time_window start/end: datetime (return as-is), ISO str (Z or +00:00), else ValueError."""
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        s = v.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError) as e:
            raise ValueError(f"time_window start/end must be ISO datetime string: {e}") from e
    raise ValueError("time_window start/end must be ISO datetime string or datetime object")


# ─── POST /v1/incidents ────────────────────────────────────────────
@router.post("", response_model=CreateIncidentResponse)
def create_incident(event: CreateIncidentRequest):
    settings = load_settings()

    if not settings.state_machine_arn:
        raise HTTPException(
            status_code=503,
            detail="Orchestrator not configured (STATE_MACHINE_ARN missing)",
        )

    ddb = DynamoStore(region=settings.aws_region)

    # IDs
    incident_id = event.incident_id or f"inc-{uuid4().hex[:12]}"
    collector_run_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()

    # Time window (clamp)
    start = _parse_dt(event.time_window.start)
    end = _parse_dt(event.time_window.end)
    start2, end2, clamped = clamp_time_window(
        start=start, end=end, max_minutes=settings.max_time_window_minutes,
    )

    # Store incident metadata
    ddb.put_incident(
        table_name=settings.incidents_table,
        rec=IncidentRecord(
            incident_id=incident_id,
            service=event.service,
            environment=event.environment,
            created_at=created_at,
            source=event.source,
            event_id=event.event_id,
            tenant_id=event.tenant_id,
        ),
    )

    # Store run record (sk=RUN#<run_id>)
    ddb.put_run(
        table_name=settings.snapshots_table,
        incident_id=incident_id,
        collector_run_id=collector_run_id,
        created_at=created_at,
        execution_arn="pending",
        status="STARTING",
    )

    # Default metric_queries for loggen (minimal MVP)
    metric_queries = [mq.model_dump() for mq in event.hints.metric_queries]
    if not metric_queries and event.service == "loggen":
        fn_name = "opsrunbook-copilot-dev-loggen"
        metric_queries = [
            {"namespace": "AWS/Lambda", "metric_name": "Invocations", "dimensions": {"FunctionName": fn_name}, "period": 300, "stat": "Sum"},
            {"namespace": "AWS/Lambda", "metric_name": "Errors", "dimensions": {"FunctionName": fn_name}, "period": 300, "stat": "Sum"},
            {"namespace": "AWS/Lambda", "metric_name": "Duration", "dimensions": {"FunctionName": fn_name}, "period": 300, "stat": "p95"},
            {"namespace": "AWS/Lambda", "metric_name": "Throttles", "dimensions": {"FunctionName": fn_name}, "period": 300, "stat": "Sum"},
        ]

    # Build SFN input
    sfn_input = {
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "service": event.service,
        "environment": event.environment,
        "time_window": {
            "start": start2.isoformat(),
            "end": end2.isoformat(),
        },
        "hints": {
            "log_groups": event.hints.log_groups,
            "metric_queries": metric_queries,
            "state_machine_arns": event.hints.state_machine_arns,
        },
        "evidence_bucket": settings.evidence_bucket,
        "event_bus_name": settings.event_bus_name,
    }

    # Start execution
    sfn = boto3.client("stepfunctions", region_name=settings.aws_region)
    try:
        resp = sfn.start_execution(
            stateMachineArn=settings.state_machine_arn,
            name=collector_run_id,
            input=json.dumps(sfn_input, default=str),
        )
        execution_arn = resp["executionArn"]
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to start orchestration: {exc}")

    # Update run record with actual execution_arn
    ddb.put_run(
        table_name=settings.snapshots_table,
        incident_id=incident_id,
        collector_run_id=collector_run_id,
        created_at=created_at,
        execution_arn=execution_arn,
        status="RUNNING",
    )

    # Async: snapshot_sk and evidence are null until orchestration completes
    return CreateIncidentResponse(
        ok=True,
        incident_id=incident_id,
        execution_arn=execution_arn,
        collector_run_id=collector_run_id,
        snapshot_sk=None,
        evidence=None,
    )


# ─── GET /v1/incidents/{id}/runs/{run_id} ─────────────────────────
@router.get("/{incident_id}/runs/{collector_run_id}", response_model=RunStatusResponse)
def get_run_status(incident_id: str, collector_run_id: str):
    settings = load_settings()
    ddb = DynamoStore(region=settings.aws_region)

    run = ddb.get_run(
        table_name=settings.snapshots_table,
        incident_id=incident_id,
        collector_run_id=collector_run_id,
    )
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    execution_arn = run.get("execution_arn", "")
    if not execution_arn or execution_arn == "pending":
        return RunStatusResponse(
            incident_id=incident_id,
            collector_run_id=collector_run_id,
            execution_arn=execution_arn,
            status="STARTING",
        )

    # Describe execution from SFN
    sfn = boto3.client("stepfunctions", region_name=settings.aws_region)
    try:
        desc = sfn.describe_execution(executionArn=execution_arn)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Cannot describe execution: {exc}")

    status = desc.get("status", "UNKNOWN")
    evidence_refs: list[EvidenceRef] = []
    error_msg = None

    if status == "SUCCEEDED":
        output = desc.get("output")
        if output:
            evidence_refs = _parse_evidence_refs(output)

    if status in ("FAILED", "TIMED_OUT", "ABORTED"):
        error_msg = desc.get("error") or desc.get("cause")

    return RunStatusResponse(
        incident_id=incident_id,
        collector_run_id=collector_run_id,
        execution_arn=execution_arn,
        status=status,
        evidence_refs=evidence_refs,
        error=error_msg,
    )


def _parse_evidence_refs(output_json: str) -> list[EvidenceRef]:
    """Parse the SFN execution output to extract evidence refs from parallel branches."""
    refs = []
    try:
        data = json.loads(output_json)
        results = data.get("results", data) if isinstance(data, dict) else data
        if isinstance(results, list):
            for branch_result in results:
                if isinstance(branch_result, dict):
                    eref = branch_result.get("evidence_ref")
                    if eref and isinstance(eref, dict) and eref.get("s3_key"):
                        refs.append(
                            EvidenceRef(
                                collector_type=eref.get("collector_type", "unknown"),
                                s3_bucket=eref.get("s3_bucket", ""),
                                s3_key=eref.get("s3_key", ""),
                                sha256=eref.get("sha256", ""),
                                byte_size=eref.get("byte_size", 0),
                                truncated=eref.get("truncated", False),
                            )
                        )
    except (json.JSONDecodeError, KeyError):
        pass
    return refs


# ─── Legacy / iter-1 endpoints (preserved) ────────────────────────

@router.get("/{incident_id}")
def get_incident_latest(incident_id: str):
    item = snapshots.latest_for_incident(incident_id)
    if not item:
        raise HTTPException(status_code=404, detail="Incident not found")
    return {"ok": True, "latest_snapshot": item}


@router.get("/{incident_id}/evidence")
def get_latest_evidence(incident_id: str):
    item = snapshots.latest_for_incident(incident_id)
    if not item:
        raise HTTPException(status_code=404, detail="Incident not found")
    bucket = item.get("evidence_bucket")
    key = item.get("evidence_key")
    if not bucket or not key:
        raise HTTPException(status_code=404, detail="Evidence not found for incident")
    payload = s3.get_json(bucket=bucket, key=key)
    return {"ok": True, "incident_id": incident_id, "evidence": payload}


@router.get("/{incident_id}/meta")
def get_incident(incident_id: str):
    settings = load_settings()
    ddb = DynamoStore(region=settings.aws_region)
    item = ddb.get_incident(table_name=settings.incidents_table, incident_id=incident_id)
    if not item:
        raise HTTPException(status_code=404, detail="incident not found")
    return item


@router.get("/{incident_id}/snapshot")
def get_snapshot(incident_id: str):
    """Return the latest snapshot for the incident (sk begins_with SNAPSHOT# only). 404 if none."""
    settings = load_settings()
    ddb = DynamoStore(region=settings.aws_region)
    item = ddb.get_latest_snapshot(
        table_name=settings.snapshots_table,
        incident_id=incident_id,
    )
    if not item:
        raise HTTPException(status_code=404, detail="snapshot not found")
    return item


@router.get("/{incident_id}/snapshot/latest")
def get_latest_snapshot(incident_id: str):
    """Alias for GET /{incident_id}/snapshot."""
    return get_snapshot(incident_id)


# ─── Packet endpoints (iter-3) ────────────────────────────────────

@router.get("/{incident_id}/packet/latest")
def get_latest_packet(incident_id: str):
    if not packets_store:
        raise HTTPException(status_code=503, detail="Packets table not configured (PACKETS_TABLE missing)")
    item = packets_store.latest_for_incident(incident_id)
    if not item:
        raise HTTPException(status_code=404, detail="packet not found")
    bucket = item.get("packet_bucket")
    key = item.get("packet_key")
    if not bucket or not key:
        return {"ok": True, "incident_id": incident_id, "packet_meta": item}
    try:
        packet_json = s3.get_json(bucket=bucket, key=key)
    except Exception:
        raise HTTPException(status_code=404, detail="packet S3 object not found")
    return {"ok": True, "incident_id": incident_id, "packet": packet_json}


@router.get("/{incident_id}/packet/{collector_run_id}")
def get_packet_by_run(incident_id: str, collector_run_id: str):
    if not packets_store:
        raise HTTPException(status_code=503, detail="Packets table not configured (PACKETS_TABLE missing)")
    item = packets_store.get_by_run_id(incident_id, collector_run_id)
    if not item:
        raise HTTPException(status_code=404, detail="packet not found")
    bucket = item.get("packet_bucket")
    key = item.get("packet_key")
    if not bucket or not key:
        return {"ok": True, "incident_id": incident_id, "packet_meta": item}
    try:
        packet_json = s3.get_json(bucket=bucket, key=key)
    except Exception:
        raise HTTPException(status_code=404, detail="packet S3 object not found")
    return {"ok": True, "incident_id": incident_id, "packet": packet_json}


# ─── Action endpoints (iter-4) ────────────────────────────────────

@router.get("/{incident_id}/actions/latest")
def get_latest_actions(incident_id: str):
    data = actions_store.get_latest(incident_id)
    if not data:
        raise HTTPException(status_code=404, detail="actions not found")
    return {"ok": True, **data}


@router.get("/{incident_id}/actions")
def list_actions(incident_id: str):
    items = actions_store.list_actions(incident_id)
    if not items:
        raise HTTPException(status_code=404, detail="actions not found")
    return {"ok": True, "incident_id": incident_id, "actions": items}


# ─── Replay harness (v1 hardening) ────────────────────────────────

@router.post("/{incident_id}/replay")
def replay_incident(incident_id: str):
    """Replay an incident: re-fetch packet, re-generate plan, compare with stored.

    Returns diff if the new plan differs from what was previously stored.
    Does NOT execute actions — analysis-only comparison.
    """
    if not packets_store:
        raise HTTPException(status_code=503, detail="Packets table not configured")

    # Load existing packet
    packet_meta = packets_store.latest_for_incident(incident_id)
    if not packet_meta:
        raise HTTPException(status_code=404, detail="No packet found for incident")

    bucket = packet_meta.get("packet_bucket")
    key = packet_meta.get("packet_key")
    if not bucket or not key:
        raise HTTPException(status_code=404, detail="Packet S3 ref missing")

    try:
        packet = s3.get_json(bucket=bucket, key=key)
    except Exception:
        raise HTTPException(status_code=404, detail="Packet S3 object not found")

    # Load existing actions
    existing_actions = actions_store.get_latest(incident_id)
    existing_plan = existing_actions.get("action_plan", {}) if existing_actions else {}

    # Re-generate plan from packet (deterministic)
    import sys
    import os
    _actions_src = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", "..",
        "infra", "terraform", "modules", "actions_runner", "src",
    )
    _actions_src = os.path.abspath(_actions_src)
    if _actions_src not in sys.path:
        sys.path.insert(0, _actions_src)

    from plan_generator import generate_action_plan
    new_plan = generate_action_plan(packet, dry_run=True)

    # Compute hashes for comparison (ignore timestamps)
    def _plan_hash(plan: dict) -> str:
        stable = {
            "incident_id": plan.get("incident_id"),
            "service": plan.get("service"),
            "environment": plan.get("environment"),
            "action_types": sorted(a.get("action_type", "") for a in plan.get("actions", [])),
            "action_count": len(plan.get("actions", [])),
            "suspected_owners": plan.get("suspected_owners", []),
        }
        return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()

    existing_hash = _plan_hash(existing_plan) if existing_plan else ""
    new_hash = _plan_hash(new_plan)

    packet_hash = packet.get("packet_hashes", {}).get("sha256", "")

    diffs: list[str] = []
    if existing_hash != new_hash:
        if len(existing_plan.get("actions", [])) != len(new_plan.get("actions", [])):
            diffs.append(f"action_count: {len(existing_plan.get('actions', []))} → {len(new_plan.get('actions', []))}")
        old_types = sorted(a.get("action_type", "") for a in existing_plan.get("actions", []))
        new_types = sorted(a.get("action_type", "") for a in new_plan.get("actions", []))
        if old_types != new_types:
            diffs.append(f"action_types: {old_types} → {new_types}")
        if existing_plan.get("suspected_owners") != new_plan.get("suspected_owners"):
            diffs.append("suspected_owners changed")

    return {
        "ok": True,
        "incident_id": incident_id,
        "packet_hash": packet_hash,
        "existing_plan_hash": existing_hash,
        "new_plan_hash": new_hash,
        "match": existing_hash == new_hash,
        "diffs": diffs,
        "new_plan_preview": {
            "action_count": len(new_plan.get("actions", [])),
            "action_types": [a.get("action_type", "") for a in new_plan.get("actions", [])],
            "suspected_owners": new_plan.get("suspected_owners", []),
        },
    }
