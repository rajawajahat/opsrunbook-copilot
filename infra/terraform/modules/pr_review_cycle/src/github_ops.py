"""GitHub API operations for the PR review cycle.

Reuses the same auth approach as actions_runner/github_client.py but
adds PR-specific read operations (get PR, list files, get file content,
post comment).
"""
import base64
import json
import time
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API = "https://api.github.com"


# ── Auth (shared with github_client.py) ──────────────────────────

def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _build_jwt(app_id: str, pem: str) -> str:
    try:
        import jwt as pyjwt
        now = int(time.time())
        return pyjwt.encode({"iat": now - 60, "exp": now + 540, "iss": app_id}, pem, algorithm="RS256")
    except ImportError:
        pass
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
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


def _make_token(*, pat=None, app_id=None, installation_id=None, pem=None) -> str:
    if app_id and installation_id and pem:
        return _get_installation_token(_build_jwt(app_id, pem), installation_id)
    if pat:
        return pat
    raise RuntimeError("No GitHub credentials available")


# ── HTTP helper ──────────────────────────────────────────────────

def _gh(method: str, path: str, token: str, body: dict | None = None) -> dict | list:
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


# ── GitHubOps ────────────────────────────────────────────────────

class GitHubOps:
    """Higher-level GitHub operations for PR review cycle."""

    def __init__(self, owner: str, *, pat=None, app_id=None, installation_id=None, pem=None):
        self._owner = owner
        self._token = _make_token(pat=pat, app_id=app_id, installation_id=installation_id, pem=pem)

    def get_pr(self, owner: str, repo: str, pr_number: int) -> dict:
        return _gh("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}", self._token)

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list:
        return _gh("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}/files?per_page=50", self._token)

    def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> tuple[str, str]:
        """Returns (content_text, file_sha)."""
        resp = _gh("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={ref}", self._token)
        content = base64.b64decode(resp.get("content", "")).decode("utf-8")
        return content, resp.get("sha", "")

    def get_file_at_ref(self, owner: str, repo: str, path: str, ref: str) -> dict:
        """Fetch file content at a specific ref. Returns structured result with
        decoded text, file sha, byte size, and line count."""
        resp = _gh("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={ref}", self._token)
        raw_b64 = resp.get("content", "")
        text = base64.b64decode(raw_b64).decode("utf-8")
        return {
            "path": path,
            "ref": ref,
            "sha": resp.get("sha", ""),
            "text": text,
            "byte_size": len(text.encode("utf-8")),
            "line_count": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        }

    def create_or_update_file(
        self, owner: str, repo: str, path: str, content: str,
        message: str, branch: str, file_sha: str | None = None,
    ) -> dict:
        encoded = base64.b64encode(content.encode()).decode()
        body: dict[str, Any] = {
            "message": message,
            "content": encoded,
            "branch": branch,
        }
        if file_sha:
            body["sha"] = file_sha
        return _gh("PUT", f"/repos/{owner}/{repo}/contents/{path}", self._token, body)

    def create_pr_comment(self, owner: str, repo: str, pr_number: int, body: str) -> dict:
        return _gh("POST", f"/repos/{owner}/{repo}/issues/{pr_number}/comments", self._token, {"body": body})

    def get_pr_comments(self, owner: str, repo: str, pr_number: int) -> list:
        return _gh("GET", f"/repos/{owner}/{repo}/issues/{pr_number}/comments?per_page=50", self._token)
