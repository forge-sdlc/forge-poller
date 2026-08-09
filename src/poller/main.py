import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from poller.config import get_settings
from poller.watcher import TicketWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

watcher = TicketWatcher()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(watcher.run())
    yield
    task.cancel()


app = FastAPI(title="Forge Poller", lifespan=lifespan)


class WatchRequest(BaseModel):
    tickets: list[str]


@app.post("/watch", status_code=202)
async def watch(
    body: WatchRequest,
    x_invite_code: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    if settings.beta_invite_code and x_invite_code != settings.beta_invite_code:
        raise HTTPException(
            status_code=403,
            detail="Wrong password.\nForge is running on an exclusive beta right now — please ask to join on #forge-sdlc",
        )
    for key in body.tickets:
        await watcher.add(key.upper())
    return {"watching": watcher.list_tickets()}


@app.delete("/watch/{ticket_key}", status_code=200)
async def unwatch(ticket_key: str) -> dict[str, Any]:
    removed = await watcher.remove(ticket_key.upper())
    if not removed:
        raise HTTPException(status_code=404, detail=f"{ticket_key} not in watch list")
    return {"watching": watcher.list_tickets()}


@app.get("/watch")
async def list_watched() -> dict[str, Any]:
    return {"watching": watcher.list_tickets()}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def run() -> None:
    uvicorn.run("poller.main:app", host="0.0.0.0", port=8001, reload=False)


if __name__ == "__main__":
    sys.exit(run())
