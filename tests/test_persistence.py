import pytest
from poller.models import TicketState
from poller.persistence import load_state, save_state


def _make_state(ticket_key: str) -> TicketState:
    return TicketState(
        ticket_key=ticket_key,
        issue_type="Story",
        status="In Progress",
        summary="Test ticket",
        labels={"forge:approved", "forge:retry"},
        last_comment_id="42",
        repo="org/repo",
        pr_number=7,
        branch="feat/x",
        head_sha="abc123",
        pr_title="My PR",
        pr_url="https://github.com/org/repo/pull/7",
        last_check_status="completed",
        last_check_conclusion="success",
        last_completed_count=3,
        last_review_id=999,
    )


def test_load_state_returns_empty_dict_when_file_missing(tmp_path):
    result = load_state(str(tmp_path / "nonexistent.json"))
    assert result == {}


def test_load_state_returns_empty_dict_on_malformed_json(tmp_path):
    bad_file = tmp_path / "state.json"
    bad_file.write_text("not valid json {{{{")
    result = load_state(str(bad_file))
    assert result == {}


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    state = _make_state("AISOS-1")
    save_state(path, {"AISOS-1": state})
    reloaded = load_state(path)
    assert "AISOS-1" in reloaded
    restored = reloaded["AISOS-1"]
    assert restored.ticket_key == "AISOS-1"
    assert restored.issue_type == "Story"
    assert restored.labels == {"forge:approved", "forge:retry"}
    assert restored.pr_number == 7
    assert restored.last_review_id == 999


def test_labels_roundtrip_as_set(tmp_path):
    path = str(tmp_path / "state.json")
    state = _make_state("AISOS-1")
    save_state(path, {"AISOS-1": state})
    reloaded = load_state(path)
    assert isinstance(reloaded["AISOS-1"].labels, set)


def test_none_fields_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    state = TicketState(
        ticket_key="AISOS-2",
        issue_type="Bug",
        status="Open",
        summary="Bug ticket",
        labels=set(),
        last_comment_id=None,
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
    save_state(path, {"AISOS-2": state})
    reloaded = load_state(path)
    restored = reloaded["AISOS-2"]
    assert restored.repo is None
    assert restored.pr_number is None
    assert restored.last_review_id is None


def test_save_is_atomic(tmp_path):
    path = str(tmp_path / "state.json")
    state = _make_state("AISOS-1")
    save_state(path, {"AISOS-1": state})
    assert not (tmp_path / "state.json.tmp").exists()


def test_save_multiple_tickets(tmp_path):
    path = str(tmp_path / "state.json")
    states = {
        "AISOS-1": _make_state("AISOS-1"),
        "AISOS-2": _make_state("AISOS-2"),
    }
    save_state(path, states)
    reloaded = load_state(path)
    assert set(reloaded.keys()) == {"AISOS-1", "AISOS-2"}
