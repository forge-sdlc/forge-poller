"""Tests for PRD proposals PR polling."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

import poller.config as config_module
from poller.models import TicketState


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")
    monkeypatch.setenv("FORGE_BOT_GITHUB_LOGIN", "forgeSmith-bot")


def _base_state(**overrides) -> TicketState:
    defaults = dict(
        ticket_key="AISOS-100",
        issue_type="Feature",
        status="In Progress",
        summary="Test Feature",
        labels={"forge:managed", "forge:prd-pending"},
        last_comment_id="10",
        repo=None,
        pr_number=None,
        branch=None,
        head_sha=None,
        pr_title=None,
        pr_url=None,
        last_check_status=None,
        last_check_conclusion=None,
        last_completed_count=None,
        last_review_id=None,
    )
    defaults.update(overrides)
    return TicketState(**defaults)


class TestTicketStateFields:
    def test_prd_pr_fields_default_to_none(self):
        state = _base_state()
        assert state.prd_pr_repo is None
        assert state.prd_pr_number is None
        assert state.prd_last_review_id is None
        assert state.prd_last_pr_comment_id is None

    def test_prd_pr_merged_defaults_to_false(self):
        state = _base_state()
        assert state.prd_pr_merged is False

    def test_prd_pr_fields_can_be_set(self):
        state = _base_state(
            prd_pr_repo="owner/proposals",
            prd_pr_number=5,
            prd_last_review_id=42,
            prd_last_pr_comment_id=101,
            prd_pr_merged=True,
        )
        assert state.prd_pr_repo == "owner/proposals"
        assert state.prd_pr_number == 5
        assert state.prd_last_review_id == 42
        assert state.prd_last_pr_comment_id == 101
        assert state.prd_pr_merged is True
