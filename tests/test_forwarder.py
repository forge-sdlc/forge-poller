import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from poller import forwarder


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
