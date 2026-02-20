from __future__ import annotations

import re
from typing import Any

# Conservative patterns to avoid leaking common secrets.
# We redact values, not keys.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Bearer tokens
    (re.compile(r"(?i)\bAuthorization:\s*Bearer\s+[A-Za-z0-9\-\._~\+/]+=*\b"), "Authorization: Bearer [REDACTED]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-\._~\+/]+=*\b"), "Bearer [REDACTED]"),

    # API keys / tokens in key=value forms
    (re.compile(r"(?i)\b(api[_-]?key|token|access[_-]?token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9\-\._~\+/=]{8,}['\"]?"),
     r"\1=[REDACTED]"),

    # AWS keys (best-effort)
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA[REDACTED]"),
    (re.compile(r"(?i)\baws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{16,}['\"]?"),
     "aws_secret_access_key=[REDACTED]"),

    # Password-like fields
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?[^'\"\s]{6,}['\"]?"),
     r"\1=[REDACTED]"),

    # Connection strings (very rough)
    (re.compile(r"(?i)\b(postgres|mysql|mongodb|redis)://[^ \n\r\t]+"), r"\1://[REDACTED]"),
]


def redact_text(text: str) -> str:
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out


def redact_obj(obj: Any) -> Any:
    """
    Recursively redact strings inside dict/list structures.
    Keeps shape intact.
    """
    if obj is None:
        return None
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    return obj
