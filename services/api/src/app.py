from dotenv import load_dotenv
from pathlib import Path

# Always load the .env from services/api/.env even if cwd changes
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from fastapi import FastAPI

from .settings import load_settings
from .routers import debug, incidents, webhooks

app = FastAPI(title="OpsRunbook Copilot", version="1.0.0")

settings = load_settings()

app.include_router(debug.router)
app.include_router(incidents.router)
app.include_router(webhooks.router)

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "1.0.0",
        "aws_region": settings.aws_region,
        "evidence_bucket": settings.evidence_bucket,
        "incidents_table": settings.incidents_table,
        "snapshots_table": settings.snapshots_table,
        "packets_table": settings.packets_table or "(not configured)",
        "state_machine_arn": settings.state_machine_arn or "(not configured)",
        "event_bus_name": settings.event_bus_name or "(not configured)",
    }
