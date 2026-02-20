"""Jira Cloud REST API client for creating issues."""
import json
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import base64


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, project_key: str, issue_type: str = "Bug"):
        self._base_url = base_url.rstrip("/")
        self._auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._project_key = project_key
        self._issue_type = issue_type

    def create_issue(
        self,
        summary: str,
        description: str,
        priority: str = "Medium",
        labels: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        priority_map = {"P0": "Highest", "P1": "High", "P2": "Medium"}
        jira_priority = priority_map.get(priority, priority)

        payload = {
            "fields": {
                "project": {"key": self._project_key},
                "issuetype": {"name": self._issue_type},
                "summary": summary[:255],
                "description": description[:30000],
                "priority": {"name": jira_priority},
            }
        }
        if labels:
            payload["fields"]["labels"] = labels[:10]

        url = f"{self._base_url}/rest/api/2/issue"
        body = json.dumps(payload).encode("utf-8")
        req = Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Basic {self._auth}")
        req.add_header("Content-Type", "application/json")

        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                issue_key = data.get("key", "")
                return {
                    "issue_key": issue_key,
                    "url": f"{self._base_url}/browse/{issue_key}",
                    "id": data.get("id", ""),
                }
        except HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")[:500]
            except Exception:
                pass
            raise RuntimeError(f"Jira API error {e.code}: {error_body}") from e
        except URLError as e:
            raise RuntimeError(f"Jira connection error: {e.reason}") from e


class DryRunJiraClient:
    """Returns fake issue refs without calling Jira."""

    def __init__(self, project_key: str = "OPS"):
        self._project_key = project_key
        self._counter = 0

    def create_issue(self, summary: str, description: str, priority: str = "Medium", labels: Optional[list[str]] = None) -> dict[str, Any]:
        self._counter += 1
        key = f"DRYRUN-{self._counter}"
        return {
            "issue_key": key,
            "url": f"https://dryrun.atlassian.net/browse/{key}",
            "id": f"dryrun-{self._counter}",
        }
