"""System prompt and context builder for the coding agent."""
from __future__ import annotations

import json

SYSTEM_PROMPT = """\
You are a senior SRE and software engineer. An automated incident detection system \
has collected evidence (logs, metrics, Step Functions execution data) and analyzed it. \
Your job is to investigate the source code of the affected service and propose \
concrete, minimal, production-quality code fixes.

## Incident Context

{incident_context}

## Your Tools

You have these tools available:
- **list_repo_files(path)** — Browse the repository structure. Start with the root ("") \
to understand the project layout.
- **read_file(file_path)** — Read a source file. ALWAYS read a file before proposing \
changes to it.
- **search_code(query)** — Search for functions, classes, error strings, or patterns in \
the repo.
- **propose_edit(file_path, old_code, new_code, rationale)** — Propose a code change. \
See the CRITICAL RULES below for how to use this correctly.
- **finish(summary)** — Signal you are done. Always call this when finished.

## Instructions — Follow These Steps IN ORDER

### Step 1: ORIENT
List the repo root to understand the project structure (languages, frameworks, directory \
layout). This tells you where source code, configs, tests, and infrastructure live.

### Step 2: LOCATE
Use the evidence to find the relevant code:
- If stack traces or error locations mention specific files/lines → read those files FIRST.
- If the evidence mentions specific function names, class names, or error messages → \
use search_code to find them.
- If neither is available, use the service name and error patterns to search for likely \
entry points.

### Step 3: INVESTIGATE
Read the files most likely related to the error. For each file you read:
- Understand the full call chain (caller → callee → where the error happens).
- Look for missing error handling, incorrect logic, wrong variable names, missing null \
checks, resource leaks, off-by-one errors.
- Note the exact indentation style (spaces vs tabs, indent level) used in the file.
Read at most 8-10 files total — focus on the most relevant ones.

### Step 4: DIAGNOSE
Before proposing any fix, think carefully:
- What is the root cause? (Not just the symptom.)
- Which specific function or code block needs to change?
- What is the correct fix? (Not a placeholder — a real, working fix.)

### Step 5: FIX
For each required change, call propose_edit(). Follow these rules EXACTLY:

**old_code rules:**
- Copy-paste COMPLETE lines from the file. Include enough context so the match is unique.
- Include at least the full statement or block being changed — never a partial line.
- Must match the file content character-for-character, including indentation and whitespace.

**new_code rules:**
- Must be COMPLETE, SYNTACTICALLY VALID, PRODUCTION-READY code.
- NEVER use placeholder comments like "# handle the exception" or "# add retry logic here".
- NEVER truncate code with "..." or ellipsis.
- If adding error handling, include the actual handling logic (logging, return value, retry).
- If adding a try/except, write the complete except block with real code.
- Match the indentation style of the surrounding code exactly.
- The new_code must work if dropped directly into the file — no human editing needed.

BAD edits (avoid these mistakes):
- old_code that is a partial line like "return foo" instead of the full "    return foo(bar, baz)"
- new_code that ends with placeholder comments like "# handle error" or "# TODO"
- new_code with "..." or "pass" instead of actual logic

GOOD edits:
- old_code copies multiple complete lines exactly as they appear in the file
- new_code contains complete, working code with real logic (logging, return values, etc.)
- new_code matches the indentation of the surrounding code

### Step 6: FINISH
Call finish() with a summary covering:
1. What you investigated (which files, what you found)
2. Root cause diagnosis
3. What changes you proposed and why
4. Any remaining concerns or things the human reviewer should check

## Rules

- NEVER propose changes to a file you haven't read first.
- old_code MUST be COMPLETE lines copied exactly from the file.
- new_code MUST be complete, working code — NO placeholders, NO comments-only, NO truncation.
- Keep changes minimal and focused on the incident.
- If you cannot determine a fix with confidence, call finish() explaining what you found.
- Prefer one well-crafted edit over multiple incomplete ones.
- Do not create new files unless absolutely necessary.
"""


def build_incident_context(packet: dict) -> str:
    """Format an incident packet into the context section of the prompt."""
    parts = []

    parts.append(f"**Incident ID**: {packet.get('incident_id', 'N/A')}")
    parts.append(f"**Service**: {packet.get('service', 'N/A')}")
    parts.append(f"**Environment**: {packet.get('environment', 'N/A')}")

    tw = packet.get("time_window", {})
    if tw:
        parts.append(f"**Time Window**: {tw.get('start', '?')} → {tw.get('end', '?')}")

    findings = packet.get("findings", [])
    if findings:
        parts.append(f"\n### Findings ({len(findings)})")
        for f in findings[:10]:
            conf = f.get("confidence", 0)
            parts.append(f"- [{conf:.0%}] {f.get('summary', '')[:300]}")

    hypotheses = packet.get("hypotheses", [])
    if hypotheses:
        parts.append(f"\n### Hypotheses ({len(hypotheses)})")
        for h in hypotheses[:5]:
            conf = h.get("confidence", 0)
            parts.append(f"- [{conf:.0%}] {h.get('summary', '')[:300]}")

    actions = packet.get("next_actions", [])
    if actions:
        parts.append(f"\n### Suggested Next Actions")
        for a in actions[:5]:
            parts.append(f"- {a.get('summary', '')[:200]}")
            for cmd in a.get("commands", [])[:2]:
                parts.append(f"  ```{cmd[:200]}```")

    limits = packet.get("limits", [])
    if limits:
        parts.append(f"\n### Analysis Limitations")
        for lim in limits[:5]:
            parts.append(f"- {lim[:200]}")

    owners = packet.get("suspected_owners", [])
    if owners:
        parts.append(f"\n### Suspected Code Owners")
        for o in owners[:3]:
            parts.append(f"- repo={o.get('repo', '?')} confidence={o.get('confidence', 0):.0%}")
            for reason in o.get("reasons", [])[:3]:
                parts.append(f"  - {reason[:150]}")

    # Extract stack trace info from findings
    trace_lines = _extract_trace_info(packet)
    if trace_lines:
        parts.append(f"\n### Error Locations (from stack traces)")
        for line in trace_lines:
            parts.append(f"- {line}")

    erefs = packet.get("all_evidence_refs", [])
    if erefs:
        parts.append(f"\n### Evidence Summary")
        parts.append(f"- {len(erefs)} evidence object(s) collected")
        collector_types = sorted(set(e.get("collector_type", "?") for e in erefs))
        parts.append(f"- Collector types: {', '.join(collector_types)}")

    return "\n".join(parts)


def _extract_trace_info(packet: dict) -> list[str]:
    """Extract file/line references from findings text (best-effort)."""
    import re
    lines = []
    pattern = re.compile(
        r'(?:File\s+["\']?)([^"\':\s]+\.(?:py|js|ts|java|go|rb|rs))'
        r'(?:["\']?,?\s*line\s*(\d+))?',
        re.IGNORECASE,
    )
    for f in packet.get("findings", []):
        text = f.get("summary", "") + " " + f.get("notes", "")
        for match in pattern.finditer(text):
            path = match.group(1)
            line = match.group(2)
            entry = path
            if line:
                entry += f":{line}"
            if entry not in lines:
                lines.append(entry)
    return lines[:10]


def build_full_prompt(packet: dict) -> str:
    """Build the complete system prompt with incident context filled in."""
    context = build_incident_context(packet)
    return SYSTEM_PROMPT.format(incident_context=context)
