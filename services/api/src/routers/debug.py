from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter

from ..settings import load_settings
from ..stores.s3_store import S3EvidenceStore
from ..stores.dynamo_store import DynamoStore, IncidentRecord, SnapshotRecord, make_snapshot_sk

router = APIRouter(prefix="/debug", tags=["debug"])


@router.post("/persist")
def debug_persist():
    
    settings = load_settings()
    s3_store = S3EvidenceStore(region=settings.aws_region)
    ddb_store = DynamoStore(region=settings.aws_region)

    incident_id = f"inc-{uuid4().hex[:12]}"
    collector_run_id = uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()

    evidence_payload = {"note": "debug persist", "incident_id": incident_id, "created_at": created_at}
    evidence_key = f"evidence/{incident_id}/{collector_run_id}.json"

    put_res = s3_store.put_json(
        bucket=settings.evidence_bucket,
        key=evidence_key,
        payload=evidence_payload,
    )

    ddb_store.put_incident(
        table_name=settings.incidents_table,
        rec=IncidentRecord(
            incident_id=incident_id,
            service="debug",
            environment="dev",
            created_at=created_at,
            source="debug",
            event_id=f"evt-{uuid4().hex[:10]}",
            tenant_id=None,
        ),
    )

    snapshot_sk = make_snapshot_sk(created_at_iso=created_at, collector_run_id=collector_run_id)
    ddb_store.put_snapshot(
        table_name=settings.snapshots_table,
        rec=SnapshotRecord(
            incident_id=incident_id,
            snapshot_sk=snapshot_sk,
            created_at=created_at,
            collector_run_id=collector_run_id,
            evidence_bucket=put_res.bucket,
            evidence_key=put_res.key,
            evidence_sha256=put_res.sha256,
            evidence_byte_size=put_res.byte_size,
            truncated=False,
        ),
    )

    return {
        "ok": True,
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "evidence": put_res.__dict__,
        "snapshot_sk": snapshot_sk,
    }
