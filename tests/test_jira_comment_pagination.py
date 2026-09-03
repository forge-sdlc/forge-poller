"""Tests for JiraClient.get_comments pagination."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import poller.config as config_module
from poller.jira import JiraClient


def _reset_settings(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")


def _page(comments, *, start_at, max_results, total):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "startAt": start_at,
        "maxResults": max_results,
        "total": total,
        "comments": comments,
    }
    return resp


def test_get_comments_follows_start_at_across_pages(monkeypatch):
    _reset_settings(monkeypatch)
    page1 = _page(
        [{"id": str(i)} for i in range(1, 101)],
        start_at=0,
        max_results=100,
        total=105,
    )
    page2 = _page(
        [{"id": str(i)} for i in range(101, 106)],
        start_at=100,
        max_results=100,
        total=105,
    )
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[page1, page2])
    context = AsyncMock()
    context.__aenter__.return_value = client

    with patch("poller.jira.httpx.AsyncClient", return_value=context):
        comments = asyncio.run(JiraClient().get_comments("BUG-1"))

    assert [c["id"] for c in comments] == [str(i) for i in range(1, 106)]
    assert client.get.await_count == 2
    first_params = client.get.await_args_list[0].kwargs["params"]
    assert first_params["startAt"] == 0
    assert first_params["maxResults"] == 100
    second_params = client.get.await_args_list[1].kwargs["params"]
    assert second_params["startAt"] == 100
