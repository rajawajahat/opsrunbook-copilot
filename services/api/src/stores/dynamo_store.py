from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import boto3


def _pk_incident(incident_id: str) -> str:
    return f"INCIDENT#{incident_id}"


@dataclass(frozen=True)
class IncidentRecord:
    incident_id: str
    service: str
    environment: str
    created_at: str  # ISO
    source: str
    event_id: str
    tenant_id: Optional[str] = None


@dataclass(frozen=True)
class SnapshotRecord:
    incident_id: str
    snapshot_sk: str
    created_at: str  # ISO
    collector_run_id: str
    evidence_bucket: str
    evidence_key: str
    evidence_sha256: str
    evidence_byte_size: int
    truncated: bool


class DynamoStore:
    def __init__(self, *, region: str):
        self._ddb = boto3.resource("dynamodb", region_name=region)

    def put_incident(
        self,
        *,
        table_name: str,
        rec: IncidentRecord,
    ) -> None:
        table = self._ddb.Table(table_name)
        item: dict[str, Any] = {
            "pk": _pk_incident(rec.incident_id),
            "sk": "META",
            "incident_id": rec.incident_id,
            "service": rec.service,
            "environment": rec.environment,
            "created_at": rec.created_at,
            "source": rec.source,
            "event_id": rec.event_id,
        }
        if rec.tenant_id:
            item["tenant_id"] = rec.tenant_id

        table.put_item(Item=item)

    def get_incident(self, *, table_name: str, incident_id: str) -> Optional[dict[str, Any]]:
        table = self._ddb.Table(table_name)
        resp = table.get_item(Key={"pk": _pk_incident(incident_id), "sk": "META"})
        return resp.get("Item")

    def put_snapshot(
        self,
        *,
        table_name: str,
        rec: SnapshotRecord,
    ) -> None:
        table = self._ddb.Table(table_name)
        item: dict[str, Any] = {
            "pk": _pk_incident(rec.incident_id),
            "sk": rec.snapshot_sk,
            "incident_id": rec.incident_id,
            "created_at": rec.created_at,
            "collector_run_id": rec.collector_run_id,
            "evidence_bucket": rec.evidence_bucket,
            "evidence_key": rec.evidence_key,
            "evidence_sha256": rec.evidence_sha256,
            "evidence_byte_size": rec.evidence_byte_size,
            "truncated": rec.truncated,
        }
        table.put_item(Item=item)

    def get_latest_snapshot(
        self,
        *,
        table_name: str,
        incident_id: str,
    ) -> Optional[dict[str, Any]]:
        """
        Since sk includes created_at ISO at the front, descending sort gives latest first.
        """
        table = self._ddb.Table(table_name)
        resp = table.query(
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": _pk_incident(incident_id),
                ":prefix": "SNAPSHOT#",
            },
            ScanIndexForward=False,  # descending
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None


    # ── Run tracking (iter-2) ─────────────────────────────────────
    def put_run(
        self,
        *,
        table_name: str,
        incident_id: str,
        collector_run_id: str,
        created_at: str,
        execution_arn: str,
        status: str,
    ) -> None:
        table = self._ddb.Table(table_name)
        table.put_item(
            Item={
                "pk": _pk_incident(incident_id),
                "sk": f"RUN#{collector_run_id}",
                "incident_id": incident_id,
                "collector_run_id": collector_run_id,
                "created_at": created_at,
                "execution_arn": execution_arn,
                "status": status,
            }
        )

    def get_run(
        self,
        *,
        table_name: str,
        incident_id: str,
        collector_run_id: str,
    ) -> Optional[dict[str, Any]]:
        table = self._ddb.Table(table_name)
        resp = table.get_item(
            Key={
                "pk": _pk_incident(incident_id),
                "sk": f"RUN#{collector_run_id}",
            }
        )
        return resp.get("Item")


def make_snapshot_sk(*, created_at_iso: str, collector_run_id: str) -> str:
    return f"SNAPSHOT#{created_at_iso}#{collector_run_id}"
