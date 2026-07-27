"""Tests for PRD proposals PR polling."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch

import poller.config as config_module
from poller.models import TicketState
from poller.watcher import TicketWatcher


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


def _make_state(ticket_key: str = "AISOS-100", **overrides) -> TicketState:
    overrides.setdefault("ticket_key", ticket_key)
    return _base_state(**overrides)


class TestPrdPrDiscovery:

    def test_discovers_prd_pr_from_jira_comment(self):
        watcher = TicketWatcher()
        state = _make_state()
        jira_comments = [
            {
                "id": "10",
                "body": "PRD published for review: https://github.com/eshulman2/enhancement-proposals/pull/5",
                "author": {"accountId": "forgebot"},
            }
        ]
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "[AISOS-100] PRD: Test",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/5",
        }

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder"):
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=[])
                result = await watcher._poll_prd_pr("AISOS-100", state, jira_comments)
            assert result["prd_pr_repo"] == "eshulman2/enhancement-proposals"
            assert result["prd_pr_number"] == 5

        asyncio.run(run())

    def test_discovers_prd_pr_from_adf_inline_card(self):
        """Jira auto-converts pasted URLs to inlineCard ADF nodes."""
        watcher = TicketWatcher()
        state = _make_state()
        jira_comments = [
            {
                "id": "10",
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "PRD published for review: "},
                                {
                                    "type": "inlineCard",
                                    "attrs": {
                                        "url": "https://github.com/eshulman2/enhancement-proposals/pull/6"
                                    },
                                },
                            ],
                        }
                    ],
                },
                "author": {"accountId": "forgebot"},
            }
        ]
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "[AISOS-100] PRD: Test",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/6",
        }

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder"):
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=[])
                result = await watcher._poll_prd_pr("AISOS-100", state, jira_comments)
            assert result["prd_pr_repo"] == "eshulman2/enhancement-proposals"
            assert result["prd_pr_number"] == 6

        asyncio.run(run())

    def test_discovers_prd_pr_from_adf_link_mark(self):
        """URLs with link marks where text differs from href."""
        watcher = TicketWatcher()
        state = _make_state()
        jira_comments = [
            {
                "id": "10",
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "Click here",
                                    "marks": [
                                        {
                                            "type": "link",
                                            "attrs": {
                                                "href": "https://github.com/eshulman2/enhancement-proposals/pull/7"
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
                "author": {"accountId": "forgebot"},
            }
        ]
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "[AISOS-100] PRD: Test",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/7",
        }

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder"):
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=[])
                result = await watcher._poll_prd_pr("AISOS-100", state, jira_comments)
            assert result["prd_pr_repo"] == "eshulman2/enhancement-proposals"
            assert result["prd_pr_number"] == 7

        asyncio.run(run())

    def test_skips_discovery_when_already_merged(self):
        watcher = TicketWatcher()
        state = _make_state(prd_pr_merged=True)
        jira_comments = [
            {"id": "10", "body": "PRD published for review: https://github.com/owner/repo/pull/5"}
        ]

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH:
                result = await watcher._poll_prd_pr("AISOS-100", state, jira_comments)
                MockGH.assert_not_called()
            assert result["prd_pr_number"] is None
            assert result["prd_pr_merged"] is True

        asyncio.run(run())

    def test_marks_merged_without_event_if_pr_already_merged_at_discovery(self):
        watcher = TicketWatcher()
        state = _make_state()
        jira_comments = [
            {"id": "10", "body": "PRD published for review: https://github.com/eshulman2/enhancement-proposals/pull/5"}
        ]
        mock_pr = {
            "merged": True,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "PRD",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/5",
        }

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                mock_fwd.forward_github = AsyncMock()
                result = await watcher._poll_prd_pr("AISOS-100", state, jira_comments)
                mock_fwd.forward_github.assert_not_called()
            assert result["prd_pr_merged"] is True

        asyncio.run(run())


class TestPrdPrMerge:

    def test_forwards_merge_event_and_sets_merged_flag(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
        )
        mock_pr = {
            "merged": True,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "[AISOS-100] PRD: Test Feature",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/5",
        }
        forwarded_events = []

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                mock_fwd.forward_github = AsyncMock(
                    side_effect=lambda p, event_type, delivery_id: forwarded_events.append(event_type)
                )
                result = await watcher._poll_prd_pr("AISOS-100", state, [])
            assert "pull_request" in forwarded_events
            assert result["prd_pr_merged"] is True

        asyncio.run(run())

    def test_does_not_forward_merge_event_when_already_marked_merged(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
            prd_pr_merged=True,
        )

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                mock_fwd.forward_github = AsyncMock()
                await watcher._poll_prd_pr("AISOS-100", state, [])
                MockGH.assert_not_called()
                mock_fwd.forward_github.assert_not_called()

        asyncio.run(run())


class TestPrdPrReviews:

    def test_forwards_new_review(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
            prd_last_review_id=None,
        )
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "[AISOS-100] PRD: Test",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/5",
        }
        mock_reviews = [
            {
                "id": 42,
                "state": "CHANGES_REQUESTED",
                "body": "Please add more detail.",
                "submitted_at": "2026-06-17T10:00:00Z",
                "user": {"login": "reviewer1"},
            }
        ]
        forwarded_events = []

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=mock_reviews)
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=[])
                mock_fwd.forward_github = AsyncMock(
                    side_effect=lambda p, event_type, delivery_id: forwarded_events.append(event_type)
                )
                result = await watcher._poll_prd_pr("AISOS-100", state, [])
            assert "pull_request_review" in forwarded_events
            assert result["prd_last_review_id"] == 42

        asyncio.run(run())

    def test_does_not_forward_already_seen_review(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
            prd_last_review_id=42,
        )
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "PRD",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/5",
        }
        mock_reviews = [
            {
                "id": 42,
                "state": "CHANGES_REQUESTED",
                "body": "Feedback",
                "submitted_at": "2026-06-17T10:00:00Z",
                "user": {"login": "reviewer1"},
            }
        ]

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=mock_reviews)
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=[])
                mock_fwd.forward_github = AsyncMock()
                await watcher._poll_prd_pr("AISOS-100", state, [])
                mock_fwd.forward_github.assert_not_called()

        asyncio.run(run())


class TestPrdPrComments:

    def test_forwards_new_comment(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
            prd_last_pr_comment_id=None,
        )
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "PRD",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/5",
        }
        # get_issue_comments returns newest-first
        mock_comments = [
            {"id": 101, "body": "Can we clarify section 3?", "user": {"login": "reviewer1"}},
        ]
        forwarded_events = []

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=mock_comments)
                mock_fwd.forward_github = AsyncMock(
                    side_effect=lambda p, event_type, delivery_id: forwarded_events.append(event_type)
                )
                result = await watcher._poll_prd_pr("AISOS-100", state, [])
            assert "issue_comment" in forwarded_events
            assert result["prd_last_pr_comment_id"] == 101

        asyncio.run(run())

    def test_skips_bot_comment(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
            prd_last_pr_comment_id=None,
        )
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "PRD",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/5",
        }
        mock_comments = [
            {"id": 101, "body": "PRD has been revised.", "user": {"login": "forgeSmith-bot"}},
        ]

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=mock_comments)
                mock_fwd.forward_github = AsyncMock()
                await watcher._poll_prd_pr("AISOS-100", state, [])
                mock_fwd.forward_github.assert_not_called()

        asyncio.run(run())

    def test_forwards_multiple_new_comments_in_chronological_order(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
            prd_last_pr_comment_id=100,
        )
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/prd/aisos-100"},
            "title": "PRD",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/5",
        }
        # newest-first (as returned by get_issue_comments)
        mock_comments = [
            {"id": 103, "body": "Third comment", "user": {"login": "reviewer1"}},
            {"id": 102, "body": "Second comment", "user": {"login": "reviewer1"}},
            {"id": 101, "body": "First comment", "user": {"login": "reviewer1"}},
            {"id": 100, "body": "Already seen", "user": {"login": "reviewer1"}},
        ]
        forwarded_bodies = []

        async def capture_forward(payload, event_type, delivery_id):
            forwarded_bodies.append(payload["comment"]["body"])

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=mock_comments)
                mock_fwd.forward_github = AsyncMock(side_effect=capture_forward)
                result = await watcher._poll_prd_pr("AISOS-100", state, [])

            assert forwarded_bodies == ["First comment", "Second comment", "Third comment"]
            assert result["prd_last_pr_comment_id"] == 103

        asyncio.run(run())


class TestPollIntegration:

    def test_poll_feature_calls_poll_prd_pr(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {"AISOS-100": _make_state()}
        called_with = []

        async def mock_poll_prd_pr(ticket_key, state, comments):
            called_with.append(ticket_key)
            return {
                "prd_pr_repo": None, "prd_pr_number": None,
                "prd_last_review_id": None, "prd_last_pr_comment_id": None,
                "prd_pr_merged": False,
            }

        async def mock_poll_spec_pr(ticket_key, state, comments, prd_updates=None):
            return {
                "spec_pr_repo": None, "spec_pr_number": None,
                "spec_last_review_id": None, "spec_last_pr_comment_id": None,
                "spec_pr_merged": False,
            }

        async def mock_sync_epics(feature_key):
            pass

        monkeypatch.setattr(watcher, "_poll_prd_pr", mock_poll_prd_pr)
        monkeypatch.setattr(watcher, "_poll_spec_pr", mock_poll_spec_pr)
        monkeypatch.setattr(watcher, "_sync_epics", mock_sync_epics)

        jira_response = {
            "fields": {
                "labels": ["forge:managed", "forge:prd-pending"],
                "status": {"name": "In Progress"},
                "summary": "Feature",
                "comment": {"comments": []},
                "issuetype": {"name": "Feature"},
            }
        }

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.get_issue = AsyncMock(return_value=jira_response)
            MockJira.return_value.get_remote_links = AsyncMock(return_value=[])
            asyncio.run(watcher._poll("AISOS-100"))

        assert called_with == ["AISOS-100"]

    def test_poll_epic_does_not_call_poll_prd_pr(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {"AISOS-200": _make_state(
            "AISOS-200",
            issue_type="Epic",
            labels={"forge:managed"},
        )}
        called = []

        async def mock_poll_prd_pr(ticket_key, state, comments):
            called.append(ticket_key)
            return {}

        async def mock_poll_spec_pr(ticket_key, state, comments, prd_updates=None):
            called.append(f"spec:{ticket_key}")
            return {}

        monkeypatch.setattr(watcher, "_poll_prd_pr", mock_poll_prd_pr)
        monkeypatch.setattr(watcher, "_poll_spec_pr", mock_poll_spec_pr)

        jira_response = {
            "fields": {
                "labels": ["forge:managed"],
                "status": {"name": "In Progress"},
                "summary": "Epic",
                "comment": {"comments": []},
                "issuetype": {"name": "Epic"},
            }
        }

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.get_issue = AsyncMock(return_value=jira_response)
            MockJira.return_value.get_remote_links = AsyncMock(return_value=[])
            asyncio.run(watcher._poll("AISOS-200"))

        assert called == []

    def test_prd_state_from_poll_prd_pr_is_persisted(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {"AISOS-100": _make_state()}

        async def mock_poll_prd_pr(ticket_key, state, comments):
            return {
                "prd_pr_repo": "eshulman2/enhancement-proposals",
                "prd_pr_number": 5,
                "prd_last_review_id": None,
                "prd_last_pr_comment_id": None,
                "prd_pr_merged": False,
            }

        async def mock_poll_spec_pr(ticket_key, state, comments, prd_updates=None):
            return {
                "spec_pr_repo": None, "spec_pr_number": None,
                "spec_last_review_id": None, "spec_last_pr_comment_id": None,
                "spec_pr_merged": False,
            }

        async def mock_sync_epics(feature_key):
            pass

        monkeypatch.setattr(watcher, "_poll_prd_pr", mock_poll_prd_pr)
        monkeypatch.setattr(watcher, "_poll_spec_pr", mock_poll_spec_pr)
        monkeypatch.setattr(watcher, "_sync_epics", mock_sync_epics)

        jira_response = {
            "fields": {
                "labels": ["forge:managed", "forge:prd-pending"],
                "status": {"name": "In Progress"},
                "summary": "Feature",
                "comment": {"comments": []},
                "issuetype": {"name": "Feature"},
            }
        }

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.get_issue = AsyncMock(return_value=jira_response)
            MockJira.return_value.get_remote_links = AsyncMock(return_value=[])
            asyncio.run(watcher._poll("AISOS-100"))

        saved = watcher._state["AISOS-100"]
        assert saved.prd_pr_repo == "eshulman2/enhancement-proposals"
        assert saved.prd_pr_number == 5
        assert saved.prd_pr_merged is False


# ── Spec PR polling tests ──────────────────────────────────────────────


class TestSpecPrDiscovery:

    def test_discovers_spec_pr_skipping_known_prd_pr(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
        )
        jira_comments = [
            {"id": "10", "body": "PRD published for review: https://github.com/eshulman2/enhancement-proposals/pull/5"},
            {"id": "11", "body": "Specification published for review: https://github.com/eshulman2/enhancement-proposals/pull/6"},
        ]
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/spec/aisos-100"},
            "title": "[AISOS-100] Spec: Test",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/6",
        }

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder"):
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=[])
                result = await watcher._poll_spec_pr("AISOS-100", state, jira_comments)
            assert result["spec_pr_repo"] == "eshulman2/enhancement-proposals"
            assert result["spec_pr_number"] == 6

        asyncio.run(run())

    def test_discovers_spec_pr_from_adf_inline_card(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
        )
        jira_comments = [
            {
                "id": "11",
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Specification published for review: "},
                                {
                                    "type": "inlineCard",
                                    "attrs": {
                                        "url": "https://github.com/eshulman2/enhancement-proposals/pull/6"
                                    },
                                },
                            ],
                        }
                    ],
                },
                "author": {"accountId": "forgebot"},
            }
        ]
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/spec/aisos-100"},
            "title": "[AISOS-100] Spec: Test",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/6",
        }

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder"):
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=[])
                result = await watcher._poll_spec_pr("AISOS-100", state, jira_comments)
            assert result["spec_pr_repo"] == "eshulman2/enhancement-proposals"
            assert result["spec_pr_number"] == 6

        asyncio.run(run())

    def test_does_not_discover_unrelated_pr_link_as_spec_pr(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
        )
        jira_comments = [
            {"id": "11", "body": "Implementation PR: https://github.com/org/app/pull/12"},
        ]

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH:
                result = await watcher._poll_spec_pr("AISOS-100", state, jira_comments)
                MockGH.assert_not_called()
            assert result["spec_pr_number"] is None

        asyncio.run(run())

    def test_does_not_discover_prd_pr_as_spec_pr(self):
        """When only the PRD PR URL exists in comments, spec should not discover it."""
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
        )
        jira_comments = [
            {"id": "10", "body": "PRD published for review: https://github.com/eshulman2/enhancement-proposals/pull/5"},
        ]

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH:
                result = await watcher._poll_spec_pr("AISOS-100", state, jira_comments)
                MockGH.assert_not_called()
            assert result["spec_pr_number"] is None

        asyncio.run(run())

    def test_discovers_spec_pr_when_prd_discovered_same_cycle(self):
        """When PRD PR is discovered in the same poll cycle, prd_updates should
        be used to filter it so spec discovery finds the correct PR."""
        watcher = TicketWatcher()
        state = _make_state()  # prd_pr_repo/prd_pr_number are None
        jira_comments = [
            {"id": "10", "body": "PRD published for review: https://github.com/eshulman2/enhancement-proposals/pull/9"},
            {"id": "11", "body": "Specification published for review: https://github.com/eshulman2/enhancement-proposals/pull/10"},
        ]
        prd_updates = {
            "prd_pr_repo": "eshulman2/enhancement-proposals",
            "prd_pr_number": 9,
        }
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/spec/aisos-100"},
            "title": "[AISOS-100] Spec: Test",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/10",
        }

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder"):
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=[])
                result = await watcher._poll_spec_pr("AISOS-100", state, jira_comments, prd_updates)
            assert result["spec_pr_repo"] == "eshulman2/enhancement-proposals"
            assert result["spec_pr_number"] == 10

        asyncio.run(run())

    def test_marks_merged_at_discovery(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
        )
        jira_comments = [
            {"id": "11", "body": "Specification published for review: https://github.com/eshulman2/enhancement-proposals/pull/6"},
        ]
        mock_pr = {"merged": True, "head": {"ref": "forge/spec/aisos-100"}}

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                mock_fwd.forward_github = AsyncMock()
                result = await watcher._poll_spec_pr("AISOS-100", state, jira_comments)
                mock_fwd.forward_github.assert_not_called()
            assert result["spec_pr_merged"] is True

        asyncio.run(run())


class TestSpecPrMerge:

    def test_forwards_merge_event(self):
        watcher = TicketWatcher()
        state = _make_state(
            prd_pr_repo="eshulman2/enhancement-proposals",
            prd_pr_number=5,
            spec_pr_repo="eshulman2/enhancement-proposals",
            spec_pr_number=6,
        )
        mock_pr = {
            "merged": True,
            "head": {"ref": "forge/spec/aisos-100"},
            "title": "[AISOS-100] Spec: Test",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/6",
        }
        forwarded_events = []

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                mock_fwd.forward_github = AsyncMock(
                    side_effect=lambda p, event_type, delivery_id: forwarded_events.append(event_type)
                )
                result = await watcher._poll_spec_pr("AISOS-100", state, [])
            assert "pull_request" in forwarded_events
            assert result["spec_pr_merged"] is True

        asyncio.run(run())


class TestSpecPrReviews:

    def test_forwards_new_review(self):
        watcher = TicketWatcher()
        state = _make_state(
            spec_pr_repo="eshulman2/enhancement-proposals",
            spec_pr_number=6,
            spec_last_review_id=None,
        )
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/spec/aisos-100"},
            "title": "Spec",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/6",
        }
        mock_reviews = [
            {
                "id": 99,
                "state": "APPROVED",
                "body": "LGTM",
                "submitted_at": "2026-06-17T10:00:00Z",
                "user": {"login": "reviewer1"},
            }
        ]
        forwarded_events = []

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=mock_reviews)
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=[])
                mock_fwd.forward_github = AsyncMock(
                    side_effect=lambda p, event_type, delivery_id: forwarded_events.append(event_type)
                )
                result = await watcher._poll_spec_pr("AISOS-100", state, [])
            assert "pull_request_review" in forwarded_events
            assert result["spec_last_review_id"] == 99

        asyncio.run(run())


class TestSpecPrComments:

    def test_forwards_new_comment(self):
        watcher = TicketWatcher()
        state = _make_state(
            spec_pr_repo="eshulman2/enhancement-proposals",
            spec_pr_number=6,
            spec_last_pr_comment_id=None,
        )
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/spec/aisos-100"},
            "title": "Spec",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/6",
        }
        mock_comments = [
            {"id": 201, "body": "Add error handling section", "user": {"login": "reviewer1"}},
        ]
        forwarded_events = []

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=mock_comments)
                mock_fwd.forward_github = AsyncMock(
                    side_effect=lambda p, event_type, delivery_id: forwarded_events.append(event_type)
                )
                result = await watcher._poll_spec_pr("AISOS-100", state, [])
            assert "issue_comment" in forwarded_events
            assert result["spec_last_pr_comment_id"] == 201

        asyncio.run(run())

    def test_skips_bot_comment(self):
        watcher = TicketWatcher()
        state = _make_state(
            spec_pr_repo="eshulman2/enhancement-proposals",
            spec_pr_number=6,
            spec_last_pr_comment_id=None,
        )
        mock_pr = {
            "merged": False,
            "head": {"ref": "forge/spec/aisos-100"},
            "title": "Spec",
            "html_url": "https://github.com/eshulman2/enhancement-proposals/pull/6",
        }
        mock_comments = [
            {"id": 201, "body": "Spec revised.", "user": {"login": "forgeSmith-bot"}},
        ]

        async def run():
            with patch("poller.watcher.GitHubClient") as MockGH, \
                 patch("poller.watcher.forwarder") as mock_fwd:
                MockGH.return_value.get_pr = AsyncMock(return_value=mock_pr)
                MockGH.return_value.get_reviews = AsyncMock(return_value=[])
                MockGH.return_value.get_issue_comments = AsyncMock(return_value=mock_comments)
                mock_fwd.forward_github = AsyncMock()
                await watcher._poll_spec_pr("AISOS-100", state, [])
                mock_fwd.forward_github.assert_not_called()

        asyncio.run(run())


class TestSpecPollIntegration:

    def test_poll_feature_calls_poll_spec_pr(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {"AISOS-100": _make_state()}
        spec_called = []

        async def mock_poll_prd_pr(ticket_key, state, comments):
            return {
                "prd_pr_repo": None, "prd_pr_number": None,
                "prd_last_review_id": None, "prd_last_pr_comment_id": None,
                "prd_pr_merged": False,
            }

        async def mock_poll_spec_pr(ticket_key, state, comments, prd_updates=None):
            spec_called.append(ticket_key)
            return {
                "spec_pr_repo": None, "spec_pr_number": None,
                "spec_last_review_id": None, "spec_last_pr_comment_id": None,
                "spec_pr_merged": False,
            }

        async def mock_sync_epics(feature_key):
            pass

        monkeypatch.setattr(watcher, "_poll_prd_pr", mock_poll_prd_pr)
        monkeypatch.setattr(watcher, "_poll_spec_pr", mock_poll_spec_pr)
        monkeypatch.setattr(watcher, "_sync_epics", mock_sync_epics)

        jira_response = {
            "fields": {
                "labels": ["forge:managed", "forge:prd-pending"],
                "status": {"name": "In Progress"},
                "summary": "Feature",
                "comment": {"comments": []},
                "issuetype": {"name": "Feature"},
            }
        }

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.get_issue = AsyncMock(return_value=jira_response)
            MockJira.return_value.get_remote_links = AsyncMock(return_value=[])
            asyncio.run(watcher._poll("AISOS-100"))

        assert spec_called == ["AISOS-100"]

    def test_spec_state_is_persisted(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {"AISOS-100": _make_state(labels={"forge:managed"})}

        async def mock_poll_prd_pr(ticket_key, state, comments):
            return {
                "prd_pr_repo": None, "prd_pr_number": None,
                "prd_last_review_id": None, "prd_last_pr_comment_id": None,
                "prd_pr_merged": False,
            }

        async def mock_poll_spec_pr(ticket_key, state, comments, prd_updates=None):
            return {
                "spec_pr_repo": "eshulman2/enhancement-proposals",
                "spec_pr_number": 6,
                "spec_last_review_id": None,
                "spec_last_pr_comment_id": None,
                "spec_pr_merged": False,
            }

        async def mock_sync_epics(feature_key):
            pass

        monkeypatch.setattr(watcher, "_poll_prd_pr", mock_poll_prd_pr)
        monkeypatch.setattr(watcher, "_poll_spec_pr", mock_poll_spec_pr)
        monkeypatch.setattr(watcher, "_sync_epics", mock_sync_epics)

        jira_response = {
            "fields": {
                "labels": ["forge:managed"],
                "status": {"name": "In Progress"},
                "summary": "Feature",
                "comment": {"comments": []},
                "issuetype": {"name": "Feature"},
            }
        }

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.get_issue = AsyncMock(return_value=jira_response)
            MockJira.return_value.get_remote_links = AsyncMock(return_value=[])
            asyncio.run(watcher._poll("AISOS-100"))

        saved = watcher._state["AISOS-100"]
        assert saved.spec_pr_repo == "eshulman2/enhancement-proposals"
        assert saved.spec_pr_number == 6
        assert saved.spec_pr_merged is False
