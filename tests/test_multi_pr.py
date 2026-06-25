"""Tests for multi-PR support."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

import poller.config as config_module
from poller.jira import extract_pr_info
from poller.models import PrState, TicketState
from poller.persistence import _from_dict, _to_dict
from poller.watcher import TicketWatcher


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")
    monkeypatch.setenv("FORGE_BOT_GITHUB_LOGIN", "forgeSmith-bot")


def _make_pr(repo="org/repo", pr_number=1, **overrides) -> PrState:
    defaults = dict(
        repo=repo,
        pr_number=pr_number,
        branch=f"forge/feature-{pr_number}",
        head_sha=f"sha{pr_number}",
        pr_title=f"PR #{pr_number}",
        pr_url=f"https://github.com/{repo}/pull/{pr_number}",
    )
    defaults.update(overrides)
    return PrState(**defaults)


def _make_state(**overrides) -> TicketState:
    defaults = dict(
        ticket_key="FEAT-1",
        issue_type="Feature",
        status="In Progress",
        summary="Cross-repo feature",
        labels={"forge:managed"},
        last_comment_id=None,
    )
    defaults.update(overrides)
    return TicketState(**defaults)


class TestExtractPrInfo:

    def test_returns_multiple_prs(self):
        links = [
            {"object": {"url": "https://github.com/org/repo-a/pull/1"}},
            {"object": {"url": "https://github.com/org/repo-b/pull/2"}},
        ]
        result = extract_pr_info(links)
        assert result == [("org/repo-a", 1), ("org/repo-b", 2)]

    def test_deduplicates(self):
        links = [
            {"object": {"url": "https://github.com/org/repo/pull/1"}},
            {"object": {"url": "https://github.com/org/repo/pull/1"}},
        ]
        result = extract_pr_info(links)
        assert result == [("org/repo", 1)]

    def test_returns_empty_list_when_no_prs(self):
        links = [{"object": {"url": "https://jira.example.com/browse/FOO-1"}}]
        assert extract_pr_info(links) == []

    def test_returns_empty_list_for_empty_input(self):
        assert extract_pr_info([]) == []


class TestStateMigration:

    def test_migrates_old_flat_pr_fields(self):
        old_data = {
            "ticket_key": "BUG-1",
            "issue_type": "Bug",
            "status": "Open",
            "summary": "Old bug",
            "labels": [],
            "last_comment_id": None,
            "repo": "org/repo",
            "pr_number": 7,
            "branch": "fix/bug",
            "head_sha": "abc123",
            "pr_title": "Fix bug",
            "pr_url": "https://github.com/org/repo/pull/7",
            "last_check_status": "completed",
            "last_check_conclusion": "success",
            "last_completed_count": 3,
            "last_review_id": 42,
            "last_pr_comment_id": 99,
        }
        state = _from_dict(old_data)
        assert len(state.prs) == 1
        pr = state.prs[0]
        assert pr.repo == "org/repo"
        assert pr.pr_number == 7
        assert pr.branch == "fix/bug"
        assert pr.head_sha == "abc123"
        assert pr.last_check_status == "completed"
        assert pr.last_check_conclusion == "success"
        assert pr.last_review_id == 42
        assert pr.last_pr_comment_id == 99

    def test_migrates_old_flat_fields_no_pr(self):
        old_data = {
            "ticket_key": "BUG-2",
            "issue_type": "Bug",
            "status": "Open",
            "summary": "No PR yet",
            "labels": [],
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
            "last_pr_comment_id": None,
        }
        state = _from_dict(old_data)
        assert state.prs == []

    def test_new_format_roundtrip(self):
        state = _make_state(prs=[_make_pr("org/a", 1), _make_pr("org/b", 2)])
        d = _to_dict(state)
        restored = _from_dict(d)
        assert len(restored.prs) == 2
        assert restored.prs[0].repo == "org/a"
        assert restored.prs[0].pr_number == 1
        assert restored.prs[1].repo == "org/b"
        assert restored.prs[1].pr_number == 2


class TestSnapshotMultiplePrs:

    def test_snapshot_discovers_multiple_prs(self):
        watcher = TicketWatcher()

        jira_mock = AsyncMock()
        jira_mock.get_issue.return_value = {
            "fields": {
                "labels": ["forge:managed"],
                "comment": {"comments": []},
                "issuetype": {"name": "Feature"},
                "status": {"name": "New"},
                "summary": "Cross-repo",
            }
        }
        jira_mock.get_remote_links.return_value = [
            {"object": {"url": "https://github.com/org/repo-a/pull/1"}},
            {"object": {"url": "https://github.com/org/repo-b/pull/2"}},
        ]

        def make_pr_response(repo, pr_number):
            return {
                "head": {"ref": f"forge/feat-{pr_number}", "sha": f"sha{pr_number}"},
                "title": f"PR #{pr_number}",
                "html_url": f"https://github.com/{repo}/pull/{pr_number}",
                "merged": False,
            }

        gh_mock = AsyncMock()
        gh_mock.get_pr.side_effect = lambda repo, num: make_pr_response(repo, num)
        gh_mock.get_check_suites.return_value = []
        gh_mock.get_reviews.return_value = []
        gh_mock.get_issue_comments.return_value = []

        with (
            patch("poller.watcher.JiraClient", return_value=jira_mock),
            patch("poller.watcher.GitHubClient", return_value=gh_mock),
        ):
            state = asyncio.run(watcher._snapshot("FEAT-1"))

        assert len(state.prs) == 2
        assert state.prs[0].repo == "org/repo-a"
        assert state.prs[1].repo == "org/repo-b"


class TestPollMultiplePrs:

    def _jira_response(self, links=None):
        return {
            "fields": {
                "labels": ["forge:managed"],
                "status": {"name": "In Progress"},
                "summary": "Cross-repo",
                "comment": {"comments": []},
                "issuetype": {"name": "Bug"},
            }
        }

    def test_discovers_new_pr_on_subsequent_poll(self):
        watcher = TicketWatcher()
        watcher._state = {"FEAT-1": _make_state(
            issue_type="Bug",
            prs=[_make_pr("org/repo-a", 1)],
        )}

        jira_mock = AsyncMock()
        jira_mock.get_issue.return_value = self._jira_response()
        jira_mock.get_remote_links.return_value = [
            {"object": {"url": "https://github.com/org/repo-a/pull/1"}},
            {"object": {"url": "https://github.com/org/repo-b/pull/2"}},
        ]

        def make_pr(repo, num):
            return {
                "head": {"ref": f"branch-{num}", "sha": f"sha{num}"},
                "title": f"PR #{num}",
                "html_url": f"https://github.com/{repo}/pull/{num}",
                "merged": False,
            }

        gh_mock = AsyncMock()
        gh_mock.get_pr.side_effect = lambda repo, num: make_pr(repo, num)
        gh_mock.get_check_suites.return_value = []
        gh_mock.get_reviews.return_value = []
        gh_mock.get_issue_comments.return_value = []

        with (
            patch("poller.watcher.JiraClient", return_value=jira_mock),
            patch("poller.watcher.GitHubClient", return_value=gh_mock),
            patch("poller.watcher.forwarder"),
        ):
            asyncio.run(watcher._poll("FEAT-1"))

        saved = watcher._state["FEAT-1"]
        assert len(saved.prs) == 2
        repos = {pr.repo for pr in saved.prs}
        assert repos == {"org/repo-a", "org/repo-b"}

    def test_all_prs_merged_removes_ticket(self):
        watcher = TicketWatcher()
        watcher._state = {"FEAT-1": _make_state(
            issue_type="Bug",
            prs=[_make_pr("org/a", 1), _make_pr("org/b", 2)],
        )}

        jira_mock = AsyncMock()
        jira_mock.get_issue.return_value = self._jira_response()
        jira_mock.get_remote_links.return_value = []

        gh_mock = AsyncMock()
        gh_mock.get_pr.return_value = {
            "head": {"sha": "sha1"},
            "merged": True,
            "title": "PR",
            "html_url": "https://github.com/org/a/pull/1",
        }

        forwarded = []

        async def capture(payload, event_type):
            forwarded.append(event_type)

        with (
            patch("poller.watcher.JiraClient", return_value=jira_mock),
            patch("poller.watcher.GitHubClient", return_value=gh_mock),
            patch("poller.watcher.forwarder.forward_github", side_effect=capture),
        ):
            asyncio.run(watcher._poll("FEAT-1"))

        assert "FEAT-1" not in watcher._state
        assert forwarded.count("pull_request") == 2

    def test_one_merged_one_open_keeps_ticket(self):
        watcher = TicketWatcher()
        watcher._state = {"FEAT-1": _make_state(
            issue_type="Bug",
            prs=[_make_pr("org/a", 1), _make_pr("org/b", 2)],
        )}

        jira_mock = AsyncMock()
        jira_mock.get_issue.return_value = self._jira_response()
        jira_mock.get_remote_links.return_value = []

        def make_pr(repo, num):
            return {
                "head": {"sha": f"sha{num}"},
                "merged": num == 1,
                "title": f"PR #{num}",
                "html_url": f"https://github.com/{repo}/pull/{num}",
            }

        gh_mock = AsyncMock()
        gh_mock.get_pr.side_effect = lambda repo, num: make_pr(repo, num)
        gh_mock.get_check_suites.return_value = []
        gh_mock.get_reviews.return_value = []
        gh_mock.get_issue_comments.return_value = []

        with (
            patch("poller.watcher.JiraClient", return_value=jira_mock),
            patch("poller.watcher.GitHubClient", return_value=gh_mock),
            patch("poller.watcher.forwarder") as mock_fwd,
        ):
            mock_fwd.forward_github = AsyncMock()
            asyncio.run(watcher._poll("FEAT-1"))

        assert "FEAT-1" in watcher._state
        saved = watcher._state["FEAT-1"]
        assert saved.prs[0].merged is True
        assert saved.prs[1].merged is False

    def test_ci_events_fire_per_pr(self):
        watcher = TicketWatcher()
        watcher._state = {"FEAT-1": _make_state(
            issue_type="Bug",
            prs=[_make_pr("org/a", 1), _make_pr("org/b", 2)],
        )}

        jira_mock = AsyncMock()
        jira_mock.get_issue.return_value = self._jira_response()
        jira_mock.get_remote_links.return_value = []

        gh_mock = AsyncMock()
        gh_mock.get_pr.return_value = {
            "head": {"sha": "sha1"},
            "merged": False,
        }
        gh_mock.get_check_suites.return_value = [
            {"status": "completed", "conclusion": "success"}
        ]
        gh_mock.get_reviews.return_value = []
        gh_mock.get_issue_comments.return_value = []

        forwarded = []

        async def capture(payload, event_type):
            forwarded.append((event_type, payload.get("repository", {}).get("full_name")))

        with (
            patch("poller.watcher.JiraClient", return_value=jira_mock),
            patch("poller.watcher.GitHubClient", return_value=gh_mock),
            patch("poller.watcher.forwarder.forward_github", side_effect=capture),
        ):
            asyncio.run(watcher._poll("FEAT-1"))

        ci_events = [(t, r) for t, r in forwarded if t == "check_suite"]
        assert len(ci_events) == 2
        repos = {r for _, r in ci_events}
        assert repos == {"org/a", "org/b"}
