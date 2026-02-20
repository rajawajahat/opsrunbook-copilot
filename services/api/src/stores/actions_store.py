"""Store for action plans and results – uses incidents table."""
import json

import boto3
from boto3.dynamodb.conditions import Key

from ..sanitize import sanitize as _sanitize


class ActionsStore:
    def __init__(self, table_name: str, region: str):
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def get_latest(self, incident_id: str) -> dict | None:
        pk = f"INCIDENT#{incident_id}"
        resp = self.table.get_item(Key={"pk": pk, "sk": "ACTIONS#LATEST"})
        item = resp.get("Item")
        if not item:
            return None

        result: dict = {"incident_id": incident_id}

        plan_sk = item.get("latest_actionplan_sk")
        if plan_sk:
            plan_resp = self.table.get_item(Key={"pk": pk, "sk": plan_sk})
            plan_item = plan_resp.get("Item")
            if plan_item and plan_item.get("plan"):
                try:
                    result["action_plan"] = json.loads(plan_item["plan"])
                except (json.JSONDecodeError, TypeError):
                    result["action_plan"] = plan_item.get("plan")
            else:
                result["action_plan"] = None
        else:
            result["action_plan"] = None

        action_sks_raw = item.get("latest_action_sks", "[]")
        try:
            action_sks = json.loads(action_sks_raw) if isinstance(action_sks_raw, str) else action_sks_raw
        except (json.JSONDecodeError, TypeError):
            action_sks = []

        results = []
        for sk in action_sks:
            act_resp = self.table.get_item(Key={"pk": pk, "sk": sk})
            act_item = act_resp.get("Item")
            if act_item:
                for field in ("external_refs", "evidence_refs"):
                    raw = act_item.get(field)
                    if isinstance(raw, str):
                        try:
                            act_item[field] = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            pass
                results.append(act_item)
        result["results"] = results
        return _sanitize(result)

    def list_actions(self, incident_id: str, limit: int = 20) -> list[dict]:
        pk = f"INCIDENT#{incident_id}"
        resp = self.table.query(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("ACTION#"),
            ScanIndexForward=False,
            Limit=limit,
        )
        items = resp.get("Items", [])
        for item in items:
            for field in ("external_refs", "evidence_refs"):
                raw = item.get(field)
                if isinstance(raw, str):
                    try:
                        item[field] = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        pass
        return _sanitize(items)
