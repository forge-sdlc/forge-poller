import logging
from typing import Any

import httpx

from poller.config import get_settings

logger = logging.getLogger(__name__)


async def forward_jira(payload: dict[str, Any]) -> None:
    settings = get_settings()
    url = f"{settings.forge_gateway_url}/api/v1/webhooks/jira"
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
    if r.is_success:
        logger.info(f"Forwarded Jira event to Forge: {r.status_code}")
    else:
        logger.warning(f"Forge rejected Jira event: {r.status_code} {r.text}")


async def forward_github(payload: dict[str, Any], event_type: str) -> None:
    settings = get_settings()
    url = f"{settings.forge_gateway_url}/api/v1/webhooks/github"
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_type,
        "X-GitHub-Delivery": f"poller-{event_type}",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
    if r.is_success:
        logger.info(f"Forwarded GitHub {event_type} event to Forge: {r.status_code}")
    else:
        logger.warning(f"Forge rejected GitHub event: {r.status_code} {r.text}")
