"""
Lambda handler: CloudWatch Metrics collector.

Input from Step Functions:
{
  "incident_id": "inc-...",
  "collector_run_id": "...",
  "metric_queries": [{"namespace":"...", "metric_name":"...", "dimensions":{}, "period":300, "stat":"Average"}],
  "time_window": {"start": "ISO", "end": "ISO"},
  "evidence_bucket": "bucket-name",
  "event_bus_name": "bus-name",
  "service": "..."
}
"""
import hashlib
import json
import os
from datetime import datetime, timezone

import boto3

MAX_DATA_POINTS = 500
MAX_QUERIES = 20
MAX_BYTES = 200_000
EVENT_SOURCE = "opsrunbook-copilot"

cw_client = boto3.client("cloudwatch")
s3_client = boto3.client("s3")
events_client = boto3.client("events")


def _auto_period(start_dt, end_dt, desired_points=300):
    span = int((end_dt - start_dt).total_seconds())
    if span <= 0:
        return 60
    raw = span // desired_points
    for p in [60, 300, 900, 3600, 21600, 86400]:
        if p >= raw:
            return p
    return 86400


def _compute_summary(values):
    if not values:
        return {"min": None, "max": None, "avg": None, "count": 0}
    return {
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "avg": round(sum(values) / len(values), 6),
        "count": len(values),
    }


def _to_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


def lambda_handler(event, context):
    incident_id = event["incident_id"]
    collector_run_id = event["collector_run_id"]
    metric_queries = event.get("metric_queries", [])
    evidence_bucket = event["evidence_bucket"]
    event_bus = event.get("event_bus_name", "")
    tw = event["time_window"]
    service = event.get("service", "")

    if not metric_queries:
        return {
            "collector_type": "metrics",
            "incident_id": incident_id,
            "collector_run_id": collector_run_id,
            "skipped": True,
            "evidence_ref": None,
            "error": None,
            "cause": None,
        }

    start_dt = datetime.fromisoformat(tw["start"].replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(tw["end"].replace("Z", "+00:00"))

    bounded = metric_queries[:MAX_QUERIES]
    truncated_queries = len(metric_queries) > MAX_QUERIES

    cw_queries = []
    for idx, mq in enumerate(bounded):
        qid = f"m{idx}"
        period = mq.get("period", 300)
        if period < 60:
            period = _auto_period(start_dt, end_dt)
        dims = [{"Name": k, "Value": v} for k, v in mq.get("dimensions", {}).items()]
        cw_queries.append({
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": mq["namespace"],
                    "MetricName": mq["metric_name"],
                    "Dimensions": dims,
                },
                "Period": period,
                "Stat": mq.get("stat", "Average"),
            },
            "ReturnData": True,
        })

    all_results = []
    next_token = None
    while True:
        kwargs = {
            "MetricDataQueries": cw_queries,
            "StartTime": start_dt,
            "EndTime": end_dt,
        }
        if next_token:
            kwargs["NextToken"] = next_token
        resp = cw_client.get_metric_data(**kwargs)
        all_results.extend(resp.get("MetricDataResults", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break

    overall_truncated = truncated_queries
    series_list = []
    for r in all_results:
        timestamps = [t.isoformat() if isinstance(t, datetime) else str(t) for t in r.get("Timestamps", [])]
        values = list(r.get("Values", []))
        series_trunc = len(values) > MAX_DATA_POINTS
        if series_trunc:
            timestamps = timestamps[:MAX_DATA_POINTS]
            values = values[:MAX_DATA_POINTS]
            overall_truncated = True

        qid = r.get("Id", "")
        idx = int(qid[1:]) if qid.startswith("m") and qid[1:].isdigit() else 0
        orig = bounded[idx] if idx < len(bounded) else bounded[0]

        series_list.append({
            "query_id": qid,
            "label": r.get("Label", orig.get("metric_name", "")),
            "timestamps": timestamps,
            "values": values,
            "stat": orig.get("stat", "Average"),
            "period": orig.get("period", 300),
            "point_count": len(values),
            "truncated": series_trunc,
            "summary": _compute_summary(values),
        })

    payload = {
        "schema": "evidence.v1",
        "collector_type": "metrics",
        "incident_id": incident_id,
        "collector_run_id": collector_run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "time_window": tw,
        "redaction": {"enabled": False},
        "sections": [{"name": "metrics", "series": series_list}],
        "series": series_list,
    }

    body = _to_bytes(payload)
    if len(body) > MAX_BYTES:
        for s in series_list:
            half = len(s["values"]) // 2
            s["values"] = s["values"][:half]
            s["timestamps"] = s["timestamps"][:half]
            s["point_count"] = half
            s["truncated"] = True
        overall_truncated = True
        payload["series"] = series_list
        body = _to_bytes(payload)

    sha = hashlib.sha256(body).hexdigest()
    key = f"evidence/{incident_id}/{collector_run_id}/metrics.json"
    s3_client.put_object(Bucket=evidence_bucket, Key=key, Body=body, ContentType="application/json")

    evidence_ref = {
        "collector_type": "metrics",
        "s3_bucket": evidence_bucket,
        "s3_key": key,
        "sha256": sha,
        "byte_size": len(body),
        "truncated": overall_truncated,
    }

    if event_bus:
        _emit_event(event_bus, incident_id, collector_run_id, "metrics", evidence_ref, tw, service)

    return {
        "collector_type": "metrics",
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
