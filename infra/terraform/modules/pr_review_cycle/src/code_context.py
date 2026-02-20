"""
Code context builder for PR review cycle.

Given a file path, git ref, and target line, fetches the file from GitHub
and extracts a windowed snippet with numbered lines for LLM consumption.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github_ops import GitHubOps


@dataclass
class CodeContext:
    path: str
    ref: str
    file_sha: str
    target_line: int
    start_line: int
    end_line: int
    snippet: str
    total_lines: int
    byte_size: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "ref": self.ref,
            "file_sha": self.file_sha,
            "target_line": self.target_line,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "snippet": self.snippet,
            "total_lines": self.total_lines,
            "byte_size": self.byte_size,
        }


def build_code_context(
    gh: "GitHubOps",
    owner: str,
    repo: str,
    path: str,
    ref: str,
    line: int,
    window: int = 20,
) -> CodeContext:
    """Fetch a file at *ref* and extract a snippet centred on *line*.

    Parameters
    ----------
    gh : GitHubOps
        Authenticated GitHub client.
    owner, repo : str
        Repository coordinates.
    path : str
        File path relative to repo root.
    ref : str
        Git ref (branch, tag, SHA).
    line : int
        1-based target line number.
    window : int
        Number of context lines above **and** below the target line.

    Returns
    -------
    CodeContext
        Structured object with the snippet, line range, and file metadata.
    """
    file_info = gh.get_file_at_ref(owner, repo, path, ref)
    return build_code_context_from_text(
        text=file_info["text"],
        path=path,
        ref=ref,
        file_sha=file_info["sha"],
        line=line,
        window=window,
    )


def build_code_context_from_text(
    text: str,
    path: str,
    ref: str = "",
    file_sha: str = "",
    line: int = 1,
    window: int = 20,
) -> CodeContext:
    """Build a CodeContext from already-loaded file text (no GitHub call)."""
    all_lines = text.split("\n")
    total = len(all_lines)

    line = max(1, min(line, total))

    start = max(1, line - window)
    end = min(total, line + window)

    selected = all_lines[start - 1 : end]
    numbered = format_snippet(selected, start)

    return CodeContext(
        path=path,
        ref=ref,
        file_sha=file_sha,
        target_line=line,
        start_line=start,
        end_line=end,
        snippet=numbered,
        total_lines=total,
        byte_size=len(text.encode("utf-8")),
    )


def format_snippet(lines: list[str], start_line: int) -> str:
    """Format lines with right-aligned line numbers.

    >>> format_snippet(["def foo():", "    pass"], 10)
    '10 | def foo():\\n11 |     pass'
    """
    if not lines:
        return ""
    max_num = start_line + len(lines) - 1
    width = len(str(max_num))
    parts = []
    for i, content in enumerate(lines):
        num = start_line + i
        parts.append(f"{num:>{width}} | {content}")
    return "\n".join(parts)


def extract_file_line_from_event(event: dict) -> list[tuple[str, int]]:
    """Pull (file_path, line) pairs from a normalised webhook event.

    Sources checked (in priority order):
      1. inline_context.path + inline_context.line  (review comment)
      2. file paths parsed from comment_body text    (issue comment)

    Returns a list because an issue comment may reference multiple files.
    """
    results: list[tuple[str, int]] = []

    inline = event.get("inline_context") or {}
    inline_path = inline.get("path", "")
    inline_line = inline.get("line") or inline.get("original_line")
    if inline_path and inline_line:
        results.append((inline_path, int(inline_line)))
        return results

    # Fallback: parse "path:line" patterns from comment body
    import re
    comment = event.get("comment_body", "")
    # Match patterns like "src/main.py:42" or "config/app.json line 10"
    for m in re.finditer(r'([\w./_-]+\.\w+)(?::(\d+)|\s+line\s+(\d+))', comment):
        fpath = m.group(1)
        line_num = int(m.group(2) or m.group(3))
        results.append((fpath, line_num))

    # If no line-specific refs, look for bare file paths (line defaults to 1)
    if not results:
        for m in re.finditer(r'([\w./_-]+\.\w+)', comment):
            results.append((m.group(1), 1))

    return results[:5]
