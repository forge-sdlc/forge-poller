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


def jira_delivery_id() -> str:
    """Build a unique delivery ID for one synthetic Jira event."""
    return f"poller-jira-{uuid4()}"


async def forward_jira(
    payload: dict[str, Any], delivery_id: str | None = None
) -> None:
    settings = get_settings()
    url = f"{settings.forge_gateway_url}/api/v1/webhooks/jira"
    delivery_id = delivery_id or jira_delivery_id()
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
