"""LangChain tool definitions for the coding agent.

Each tool wraps a GitHubAPI method and formats the result as a string
for the LLM's consumption. Repo context (owner, repo, ref) is captured
via closure when `build_tools()` is called.
"""
from __future__ import annotations

from langchain_core.tools import tool

from .github_tools import GitHubAPI
from .models import ProposedEdit

# Mutable shared state — accumulated edits and call log
_edits: list[ProposedEdit] = []
_tool_log: list[dict] = []
_finished: bool = False
_finish_summary: str = ""


def reset_state():
    """Reset shared state between agent runs."""
    global _finished, _finish_summary
    _edits.clear()
    _tool_log.clear()
    _finished = False
    _finish_summary = ""


def get_edits() -> list[ProposedEdit]:
    return list(_edits)


def get_tool_log() -> list[dict]:
    return list(_tool_log)


def is_finished() -> bool:
    return _finished


def get_finish_summary() -> str:
    return _finish_summary


def build_tools(gh: GitHubAPI) -> list:
    """Create the LangChain tools bound to a specific GitHub repo context."""

    @tool
    def list_repo_files(path: str = "") -> str:
        """List files and directories at the given path in the repository.
        Use empty string for the repo root. Returns file names with types and sizes."""
        _tool_log.append({"tool": "list_repo_files", "args": {"path": path}})
        try:
            items = gh.list_tree(path)
            if not items:
                return "No files found at this path."
            lines = []
            for item in items:
                icon = "dir" if item["type"] == "dir" else "file"
                size_str = f" ({item['size']:,}B)" if item["type"] != "dir" else ""
                lines.append(f"  [{icon}] {item['path']}{size_str}")
            return f"Contents of '{path or '/'}':\n" + "\n".join(lines)
        except Exception as e:
            return f"Error listing files: {e}"

    @tool
    def read_file(file_path: str) -> str:
        """Read the contents of a source file from the repository.
        Returns the file with line numbers. When using propose_edit, copy the exact
        text from this output (without the line numbers) as old_code."""
        _tool_log.append({"tool": "read_file", "args": {"file_path": file_path}})
        try:
            content = gh.read_file(file_path)
            if content is None:
                return f"File not found: {file_path}"
            if content.startswith("["):
                return content
            lines = content.split("\n")
            numbered = [f"{i+1:4d}| {line}" for i, line in enumerate(lines)]
            return f"--- {file_path} ({len(lines)} lines) ---\n" + "\n".join(numbered)
        except Exception as e:
            return f"Error reading file: {e}"

    @tool
    def search_code(query: str) -> str:
        """Search for code matching the query in the repository.
        Useful for finding where specific functions, classes, or error strings are used."""
        _tool_log.append({"tool": "search_code", "args": {"query": query}})
        try:
            results = gh.search_code(query)
            if not results:
                return f"No results found for '{query}'."
            lines = [f"Search results for '{query}':"]
            for r in results:
                lines.append(f"  - {r['path']}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error searching code: {e}"

    @tool
    def propose_edit(file_path: str, old_code: str, new_code: str, rationale: str) -> str:
        """Propose a code change to fix the incident.
        - file_path: exact path of the file to edit
        - old_code: COMPLETE lines copied exactly from the file (include full statements,
          not partial lines). Must match character-for-character including indentation.
        - new_code: COMPLETE, SYNTACTICALLY VALID replacement code. Must be production-ready.
          NEVER use placeholder comments like '# handle error' or '# TODO'. Write real code
          with actual logic (logging calls, return values, retry loops, etc).
        - rationale: why this change fixes the issue

        The edit is NOT applied immediately — it is accumulated and applied after you call finish().
        You MUST have read the file first before proposing an edit."""
        _tool_log.append({
            "tool": "propose_edit",
            "args": {"file_path": file_path, "rationale": rationale},
        })

        warnings = []
        placeholder_phrases = [
            "# handle", "# todo", "# add", "# implement", "# fix",
            "# log and", "# retry", "# process", "pass",
        ]
        new_lower = new_code.lower().strip()
        for phrase in placeholder_phrases:
            if new_lower.endswith(phrase) or new_lower.endswith(phrase + " here"):
                warnings.append(
                    f"WARNING: new_code ends with placeholder '{phrase}'. "
                    f"Write real implementation code, not placeholder comments."
                )
                break

        if len(old_code.strip()) < 10:
            warnings.append(
                "WARNING: old_code is very short. Use complete lines from the file "
                "to avoid matching the wrong location."
            )

        if "..." in new_code or "…" in new_code:
            warnings.append(
                "WARNING: new_code contains '...' — do not truncate code. "
                "Write the complete replacement."
            )

        _edits.append(ProposedEdit(
            file_path=file_path,
            old_code=old_code,
            new_code=new_code,
            rationale=rationale,
        ))

        result = (
            f"Edit #{len(_edits)} recorded for {file_path}.\n"
            f"Rationale: {rationale}\n"
            f"Total edits so far: {len(_edits)}"
        )
        if warnings:
            result += "\n\n" + "\n".join(warnings)
            result += "\nPlease call propose_edit again with improved code if needed."
        return result

    @tool
    def finish(summary: str) -> str:
        """Signal that you have completed your investigation and are ready to apply changes.
        Provide a summary of:
        - What you investigated
        - What the root cause is
        - What changes you proposed (if any)
        - Why you believe the changes fix the issue (or why no fix was possible)"""
        global _finished, _finish_summary
        _tool_log.append({"tool": "finish", "args": {"summary": summary[:500]}})
        _finished = True
        _finish_summary = summary
        edit_count = len(_edits)
        if edit_count > 0:
            files = ", ".join(sorted(set(e.file_path for e in _edits)))
            return f"Done. {edit_count} edit(s) proposed for: {files}. Ready to create PR."
        return "Done. No edits proposed. Investigation complete."

    return [list_repo_files, read_file, search_code, propose_edit, finish]
