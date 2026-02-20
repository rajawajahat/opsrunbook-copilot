"""
Lambda handler: Step Functions evidence collector.

Input from Step Functions:
- incident_id, collector_run_id, evidence_bucket, event_bus_name, service
- time_window: { start, end }
- orchestrator_execution_arn ($$.Execution.Id) – always collect this execution
- orchestrator_state_machine_arn ($$.StateMachine.Id)
- state_machine_arns (from hints) – optional, collect recent failed executions in window
"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

import boto3

MAX_EXECUTIONS = 20
MAX_ERROR_LENGTH = 1000
MAX_HISTORY_TAIL = 50
MAX_INPUT_OUTPUT_BYTES = 2000
MAX_BYTES = 200_000
FAILED_STATUSES = ["FAILED", "TIMED_OUT", "ABORTED"]
EVENT_SOURCE = "opsrunbook-copilot"

sfn_client = boto3.client("stepfunctions")
s3_client = boto3.client("s3")
events_client = boto3.client("events")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(s: Optional[str], max_len: int) -> Optional[str]:
    if s is None:
        return None
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...[truncated]"


def _to_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def _ts(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v)


def _infer_last_failed_state(events: list[dict]) -> Optional[str]:
    """Walk history events (newest-first) and return the name of the last failed state."""
    for evt in events:
        etype = evt.get("type", "")
        if "Failed" not in etype and "TimedOut" not in etype and "Aborted" not in etype:
            continue
        details = (
            evt.get("taskFailedEventDetails")
            or evt.get("executionFailedEventDetails")
            or evt.get("lambdaFunctionFailedEventDetails")
            or {}
        )
        if details.get("name"):
            return details["name"]
        # Fall back: scan for preceding TaskStateEntered
        for prev in events:
            if prev.get("type") == "TaskStateEntered":
                sd = prev.get("stateEnteredEventDetails", {})
                if sd.get("name"):
                    return sd["name"]
        return etype
    return None


# ---------------------------------------------------------------------------
# Orchestrator execution section
# ---------------------------------------------------------------------------

def _collect_orchestrator_execution(execution_arn: str, state_machine_arn: Optional[str]) -> dict[str, Any]:
    """DescribeExecution + GetExecutionHistory (bounded, reverseOrder). Returns a section dict."""
    section: dict[str, Any] = {
        "name": "orchestrator_execution",
        "execution_arn": execution_arn,
        "state_machine_arn": state_machine_arn,
        "status": None,
        "start_date": None,
        "stop_date": None,
        "input": None,
        "output": None,
        "error": None,
        "cause": None,
        "last_failed_state": None,
        "history_events_count": 0,
        "history_tail": [],
    }

    try:
        desc = sfn_client.describe_execution(executionArn=execution_arn)
        section["status"] = desc.get("status")
        section["start_date"] = _ts(desc.get("startDate"))
        section["stop_date"] = _ts(desc.get("stopDate"))
        section["error"] = _truncate(desc.get("error"), MAX_ERROR_LENGTH)
        section["cause"] = _truncate(desc.get("cause"), MAX_ERROR_LENGTH)
        inp = desc.get("input")
        section["input"] = _truncate(inp, MAX_INPUT_OUTPUT_BYTES) if isinstance(inp, str) else inp
        out = desc.get("output")
        section["output"] = _truncate(out, MAX_INPUT_OUTPUT_BYTES) if isinstance(out, str) else out
    except Exception as e:
        section["error"] = str(e)[:MAX_ERROR_LENGTH]

    raw_events: list[dict] = []
    try:
        hist = sfn_client.get_execution_history(
            executionArn=execution_arn,
            maxResults=MAX_HISTORY_TAIL,
            reverseOrder=True,
        )
        raw_events = hist.get("events", [])
        section["history_events_count"] = len(raw_events)

        tail = []
        for evt in raw_events[:MAX_HISTORY_TAIL]:
            entry: dict[str, Any] = {
                "id": evt.get("id"),
                "type": evt.get("type"),
                "timestamp": _ts(evt.get("timestamp")),
            }
            etype = evt.get("type", "")
            if "Failed" in etype or "TimedOut" in etype:
                details = (
                    evt.get("executionFailedEventDetails")
                    or evt.get("taskFailedEventDetails")
                    or evt.get("lambdaFunctionFailedEventDetails")
                    or {}
                )
                entry["error"] = _truncate(details.get("error"), 200)
                entry["cause"] = _truncate(details.get("cause"), 300)
            tail.append(entry)
        section["history_tail"] = tail
    except Exception as e:
        section["history_error"] = str(e)[:500]

    section["last_failed_state"] = _infer_last_failed_state(raw_events)
    return section


# ---------------------------------------------------------------------------
# Failed executions section (hints.state_machine_arns)
# ---------------------------------------------------------------------------

def _list_failed(sm_arn: str, status: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    results = []
    next_token = None
    while True:
        kwargs: dict = {
            "stateMachineArn": sm_arn,
            "statusFilter": status,
            "maxResults": 100,
        }
        if next_token:
            kwargs["nextToken"] = next_token
        resp = sfn_client.list_executions(**kwargs)
        for ex in resp.get("executions", []):
            sd = ex.get("startDate")
            if sd and sd < start_dt:
                return results
            if sd and sd > end_dt:
                continue
            results.append({
                "execution_arn": ex["executionArn"],
                "state_machine_arn": sm_arn,
                "name": ex.get("name", ""),
                "status": ex.get("status", status),
                "start_date": _ts(sd),
                "stop_date": _ts(ex.get("stopDate")),
            })
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return results


def _enrich_failed(ex: dict) -> dict:
    arn = ex["execution_arn"]
    try:
        desc = sfn_client.describe_execution(executionArn=arn)
        ex["error"] = _truncate(desc.get("error"), MAX_ERROR_LENGTH)
        ex["cause"] = _truncate(desc.get("cause"), MAX_ERROR_LENGTH)
    except Exception:
        pass
    try:
        hist = sfn_client.get_execution_history(
            executionArn=arn, maxResults=MAX_HISTORY_TAIL, reverseOrder=True
        )
        events = hist.get("events", [])
        ex["last_failed_state"] = _infer_last_failed_state(events)
    except Exception:
        pass
    return ex


# ---------------------------------------------------------------------------
# MAX_BYTES enforcement (staged)
# ---------------------------------------------------------------------------

def _enforce_budget(payload: dict, sections: list[dict]) -> tuple[bytes, bool]:
    """Serialize; if over budget, first drop history_tail, then truncate error/cause."""
    body = _to_bytes(payload)
    if len(body) <= MAX_BYTES:
        return body, False

    # Stage 1: drop history_tail from orchestrator section
    for sec in sections:
        if sec.get("name") == "orchestrator_execution":
            sec["history_tail"] = sec.get("history_tail", [])[:5]
            sec.pop("input", None)
            sec.pop("output", None)
    body = _to_bytes(payload)
    if len(body) <= MAX_BYTES:
        return body, True

    # Stage 2: drop history_tail entirely
    for sec in sections:
        if sec.get("name") == "orchestrator_execution":
            sec["history_tail"] = []
    body = _to_bytes(payload)
    if len(body) <= MAX_BYTES:
        return body, True

    # Stage 3: truncate error/cause everywhere
    for sec in sections:
        if sec.get("name") == "orchestrator_execution":
            sec["error"] = _truncate(sec.get("error"), 200)
            sec["cause"] = _truncate(sec.get("cause"), 200)
        if sec.get("name") == "failed_executions":
            for ex in sec.get("executions", []):
                ex["error"] = _truncate(ex.get("error"), 200)
                ex["cause"] = _truncate(ex.get("cause"), 200)
    body = _to_bytes(payload)
    return body, True


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict, context: Any) -> dict:
    incident_id = event["incident_id"]
    collector_run_id = event["collector_run_id"]
    evidence_bucket = event["evidence_bucket"]
    event_bus = event.get("event_bus_name", "")
    tw = event["time_window"]
    service = event.get("service", "")
    orchestrator_execution_arn = event.get("orchestrator_execution_arn")
    orchestrator_state_machine_arn = event.get("orchestrator_state_machine_arn")
    state_machine_arns = event.get("state_machine_arns") or []

    sections: list[dict] = []
    truncated = False

    # 1. Always collect orchestrator execution when ARN is provided
    if orchestrator_execution_arn:
        sections.append(
            _collect_orchestrator_execution(orchestrator_execution_arn, orchestrator_state_machine_arn)
        )

    # 2. Optionally collect recent failed executions for configured state machines
    if state_machine_arns:
        start_dt = datetime.fromisoformat(tw["start"].replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(tw["end"].replace("Z", "+00:00"))
        all_failed = []
        for arn in state_machine_arns:
            for status in FAILED_STATUSES:
                all_failed.extend(_list_failed(arn, status, start_dt, end_dt))

        # De-dup: remove orchestrator execution from the failed list
        if orchestrator_execution_arn:
            all_failed = [e for e in all_failed if e["execution_arn"] != orchestrator_execution_arn]

        all_failed.sort(key=lambda e: e.get("start_date") or "", reverse=True)
        total_found = len(all_failed)
        if total_found > MAX_EXECUTIONS:
            truncated = True
        all_failed = all_failed[:MAX_EXECUTIONS]
        enriched = [_enrich_failed(ex) for ex in all_failed]
        sections.append({
            "name": "failed_executions",
            "state_machine_arns": state_machine_arns,
            "total_found": total_found,
            "executions": enriched,
        })

    # 3. Nothing to persist → skipped
    if not sections:
        return {
            "collector_type": "stepfn",
            "incident_id": incident_id,
            "collector_run_id": collector_run_id,
            "skipped": True,
            "evidence_ref": None,
            "error": None,
            "cause": None,
        }

    # 4. Build payload and enforce byte budget
    payload = {
        "schema": "evidence.v1",
        "collector_type": "stepfn",
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "time_window": tw,
        "redaction": {"enabled": False},
        "sections": sections,
    }

    body, budget_truncated = _enforce_budget(payload, sections)
    truncated = truncated or budget_truncated

    sha = hashlib.sha256(body).hexdigest()
    key = f"evidence/{incident_id}/{collector_run_id}/stepfn.json"
    s3_client.put_object(Bucket=evidence_bucket, Key=key, Body=body, ContentType="application/json")

    evidence_ref = {
        "collector_type": "stepfn",
        "s3_bucket": evidence_bucket,
        "s3_key": key,
        "sha256": sha,
        "byte_size": len(body),
        "truncated": truncated,
    }

    if event_bus:
        _emit_event(event_bus, incident_id, collector_run_id, "stepfn", evidence_ref, tw, service)

    return {
        "collector_type": "stepfn",
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "skipped": False,
        "evidence_ref": evidence_ref,
        "error": None,
        "cause": None,
    }


def _emit_event(bus: str, incident_id: str, run_id: str, collector_type: str, evidence_ref: dict, tw: dict, service: str) -> None:
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
