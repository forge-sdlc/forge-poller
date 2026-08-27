"""Tests for Jira ticket comment polling and forwarding."""

import asyncio
from unittest.mock import AsyncMock, patch

import poller.config as config_module
from poller import payloads
from poller.models import TicketState
from poller.watcher import TicketWatcher


def _reset_settings(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")
    monkeypatch.setenv("FORGE_BOT_ACCOUNT_ID", "forge-bot")


def _comment(comment_id: str, body: str, account_id: str, email: str | None = None):
    return {
        "id": comment_id,
        "body": body,
        "author": {
            "accountId": account_id,
            "displayName": account_id,
            "emailAddress": email if email is not None else f"{account_id}@example.com",
        },
    }


def _issue(comments: list[dict]):
    return {
        "fields": {
            "labels": ["forge:managed"],
            "comment": {"comments": comments},
            "issuetype": {"name": "Bug"},
            "status": {"name": "Open"},
            "summary": "Comment polling",
        }
    }


def _watcher(last_comment_id: str | None = "1") -> TicketWatcher:
    watcher = TicketWatcher()
    watcher._state = {
        "BUG-1": TicketState(
            ticket_key="BUG-1",
            issue_type="Bug",
            status="Open",
            summary="Comment polling",
            labels={"forge:managed"},
            last_comment_id=last_comment_id,
        )
    }
    return watcher


def test_comment_created_payload_email_always_empty():
    payload = payloads.comment_created(
        ticket_key="BUG-1",
        issue_type="Bug",
        status="Open",
        summary="s",
        labels={"forge:managed"},
        body="! revise",
        author_account_id="human",
        author_display_name="Human",
        author_email="human@example.com",
    )
    assert payload["comment"]["author"]["emailAddress"] == ""


def test_poll_forwards_all_new_comments_in_order(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "! revise this", "human"),
            _comment("3", "? what next", "human"),
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    bodies = [call.args[0]["comment"]["body"] for call in forwarded.await_args_list]
    assert bodies == ["! revise this", "? what next"]
    assert watcher._state["BUG-1"].last_comment_id == "3"


def test_poll_forwards_bot_authored_jira_comments(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "Forge status update", "forge-bot"),
            _comment("3", "! human reply", "human"),
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    bodies = [call.args[0]["comment"]["body"] for call in forwarded.await_args_list]
    assert bodies == ["Forge status update", "! human reply"]
    assert watcher._state["BUG-1"].last_comment_id == "3"


def test_poll_blank_email_in_forwarded_payload(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "! revise", "human", email="human@example.com"),
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    assert len(forwarded.await_args_list) == 1
    payload = forwarded.await_args_list[0].args[0]
    assert payload["comment"]["author"]["emailAddress"] == ""
    assert watcher._state["BUG-1"].last_comment_id == "2"


def test_poll_advances_cursor_after_bot_tip_comment(monkeypatch):
    """Previously a bot tip comment was skipped but still advanced the cursor,
    dropping a concurrent human comment that was not the tip."""
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "! human", "human"),
            _comment("3", "bot tip", "forge-bot"),
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    bodies = [call.args[0]["comment"]["body"] for call in forwarded.await_args_list]
    assert bodies == ["! human", "bot tip"]
    assert watcher._state["BUG-1"].last_comment_id == "3"
