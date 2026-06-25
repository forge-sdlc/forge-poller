"""Tests for automatic Epic watch-list synchronisation."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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


def _make_state(ticket_key: str, issue_type: str = "Feature", labels: set | None = None) -> TicketState:
    return TicketState(
        ticket_key=ticket_key,
        issue_type=issue_type,
        status="In Progress",
        summary="Test",
        labels=labels or set(),
        last_comment_id=None,
        prs=[],
    )


class TestJiraClientSearchChildren:
    """Tests for the new search_children method on JiraClient."""

    def test_search_children_returns_child_keys(self):
        from poller.jira import JiraClient

        async def run():
            client = JiraClient()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "issues": [
                    {"key": "AISOS-567"},
                    {"key": "AISOS-568"},
                ]
            }
            mock_response.raise_for_status = MagicMock()

            with patch("httpx.AsyncClient") as mock_http:
                mock_http.return_value.__aenter__.return_value.post = AsyncMock(
                    return_value=mock_response
                )
                result = await client.search_children("AISOS-566")

            assert result == ["AISOS-567", "AISOS-568"]

        asyncio.run(run())

    def test_search_children_returns_empty_when_none_found(self):
        from poller.jira import JiraClient

        async def run():
            client = JiraClient()
            mock_response = MagicMock()
            mock_response.json.return_value = {"issues": []}
            mock_response.raise_for_status = MagicMock()

            with patch("httpx.AsyncClient") as mock_http:
                mock_http.return_value.__aenter__.return_value.post = AsyncMock(
                    return_value=mock_response
                )
                result = await client.search_children("AISOS-566")

            assert result == []

        asyncio.run(run())


class TestSyncEpics:
    """Tests for TicketWatcher._sync_epics."""

    def test_adds_new_children_not_yet_watched(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-566": _make_state("AISOS-566", issue_type="Feature"),
        }

        added = []

        async def mock_add(key):
            added.append(key)

        async def mock_search_children(parent_key):
            return ["AISOS-567", "AISOS-568"]

        monkeypatch.setattr(watcher, "add", mock_add)

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.search_children = mock_search_children
            asyncio.run(watcher._sync_epics("AISOS-566"))

        assert sorted(added) == ["AISOS-567", "AISOS-568"]

    def test_skips_children_already_watched(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-566": _make_state("AISOS-566", issue_type="Feature"),
            "AISOS-567": _make_state(
                "AISOS-567",
                issue_type="Epic",
                labels={"forge:managed", "forge:parent:AISOS-566"},
            ),
        }

        added = []

        async def mock_add(key):
            added.append(key)

        async def mock_search_children(parent_key):
            return ["AISOS-567", "AISOS-568"]

        monkeypatch.setattr(watcher, "add", mock_add)

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.search_children = mock_search_children
            asyncio.run(watcher._sync_epics("AISOS-566"))

        assert added == ["AISOS-568"]

    def test_removes_children_no_longer_active(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-566": _make_state("AISOS-566", issue_type="Feature"),
            "AISOS-567": _make_state(
                "AISOS-567",
                issue_type="Epic",
                labels={"forge:managed", "forge:parent:AISOS-566"},
            ),
        }

        removed = []

        async def mock_remove(key):
            removed.append(key)
            return True

        async def mock_search_children(parent_key):
            return []  # AISOS-567 was archived

        monkeypatch.setattr(watcher, "remove", mock_remove)

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.search_children = mock_search_children
            asyncio.run(watcher._sync_epics("AISOS-566"))

        assert removed == ["AISOS-567"]

    def test_does_not_remove_children_of_other_features(self, monkeypatch):
        """Children of a different feature are not touched during sync."""
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-566": _make_state("AISOS-566", issue_type="Feature"),
            "AISOS-600": _make_state(
                "AISOS-600",
                issue_type="Epic",
                labels={"forge:managed", "forge:parent:AISOS-999"},  # different parent
            ),
        }

        removed = []

        async def mock_remove(key):
            removed.append(key)
            return True

        async def mock_search_children(parent_key):
            return []

        monkeypatch.setattr(watcher, "remove", mock_remove)

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.search_children = mock_search_children
            asyncio.run(watcher._sync_epics("AISOS-566"))

        assert removed == []

    def test_handles_search_failure_gracefully(self, monkeypatch):
        """If search_children raises, _sync_epics logs and does not crash."""
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-566": _make_state("AISOS-566", issue_type="Feature"),
        }

        async def mock_search_children(parent_key):
            raise Exception("Jira unavailable")

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.search_children = mock_search_children
            # Should not raise
            asyncio.run(watcher._sync_epics("AISOS-566"))


class TestWatchListShowsChildren:
    """Tests that /watch groups child tickets under their parent."""

    def test_children_nested_under_parent(self):
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-566": _make_state("AISOS-566", issue_type="Feature"),
            "AISOS-567": _make_state(
                "AISOS-567",
                issue_type="Epic",
                labels={"forge:managed", "forge:parent:AISOS-566"},
            ),
            "AISOS-568": _make_state(
                "AISOS-568",
                issue_type="Epic",
                labels={"forge:managed", "forge:parent:AISOS-566"},
            ),
        }

        result = watcher.list()

        parents = [t for t in result if t["ticket_key"] == "AISOS-566"]
        assert len(parents) == 1
        children = parents[0]["children"]
        assert sorted(c["ticket_key"] for c in children) == ["AISOS-567", "AISOS-568"]

    def test_children_not_duplicated_at_top_level(self):
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-566": _make_state("AISOS-566", issue_type="Feature"),
            "AISOS-567": _make_state(
                "AISOS-567",
                issue_type="Epic",
                labels={"forge:managed", "forge:parent:AISOS-566"},
            ),
        }

        result = watcher.list()

        top_level_keys = [t["ticket_key"] for t in result]
        assert "AISOS-567" not in top_level_keys
        assert "AISOS-566" in top_level_keys

    def test_tickets_without_parent_have_empty_children(self):
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-566": _make_state("AISOS-566", issue_type="Feature"),
        }

        result = watcher.list()

        assert result[0]["children"] == []

    def test_orphan_children_appear_at_top_level(self):
        """Children whose parent is not watched appear at the top level."""
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-567": _make_state(
                "AISOS-567",
                issue_type="Epic",
                labels={"forge:managed", "forge:parent:AISOS-566"},
            ),
        }

        result = watcher.list()

        assert len(result) == 1
        assert result[0]["ticket_key"] == "AISOS-567"


class TestPollTriggersEpicSync:
    """Tests that _poll calls _sync_epics for Feature tickets."""

    def test_poll_feature_calls_sync_epics(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-566": _make_state(
                "AISOS-566",
                issue_type="Feature",
                labels={"forge:managed", "forge:plan-pending"},
            ),
        }

        synced = []

        async def mock_sync_epics(feature_key):
            synced.append(feature_key)

        monkeypatch.setattr(watcher, "_sync_epics", mock_sync_epics)

        jira_response = {
            "fields": {
                "labels": ["forge:managed", "forge:plan-pending"],
                "status": {"name": "In Progress"},
                "summary": "Feature",
                "comment": {"comments": []},
                "issuetype": {"name": "Feature"},
            }
        }

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.get_issue = AsyncMock(return_value=jira_response)
            MockJira.return_value.get_remote_links = AsyncMock(return_value=[])
            asyncio.run(watcher._poll("AISOS-566"))

        assert synced == ["AISOS-566"]

    def test_poll_epic_does_not_call_sync_epics(self, monkeypatch):
        watcher = TicketWatcher()
        watcher._state = {
            "AISOS-567": _make_state(
                "AISOS-567",
                issue_type="Epic",
                labels={"forge:managed", "forge:parent:AISOS-566"},
            ),
        }

        synced = []

        async def mock_sync_epics(feature_key):
            synced.append(feature_key)

        monkeypatch.setattr(watcher, "_sync_epics", mock_sync_epics)

        jira_response = {
            "fields": {
                "labels": ["forge:managed", "forge:parent:AISOS-566"],
                "status": {"name": "In Progress"},
                "summary": "Epic",
                "comment": {"comments": []},
                "issuetype": {"name": "Epic"},
            }
        }

        with patch("poller.watcher.JiraClient") as MockJira:
            MockJira.return_value.get_issue = AsyncMock(return_value=jira_response)
            MockJira.return_value.get_remote_links = AsyncMock(return_value=[])
            asyncio.run(watcher._poll("AISOS-567"))

        assert synced == []
