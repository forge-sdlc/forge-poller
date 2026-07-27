"""Tests for snapshot/poll CI state handling fixes.

Covers two bugs:
1. _snapshot() captured current CI conclusion as baseline, causing the poller to
   never fire if a ticket was registered after CI already completed.
2. CI check exceptions were logged at DEBUG level, hiding failures silently.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import poller.config as config_module

from poller.models import PrState, TicketState
from poller.watcher import TicketWatcher


def reset_settings_env(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")


def _make_jira_mock(labels=None, comments=None):
    mock = AsyncMock()
    mock.get_issue.return_value = {
        "fields": {
            "labels": labels or [],
            "comment": {"comments": comments or []},
            "issuetype": {"name": "Bug"},
            "status": {"name": "In Progress"},
            "summary": "Test bug",
        }
    }
    mock.get_remote_links.return_value = [
        {"object": {"url": "https://github.com/forge-sdlc/forge/pull/52"}}
    ]
    return mock


def _make_github_mock(suite_status="completed", suite_conclusion="failure", merged=False):
    mock = AsyncMock()
    mock.get_pr.return_value = {
        "head": {"ref": "forge/aisos-701", "sha": "abc123"},
        "title": "Test PR",
        "html_url": "https://github.com/forge-sdlc/forge/pull/52",
        "merged": merged,
    }
    mock.get_check_suites.return_value = [
        {"status": suite_status, "conclusion": suite_conclusion}
    ]
    mock.get_reviews.return_value = []
    mock.get_issue_comments.return_value = []
    return mock


def _make_github_mock_for_sha(head_sha, suite_conclusion="success"):
    mock = _make_github_mock(suite_status="completed", suite_conclusion=suite_conclusion)
    mock.get_pr.return_value["head"]["sha"] = head_sha
    return mock


def _make_watched_state(
    last_check_status=None,
    last_check_conclusion=None,
    last_reported_head_sha=None,
) -> TicketState:
    return TicketState(
        ticket_key="BUG-42",
        issue_type="Bug",
        status="In Progress",
        summary="Test bug",
        labels=set(),
        last_comment_id=None,
        prs=[PrState(
            repo="forge-sdlc/forge",
            pr_number=52,
            branch="forge/aisos-701",
            head_sha="abc123",
            pr_title="Test PR",
            pr_url="https://github.com/forge-sdlc/forge/pull/52",
            last_check_status=last_check_status,
            last_check_conclusion=last_check_conclusion,
            last_reported_head_sha=last_reported_head_sha,
        )],
    )


class TestSnapshotDoesNotCaptureCiState:
    """_snapshot() must not record CI conclusion as baseline.

    Recording it causes missed triggers when a ticket is registered after CI
    has already completed — every subsequent poll sees the same conclusion and
    treats it as 'no change'.
    """

    def test_snapshot_leaves_ci_conclusion_as_none_when_ci_completed(self, monkeypatch):
        """CI already done at registration time → snapshot stores None, not the conclusion."""
        reset_settings_env(monkeypatch)
        watcher = TicketWatcher()
        mock_jira = _make_jira_mock()
        mock_gh = _make_github_mock(suite_status="completed", suite_conclusion="failure")

        with (
            patch("poller.watcher.JiraClient", return_value=mock_jira),
            patch("poller.watcher.GitHubClient", return_value=mock_gh),
        ):
            state = asyncio.run(watcher._snapshot("BUG-42"))

        assert state.prs[0].last_check_status is None
        assert state.prs[0].last_check_conclusion is None

    def test_snapshot_leaves_ci_conclusion_as_none_when_ci_succeeded(self, monkeypatch):
        """Same: snapshot must not record 'success' as baseline either."""
        reset_settings_env(monkeypatch)
        watcher = TicketWatcher()
        mock_jira = _make_jira_mock()
        mock_gh = _make_github_mock(suite_status="completed", suite_conclusion="success")

        with (
            patch("poller.watcher.JiraClient", return_value=mock_jira),
            patch("poller.watcher.GitHubClient", return_value=mock_gh),
        ):
            state = asyncio.run(watcher._snapshot("BUG-42"))

        assert state.prs[0].last_check_status is None
        assert state.prs[0].last_check_conclusion is None

    def test_first_poll_fires_trigger_when_ci_completed_before_registration(self, monkeypatch):
        """When ticket is registered after CI already failed, the first poll fires the trigger."""
        reset_settings_env(monkeypatch)
        watcher = TicketWatcher()
        # Snapshot stored None (fixed snapshot) → first poll must fire
        watcher._state = {"BUG-42": _make_watched_state(
            last_check_status=None,
            last_check_conclusion=None,
        )}

        mock_jira = _make_jira_mock()
        mock_gh = _make_github_mock(suite_status="completed", suite_conclusion="failure")

        forwarded = []

        async def fake_forward(payload, event_type, delivery_id):
            forwarded.append((payload, event_type, delivery_id))

        with (
            patch("poller.watcher.JiraClient", return_value=mock_jira),
            patch("poller.watcher.GitHubClient", return_value=mock_gh),
            patch("poller.watcher.forwarder.forward_github", side_effect=fake_forward),
        ):
            asyncio.run(watcher._poll("BUG-42"))

        assert len(forwarded) == 1
        assert forwarded[0][1] == "check_suite"
        assert forwarded[0][0]["check_suite"]["conclusion"] == "failure"
        assert forwarded[0][0]["check_suite"]["head_sha"] == "abc123"
        assert forwarded[0][2].startswith("poller-check_suite-")


class TestCiRerunFiresTrigger:
    """When CI is re-run on the same SHA and completes with the same conclusion,
    the poller must still forward the event."""

    def test_rerun_with_same_conclusion_fires_trigger(self, monkeypatch):
        reset_settings_env(monkeypatch)
        watcher = TicketWatcher()
        # First poll already recorded failure
        watcher._state = {"BUG-42": _make_watched_state(
            last_check_status="completed",
            last_check_conclusion="failure",
        )}

        # Simulate re-run: first poll sees suites in-progress, second sees completed
        mock_jira = _make_jira_mock()

        gh_in_progress = _make_github_mock(suite_status="in_progress", suite_conclusion=None)
        gh_completed = _make_github_mock(suite_status="completed", suite_conclusion="failure")

        forwarded = []

        async def fake_forward(payload, event_type, delivery_id):
            forwarded.append((payload, event_type))

        # Poll 1: suites in-progress → resets last_check_status/conclusion
        with (
            patch("poller.watcher.JiraClient", return_value=mock_jira),
            patch("poller.watcher.GitHubClient", return_value=gh_in_progress),
            patch("poller.watcher.forwarder.forward_github", side_effect=fake_forward),
        ):
            asyncio.run(watcher._poll("BUG-42"))

        assert len(forwarded) == 0
        state = watcher._state["BUG-42"]
        assert state.prs[0].last_check_status is None
        assert state.prs[0].last_check_conclusion is None

        # Poll 2: suites completed again → fires trigger
        with (
            patch("poller.watcher.JiraClient", return_value=mock_jira),
            patch("poller.watcher.GitHubClient", return_value=gh_completed),
            patch("poller.watcher.forwarder.forward_github", side_effect=fake_forward),
        ):
            asyncio.run(watcher._poll("BUG-42"))

        assert len(forwarded) == 1
        assert forwarded[0][1] == "check_suite"
        assert forwarded[0][0]["check_suite"]["conclusion"] == "failure"

    def test_new_head_sha_with_same_conclusion_fires_trigger(self, monkeypatch):
        reset_settings_env(monkeypatch)
        watcher = TicketWatcher()
        watcher._state = {"BUG-42": _make_watched_state(
            last_check_status="completed",
            last_check_conclusion="success",
            last_reported_head_sha="abc123",
        )}

        mock_jira = _make_jira_mock()
        mock_gh = _make_github_mock_for_sha("def456", suite_conclusion="success")
        forwarded = []

        async def fake_forward(payload, event_type, delivery_id):
            forwarded.append((payload, event_type))

        with (
            patch("poller.watcher.JiraClient", return_value=mock_jira),
            patch("poller.watcher.GitHubClient", return_value=mock_gh),
            patch("poller.watcher.forwarder.forward_github", side_effect=fake_forward),
        ):
            asyncio.run(watcher._poll("BUG-42"))

        assert len(forwarded) == 1
        assert forwarded[0][1] == "check_suite"
        state = watcher._state["BUG-42"]
        assert state.prs[0].last_reported_head_sha == "def456"


class TestCiCheckExceptionLogLevel:
    """CI check exceptions must be logged at WARNING, not DEBUG."""

    def test_github_api_failure_logged_at_warning(self, monkeypatch, caplog):
        """When get_check_suites raises, the error appears at WARNING level."""
        reset_settings_env(monkeypatch)
        watcher = TicketWatcher()
        watcher._state = {"BUG-42": _make_watched_state()}

        mock_jira = _make_jira_mock()
        mock_gh = AsyncMock()
        mock_gh.get_pr.return_value = {
            "head": {"sha": "abc123"},
            "merged": False,
        }
        mock_gh.get_check_suites.side_effect = RuntimeError("GitHub API unavailable")
        mock_gh.get_reviews.return_value = []
        mock_gh.get_issue_comments.return_value = []

        with (
            patch("poller.watcher.JiraClient", return_value=mock_jira),
            patch("poller.watcher.GitHubClient", return_value=mock_gh),
            caplog.at_level(logging.DEBUG, logger="poller.watcher"),
        ):
            asyncio.run(watcher._poll("BUG-42"))

        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("CI check failed" in m for m in warning_messages), (
            f"Expected WARNING about CI check failure, got: {caplog.records}"
        )
