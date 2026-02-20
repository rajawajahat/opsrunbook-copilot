from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BudgetResult:
    payload: dict[str, Any]
    truncated: bool
    byte_size: int


def _json_size_bytes(obj: Any) -> int:
    # stable + compact
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def apply_budgets(
    *,
    payload: dict[str, Any],
    max_rows_per_section: int,
    max_total_bytes: int,
) -> BudgetResult:
    """
    Enforce:
    - max rows per list field (common for log rows)
    - max total JSON byte size

    Strategy:
    1) Trim known list fields first
    2) If still too large, progressively trim evidence_rows fields if present
    3) If still too large, replace large sections with a note
    """
    truncated = False
    work = payload

    # 1) Trim common list fields
    for key in list(work.keys()):
        val = work.get(key)
        if isinstance(val, list) and len(val) > max_rows_per_section:
            work[key] = val[:max_rows_per_section]
            truncated = True

    size = _json_size_bytes(work)
    if size <= max_total_bytes:
        return BudgetResult(payload=work, truncated=truncated, byte_size=size)

    # 2) If too large, try trimming nested "rows" lists in sections
    # Common shape we will use: {"sections":[{"name": "...", "rows":[...]}, ...]}
    sections = work.get("sections")
    if isinstance(sections, list):
        new_sections = []
        for sec in sections:
            if isinstance(sec, dict) and isinstance(sec.get("rows"), list):
                rows = sec["rows"]
                if len(rows) > max_rows_per_section:
                    sec = dict(sec)
                    sec["rows"] = rows[:max_rows_per_section]
                    truncated = True
            new_sections.append(sec)
        work["sections"] = new_sections

    size = _json_size_bytes(work)
    if size <= max_total_bytes:
        return BudgetResult(payload=work, truncated=truncated, byte_size=size)

    # 3) Last resort: drop raw rows, keep only metadata + note
    if isinstance(sections, list):
        minimized = dict(work)
        minimized["sections"] = [
            {
                "name": (sec.get("name") if isinstance(sec, dict) else "unknown"),
                "note": "Dropped section rows due to size budget",
            }
            for sec in sections
        ]
        minimized["note"] = "Evidence was truncated to fit size budget"
        truncated = True
        size = _json_size_bytes(minimized)
        return BudgetResult(payload=minimized, truncated=truncated, byte_size=size)

    # If no sections exist, return a minimal payload
    minimized = {"note": "Evidence was truncated to fit size budget"}
    truncated = True
    size = _json_size_bytes(minimized)
    return BudgetResult(payload=minimized, truncated=truncated, byte_size=size)
