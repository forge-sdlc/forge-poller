import asyncio
from unittest.mock import AsyncMock

import pytest
import poller.config as config_module
from poller import forwarder
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


def _make_state(ticket_key: str) -> TicketState:
    return TicketState(
        ticket_key=ticket_key,
        issue_type="Story",
        status="In Progress",
        summary="Test",
        labels={"forge:approved"},
        last_comment_id="1",
        prs=[],
    )


def test_watcher_starts_empty_without_state_file():
    watcher = TicketWatcher()
    assert watcher._state == {}


def test_watcher_loads_state_from_file_on_init(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    save_state(str(state_file), {"AISOS-1": _make_state("AISOS-1")})
    monkeypatch.setenv("POLLER_STATE_FILE", str(state_file))
    watcher = TicketWatcher()
    assert "AISOS-1" in watcher._state
    assert watcher._state["AISOS-1"].labels == {"forge:approved"}


def test_watcher_saves_after_add(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("POLLER_STATE_FILE", str(state_file))
    watcher = TicketWatcher()

    async def mock_snapshot(ticket_key: str) -> TicketState:
        return _make_state(ticket_key)

    monkeypatch.setattr(watcher, "_snapshot", mock_snapshot)
    asyncio.run(watcher.add("AISOS-1"))

    reloaded = load_state(str(state_file))
    assert "AISOS-1" in reloaded


def test_add_forwards_bootstrap_event_for_managed_ticket(monkeypatch):
    watcher = TicketWatcher()
    state = _make_state("AISOS-1")
    state.labels = {"custom-label", "forge:managed"}

    async def mock_snapshot(_ticket_key: str) -> TicketState:
        return state

    forward = AsyncMock()
    monkeypatch.setattr(watcher, "_snapshot", mock_snapshot)
    monkeypatch.setattr(forwarder, "forward_jira", forward)

    asyncio.run(watcher.add("AISOS-1"))

    forward.assert_awaited_once()
    payload = forward.await_args.args[0]
    assert payload["issue"]["fields"]["labels"] == ["custom-label", "forge:managed"]
    change = payload["changelog"]["items"][0]
    assert change["fromString"] == "custom-label"
    assert change["toString"] == "custom-label, forge:managed"
    assert watcher._state["AISOS-1"].labels == {"custom-label", "forge:managed"}


def test_add_does_not_forward_bootstrap_event_for_unmanaged_ticket(monkeypatch):
    watcher = TicketWatcher()
    state = _make_state("AISOS-1")
    state.labels = {"custom-label"}

    async def mock_snapshot(_ticket_key: str) -> TicketState:
        return state

    forward = AsyncMock()
    monkeypatch.setattr(watcher, "_snapshot", mock_snapshot)
    monkeypatch.setattr(forwarder, "forward_jira", forward)

    asyncio.run(watcher.add("AISOS-1"))

    forward.assert_not_awaited()


def test_watcher_saves_after_remove(tmp_path, monkeypatch):
    state_file = tmp_path / "state.json"
    save_state(str(state_file), {"AISOS-1": _make_state("AISOS-1")})
    monkeypatch.setenv("POLLER_STATE_FILE", str(state_file))
    watcher = TicketWatcher()

    asyncio.run(watcher.remove("AISOS-1"))

    reloaded = load_state(str(state_file))
    assert "AISOS-1" not in reloaded


def test_watcher_state_file_empty_when_not_configured():
    watcher = TicketWatcher()
    assert watcher._state_file == ""
