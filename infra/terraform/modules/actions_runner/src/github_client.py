"""GitHub REST API client for creating branches, commits, and PRs.

Supports two auth modes (auto-selected by available config):
  A) GitHub App  – GITHUB_APP_ID + GITHUB_APP_INSTALLATION_ID + PEM key
  B) PAT         – GITHUB_TOKEN
"""
import base64
import json
import time
import hashlib
import hmac
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = "https://api.github.com"


# ── Auth helpers ─────────────────────────────────────────────────

def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _build_jwt(app_id: str, pem: str) -> str:
    """Build a JWT for GitHub App authentication (RS256)."""
    try:
        import jwt as pyjwt  # PyJWT if bundled
        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 540, "iss": app_id}
        return pyjwt.encode(payload, pem, algorithm="RS256")
    except ImportError:
        pass

    # Fallback: hand-rolled RS256 using stdlib + cryptography (Lambda layer)
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise RuntimeError("Cannot build GitHub App JWT: neither PyJWT nor cryptography available")

    now = int(time.time())
    header = _base64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _base64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode())
    signing_input = f"{header}.{claims}".encode()

    private_key = serialization.load_pem_private_key(pem.encode(), password=None)
    sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_base64url(sig)}"


def _get_installation_token(jwt_token: str, installation_id: str) -> str:
    url = f"{API}/app/installations/{installation_id}/access_tokens"
    req = Request(url, data=b"", method="POST")
    req.add_header("Authorization", f"Bearer {jwt_token}")
    req.add_header("Accept", "application/vnd.github+json")
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())["token"]


def _make_token(
    *,
    pat: str | None = None,
    app_id: str | None = None,
    installation_id: str | None = None,
    pem: str | None = None,
) -> str:
    """Return a usable token, preferring App auth when all params are set."""
    if app_id and installation_id and pem:
        jwt_tok = _build_jwt(app_id, pem)
        return _get_installation_token(jwt_tok, installation_id)
    if pat:
        return pat
    raise RuntimeError("No GitHub credentials: set GITHUB_TOKEN or App credentials")


# ── HTTP helper ──────────────────────────────────────────────────

def _gh(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"{API}{path}" if path.startswith("/") else path
    data = json.dumps(body).encode() if body else None
    req = Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:800]
        except Exception:
            pass
        raise RuntimeError(f"GitHub API {method} {path} => {e.code}: {err_body}") from e


# ── Main client ──────────────────────────────────────────────────

class GitHubClient:
    def __init__(
        self,
        owner: str,
        *,
        pat: str | None = None,
        app_id: str | None = None,
        installation_id: str | None = None,
        pem: str | None = None,
        default_branch_fallback: str = "main",
    ):
        self._owner = owner
        self._token = _make_token(pat=pat, app_id=app_id, installation_id=installation_id, pem=pem)
        self._default_branch_fallback = default_branch_fallback

    def get_default_branch(self, repo: str) -> str:
        try:
            data = _gh("GET", f"/repos/{self._owner}/{repo}", self._token)
            return data.get("default_branch", self._default_branch_fallback)
        except Exception:
            return self._default_branch_fallback

    def _get_ref_sha(self, repo: str, branch: str) -> str:
        data = _gh("GET", f"/repos/{self._owner}/{repo}/git/ref/heads/{branch}", self._token)
        return data["object"]["sha"]

    def _create_branch(self, repo: str, branch: str, sha: str) -> dict:
        return _gh("POST", f"/repos/{self._owner}/{repo}/git/refs", self._token, {
            "ref": f"refs/heads/{branch}",
            "sha": sha,
        })

    def _create_or_update_file(self, repo: str, branch: str, path: str, content: str, message: str) -> dict:
        encoded = base64.b64encode(content.encode()).decode()

        # Check if file already exists to get its sha for update
        file_sha = None
        try:
            existing = _gh("GET", f"/repos/{self._owner}/{repo}/contents/{path}?ref={branch}", self._token)
            file_sha = existing.get("sha")
        except RuntimeError:
            pass

        body: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        if file_sha:
            body["sha"] = file_sha

        return _gh("PUT", f"/repos/{self._owner}/{repo}/contents/{path}", self._token, body)

    def _create_pr(self, repo: str, title: str, body: str, head: str, base: str) -> dict:
        return _gh("POST", f"/repos/{self._owner}/{repo}/pulls", self._token, {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        })

    def file_exists(self, repo_full_name: str, path: str) -> bool:
        """Check if a file exists in the repo's default branch."""
        parts = repo_full_name.split("/", 1)
        if len(parts) == 2:
            owner, repo_name = parts
        else:
            owner, repo_name = self._owner, repo_full_name

        default_branch = self.get_default_branch(repo_name)
        try:
            _gh("GET", f"/repos/{owner}/{repo_name}/contents/{path}?ref={default_branch}", self._token)
            return True
        except RuntimeError:
            return False

    def find_open_pr(self, repo: str, head_branch: str) -> dict | None:
        """Find an existing open PR for a given head branch."""
        try:
            prs = _gh("GET", f"/repos/{self._owner}/{repo}/pulls?head={self._owner}:{head_branch}&state=open", self._token)
            if isinstance(prs, list) and prs:
                return prs[0]
        except RuntimeError:
            pass
        return None

    def create_pr_with_notes(
        self,
        *,
        repo: str,
        branch_name: str,
        pr_title: str,
        pr_body: str,
        file_path: str,
        file_content: str,
        commit_message: str,
        collector_run_id: str = "",
    ) -> dict[str, Any]:
        """End-to-end: create branch, commit file, open PR. Returns external_refs dict.

        Idempotency: if a PR already exists for branch_name, update the file
        and return the existing PR instead of creating a new one.
        """
        default_branch = self.get_default_branch(repo)
        base_sha = self._get_ref_sha(repo, default_branch)

        actual_branch = branch_name
        branch_existed = False
        try:
            self._create_branch(repo, actual_branch, base_sha)
        except RuntimeError as e:
            if "422" in str(e) or "Reference already exists" in str(e):
                branch_existed = True
            else:
                raise

        commit_resp = self._create_or_update_file(repo, actual_branch, file_path, file_content, commit_message)
        commit_sha = commit_resp.get("commit", {}).get("sha", "")

        # Idempotency: reuse existing PR if branch already existed
        if branch_existed:
            existing_pr = self.find_open_pr(repo, actual_branch)
            if existing_pr:
                return {
                    "github_owner": self._owner,
                    "github_repo": repo,
                    "branch": actual_branch,
                    "default_branch": default_branch,
                    "pr_url": existing_pr.get("html_url", ""),
                    "pr_number": existing_pr.get("number", 0),
                    "commit_sha": commit_sha,
                    "reused_pr": True,
                }

        pr_resp = self._create_pr(repo, pr_title, pr_body, actual_branch, default_branch)

        return {
            "github_owner": self._owner,
            "github_repo": repo,
            "branch": actual_branch,
            "default_branch": default_branch,
            "pr_url": pr_resp.get("html_url", ""),
            "pr_number": pr_resp.get("number", 0),
            "commit_sha": commit_sha,
        }


class DryRunGitHubClient:
    """Returns fake PR refs without calling GitHub."""

    def __init__(self, owner: str = "dry-run-owner"):
        self._owner = owner
        self._counter = 0

    def get_default_branch(self, repo: str) -> str:
        return "main"

    def file_exists(self, repo_full_name: str, path: str) -> bool:
        return True

    def find_open_pr(self, repo: str, head_branch: str) -> dict | None:
        return None

    def create_pr_with_notes(self, *, repo: str, branch_name: str, pr_title: str,
                             pr_body: str, file_path: str, file_content: str,
                             commit_message: str, collector_run_id: str = "") -> dict[str, Any]:
        self._counter += 1
        return {
            "github_owner": self._owner,
            "github_repo": repo,
            "branch": branch_name,
            "default_branch": "main",
            "pr_url": f"https://github.com/{self._owner}/{repo}/pull/{self._counter}",
            "pr_number": self._counter,
            "commit_sha": f"dryrun-sha-{self._counter}",
        }
