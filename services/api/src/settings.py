import os
from typing import Optional
from pydantic import BaseModel


class Settings(BaseModel):
    aws_region: str = "us-east-1"
    evidence_bucket: str
    incidents_table: str
    snapshots_table: str
    packets_table: str = ""

    # Orchestrator
    state_machine_arn: str = ""
    event_bus_name: str = ""

    # Budgets
    max_rows_per_query: int = 100
    max_bytes_total: int = 200_000
    max_time_window_minutes: int = 15


def load_settings() -> Settings:
    return Settings(
        aws_region=os.getenv("AWS_REGION", "us-east-1"),
        evidence_bucket=os.environ["EVIDENCE_BUCKET"],
        incidents_table=os.environ["INCIDENTS_TABLE"],
        snapshots_table=os.environ["SNAPSHOTS_TABLE"],
        packets_table=os.getenv("PACKETS_TABLE", ""),
        state_machine_arn=os.getenv("STATE_MACHINE_ARN", ""),
        event_bus_name=os.getenv("EVENT_BUS_NAME", ""),
        max_rows_per_query=int(os.getenv("MAX_ROWS_PER_QUERY", "100")),
        max_bytes_total=int(os.getenv("MAX_BYTES_TOTAL", "200000")),
        max_time_window_minutes=int(os.getenv("MAX_TIME_WINDOW_MINUTES", "15")),
    )
