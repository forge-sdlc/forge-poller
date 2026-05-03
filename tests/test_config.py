import pytest
import poller.config as config_module


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")


def test_beta_invite_code_reads_from_env(monkeypatch):
    monkeypatch.setenv("BETA_INVITE_CODE", "supersecret")
    settings = config_module.Settings()
    assert settings.beta_invite_code == "supersecret"


def test_beta_invite_code_defaults_to_empty():
    settings = config_module.Settings()
    assert settings.beta_invite_code == ""
