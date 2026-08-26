import asyncio
from unittest.mock import AsyncMock, patch

from poller.models import TicketState
from poller.watcher import TicketWatcher


def _comment(comment_id, body, account_id):
    return {
        "id": comment_id,
        "body": body,
        "author": {
            "accountId": account_id,
            "displayName": account_id,
            "emailAddress": f"{account_id}@example.com",
        },
    }


def _issue(comments):
    return {
        "fields": {
            "labels": [],
            "comment": {"comments": comments},
            "issuetype": {"name": "Bug"},
            "status": {"name": "Open"},
            "summary": "Comment polling",
        }
    }


def _watcher():
    watcher = TicketWatcher()
    watcher._state = {
        "BUG-1": TicketState(
            ticket_key="BUG-1",
            issue_type="Bug",
            status="Open",
            summary="Comment polling",
            labels=set(),
            last_comment_id="1",
        )
    }
    return watcher


def test_poll_forwards_human_and_forge_comments_in_order():
    watcher = _watcher()
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "! revise this", "human"),
            _comment("3", "Forge status", "forge-bot"),
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    assert [call.args[0]["comment"]["body"] for call in forwarded.await_args_list] == [
        "! revise this",
        "Forge status",
    ]
    assert watcher._state["BUG-1"].last_comment_id == "3"
    assert all(call.kwargs["delivery_id"] for call in forwarded.await_args_list)


def test_poll_does_not_advance_cursor_when_comment_forwarding_fails():
    watcher = _watcher()
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "! revise this", "human"),
        ]
    )
    jira.get_remote_links.return_value = []

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch(
            "poller.watcher.forwarder.forward_jira",
            AsyncMock(side_effect=RuntimeError("gateway unavailable")),
        ),
    ):
        try:
            asyncio.run(watcher._poll("BUG-1"))
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected forwarding failure")

    assert watcher._state["BUG-1"].last_comment_id == "1"
