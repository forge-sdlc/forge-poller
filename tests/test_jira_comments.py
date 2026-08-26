import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from poller.jira import JiraClient


def _response(payload):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = payload
    return response


def test_get_issue_replaces_partial_embedded_comments_with_all_pages():
    issue = _response({"fields": {"comment": {"comments": [{"id": "3"}]}}})
    first_page = _response(
        {
            "comments": [{"id": str(i)} for i in range(1, 101)],
            "total": 101,
        }
    )
    second_page = _response({"comments": [{"id": "101"}], "total": 101})
    client = AsyncMock()
    client.get = AsyncMock(side_effect=[issue, first_page, second_page])
    context = AsyncMock()
    context.__aenter__.return_value = client

    with patch("poller.jira.httpx.AsyncClient", return_value=context):
        result = asyncio.run(JiraClient().get_issue("BUG-1"))

    comments = result["fields"]["comment"]["comments"]
    assert len(comments) == 101
    assert comments[0]["id"] == "1"
    assert comments[-1]["id"] == "101"
    assert client.get.await_args_list[-1].kwargs["params"]["startAt"] == 100
