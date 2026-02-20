"""EventBridge domain event emitter for OpsRunbook Copilot."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

EVENT_SOURCE = "opsrunbook-copilot"


class EventBridgeEmitter:
    def __init__(self, *, region: str, event_bus_name: str):
        self._client = boto3.client("events", region_name=region)
        self._bus = event_bus_name

    def emit_evidence_collected(
        self,
        *,
        incident_id: str,
        collector_run_id: str,
        collector_type: str,
        evidence_ref: dict[str, Any],
        time_window: dict[str, str],
        service: str,
    ) -> None:
        self._put(
            detail_type="evidence.collected",
            detail={
                "incident_id": incident_id,
                "collector_run_id": collector_run_id,
                "collector_type": collector_type,
                "evidence_ref": evidence_ref,
                "time_window": time_window,
                "service": service,
                "emitted_at": _now_iso(),
            },
        )

    def emit_incident_analyzed(
        self,
        *,
        incident_id: str,
        collector_run_id: str,
        service: str,
        evidence_refs: list[dict[str, Any]],
    ) -> None:
        self._put(
            detail_type="incident.analyzed",
            detail={
                "incident_id": incident_id,
                "collector_run_id": collector_run_id,
                "service": service,
                "evidence_refs": evidence_refs,
                "emitted_at": _now_iso(),
            },
        )

    def _put(self, *, detail_type: str, detail: dict[str, Any]) -> None:
        self._client.put_events(
            Entries=[
                {
                    "Source": EVENT_SOURCE,
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail, default=str),
                    "EventBusName": self._bus,
                }
            ]
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
