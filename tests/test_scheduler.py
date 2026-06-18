import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

import poller.config as config_module
from poller.models import TicketState
from poller.persistence import load_state, save_state
from poller.watcher import TicketWatcher


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")
    monkeypatch.setenv("POLL_INTERVAL", "10")
    monkeypatch.setenv("POLLER_JITTER_RATIO", "0")


def _make_state(ticket_key: str, **overrides) -> TicketState:
    values = {
        "ticket_key": ticket_key,
        "issue_type": "Bug",
        "status": "In Progress",
        "summary": "Test",
        "labels": set(),
        "last_comment_id": None,
        "repo": None,
        "pr_number": None,
        "branch": None,
        "head_sha": None,
        "pr_title": None,
        "pr_url": None,
        "last_check_status": None,
        "last_check_conclusion": None,
        "last_completed_count": None,
        "last_review_id": None,
    }
    values.update(overrides)
    return TicketState(**values)


def test_loaded_tickets_are_scheduled_from_state_file(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    due_at = time.time() + 60
    save_state(
        str(state_file),
        {"AISOS-1": _make_state("AISOS-1", next_poll_at=due_at, poll_interval_seconds=60)},
    )
    monkeypatch.setenv("POLLER_STATE_FILE", str(state_file))

    watcher = TicketWatcher()

    assert watcher._state["AISOS-1"].next_poll_at == due_at
    assert watcher._next_due_at_locked() == due_at


def test_successful_poll_resets_failure_count_and_reschedules(monkeypatch):
    watcher = TicketWatcher()
    watcher._state = {
        "AISOS-1": _make_state(
            "AISOS-1",
            repo="org/repo",
            pr_number=1,
            failure_count=4,
            last_check_conclusion=None,
        )
    }
    monkeypatch.setattr(watcher, "_jitter", lambda seconds: float(seconds))

    asyncio.run(watcher._reschedule_after_poll("AISOS-1", success=True))

    state = watcher._state["AISOS-1"]
    assert state.failure_count == 0
    assert state.poll_interval_seconds == 10
    assert state.last_polled_at is not None
    assert state.next_poll_at is not None


def test_failed_poll_uses_exponential_backoff(monkeypatch):
    monkeypatch.setenv("POLLER_MAX_POLL_INTERVAL", "300")
    watcher = TicketWatcher()
    watcher._state = {"AISOS-1": _make_state("AISOS-1", failure_count=1)}
    monkeypatch.setattr(watcher, "_jitter", lambda seconds: float(seconds))

    asyncio.run(watcher._reschedule_after_poll("AISOS-1", success=False))

    state = watcher._state["AISOS-1"]
    assert state.failure_count == 2
    assert state.poll_interval_seconds == 40


def test_launch_due_polls_honors_max_concurrency(monkeypatch):
    monkeypatch.setenv("POLLER_MAX_CONCURRENCY", "2")
    watcher = TicketWatcher()
    watcher._state = {
        "AISOS-1": _make_state("AISOS-1"),
        "AISOS-2": _make_state("AISOS-2"),
        "AISOS-3": _make_state("AISOS-3"),
    }

    async def noop(_ticket_key):
        await asyncio.sleep(0.01)

    monkeypatch.setattr(watcher, "_poll_and_reschedule", noop)

    async def run_launch():
        now = time.time()
        async with watcher._lock:
            for key in watcher._state:
                watcher._schedule_locked(key, now - 1)
        await watcher._launch_due_polls()
        assert len(watcher._inflight) == 2
        await asyncio.gather(*watcher._poll_tasks)

    asyncio.run(run_launch())


def test_archived_ticket_is_removed_without_forwarding():
    watcher = TicketWatcher()
    watcher._state = {"AISOS-1": _make_state("AISOS-1", labels={"forge:approved"})}

    mock_jira = AsyncMock()
    mock_jira.get_issue.return_value = {
        "fields": {
            "labels": ["forge:approved", "forge:archived"],
            "comment": {"comments": []},
            "issuetype": {"name": "Bug"},
            "status": {"name": "Done"},
            "summary": "Archived ticket",
        }
    }

    with (
        patch("poller.watcher.JiraClient", return_value=mock_jira),
        patch("poller.watcher.forwarder.forward_jira") as mock_forward_jira,
        patch("poller.watcher.forwarder.forward_github") as mock_forward_github,
    ):
        asyncio.run(watcher._poll("AISOS-1"))

    assert "AISOS-1" not in watcher._state
    mock_forward_jira.assert_not_called()
    mock_forward_github.assert_not_called()


def test_reschedule_persists_scheduler_metadata(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("POLLER_STATE_FILE", str(state_file))
    watcher = TicketWatcher()
    watcher._state = {"AISOS-1": _make_state("AISOS-1")}
    monkeypatch.setattr(watcher, "_jitter", lambda seconds: float(seconds))

    asyncio.run(watcher._reschedule_after_poll("AISOS-1", success=True))

    reloaded = load_state(str(state_file))
    assert reloaded["AISOS-1"].last_polled_at is not None
    assert reloaded["AISOS-1"].next_poll_at is not None
    assert reloaded["AISOS-1"].poll_interval_seconds == 40
