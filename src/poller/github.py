from typing import Any

import httpx

from poller.config import get_settings


class GitHubClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._headers = {
            "Authorization": f"Bearer {settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get_pr(self, repo: str, pr_number: int) -> dict[str, Any]:
        async with httpx.AsyncClient(headers=self._headers) as client:
            r = await client.get(f"https://api.github.com/repos/{repo}/pulls/{pr_number}")
            r.raise_for_status()
            return r.json()

    async def get_check_runs(self, repo: str, head_sha: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(headers=self._headers) as client:
            r = await client.get(
                f"https://api.github.com/repos/{repo}/commits/{head_sha}/check-runs",
                params={"per_page": 100},
            )
            r.raise_for_status()
            return r.json().get("check_runs", [])

    async def get_reviews(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(headers=self._headers) as client:
            r = await client.get(
                f"https://api.github.com/repos/{repo}/pulls/{pr_number}/reviews",
                params={"per_page": 100},
            )
            r.raise_for_status()
            return r.json()


def latest_check_conclusion(check_runs: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Return (status, conclusion) of the most recently completed check suite run, or None."""
    completed = [c for c in check_runs if c.get("status") == "completed"]
    if not completed:
        return None
    latest = max(completed, key=lambda c: c.get("completed_at", ""))
    return latest.get("status", ""), latest.get("conclusion", "")


def latest_review(reviews: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the most recent non-pending review, or None."""
    substantive = [r for r in reviews if r.get("state") != "PENDING"]
    if not substantive:
        return None
    return max(substantive, key=lambda r: r.get("submitted_at", ""))
