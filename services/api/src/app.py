from dotenv import load_dotenv
from pathlib import Path

# Always load .env from services/api/.env regardless of cwd
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from fastapi import Depends, FastAPI

from .auth import require_api_key
from .settings import load_settings
from .routers import debug, incidents, webhooks

app = FastAPI(
    title="OpsRunbook Copilot",
    version="1.0.0",
    description="Agentic incident analysis pipeline: CloudWatch → LLM → Jira → GitHub PR",
)

settings = load_settings()

app.include_router(debug.router)
app.include_router(incidents.router)
app.include_router(webhooks.router)


@app.get("/health", tags=["health"])
def health():
    """Public liveness probe — does NOT expose infrastructure resource names."""
    return {"status": "ok", "version": "1.0.0"}


@app.get("/health/details", tags=["health"], dependencies=[Depends(require_api_key)])
def health_details():
    """Authenticated readiness probe — confirms all required config is present."""
    return {
        "status": "ok",
        "version": "1.0.0",
        "aws_region": settings.aws_region,
        "evidence_bucket_configured": bool(settings.evidence_bucket),
        "incidents_table_configured": bool(settings.incidents_table),
        "snapshots_table_configured": bool(settings.snapshots_table),
        "packets_table_configured": bool(settings.packets_table),
        "state_machine_configured": bool(settings.state_machine_arn),
        "event_bus_configured": bool(settings.event_bus_name),
    }
