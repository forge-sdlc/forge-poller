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

    async def get_comments(self, key: str) -> list[dict[str, Any]]:
        """Return all issue comments oldest-first, following startAt pagination.

        GET issue embeds only a recent window of comments. Use this when the
        saved cursor is absent from that window so intervening comments are not
        permanently skipped.
        """
        comments: list[dict[str, Any]] = []
        start_at = 0
        max_results = 100
        async with httpx.AsyncClient(headers=self._headers) as client:
            while True:
                r = await client.get(
                    f"{self._base}/rest/api/3/issue/{key}/comment",
                    params={
                        "startAt": start_at,
                        "maxResults": max_results,
                        "orderBy": "created",
                    },
                )
                r.raise_for_status()
                data = r.json()
                page = data.get("comments", [])
                comments.extend(page)
                total = int(data.get("total", len(comments)))
                if not page or start_at + len(page) >= total:
                    break
                start_at += len(page)
        return comments

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


def extract_pr_info(remote_links: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Return all (owner/repo, pr_number) pairs from Jira remote links."""
    results: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for link in remote_links:
        url = link.get("object", {}).get("url", "")
        m = _GITHUB_PR_PATTERN.search(url)
        if m:
            key = (m.group(1), int(m.group(2)))
            if key not in seen:
                seen.add(key)
                results.append(key)
    return results
