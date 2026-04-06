"""LangGraph ReAct coding agent."""
from __future__ import annotations

import json
import time

from langgraph.prebuilt import create_react_agent

from .github_tools import GitHubAPI
from .models import AgentResult
from .prompt import build_full_prompt
from .tools import build_tools, get_edits, get_finish_summary, get_tool_log, is_finished, reset_state

MAX_ITERATIONS = 15
MAX_RETRIES = 5


def run_agent(
    *,
    llm,
    packet: dict,
    gh: GitHubAPI,
    verbose: bool = True,
) -> AgentResult:
    """Run the coding agent loop and return the result.

    Args:
        llm: A LangChain chat model (e.g., ChatGroq).
        packet: The incident analysis packet from the analyzer.
        gh: A GitHubAPI instance pointed at the target repo.
        verbose: Print progress to stdout.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return _run_agent_inner(llm=llm, packet=packet, gh=gh, verbose=verbose)
        except Exception as e:
            err_str = str(e)
            is_tool_format_error = "tool_use_failed" in err_str or "Failed to call a function" in err_str
            if is_tool_format_error and attempt < MAX_RETRIES:
                wait = attempt * 2
                if verbose:
                    print(f"\n[agent] Tool call format error (attempt {attempt}/{MAX_RETRIES}), retrying in {wait}s...")
                time.sleep(wait)
                continue
            raise

    raise RuntimeError("Agent exhausted retries")


def _run_agent_inner(
    *,
    llm,
    packet: dict,
    gh: GitHubAPI,
    verbose: bool = True,
) -> AgentResult:
    reset_state()

    system_prompt = build_full_prompt(packet)
    tools = build_tools(gh)

    agent_executor = create_react_agent(
        llm,
        tools,
        prompt=system_prompt,
    )

    if verbose:
        print(f"\n{'='*60}")
        print(f"  Coding Agent — incident {packet.get('incident_id', '?')}")
        print(f"  Repo: {gh.owner}/{gh.repo} (ref: {gh.ref})")
        print(f"  Max iterations: {MAX_ITERATIONS}")
        print(f"{'='*60}\n")

    start = time.time()
    iterations = 0

    inputs = {"messages": [("user", "Investigate this incident and propose code fixes.")]}

    for chunk in agent_executor.stream(inputs, stream_mode="updates"):
        iterations += 1

        if verbose:
            _print_step(chunk, iterations)

        if is_finished():
            if verbose:
                print(f"\n[agent] Finished after {iterations} step(s)")
            break

        if iterations >= MAX_ITERATIONS:
            if verbose:
                print(f"\n[agent] Hit iteration limit ({MAX_ITERATIONS})")
            break

    elapsed = time.time() - start
    edits = get_edits()
    tool_log = get_tool_log()
    summary = get_finish_summary() or "Agent did not call finish()."

    if verbose:
        print(f"\n{'='*60}")
        print(f"  Agent complete in {elapsed:.1f}s")
        print(f"  Iterations: {iterations}")
        print(f"  Proposed edits: {len(edits)}")
        print(f"  Tool calls: {len(tool_log)}")
        print(f"{'='*60}\n")

    return AgentResult(
        incident_id=packet.get("incident_id", "unknown"),
        repo=f"{gh.owner}/{gh.repo}",
        summary=summary,
        proposed_edits=edits,
        iterations=iterations,
        tool_calls=tool_log,
    )


def _print_step(chunk: dict, step_num: int):
    """Pretty-print a single agent step."""
    for node_name, node_data in chunk.items():
        messages = node_data.get("messages", [])
        for msg in messages:
            msg_type = getattr(msg, "type", "unknown")
            if msg_type == "ai":
                content = getattr(msg, "content", "")
                tool_calls = getattr(msg, "tool_calls", [])
                if content:
                    print(f"\n[Step {step_num}] {_truncate(content, 300)}")
                for tc in tool_calls:
                    args_preview = {
                        k: _truncate(str(v), 100) for k, v in tc.get("args", {}).items()
                    }
                    print(f"  → calling {tc.get('name', '?')}({json.dumps(args_preview)})")
            elif msg_type == "tool":
                content = getattr(msg, "content", "")
                name = getattr(msg, "name", "?")
                preview = _truncate(content, 500)
                print(f"  ← {name}: {preview}")


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."
