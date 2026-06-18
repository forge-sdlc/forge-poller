import json
import logging
import os
from dataclasses import asdict, fields

from poller.models import TicketState

logger = logging.getLogger(__name__)


def _to_dict(state: TicketState) -> dict:
    d = asdict(state)
    d["labels"] = sorted(state.labels)
    return d


def _from_dict(d: dict) -> TicketState:
    data = dict(d)
    data["labels"] = set(data.get("labels", []))
    valid_fields = {f.name for f in fields(TicketState)}
    data = {key: value for key, value in data.items() if key in valid_fields}
    return TicketState(**data)


def load_state(path: str) -> dict[str, TicketState]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            items = json.load(f)
        return {item["ticket_key"]: _from_dict(item) for item in items}
    except Exception as e:
        logger.warning(f"Could not load state file {path!r}: {e} — starting with empty state")
        return {}


def save_state(path: str, state: dict[str, TicketState]) -> None:
    data = [_to_dict(s) for s in state.values()]
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)
