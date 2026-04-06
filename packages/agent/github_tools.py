"""Lightweight GitHub API helpers for the coding agent.

Uses requests + PAT directly — no dependency on Lambda module code.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

import requests

_MAX_FILE_SIZE = 512_000  # 500 KB — skip huge files


class GitHubAPI:
    """Thin wrapper around GitHub REST API v3."""

    def __init__(self, token: str, owner: str, repo: str, ref: str = "main"):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.ref = ref
        self._base = "https://api.github.com"
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self._session.get(f"{self._base}{path}", params=params)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            reset = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(reset - int(time.time()), 1)
            print(f"[github] Rate limited, waiting {wait}s...")
            time.sleep(wait)
            resp = self._session.get(f"{self._base}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, body: dict) -> Any:
        resp = self._session.put(f"{self._base}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> Any:
        resp = self._session.post(f"{self._base}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    # ── Read operations ──────────────────────────────────────────

    def get_default_branch(self) -> str:
        data = self._get(f"/repos/{self.owner}/{self.repo}")
        branch = data.get("default_branch", "main")
        self.ref = branch
        return branch

    def list_tree(self, path: str = "") -> list[dict]:
        """List files/dirs at a path. Returns [{path, type, size}]."""
        if path:
            data = self._get(
                f"/repos/{self.owner}/{self.repo}/contents/{path}",
                params={"ref": self.ref},
            )
            if isinstance(data, list):
                return [
                    {"path": item["path"], "type": item["type"], "size": item.get("size", 0)}
                    for item in data
                ]
            return [{"path": data["path"], "type": data["type"], "size": data.get("size", 0)}]

        data = self._get(
            f"/repos/{self.owner}/{self.repo}/git/trees/{self.ref}",
            params={"recursive": "false"},
        )
        return [
            {"path": item["path"], "type": "dir" if item["type"] == "tree" else "file", "size": item.get("size", 0)}
            for item in data.get("tree", [])
        ]

    def read_file(self, file_path: str) -> str | None:
        """Read file content. Returns decoded text or None if too large / binary."""
        try:
            data = self._get(
                f"/repos/{self.owner}/{self.repo}/contents/{file_path}",
                params={"ref": self.ref},
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

        size = data.get("size", 0)
        if size > _MAX_FILE_SIZE:
            return f"[FILE TOO LARGE: {size:,} bytes — skipped]"

        content_b64 = data.get("content", "")
        if not content_b64:
            return None

        try:
            return base64.b64decode(content_b64).decode("utf-8")
        except (UnicodeDecodeError, Exception):
            return "[BINARY FILE — skipped]"

    def search_code(self, query: str, max_results: int = 10) -> list[dict]:
        """Search code in this repo. Returns [{path, matches}]."""
        q = f"{query} repo:{self.owner}/{self.repo}"
        try:
            data = self._get("/search/code", params={"q": q, "per_page": max_results})
        except requests.HTTPError:
            return []

        results = []
        for item in data.get("items", [])[:max_results]:
            results.append({
                "path": item.get("path", ""),
                "name": item.get("name", ""),
                "url": item.get("html_url", ""),
            })
        return results

    # ── Write operations ─────────────────────────────────────────

    def create_branch(self, branch_name: str) -> str:
        """Create a branch from the current ref. Returns the new branch SHA."""
        base_sha = self._get(
            f"/repos/{self.owner}/{self.repo}/git/ref/heads/{self.ref}"
        )["object"]["sha"]

        try:
            self._post(
                f"/repos/{self.owner}/{self.repo}/git/refs",
                {"ref": f"refs/heads/{branch_name}", "sha": base_sha},
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 422:
                pass  # branch already exists
            else:
                raise
        return base_sha

    def create_or_update_file(
        self, branch: str, file_path: str, content: str, message: str,
    ) -> dict:
        """Create or update a file on a branch. Returns commit info."""
        encoded = base64.b64encode(content.encode()).decode()

        file_sha = None
        try:
            existing = self._get(
                f"/repos/{self.owner}/{self.repo}/contents/{file_path}",
                params={"ref": branch},
            )
            file_sha = existing.get("sha")
        except requests.HTTPError:
            pass

        body: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        if file_sha:
            body["sha"] = file_sha

        return self._put(
            f"/repos/{self.owner}/{self.repo}/contents/{file_path}",
            body,
        )

    def read_file_at_ref(self, file_path: str, ref: str) -> str | None:
        """Read file content at a specific ref (branch/tag/sha)."""
        try:
            data = self._get(
                f"/repos/{self.owner}/{self.repo}/contents/{file_path}",
                params={"ref": ref},
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None
            raise

        size = data.get("size", 0)
        if size > _MAX_FILE_SIZE:
            return f"[FILE TOO LARGE: {size:,} bytes — skipped]"

        content_b64 = data.get("content", "")
        if not content_b64:
            return None

        try:
            return base64.b64decode(content_b64).decode("utf-8")
        except (UnicodeDecodeError, Exception):
            return "[BINARY FILE — skipped]"

    def apply_edit(self, branch: str, file_path: str, old_code: str, new_code: str, message: str) -> dict:
        """Read a file from the target branch, apply a search-and-replace edit, commit the result."""
        current = self.read_file_at_ref(file_path, ref=branch)
        if current is None:
            raise ValueError(f"File {file_path} not found on branch {branch}")

        if old_code not in current:
            raise ValueError(
                f"old_code not found in {file_path}. "
                f"The file may have changed or the old_code doesn't match exactly."
            )

        updated = current.replace(old_code, new_code, 1)
        return self.create_or_update_file(branch, file_path, updated, message)

    def create_pull_request(self, title: str, body: str, head: str, base: str | None = None) -> dict:
        """Create a pull request. Returns PR data including html_url and number."""
        return self._post(
            f"/repos/{self.owner}/{self.repo}/pulls",
            {
                "title": title,
                "body": body,
                "head": head,
                "base": base or self.ref,
            },
        )
