"""MS Teams incoming webhook notifier."""
import json
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


class TeamsNotifier:
    def __init__(self, webhook_url: str):
        self._webhook_url = webhook_url

    def send_message(self, title: str, body_md: str, links: Optional[list[dict]] = None) -> dict[str, Any]:
        card = {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": "d63384",
            "summary": title[:200],
            "sections": [{
                "activityTitle": title,
                "text": body_md[:4000],
                "markdown": True,
            }],
        }
        if links:
            card["potentialAction"] = [
                {"@type": "OpenUri", "name": lnk.get("name", "Link"), "targets": [{"os": "default", "uri": lnk["url"]}]}
                for lnk in links[:5]
            ]

        payload = json.dumps(card).encode("utf-8")
        req = Request(self._webhook_url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")

        try:
            with urlopen(req, timeout=10) as resp:
                resp_text = resp.read().decode("utf-8")[:200]
                return {"status_code": resp.status, "response": resp_text}
        except HTTPError as e:
            raise RuntimeError(f"Teams webhook error {e.code}") from e
        except URLError as e:
            raise RuntimeError(f"Teams connection error: {e.reason}") from e


class DryRunTeamsNotifier:
    """Logs message without sending."""

    def __init__(self):
        self._counter = 0

    def send_message(self, title: str, body_md: str, links: Optional[list[dict]] = None) -> dict[str, Any]:
        self._counter += 1
        return {
            "status_code": 200,
            "response": "DRYRUN-OK",
            "message_id": f"dryrun-teams-{self._counter}",
        }
