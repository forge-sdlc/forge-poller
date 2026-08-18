import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from poller.github import GitHubClient, latest_review


def _page(items, next_url=None):
    """Build a fake httpx.Response for one page of a list endpoint."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = items
    resp.links = {"next": {"url": next_url}} if next_url else {}
    return resp


def _client_returning(pages):
    """Fake httpx.AsyncClient whose .get() yields each page in order."""
    client = AsyncMock()
    client.get = AsyncMock(side_effect=pages)
    context = AsyncMock()
    context.__aenter__.return_value = client
    return context, client


def test_get_reviews_follows_link_header_across_pages():
    # GitHub returns reviews oldest-first; the newest review is on page 2.
    page1 = _page(
        [{"id": i, "state": "COMMENTED", "submitted_at": f"2026-08-17T00:00:{i:02d}Z"}
         for i in range(1, 101)],
        next_url="https://api.github.com/next-page",
    )
    page2 = _page(
        [{"id": 999, "state": "COMMENTED", "submitted_at": "2026-08-18T08:09:41Z"}],
    )
    context, client = _client_returning([page1, page2])

    with patch("poller.github.httpx.AsyncClient", return_value=context):
        reviews = asyncio.run(GitHubClient().get_reviews("owner/repo", 242))

    assert len(reviews) == 101
    assert client.get.await_count == 2
    # The paginated result surfaces the newest review that page-1-only would miss.
    assert latest_review(reviews)["id"] == 999


def test_get_issue_comments_paginates_and_returns_newest_first():
    page1 = _page(
        [{"id": i} for i in range(1, 101)],
        next_url="https://api.github.com/next-page",
    )
    page2 = _page([{"id": 101}, {"id": 102}])
    context, _ = _client_returning([page1, page2])

    with patch("poller.github.httpx.AsyncClient", return_value=context):
        comments = asyncio.run(GitHubClient().get_issue_comments("owner/repo", 242))

    assert len(comments) == 102
    assert comments[0]["id"] == 102  # newest-first
