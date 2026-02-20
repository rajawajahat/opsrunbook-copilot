"""Recursive sanitizer for JSON-unsafe control characters and DynamoDB Decimals."""
import re
from decimal import Decimal
from typing import Any

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def sanitize(obj: Any) -> Any:
    """Strip codepoints 0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F from all strings.

    Also converts ``Decimal`` (returned by DynamoDB) to ``int``/``float``.
    Preserves \\t (0x09), \\n (0x0A), and \\r (0x0D) which are legal in JSON.
    """
    if isinstance(obj, str):
        return _CONTROL_CHAR_RE.sub("", obj)
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    return obj
