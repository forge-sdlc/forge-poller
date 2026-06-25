import json
import logging
import os
from dataclasses import asdict, fields

from poller.models import PrState, TicketState

logger = logging.getLogger(__name__)

_PR_STATE_FIELDS = {f.name for f in fields(PrState)}
_OLD_FLAT_PR_FIELDS = {
    "repo", "pr_number", "branch", "head_sha", "pr_title", "pr_url",
    "last_check_status", "last_check_conclusion", "last_completed_count",
    "last_review_id", "last_pr_comment_id",
}


def _to_dict(state: TicketState) -> dict:
    d = asdict(state)
    d["labels"] = sorted(state.labels)
    return d


def _from_dict(d: dict) -> TicketState:
    data = dict(d)
    data["labels"] = set(data.get("labels", []))

    if "prs" not in data and "pr_number" in data:
        pr_number = data.get("pr_number")
        if pr_number is not None:
            data["prs"] = [{
                "repo": data.get("repo", ""),
                "pr_number": pr_number,
                "branch": data.get("branch", ""),
                "head_sha": data.get("head_sha", ""),
                "pr_title": data.get("pr_title", ""),
                "pr_url": data.get("pr_url", ""),
                "last_check_status": data.get("last_check_status"),
                "last_check_conclusion": data.get("last_check_conclusion"),
                "last_completed_count": data.get("last_completed_count"),
                "last_review_id": data.get("last_review_id"),
                "last_pr_comment_id": data.get("last_pr_comment_id"),
            }]
        else:
            data["prs"] = []

    for key in _OLD_FLAT_PR_FIELDS:
        data.pop(key, None)

    if "prs" in data:
        data["prs"] = [
            PrState(**{k: v for k, v in pr.items() if k in _PR_STATE_FIELDS})
            for pr in data["prs"]
        ]

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
