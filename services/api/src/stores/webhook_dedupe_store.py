"""Dedupe + PR pause store for GitHub webhooks – uses incidents table."""
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError


class WebhookDedupeStore:
    def __init__(self, table_name: str, region: str):
        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def already_processed(self, delivery_id: str) -> bool:
        resp = self._table.get_item(
            Key={"pk": "WEBHOOK#DELIVERY", "sk": f"DLV#{delivery_id}"},
            ProjectionExpression="pk",
        )
        return "Item" in resp

    def mark_processed(self, delivery_id: str, outcome: str = "processed") -> None:
        self._table.put_item(Item={
            "pk": "WEBHOOK#DELIVERY",
            "sk": f"DLV#{delivery_id}",
            "delivery_id": delivery_id,
            "outcome": outcome,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        })

    def set_pr_paused(self, repo_full_name: str, pr_number: int | None, paused: bool) -> None:
        if not pr_number:
            return
        self._table.put_item(Item={
            "pk": f"WEBHOOK#PR#{repo_full_name}",
            "sk": f"PR#{pr_number}",
            "paused": paused,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    def is_pr_paused(self, repo_full_name: str, pr_number: int | None) -> bool:
        if not pr_number:
            return False
        resp = self._table.get_item(
            Key={"pk": f"WEBHOOK#PR#{repo_full_name}", "sk": f"PR#{pr_number}"},
        )
        item = resp.get("Item")
        if not item:
            return False
        return bool(item.get("paused"))
