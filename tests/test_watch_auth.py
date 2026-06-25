import asyncio

import pytest
import poller.config as config_module
import poller.main as main_module
from fastapi import HTTPException
from poller.main import WatchRequest


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")


@pytest.fixture
def mock_add(monkeypatch):
    async def noop(key):
        pass
    async def noop_run():
        pass
    monkeypatch.setattr(main_module.watcher, "add", noop)
    monkeypatch.setattr(main_module.watcher, "run", noop_run)


def test_watch_passes_when_no_code_configured(mock_add):
    resp = asyncio.run(main_module.watch(WatchRequest(tickets=["AISOS-1"])))
    assert "watching" in resp


def test_watch_passes_with_correct_code(monkeypatch, mock_add):
    monkeypatch.setenv("BETA_INVITE_CODE", "letmein")
    resp = asyncio.run(
        main_module.watch(
            WatchRequest(tickets=["AISOS-1"]),
            x_invite_code="letmein",
        )
    )
    assert "watching" in resp


def test_watch_rejects_wrong_code(monkeypatch, mock_add):
    monkeypatch.setenv("BETA_INVITE_CODE", "letmein")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            main_module.watch(
                WatchRequest(tickets=["AISOS-1"]),
                x_invite_code="wrong",
            )
        )
    assert exc_info.value.status_code == 403
    assert "Wrong password" in exc_info.value.detail


def test_watch_rejects_missing_code_header(monkeypatch, mock_add):
    monkeypatch.setenv("BETA_INVITE_CODE", "letmein")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main_module.watch(WatchRequest(tickets=["AISOS-1"])))
    assert exc_info.value.status_code == 403
