from datetime import datetime, timedelta, timezone

import pytest

from src.evidence.time_window import clamp_time_window
from src.evidence.redact import redact_text, redact_obj
from src.evidence.budget import apply_budgets


def test_clamp_time_window():
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=60)
    new_start, new_end, clamped = clamp_time_window(start=start, end=end, max_minutes=15)
    assert clamped is True
    assert (new_end - new_start).total_seconds() == 15 * 60


def test_redact_text_bearer():
    s = "Authorization: Bearer abc.def.ghi"
    out = redact_text(s)
    assert "REDACTED" in out


def test_redact_obj_nested():
    obj = {"a": ["password=supersecret", {"k": "AKIA1234567890ABCDEF"}]}
    out = redact_obj(obj)
    assert "REDACTED" in str(out)


def test_budget_trims_lists_and_size():
    payload = {
        "rows": [{"i": i, "msg": "x" * 50} for i in range(500)],
        "meta": {"ok": True},
    }
    res = apply_budgets(payload=payload, max_rows_per_section=50, max_total_bytes=10_000)
    assert res.truncated is True
    assert len(res.payload["rows"]) == 50


def test_budget_last_resort_sections():
    payload = {
        "sections": [
            {"name": "recent", "rows": [{"msg": "x" * 200} for _ in range(200)]},
            {"name": "top", "rows": [{"msg": "y" * 200} for _ in range(200)]},
        ]
    }
    res = apply_budgets(payload=payload, max_rows_per_section=20, max_total_bytes=1_000)
    assert res.truncated is True
    assert "Dropped section rows" in str(res.payload)
