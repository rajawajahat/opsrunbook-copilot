import boto3
from boto3.dynamodb.conditions import Key


class PacketsStore:
    def __init__(self, table_name: str, region: str):
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def latest_for_incident(self, incident_id: str) -> dict | None:
        pk = f"INCIDENT#{incident_id}"
        resp = self.table.query(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("PACKET#"),
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        return items[0] if items else None

    def get_by_run_id(self, incident_id: str, collector_run_id: str) -> dict | None:
        pk = f"INCIDENT#{incident_id}"
        resp = self.table.query(
            KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("PACKET#"),
            FilterExpression="collector_run_id = :rid",
            ExpressionAttributeValues={":rid": collector_run_id},
            ScanIndexForward=False,
            Limit=10,
        )
        items = resp.get("Items", [])
        return items[0] if items else None
