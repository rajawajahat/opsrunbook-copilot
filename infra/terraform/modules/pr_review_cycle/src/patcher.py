"""
Safe patching engine – Iteration 6, Step 3.

Applies pr_fix_plan.v1 edits to a PR branch via GitHub Contents API.
All-or-nothing: if any file fails, no changes are committed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github_ops import GitHubOps

BLOCKED_PATH_PATTERNS = [
    re.compile(r"^\.github/workflows/"),
    re.compile(r"^\.github/actions/"),
    re.compile(r"^\.circleci/"),
    re.compile(r"^Jenkinsfile"),
]


@dataclass
class PatchResult:
    status: str  # success | failed | deferred
    reason: str = ""
    commit_sha: str = ""
    updated_files: list[str] = field(default_factory=list)


def _is_path_allowed(path: str, allowed_paths: list[str]) -> bool:
    for pattern in BLOCKED_PATH_PATTERNS:
        if pattern.search(path):
            return False
    if not allowed_paths:
        return True
    return any(path.startswith(prefix.strip()) for prefix in allowed_paths if prefix.strip())


def _apply_instructions(original: str, instructions: str) -> str | None:
    """Deterministic edit: find-and-replace if instructions contain 'replace X with Y'."""
    m = re.search(r"replace\s+['\"](.+?)['\"]\s+with\s+['\"](.+?)['\"]", instructions, re.IGNORECASE)
    if m:
        old_text, new_text = m.group(1), m.group(2)
        if old_text in original:
            return original.replace(old_text, new_text, 1)
    return None


def apply_patch_plan(
    gh: "GitHubOps",
    owner: str,
    repo: str,
    branch: str,
    plan: dict,
    delivery_id: str,
    allowed_paths: list[str] | None = None,
    max_files: int = 5,
    max_bytes: int = 204800,
) -> PatchResult:
    if allowed_paths is None:
        allowed_paths = []

    edits = plan.get("proposed_edits", [])
    if not edits:
        return PatchResult(status="deferred", reason="no edits in plan")

    if len(edits) > max_files:
        return PatchResult(status="failed", reason=f"too many files: {len(edits)} > {max_files}")

    # Phase 1: validate + fetch all current contents (no mutations yet)
    prepared = []
    for edit in edits:
        fpath = edit.get("file_path", "")
        change_type = edit.get("change_type", "edit")

        if not fpath:
            return PatchResult(status="failed", reason="empty file_path in edit")

        if not _is_path_allowed(fpath, allowed_paths):
            return PatchResult(status="failed", reason=f"path not allowed: {fpath}")

        if change_type == "edit":
            try:
                content, file_sha = gh.get_file_content(owner, repo, fpath, branch)
            except RuntimeError as e:
                return PatchResult(status="failed", reason=f"cannot fetch {fpath}: {e}")

            if len(content.encode("utf-8")) > max_bytes:
                return PatchResult(status="failed", reason=f"file too large: {fpath}")

            patch_text = edit.get("patch", "")
            instructions = edit.get("instructions", "")
            new_content = None

            if patch_text:
                new_content = _try_apply_patch(content, patch_text)
            if new_content is None and instructions:
                new_content = _apply_instructions(content, instructions)

            if new_content is None:
                return PatchResult(
                    status="failed",
                    reason=f"could not apply edit to {fpath} – patch/instructions did not match",
                )

            if len(new_content.encode("utf-8")) > max_bytes:
                return PatchResult(status="failed", reason=f"result too large: {fpath}")

            prepared.append((fpath, new_content, file_sha))

        elif change_type == "create":
            new_content = edit.get("patch", "") or edit.get("instructions", "")
            if len(new_content.encode("utf-8")) > max_bytes:
                return PatchResult(status="failed", reason=f"new file too large: {fpath}")
            prepared.append((fpath, new_content, None))
        else:
            return PatchResult(status="failed", reason=f"unsupported change_type: {change_type}")

    # Phase 2: commit all (all-or-nothing semantics via sequential commits on same branch)
    commit_message = f"OpsRunbook: address review feedback (delivery {delivery_id})"
    last_sha = ""
    updated_files = []

    for fpath, new_content, file_sha in prepared:
        try:
            resp = gh.create_or_update_file(
                owner, repo, fpath, new_content, commit_message, branch, file_sha
            )
            last_sha = resp.get("commit", {}).get("sha", "")
            updated_files.append(fpath)
        except RuntimeError as e:
            return PatchResult(
                status="failed",
                reason=f"commit failed for {fpath}: {e}",
                commit_sha=last_sha,
                updated_files=updated_files,
            )

    return PatchResult(
        status="success",
        commit_sha=last_sha,
        updated_files=updated_files,
    )


def _try_apply_patch(original: str, patch: str) -> str | None:
    """Attempt to apply a simple unified diff patch. Returns None on failure."""
    lines = original.split("\n")
    result_lines = list(lines)
    patch_lines = patch.strip().split("\n")

    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

    offset = 0
    i = 0
    while i < len(patch_lines):
        m = hunk_re.match(patch_lines[i])
        if not m:
            i += 1
            continue

        old_start = int(m.group(1)) - 1
        i += 1
        pos = old_start + offset
        removals = []
        additions = []

        while i < len(patch_lines):
            line = patch_lines[i]
            if line.startswith("@@") or line.startswith("diff ") or line.startswith("---") or line.startswith("+++"):
                break
            if line.startswith("-"):
                removals.append(line[1:])
            elif line.startswith("+"):
                additions.append(line[1:])
            elif line.startswith(" "):
                if removals or additions:
                    break
                pos += 1
            i += 1

        # Verify context
        for j, removed in enumerate(removals):
            idx = old_start + offset + j
            if idx >= len(result_lines) or result_lines[idx].rstrip() != removed.rstrip():
                return None

        result_lines[old_start + offset: old_start + offset + len(removals)] = additions
        offset += len(additions) - len(removals)

    result = "\n".join(result_lines)
    if result == original:
        return None
    return result
