"""Safety checks for the optional operator script; no live Redis or Forge needed."""

import asyncio
import copy
import json
import runpy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def repair():
    return runpy.run_path(Path(__file__).parents[1] / "scripts" / "repair_jira_ledger.py")


@pytest.fixture
def evidence():
    observation = {
        "observation_id": "old-observation",
        "source_system": "jira",
        "source": "webhook",
        "resource": {"resource_type": "issue", "external_id": "BUG-1", "namespace": None},
        "resource_revision": None,
        "revision_order": None,
        "received_at": "2026-09-17T12:00:00+00:00",
        "facts": {"comment_text": "! original", "event_type": "comment_created"},
        "correlation": {"provider_event_id": "old-event"},
    }
    resource = {"latest": observation, "latest_delivery_identity": "old-delivery"}
    accepted = {
        "observation": copy.deepcopy(observation),
        "delivery_identity": "old-delivery",
        "disposition": "accepted",
    }
    baseline = {
        **copy.deepcopy(observation),
        "resource_revision": "comment:1",
        "revision_order": 10,
    }
    replay = {
        **copy.deepcopy(baseline),
        "resource_revision": "comment:2",
        "revision_order": 20,
        "facts": {"comment_text": "! next", "event_type": "comment_created"},
    }
    marker = {
        "observation": copy.deepcopy(replay),
        "disposition": "conflict",
        "reason": "opaque revisions cannot be ordered safely",
    }
    return resource, [accepted], baseline, [("delivery-2", replay, marker)]


def test_repair_changes_only_baseline_metadata_and_selects_rejected_delivery(repair, evidence):
    resource, history, baseline, replays = evidence
    before = copy.deepcopy(resource)
    updated, delete = repair["build_repair"](resource, history, baseline, replays)
    assert resource == before
    expected = copy.deepcopy(resource)
    expected["latest"].update(resource_revision="comment:1", revision_order=10)
    assert updated == expected
    assert delete == ["delivery-2"]


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("ordered", "already ordered"),
        ("facts", "facts"),
        ("resource", "resource"),
        ("history", "accepted"),
        ("order", "revision"),
        ("equal", "newer"),
        ("marker_accepted", "rejected"),
        ("marker_policy", "rejected"),
        ("marker_facts", "facts"),
        ("replay_accepted", "already accepted"),
        ("replay_duplicate", "newer"),
        ("source", "Only Jira"),
        ("marker_order", "revision differs"),
    ],
)
def test_unsafe_repair_is_refused(repair, evidence, mutation, message):
    resource, history, baseline, replays = evidence
    if mutation == "ordered":
        resource["latest"]["revision_order"] = 1
    elif mutation == "facts":
        baseline["facts"]["comment_text"] = "different"
    elif mutation == "resource":
        baseline["resource"]["external_id"] = "BUG-2"
    elif mutation == "history":
        history.clear()
    elif mutation == "order":
        baseline["revision_order"] = None
    elif mutation == "equal":
        replays[0][1]["revision_order"] = 10
    elif mutation == "marker_accepted":
        replays[0][2]["disposition"] = "accepted"
    elif mutation == "marker_policy":
        replays[0][2]["reason"] = "external observation attempted to set workflow-owned facts"
    elif mutation == "marker_facts":
        replays[0][2]["observation"]["facts"]["comment_text"] = "different"
    elif mutation == "replay_accepted":
        history.append({"observation": replays[0][1], "disposition": "accepted"})
    elif mutation == "replay_duplicate":
        replays.append(replays[0])
    elif mutation == "source":
        baseline["source_system"] = "github"
    elif mutation == "marker_order":
        replays[0][2]["observation"]["revision_order"] = 99
    with pytest.raises(ValueError, match=message):
        repair["build_repair"](resource, history, baseline, replays)


def _redis(values):
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=lambda key: values.get(key))
    redis.lrange = AsyncMock(side_effect=lambda key, *_: values.get(key, []))
    pipeline = MagicMock()
    pipeline.watch = AsyncMock()
    pipeline.get = redis.get
    pipeline.lrange = redis.lrange

    async def execute():
        for args in pipeline.set.call_args_list:
            values[args.args[0]] = args.args[1]
        for args in pipeline.delete.call_args_list:
            values.pop(args.args[0], None)

    pipeline.execute = AsyncMock(side_effect=execute)
    pipeline.__aenter__ = AsyncMock(return_value=pipeline)
    pipeline.__aexit__ = AsyncMock(return_value=False)
    redis.pipeline.return_value = pipeline
    return redis, pipeline


def test_apply_backs_up_before_atomic_update_and_keeps_history(repair, tmp_path):
    values = {"resource": "old", "delivery": "rejected", "history": ["accepted"]}
    redis, pipe = _redis(values)
    expected = {
        "resource": {"kind": "string", "value": "old"},
        "delivery": {"kind": "string", "value": "rejected"},
        "history": {"kind": "list", "value": ["accepted"]},
    }
    backup = tmp_path / "backup.json"
    asyncio.run(
        repair["apply_repair"](redis, expected, "resource", "new", ["delivery"], backup, {})
    )
    assert values == {"resource": "new", "history": ["accepted"]}
    assert json.loads(backup.read_text())["before"] == expected
    assert backup.stat().st_mode & 0o777 == 0o600
    pipe.execute.assert_awaited_once()


def test_concurrent_change_aborts_without_writing(repair, tmp_path):
    redis, pipe = _redis({"resource": "changed"})
    expected = {"resource": {"kind": "string", "value": "old"}}
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(
            repair["apply_repair"](
                redis,
                expected,
                "resource",
                "new",
                [],
                tmp_path / "backup.json",
                {},
            )
        )
    pipe.set.assert_not_called()
    pipe.execute.assert_not_awaited()


def test_existing_backup_is_never_overwritten(repair, tmp_path):
    redis, pipe = _redis({"resource": "old"})
    backup = tmp_path / "backup.json"
    backup.write_text("original backup")
    with pytest.raises(FileExistsError):
        asyncio.run(
            repair["apply_repair"](
                redis,
                {"resource": {"kind": "string", "value": "old"}},
                "resource",
                "new",
                [],
                backup,
                {},
            )
        )
    assert backup.read_text() == "original backup"
    pipe.execute.assert_not_awaited()


def test_existing_provider_identity_cannot_be_changed(repair, evidence):
    resource, history, baseline, replays = evidence
    resource["latest"]["resource_revision"] = "comment:known"
    history[0]["observation"]["resource_revision"] = "comment:known"
    with pytest.raises(ValueError, match="identity"):
        repair["build_repair"](resource, history, baseline, replays)


def test_baseline_cannot_postdate_original_receipt(repair, evidence):
    resource, history, baseline, replays = evidence
    baseline["revision_order"] = 9_999_999_999_999_999
    with pytest.raises(ValueError, match="receipt"):
        repair["build_repair"](resource, history, baseline, replays)


def test_missing_rejected_order_can_be_enriched(repair, evidence):
    resource, history, baseline, replays = evidence
    replays[0][2]["observation"]["revision_order"] = None
    _, deletions = repair["build_repair"](resource, history, baseline, replays)
    assert deletions == ["delivery-2"]


def test_backup_directory_must_be_synced_before_redis_write(repair, tmp_path, monkeypatch):
    redis, pipe = _redis({"resource": "old"})
    fsync = MagicMock(side_effect=[None, OSError("directory sync failed")])
    monkeypatch.setattr(repair["os"], "fsync", fsync)
    with pytest.raises(OSError, match="directory sync"):
        asyncio.run(
            repair["apply_repair"](
                redis,
                {"resource": {"kind": "string", "value": "old"}},
                "resource",
                "new",
                [],
                tmp_path / "backup.json",
                {},
            )
        )
    pipe.execute.assert_not_awaited()


@pytest.mark.parametrize("same_identity", [False, True])
def test_promoted_baseline_has_an_accepted_delivery_marker(repair, evidence, same_identity):
    resource, history, baseline, replays = evidence
    updated, _ = repair["build_repair"](resource, history, baseline, replays)
    identity = "old-delivery" if same_identity else "new-delivery"
    existing = history[0] if same_identity else None
    promoted, marker = repair["promote_delivery"](updated, history, identity, existing)
    assert promoted["latest_delivery_identity"] == identity
    assert marker["delivery_identity"] == identity
    assert marker["disposition"] == "accepted"
    assert marker["observation"] == promoted["latest"]
    assert marker["observation"]["observation_id"] == "old-observation"
    assert history[0]["observation"]["revision_order"] is None


def test_promoted_identity_cannot_overwrite_another_delivery(repair, evidence):
    resource, history, baseline, replays = evidence
    updated, _ = repair["build_repair"](resource, history, baseline, replays)
    with pytest.raises(ValueError, match="already observed"):
        repair["promote_delivery"](updated, history, "new-delivery", history[0])


def test_ticket_lock_acquired_during_planning_aborts_repair(repair, tmp_path):
    redis, pipe = _redis({"resource": "old", "ticket-lock": "new-worker"})
    expected = {
        "resource": {"kind": "string", "value": "old"},
        "ticket-lock": {"kind": "string", "value": None},
    }
    with pytest.raises(ValueError, match="ticket-lock"):
        asyncio.run(
            repair["apply_repair"](
                redis,
                expected,
                "resource",
                "new",
                [],
                tmp_path / "backup.json",
                {},
            )
        )
    pipe.execute.assert_not_awaited()


def test_post_write_verification_detects_failed_write(repair, tmp_path):
    redis, pipe = _redis({"resource": "old"})
    pipe.execute.side_effect = None
    with pytest.raises(ValueError, match="Post-repair verification"):
        asyncio.run(
            repair["apply_repair"](
                redis,
                {"resource": {"kind": "string", "value": "old"}},
                "resource",
                "new",
                [],
                tmp_path / "backup.json",
                {},
            )
        )
    pipe.execute.assert_awaited_once()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--inspect", "--apply"],
        [],
        [
            "--expected-observation",
            "id",
            "--baseline-payload",
            "baseline.json",
            "--apply",
            "--quiesced",
            "--backup",
            "backup.json",
            "--replay-payload",
            "replay.json",
        ],
    ],
)
def test_cli_rejects_incomplete_or_conflicting_safety_flags(repair, arguments):
    with pytest.raises(SystemExit) as error:
        repair["main"](["--ticket", "BUG-1", *arguments])
    assert error.value.code == 2


def test_cli_reports_safe_failure_without_traceback(repair, monkeypatch, capsys):
    run = AsyncMock(side_effect=ValueError("operator evidence is missing"))
    monkeypatch.setitem(repair["main"].__globals__, "run", run)
    with pytest.raises(SystemExit) as error:
        repair["main"](["--ticket", "BUG-1", "--inspect"])
    assert error.value.code == 1
    assert "Repair refused: operator evidence is missing" in capsys.readouterr().err


def test_script_help_entrypoint_needs_no_forge_or_redis(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["repair_jira_ledger.py", "--help"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            Path(__file__).parents[1] / "scripts" / "repair_jira_ledger.py", run_name="__main__"
        )
    assert error.value.code == 0
    assert "--quiesced" in capsys.readouterr().out
