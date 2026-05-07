import base64
import re
from typing import Any

import httpx

from poller.config import get_settings

_GITHUB_PR_PATTERN = re.compile(
    r"github\.com/([^/]+/[^/]+)/pull/(\d+)", re.IGNORECASE
)


class JiraClient:
    def __init__(self) -> None:
        settings = get_settings()
        token = base64.b64encode(
            f"{settings.jira_user_email}:{settings.jira_api_token}".encode()
        ).decode()
        self._base = settings.jira_base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    async def get_issue(self, key: str) -> dict[str, Any]:
        fields = "summary,issuetype,status,labels,comment"
        async with httpx.AsyncClient(headers=self._headers) as client:
            r = await client.get(
                f"{self._base}/rest/api/3/issue/{key}",
                params={"fields": fields},
            )
            r.raise_for_status()
            return r.json()

    async def get_remote_links(self, key: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(headers=self._headers) as client:
            r = await client.get(f"{self._base}/rest/api/3/issue/{key}/remotelink")
            r.raise_for_status()
            return r.json()

    async def search_children(self, parent_key: str) -> list[str]:
        """Return keys of active forge-managed children of parent_key."""
        jql = (
            f'labels = "forge:managed" AND labels = "forge:parent:{parent_key}"'
            f' AND issuetype in (Epic, Task)'
        )
        async with httpx.AsyncClient(headers=self._headers) as client:
            r = await client.post(
                f"{self._base}/rest/api/3/search/jql",
                json={"jql": jql, "fields": ["summary"], "maxResults": 100},
            )
            r.raise_for_status()
            return [issue["key"] for issue in r.json().get("issues", [])]


def extract_pr_info(remote_links: list[dict[str, Any]]) -> tuple[str, int, str] | None:
    """Return (owner/repo, pr_number, branch) from Jira remote links, or None."""
    for link in remote_links:
        url = link.get("object", {}).get("url", "")
        m = _GITHUB_PR_PATTERN.search(url)
        if m:
            repo = m.group(1)
            pr_number = int(m.group(2))
            return repo, pr_number, ""
    return None
