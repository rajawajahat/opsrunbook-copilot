import boto3

from boto3.dynamodb.conditions import Key


class SnapshotsStore:
    def __init__(self, table_name: str, region: str):
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def latest_for_incident(self, incident_id: str) -> dict | None:
        """Return the latest SNAPSHOT# item for the incident (excludes RUN# items)."""
        pk = f"INCIDENT#{incident_id}"
        resp = self.table.query(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("SNAPSHOT#"),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
