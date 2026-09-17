import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from poller import forwarder


def test_jira_delivery_id_is_unique_and_namespaced():
    first = forwarder.jira_delivery_id()
    second = forwarder.jira_delivery_id()

    assert first.startswith("poller-jira-")
    assert second.startswith("poller-jira-")
    assert first != second


def test_forward_jira_sends_delivery_id_and_warns_on_duplicate(caplog):
    response = MagicMock()
    response.is_success = True
    response.status_code = 202
    response.json.return_value = {"status": "duplicate"}

    client = AsyncMock()
    client.post.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client

    with (
        patch("poller.forwarder.httpx.AsyncClient", return_value=context),
        caplog.at_level(logging.WARNING),
    ):
        asyncio.run(
            forwarder.forward_jira(
                {"webhookEvent": "jira:issue_updated"},
                delivery_id="poller-jira-unique",
            )
        )

    assert client.post.await_args.kwargs["headers"][
        "X-Atlassian-Webhook-Identifier"
    ] == "poller-jira-unique"
    assert "skipped duplicate" in caplog.text


@pytest.mark.parametrize("body", [
    {"status": "ignored"}, {}, None, "not-json", {"status": "skipped", "reason": "unknown policy"},
])
def test_jira_success_without_queue_ack_is_not_treated_as_delivery(body):
    response = httpx.Response(202, json=body) if body != "not-json" else httpx.Response(202, text=body)
    context = AsyncMock()
    context.__aenter__.return_value.post.return_value = response
    with patch("poller.forwarder.httpx.AsyncClient", return_value=context):
        with pytest.raises(RuntimeError, match="acknowledge"):
            asyncio.run(forwarder.forward_jira({"webhookEvent": "comment_created"}))


def test_jira_queue_ack_logs_ticket_comment_and_delivery_without_body(caplog):
    context = AsyncMock()
    context.__aenter__.return_value.post.return_value = httpx.Response(202, json={"status": "queued"})
    payload = {
        "webhookEvent": "comment_created", "issue": {"key": "BUG-1"},
        "comment": {"id": "123", "body": "private feedback"},
    }
    with (
        patch("poller.forwarder.httpx.AsyncClient", return_value=context),
        caplog.at_level(logging.INFO),
    ):
        asyncio.run(forwarder.forward_jira(payload, delivery_id="delivery-123"))
    assert all(value in caplog.text for value in ("BUG-1", "123", "delivery-123", "queued"))
    assert "private feedback" not in caplog.text


@pytest.mark.parametrize("reason", [
    "missing forge:managed label", "self-comment", "Sub-task must have forge:parent label",
])
def test_known_terminal_jira_skip_is_reported_without_retry(reason, caplog):
    context = AsyncMock()
    context.__aenter__.return_value.post.return_value = httpx.Response(
        200, json={"status": "skipped", "reason": reason},
    )
    with patch("poller.forwarder.httpx.AsyncClient", return_value=context):
        result = asyncio.run(forwarder.forward_jira(
            {"issue": {"key": "BUG-1"}, "comment": {"id": "42", "body": "private feedback"}},
            delivery_id="skip-id",
        ))
    assert result == "skipped"
    assert all(value in caplog.text for value in ("not queued", "BUG-1", "42", "skip-id", reason))
    assert "private feedback" not in caplog.text


def test_github_delivery_id_is_stable_and_event_specific():
    first = forwarder.github_delivery_id(
        "check_suite", "forge-sdlc/forge", 213, "sha-1", "suite-1:updated-1:failure"
    )

    assert first == forwarder.github_delivery_id(
        "check_suite", "forge-sdlc/forge", 213, "sha-1", "suite-1:updated-1:failure"
    )
    assert first != forwarder.github_delivery_id(
        "check_suite", "forge-sdlc/forge", 213, "sha-2", "suite-2:updated-2:failure"
    )
    assert first != "poller-check_suite"


def test_forward_github_sends_delivery_id_and_warns_on_duplicate(caplog):
    response = MagicMock()
    response.is_success = True
    response.status_code = 202
    response.json.return_value = {"status": "duplicate"}

    client = AsyncMock()
    client.post.return_value = response
    context = AsyncMock()
    context.__aenter__.return_value = client

    with (
        patch("poller.forwarder.httpx.AsyncClient", return_value=context),
        caplog.at_level(logging.WARNING),
    ):
        asyncio.run(
            forwarder.forward_github(
                {"action": "completed"},
                event_type="check_suite",
                delivery_id="poller-check_suite-unique",
            )
        )

    assert client.post.await_args.kwargs["headers"]["X-GitHub-Delivery"] == (
        "poller-check_suite-unique"
    )
    assert "skipped duplicate" in caplog.text
