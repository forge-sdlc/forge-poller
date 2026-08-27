import hashlib
import logging
from typing import Any
from uuid import uuid4

import httpx

from poller.config import get_settings

logger = logging.getLogger(__name__)


def github_delivery_id(event_type: str, *identity: object) -> str:
    """Build a stable delivery ID for one logical synthetic GitHub event."""
    raw_identity = "\x1f".join(str(part) for part in identity)
    digest = hashlib.sha256(raw_identity.encode()).hexdigest()[:24]
    return f"poller-{event_type}-{digest}"


def jira_delivery_id(payload: dict[str, Any] | None = None) -> str:
    """Build a replay-stable ID when a Jira provider identity is available.

    The random fallback is retained for legacy callers that do not provide a
    webhook-shaped payload.  Provider comment IDs and issue ``updated`` values
    are stable across poller restarts and therefore make the forwarded
    delivery interchangeable with a native Jira webhook at Forge.
    """
    if payload is None:
        return f"poller-jira-{uuid4()}"
    issue = payload.get("issue", {})
    key = issue.get("key", "") if isinstance(issue, dict) else ""
    comment = payload.get("comment", {})
    if isinstance(comment, dict) and comment.get("id") is not None:
        identity = ("comment", key, comment["id"])
    else:
        fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
        updated = fields.get("updated") if isinstance(fields, dict) else None
        if isinstance(updated, str) and updated:
            identity = ("issue", key, updated)
        else:
            changelog = payload.get("changelog", {})
            items = changelog.get("items") if isinstance(changelog, dict) else None
            if items:
                identity = ("changelog", key, items)
            else:
                return f"poller-jira-{uuid4()}"
    raw_identity = "\x1f".join(str(part) for part in identity)
    digest = hashlib.sha256(raw_identity.encode()).hexdigest()[:24]
    return f"poller-jira-{digest}"


async def forward_jira(
    payload: dict[str, Any], delivery_id: str | None = None
) -> None:
    settings = get_settings()
    url = f"{settings.forge_gateway_url}/api/v1/webhooks/jira"
    delivery_id = delivery_id or jira_delivery_id(payload)
    headers = {
        "Content-Type": "application/json",
        "X-Atlassian-Webhook-Identifier": delivery_id,
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.is_success:
        try:
            response_status = r.json().get("status")
        except (ValueError, AttributeError):
            response_status = None
        if response_status == "duplicate":
            logger.warning(f"Forge skipped duplicate Jira event {delivery_id}")
        else:
            logger.info(
                f"Forwarded Jira event {delivery_id} to Forge: {r.status_code}"
            )
    else:
        raise RuntimeError(
            f"Forge rejected Jira event: {r.status_code} {r.text}"
        )


async def forward_github(payload: dict[str, Any], event_type: str, delivery_id: str) -> None:
    settings = get_settings()
    url = f"{settings.forge_gateway_url}/api/v1/webhooks/github"
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": delivery_id,
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.is_success:
        try:
            response_status = r.json().get("status")
        except (ValueError, AttributeError):
            response_status = None
        if response_status == "duplicate":
            logger.warning(
                f"Forge skipped duplicate GitHub {event_type} event {delivery_id}"
            )
        else:
            logger.info(
                f"Forwarded GitHub {event_type} event {delivery_id} to Forge: {r.status_code}"
            )
    else:
        raise RuntimeError(
            f"Forge rejected GitHub {event_type} event: {r.status_code} {r.text}"
        )
