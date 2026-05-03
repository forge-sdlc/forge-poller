import pytest
import poller.config as config_module
import poller.main as main_module
from fastapi.testclient import TestClient
from poller.main import app


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
    monkeypatch.setattr(main_module.watcher, "add", noop)


def test_watch_passes_when_no_code_configured(mock_add):
    with TestClient(app) as client:
        resp = client.post("/watch", json={"tickets": ["AISOS-1"]})
    assert resp.status_code == 202


def test_watch_passes_with_correct_code(monkeypatch, mock_add):
    monkeypatch.setenv("BETA_INVITE_CODE", "letmein")
    with TestClient(app) as client:
        resp = client.post(
            "/watch",
            json={"tickets": ["AISOS-1"]},
            headers={"X-Invite-Code": "letmein"},
        )
    assert resp.status_code == 202


def test_watch_rejects_wrong_code(monkeypatch, mock_add):
    monkeypatch.setenv("BETA_INVITE_CODE", "letmein")
    with TestClient(app) as client:
        resp = client.post(
            "/watch",
            json={"tickets": ["AISOS-1"]},
            headers={"X-Invite-Code": "wrong"},
        )
    assert resp.status_code == 403
    assert "Wrong password" in resp.json()["detail"]


def test_watch_rejects_missing_code_header(monkeypatch, mock_add):
    monkeypatch.setenv("BETA_INVITE_CODE", "letmein")
    with TestClient(app) as client:
        resp = client.post("/watch", json={"tickets": ["AISOS-1"]})
    assert resp.status_code == 403
