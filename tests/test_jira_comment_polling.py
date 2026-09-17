"""Tests for Jira ticket comment polling and forwarding."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import poller.config as config_module
from poller import payloads
from poller.models import TicketState
from poller.watcher import TicketWatcher


def _reset_settings(monkeypatch):
    monkeypatch.setattr(config_module, "_settings", None)
    monkeypatch.setenv("JIRA_BASE_URL", "http://jira.example.com")
    monkeypatch.setenv("JIRA_USER_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtoken")
    monkeypatch.setenv("FORGE_BOT_ACCOUNT_ID", "forge-bot")


def _comment(
    comment_id: str,
    body: str,
    account_id: str,
    email: str | None = None,
    *,
    created: str | None = None,
):
    return {
        "id": comment_id,
        "body": body,
        "created": created or f"2026-09-17T08:00:{int(comment_id):02d}.000+0000",
        "author": {
            "accountId": account_id,
            "displayName": account_id,
            "emailAddress": email if email is not None else f"{account_id}@example.com",
        },
    }


def _issue(
    comments: list[dict],
    *,
    labels: list[str] | None = None,
    updated: str | None = "2026-09-17T10:00:00.000+0000",
):
    fields = {
        "labels": labels if labels is not None else ["forge:managed"],
        "comment": {"comments": comments},
        "issuetype": {"name": "Bug"},
        "status": {"name": "Open"},
        "summary": "Comment polling",
    }
    if updated is not None:
        fields["updated"] = updated
    return {
        "fields": fields
    }


def _watcher(last_comment_id: str | None = "1") -> TicketWatcher:
    watcher = TicketWatcher()
    watcher._state = {
        "BUG-1": TicketState(
            ticket_key="BUG-1",
            issue_type="Bug",
            status="Open",
            summary="Comment polling",
            labels={"forge:managed"},
            last_comment_id=last_comment_id,
        )
    }
    return watcher


def test_comment_created_payload_email_always_empty():
    payload = payloads.comment_created(
        ticket_key="BUG-1",
        issue_type="Bug",
        status="Open",
        summary="s",
        labels={"forge:managed"},
        body="! revise",
        author_account_id="human",
        author_display_name="Human",
        author_email="human@example.com",
    )
    assert payload["comment"]["author"]["emailAddress"] == ""


def test_poll_forwards_all_new_comments_in_order(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "! revise this", "human"),
            _comment("3", "? what next", "human"),
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    bodies = [call.args[0]["comment"]["body"] for call in forwarded.await_args_list]
    assert bodies == ["! revise this", "? what next"]
    assert watcher._state["BUG-1"].last_comment_id == "3"


def test_poll_preserves_jira_comment_revision_metadata(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    new_comment = _comment("2", "! revise", "human")
    new_comment.update(
        {
            "created": "2026-09-17T08:46:03.123+0000",
            "updated": "2026-09-17T08:46:04.456+0000",
        }
    )
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            new_comment,
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    forwarded_comment = forwarded.await_args.args[0]["comment"]
    assert {
        "id": forwarded_comment.get("id"),
        "created": forwarded_comment.get("created"),
        "updated": forwarded_comment.get("updated"),
    } == {
        "id": "2",
        "created": "2026-09-17T08:46:03.123+0000",
        "updated": "2026-09-17T08:46:04.456+0000",
    }


def test_poll_preserves_jira_issue_revision_metadata_for_label_changes(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [_comment("1", "old", "human")],
        labels=["forge:managed", "forge:retry"],
        updated="2026-09-17T10:46:58.789+0000",
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    forwarded_issue = forwarded.await_args.args[0]["issue"]
    assert forwarded_issue["fields"]["updated"] == "2026-09-17T10:46:58.789+0000"


def test_poll_forwards_bot_authored_jira_comments(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "Forge status update", "forge-bot"),
            _comment("3", "! human reply", "human"),
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    bodies = [call.args[0]["comment"]["body"] for call in forwarded.await_args_list]
    assert bodies == ["Forge status update", "! human reply"]
    assert watcher._state["BUG-1"].last_comment_id == "3"


def test_poll_blank_email_in_forwarded_payload(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "! revise", "human", email="human@example.com"),
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    assert len(forwarded.await_args_list) == 1
    payload = forwarded.await_args_list[0].args[0]
    assert payload["comment"]["author"]["emailAddress"] == ""
    assert watcher._state["BUG-1"].last_comment_id == "2"


def test_poll_advances_cursor_after_bot_tip_comment(monkeypatch):
    """Previously a bot tip comment was skipped but still advanced the cursor,
    dropping a concurrent human comment that was not the tip."""
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("1", "old", "human"),
            _comment("2", "! human", "human"),
            _comment("3", "bot tip", "forge-bot"),
        ]
    )
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    bodies = [call.args[0]["comment"]["body"] for call in forwarded.await_args_list]
    assert bodies == ["! human", "bot tip"]
    assert watcher._state["BUG-1"].last_comment_id == "3"


def test_poll_paginates_when_cursor_missing_from_embedded_page(monkeypatch):
    """Embedded issue comments omit older ones; paginate until the cursor is found
    so intervening comments are forwarded instead of only the tip."""
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="1")
    # Embedded page has only the newest comments — cursor "1" is gone.
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("4", "! first missed", "human"),
            _comment("5", "! second missed", "human"),
        ]
    )
    jira.get_comments.return_value = [
        _comment("1", "old", "human"),
        _comment("2", "in the gap", "human"),
        _comment("3", "! command in gap", "human"),
        _comment("4", "! first missed", "human"),
        _comment("5", "! second missed", "human"),
    ]
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    bodies = [call.args[0]["comment"]["body"] for call in forwarded.await_args_list]
    assert bodies == ["in the gap", "! command in gap", "! first missed", "! second missed"]
    assert watcher._state["BUG-1"].last_comment_id == "5"
    jira.get_comments.assert_awaited_once_with("BUG-1")


def test_positional_author_email_does_not_become_comment_id():
    payload = payloads.comment_created(
        "BUG-1", "Bug", "Open", "s", {"forge:managed"}, "! revise", "human", "Human",
        "human@example.com",
    )
    assert "id" not in payload["comment"]
    assert "human@example.com" not in str(payload)


@pytest.mark.parametrize("created", ["", "not-a-date", "2026-09-17T10:00:00", None])
def test_partial_comment_revision_is_rejected(created):
    with pytest.raises(ValueError, match="created"):
        payloads.comment_created(
            "BUG-1", "Bug", "Open", "s", set(), "! revise", "a", "A",
            comment_id="42", created=created,
        )


@pytest.mark.parametrize("updated", ["", "invalid", "2026-09-17T10:00:00"])
def test_invalid_label_revision_is_rejected(updated):
    with pytest.raises(ValueError, match="updated"):
        payloads.label_changed("BUG-1", "Bug", "Open", "s", set(), {"x"}, updated=updated)


def _poll_jira(monkeypatch, watcher, issue, *, comments=None, forward=None):
    _reset_settings(monkeypatch)
    jira = AsyncMock()
    jira.get_issue.return_value = issue
    jira.get_comments.return_value = comments or []
    jira.get_remote_links.return_value = []
    forwarded = forward if forward is not None else AsyncMock()
    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))
    return forwarded


def test_comments_precede_newer_label_snapshot(monkeypatch):
    watcher = _watcher()
    comments = [_comment("1", "old", "a"), _comment("2", "! revise", "a")]
    forwarded = _poll_jira(
        monkeypatch, watcher, _issue(comments, labels=["forge:managed", "forge:retry"]),
    )
    assert [call.args[0]["webhookEvent"] for call in forwarded.await_args_list] == [
        "comment_created", "jira:issue_updated",
    ]


def test_equal_time_label_is_deferred_without_losing_label_change(monkeypatch, caplog):
    watcher = _watcher()
    comment = _comment("2", "! revise", "a")
    issue = _issue(
        [_comment("1", "old", "a"), comment],
        labels=["forge:managed", "forge:retry"], updated=comment["created"],
    )
    forwarded = _poll_jira(monkeypatch, watcher, issue)
    assert [call.args[0]["webhookEvent"] for call in forwarded.await_args_list] == ["comment_created"]
    assert watcher._state["BUG-1"].labels == {"forge:managed"}
    assert watcher._state["BUG-1"].last_comment_id == "2"
    assert "deferred" in caplog.text
    again = _poll_jira(monkeypatch, watcher, issue)
    assert again.await_count == 0
    issue["fields"]["updated"] = "2026-09-17T10:00:00.000+0000"
    final = _poll_jira(monkeypatch, watcher, issue)
    assert final.await_args.args[0]["webhookEvent"] == "jira:issue_updated"
    assert watcher._state["BUG-1"].labels == {"forge:managed", "forge:retry"}


def test_colliding_comments_retain_cursor_and_defer_labels(monkeypatch, caplog):
    watcher = _watcher()
    first = _comment("2", "! one", "a")
    second = _comment("3", "! two", "a", created=first["created"])
    forwarded = _poll_jira(
        monkeypatch, watcher,
        _issue([_comment("1", "old", "a"), first, second], labels=["forge:retry"]),
    )
    assert forwarded.await_count == 0
    assert watcher._state["BUG-1"].last_comment_id == "1"
    assert watcher._state["BUG-1"].labels == {"forge:managed"}
    assert "same timestamp" in caplog.text


def test_unresolved_comment_gap_defers_labels_too(monkeypatch):
    watcher = _watcher(last_comment_id="missing")
    forwarded = _poll_jira(
        monkeypatch, watcher,
        _issue([_comment("3", "! missed", "a")], labels=["forge:retry"]),
        comments=[_comment("3", "! missed", "a")],
    )
    assert forwarded.await_count == 0
    assert watcher._state["BUG-1"].labels == {"forge:managed"}


def test_invalid_comment_does_not_emit_label_or_advance_cursor(monkeypatch):
    watcher = _watcher()
    comment = _comment("2", "! revise", "a")
    comment.pop("created")
    forwarded = AsyncMock()
    with pytest.raises(ValueError, match="created"):
        _poll_jira(
            monkeypatch, watcher,
            _issue([_comment("1", "old", "a"), comment], labels=["forge:retry"]),
            forward=forwarded,
        )
    assert forwarded.await_count == 0
    assert watcher._state["BUG-1"].last_comment_id == "1"


def test_failed_comment_delivery_preserves_cursor_and_labels(monkeypatch):
    watcher = _watcher()
    forwarded = AsyncMock(side_effect=RuntimeError("gateway unavailable"))
    with pytest.raises(RuntimeError, match="gateway unavailable"):
        _poll_jira(
            monkeypatch, watcher,
            _issue([_comment("1", "old", "a"), _comment("2", "! revise", "a")], labels=[]),
            forward=forwarded,
        )
    assert watcher._state["BUG-1"].last_comment_id == "1"
    assert watcher._state["BUG-1"].labels == {"forge:managed"}


def test_live_issue_type_is_used_for_forwarding_and_state(monkeypatch):
    watcher = _watcher()
    issue = _issue([_comment("1", "old", "a"), _comment("2", "! revise", "a")])
    issue["fields"]["issuetype"]["name"] = "Task"
    forwarded = _poll_jira(monkeypatch, watcher, issue)
    assert forwarded.await_args.args[0]["issue"]["fields"]["issuetype"]["name"] == "Task"
    assert watcher._state["BUG-1"].issue_type == "Task"


def test_truncated_embedded_page_is_paginated_even_when_tip_equals_cursor(monkeypatch):
    watcher = _watcher()
    old = _comment("1", "old", "a")
    issue = _issue([old])
    issue["fields"]["comment"]["total"] = 2
    forwarded = _poll_jira(
        monkeypatch, watcher, issue, comments=[old, _comment("2", "! hidden", "a")],
    )
    assert forwarded.await_args.args[0]["comment"]["id"] == "2"
    assert watcher._state["BUG-1"].last_comment_id == "2"


def test_partial_batch_ack_is_persisted_and_pending_payload_survives_restart(monkeypatch, tmp_path):
    _reset_settings(monkeypatch)
    state_file = tmp_path / "poller-state.json"
    watcher = _watcher()
    watcher._state_file = str(state_file)
    comments = [_comment("1", "old", "a"), _comment("2", "! one", "a"), _comment("3", "! two", "a")]
    failed = AsyncMock(side_effect=[None, TimeoutError("uncertain delivery")])
    with pytest.raises(TimeoutError):
        _poll_jira(monkeypatch, watcher, _issue(comments), forward=failed)
    assert watcher._state["BUG-1"].last_comment_id == "2"
    pending = failed.await_args
    monkeypatch.setenv("POLLER_STATE_FILE", str(state_file))
    config_module._settings = None
    restarted = TicketWatcher()
    forwarded = _poll_jira(monkeypatch, restarted, _issue(comments, labels=["forge:retry"]))
    assert forwarded.await_args_list[0] == pending
    comment_ids = [
        call.args[0]["comment"]["id"] for call in forwarded.await_args_list
        if "comment" in call.args[0]
    ]
    assert comment_ids == ["3"]
    assert restarted._state["BUG-1"].last_comment_id == "3"
    assert restarted._state["BUG-1"].pending_jira_delivery is None


def test_changed_issue_revision_during_pagination_preserves_cursor(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher()
    issue = _issue([_comment("4", "! last", "a")])
    changed = _issue([_comment("5", "! newest", "a")], updated="2026-09-17T10:01:00.000+0000")
    jira = AsyncMock()
    jira.get_issue.side_effect = [issue, changed]
    jira.get_comments.return_value = [
        _comment("1", "old", "a"), _comment("2", "! one", "a"), _comment("4", "! gap", "a"),
    ]
    forwarded = AsyncMock()
    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
        pytest.raises(ValueError, match="changed during comment pagination"),
    ):
        asyncio.run(watcher._poll("BUG-1"))
    assert forwarded.await_count == 0
    assert watcher._state["BUG-1"].last_comment_id == "1"


def test_deleted_last_comment_does_not_reset_cursor_or_send_labels(monkeypatch):
    watcher = _watcher()
    forwarded = _poll_jira(monkeypatch, watcher, _issue([], labels=["forge:retry"]))
    assert forwarded.await_count == 0
    assert watcher._state["BUG-1"].last_comment_id == "1"
    assert watcher._state["BUG-1"].labels == {"forge:managed"}


def test_failed_bootstrap_is_pending_and_retried_after_restart(monkeypatch, tmp_path):
    _reset_settings(monkeypatch)
    state_file = tmp_path / "state.json"
    monkeypatch.setenv("POLLER_STATE_FILE", str(state_file))
    watcher = TicketWatcher()
    initial = TicketState("BUG-1", "Bug", "Open", "s", {"forge:managed"}, "1",
                          updated="2026-09-17T08:00:00.000+0000")
    failed = AsyncMock(side_effect=TimeoutError("uncertain bootstrap"))
    with (
        patch.object(watcher, "_snapshot", AsyncMock(return_value=initial)),
        patch("poller.watcher.forwarder.forward_jira", failed),
        pytest.raises(TimeoutError),
    ):
        asyncio.run(watcher.add("BUG-1"))
    assert watcher._state["BUG-1"].next_poll_at is not None
    restarted = TicketWatcher()
    success = AsyncMock()
    with patch("poller.watcher.forwarder.forward_jira", success):
        asyncio.run(restarted._flush_jira_delivery("BUG-1"))
    assert success.await_args == failed.await_args
    assert restarted._state["BUG-1"].pending_jira_delivery is None
    assert TicketWatcher()._state["BUG-1"].pending_jira_delivery is None


@pytest.mark.parametrize("field, value", [("comment_id", ""), ("updated", "2026-09-17T07:00:00+00:00")])
def test_inconsistent_comment_metadata_is_rejected(field, value):
    metadata = {"comment_id": "2", "created": "2026-09-17T08:00:00+00:00", field: value}
    with pytest.raises(ValueError):
        payloads.comment_created("BUG-1", "Bug", "Open", "s", set(), "! revise", "a", "A", **metadata)


def test_no_cursor_delivers_entire_initial_comment_window(monkeypatch):
    watcher = _watcher(last_comment_id=None)
    forwarded = _poll_jira(monkeypatch, watcher, _issue([_comment("1", "! first", "a")]))
    assert forwarded.await_count == 1
    assert watcher._state["BUG-1"].last_comment_id == "1"


def test_duplicate_comment_identity_in_embedded_window_is_rejected(monkeypatch):
    watcher = _watcher()
    forwarded = AsyncMock()
    with pytest.raises(ValueError, match="duplicate comment ID"):
        _poll_jira(monkeypatch, watcher, _issue([
            _comment("1", "old", "a"), _comment("2", "! first", "a"),
            _comment("2", "! duplicate", "a", created="2026-09-17T08:00:03+00:00"),
        ]), forward=forwarded)
    forwarded.assert_not_awaited()
    assert watcher._state["BUG-1"].last_comment_id == "1"


def test_pending_delivery_cannot_be_replaced(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher()
    pending = {"payload": {}, "delivery_id": "still-pending"}
    watcher._state["BUG-1"].pending_jira_delivery = pending
    with pytest.raises(RuntimeError, match="still pending"):
        asyncio.run(watcher._forward_jira("BUG-1", {"another": "event"}))
    assert watcher._state["BUG-1"].pending_jira_delivery is pending


def test_removal_during_pending_send_does_not_resurrect_ticket(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher()
    watcher._state["BUG-1"].pending_jira_delivery = {"payload": {}, "delivery_id": "pending"}

    async def remove_on_send(*args, **kwargs):
        await watcher.remove("BUG-1")

    with patch("poller.watcher.forwarder.forward_jira", AsyncMock(side_effect=remove_on_send)):
        asyncio.run(watcher._poll("BUG-1"))
        asyncio.run(watcher._forward_jira("BUG-1", {}))
    assert "BUG-1" not in watcher._state


def test_concurrent_registration_creates_only_one_bootstrap(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = TicketWatcher()
    watcher._state = {}

    async def run():
        both_snapshots_started = asyncio.Event()
        snapshots = 0

        async def snapshot(_key):
            nonlocal snapshots
            snapshots += 1
            if snapshots == 2:
                both_snapshots_started.set()
            await both_snapshots_started.wait()
            return TicketState("BUG-1", "Bug", "Open", "s", {"forge:managed"}, None,
                               updated="2026-09-17T08:00:00+00:00")

        forwarded = AsyncMock()
        with (
            patch.object(watcher, "_snapshot", snapshot),
            patch("poller.watcher.forwarder.forward_jira", forwarded),
        ):
            await asyncio.gather(watcher.add("BUG-1"), watcher.add("BUG-1"))
            await watcher.add("BUG-1")
        forwarded.assert_awaited_once()
        assert snapshots == 2

    asyncio.run(run())


def test_pending_delivery_is_not_sent_when_state_cannot_be_saved(monkeypatch):
    _reset_settings(monkeypatch)
    watcher = _watcher()
    watcher._state["BUG-1"].pending_jira_delivery = {"payload": {}, "delivery_id": "pending"}
    forwarded = AsyncMock()
    with (
        patch.object(watcher, "_save_state", side_effect=OSError("disk failure")),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
        pytest.raises(OSError, match="disk failure"),
    ):
        asyncio.run(watcher._flush_jira_delivery("BUG-1"))
    forwarded.assert_not_awaited()


def test_registration_can_retry_after_initial_state_write_failure(monkeypatch, tmp_path):
    _reset_settings(monkeypatch)
    watcher = TicketWatcher()
    watcher._state = {}
    watcher._state_file = str(tmp_path / "state.json")
    state = TicketState("BUG-1", "Bug", "Open", "s", {"forge:managed"}, None,
                        updated="2026-09-17T08:00:00+00:00")
    forwarded = AsyncMock()
    with (
        patch.object(watcher, "_snapshot", AsyncMock(return_value=state)),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        with patch.object(watcher, "_save_state", side_effect=OSError("disk failure")):
            with pytest.raises(OSError):
                asyncio.run(watcher.add("BUG-1"))
        forwarded.assert_not_awaited()
        assert "BUG-1" not in watcher._state
        asyncio.run(watcher.add("BUG-1"))
    forwarded.assert_awaited_once()
    assert watcher._state["BUG-1"].next_poll_at is not None


def test_terminal_unmanaged_skip_does_not_block_later_label_restoration(monkeypatch):
    watcher = _watcher()
    comments = [_comment("1", "old", "a")]
    forwarded = AsyncMock(side_effect=["skipped", "queued"])
    _poll_jira(monkeypatch, watcher, _issue(comments, labels=[]), forward=forwarded)
    assert watcher._state["BUG-1"].pending_jira_delivery is None
    assert watcher._state["BUG-1"].labels == set()
    _poll_jira(monkeypatch, watcher, _issue(comments), forward=forwarded)
    assert forwarded.await_count == 2
    assert watcher._state["BUG-1"].labels == {"forge:managed"}
    assert forwarded.await_args_list[0].kwargs["delivery_id"] != forwarded.await_args_list[1].kwargs["delivery_id"]


def test_poll_does_not_advance_cursor_when_paginated_cursor_still_missing(monkeypatch):
    """If the cursor was deleted and never appears in the full comment list,
    do not advance last_comment_id (avoids permanently dropping the gap)."""
    _reset_settings(monkeypatch)
    watcher = _watcher(last_comment_id="deleted-cursor")
    jira = AsyncMock()
    jira.get_issue.return_value = _issue(
        [
            _comment("4", "! a", "human"),
            _comment("5", "! b", "human"),
        ]
    )
    jira.get_comments.return_value = [
        _comment("4", "! a", "human"),
        _comment("5", "! b", "human"),
    ]
    jira.get_remote_links.return_value = []
    forwarded = AsyncMock()

    with (
        patch("poller.watcher.JiraClient", return_value=jira),
        patch("poller.watcher.forwarder.forward_jira", forwarded),
    ):
        asyncio.run(watcher._poll("BUG-1"))

    assert forwarded.await_count == 0
    assert watcher._state["BUG-1"].last_comment_id == "deleted-cursor"
    jira.get_comments.assert_awaited_once_with("BUG-1")
