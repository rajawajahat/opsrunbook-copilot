"""
Simple API key authentication for OpsRunbook Copilot.

Set API_KEY in your .env to a strong random string.
Pass it as the X-API-Key header on every request.

To generate a key:
    python -c "import secrets; print(secrets.token_urlsafe(32))"

If API_KEY is not set the server starts but all protected routes
return 401, ensuring you cannot accidentally run an open API in prod.
"""
from __future__ import annotations

import os

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str | None = Security(_header_scheme)) -> str:
    """FastAPI dependency — inject with Depends(require_api_key)."""
    expected = os.getenv("API_KEY", "").strip()
    if not expected:
        raise HTTPException(
            status_code=401,
            detail="API_KEY not configured on this server. Set API_KEY in the environment.",
        )
    if not api_key or api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")
    return api_key
