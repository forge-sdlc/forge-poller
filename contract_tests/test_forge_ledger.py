"""Run explicitly in Forge's environment; these tests never contact live services."""

import asyncio
import copy
import json
import runpy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forge.orchestrator.event_adapters.jira import JiraEventAdapter
from forge.reconciliation.ledger import InMemoryObservationLedger, RedisObservationLedger
from forge.reconciliation.models import ObservationDecision, ReconciledResource
from poller import payloads
from poller.watcher import _extract_comment_body

ROOT = Path(__file__).parents[1]
TIMESTAMP = "2026-09-17T08:00:02.000+0000"


def observe(payload):
    return (
        JiraEventAdapter()
        .adapt(
            SimpleNamespace(
                source="jira",
                event_id="transport-id",
                event_type=payload["webhookEvent"],
                ticket_key="BUG-1",
                payload=payload,
                timestamp=datetime(2026, 9, 17, 12, tzinfo=UTC),
                normalized_event=None,
            )
        )
        .observation
    )


def comment(comment_id="2", created=TIMESTAMP, body="! revise"):
    return payloads.comment_created(
        "BUG-1",
        "Bug",
        "Open",
        "s",
        {"forge:managed"},
        body,
        "human",
        "Human",
        comment_id=comment_id,
        created=created,
    )


def label(updated):
    return payloads.label_changed(
        "BUG-1",
        "Bug",
        "Open",
        "s",
        {"forge:managed"},
        {"forge:managed", "forge:retry"},
        updated=updated,
    )


def dispositions(*events):
    async def run():
        ledger = InMemoryObservationLedger()
        return [(await ledger.record(observe(event))).disposition.value for event in events]

    return asyncio.run(run())


def test_forwarded_recovered_comments_and_newer_labels_are_accepted(monkeypatch):
    polling = runpy.run_path(ROOT / "tests" / "test_jira_comment_polling.py")
    polling["_reset_settings"](monkeypatch)
    monkeypatch.setenv("POLLER_STATE_FILE", "")
    watcher = polling["_watcher"]()
    make = polling["_comment"]
    issue = polling["_issue"]([make("3", "! third", "human")], labels=["forge:retry"])
    forwarded = polling["_poll_jira"](
        monkeypatch,
        watcher,
        issue,
        comments=[
            make("1", "old", "human"),
            make("2", "! second", "human"),
            make("3", "! third", "human"),
        ],
    )
    events = [call.args[0] for call in forwarded.await_args_list]
    assert len(events) == 3
    assert dispositions(*events) == ["accepted", "accepted", "accepted"]


def test_equal_time_label_deferral_avoids_ledger_conflict(monkeypatch):
    polling = runpy.run_path(ROOT / "tests" / "test_jira_comment_polling.py")
    polling["_reset_settings"](monkeypatch)
    monkeypatch.setenv("POLLER_STATE_FILE", "")
    watcher = polling["_watcher"]()
    make = polling["_comment"]
    comments = [make("1", "old", "human"), make("2", "! revised", "human")]
    issue = polling["_issue"](comments, labels=["forge:retry"], updated=TIMESTAMP)
    forwarded = polling["_poll_jira"](monkeypatch, watcher, issue)
    events = [call.args[0] for call in forwarded.await_args_list]
    assert len(events) == 1
    assert dispositions(*events) == ["accepted"]
    assert watcher._state["BUG-1"].labels == {"forge:managed"}


# Characterization tests: passing means the documented Forge limitation still exists.
def test_current_forge_conflicts_on_equal_time_issue_and_comment():
    assert dispositions(comment(), label(TIMESTAMP)) == ["accepted", "conflict"]


def test_current_forge_conflicts_on_same_time_distinct_comments():
    assert dispositions(comment("2"), comment("3")) == ["accepted", "conflict"]


def test_current_forge_conflicts_on_raw_timestamp_aliases():
    assert dispositions(label(TIMESTAMP), label("2026-09-17T11:00:02.000+0300")) == [
        "accepted",
        "conflict",
    ]


def test_current_forge_conflicts_on_cross_ingress_comment_order():
    webhook = comment()
    webhook["issue"]["fields"]["updated"] = "2026-09-17T08:00:03.000+0000"
    assert dispositions(webhook, comment()) == ["accepted", "conflict"]


def test_current_forge_conflicts_on_adf_normalization_between_ingress_sources():
    adf = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "! revise"},
                    {"type": "text", "text": " now"},
                ],
            }
        ],
    }
    webhook = comment()
    webhook["comment"]["body"] = adf
    assert dispositions(webhook, comment(body=_extract_comment_body(adf))) == [
        "accepted",
        "conflict",
    ]


def test_current_forge_rejects_ordered_event_after_unversioned_baseline():
    legacy = payloads.comment_created(
        "BUG-1",
        "Bug",
        "Open",
        "s",
        {"forge:managed"},
        "! original",
        "human",
        "Human",
    )
    assert dispositions(legacy, comment()) == ["accepted", "conflict"]


@pytest.mark.parametrize(
    "scenario",
    [
        "dry-run",
        "apply",
        "inspect",
        "no-history",
        "wrong-observation",
        "locked",
        "changed-baseline",
        "wrong-ticket",
        "unsupported-event",
        "conflicting-created",
        "replay-order",
        "changelog-apply",
        "changelog-mismatch",
        "known-comment-id",
    ],
)
def test_operator_script_against_actual_forge_models_and_redis_ledger(
    monkeypatch, tmp_path, capsys, scenario
):
    apply = scenario != "dry-run"
    repair = runpy.run_path(ROOT / "scripts" / "repair_jira_ledger.py")
    helpers = runpy.run_path(ROOT / "tests" / "test_ledger_repair.py")
    legacy = payloads.comment_created(
        "BUG-1",
        "Bug",
        "Open",
        "s",
        {"forge:managed"},
        "! original",
        "human",
        "Human",
    )
    if scenario.startswith("changelog-"):
        legacy = payloads.label_changed("BUG-1", "Bug", "Open", "s", set(), {"forge:managed"})
    elif scenario == "known-comment-id":
        legacy["comment"]["id"] = "1"

    async def recorded():
        ledger = InMemoryObservationLedger()
        baseline = await ledger.record(observe(legacy))
        rejected = await ledger.record(observe(comment()))
        return baseline, rejected, await ledger.latest(baseline.observation)

    baseline, rejected, current = asyncio.run(recorded())
    history = [baseline.model_dump_json(), rejected.model_dump_json()]
    resource_key = RedisObservationLedger._resource_key(baseline.observation)
    history_key = RedisObservationLedger._history_key(baseline.observation)
    values = {
        resource_key: current.model_dump_json(),
        history_key: history,
        "forge:observations:run:BUG-1": history,
        "forge:observations:delivery:" + baseline.delivery_identity: baseline.model_dump_json(),
        "forge:observations:delivery:" + rejected.delivery_identity: rejected.model_dump_json(),
    }
    before = copy.deepcopy(values)
    redis, pipeline = helpers["_redis"](values)
    redis.aclose = AsyncMock()
    forge = repair["load_forge"]()
    forge.connect = AsyncMock(return_value=redis)
    monkeypatch.setitem(repair["run"].__globals__, "load_forge", lambda: forge)
    original = tmp_path / "baseline.json"
    original.write_text(json.dumps(comment("1", "2026-09-17T08:00:01.000+0000", "! original")))
    if scenario.startswith("changelog-"):
        original_payload = copy.deepcopy(legacy)
        original_payload["issue"]["fields"]["updated"] = "2026-09-17T08:00:01.000+0000"
        original.write_text(json.dumps(original_payload))
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps(comment()))
    backup = tmp_path / "backup.json"
    args = SimpleNamespace(
        ticket="BUG-1",
        inspect=False,
        expected_observation=current.latest.observation_id,
        baseline_payload=original,
        replay_payload=[replay],
        apply=apply,
        backup=backup,
    )
    expected_error = None
    if scenario == "inspect":
        args.inspect = True
        asyncio.run(repair["run"](args))
        assert json.loads(capsys.readouterr().out)["current"] == current.model_dump(mode="json")
        assert values == before
        redis.aclose.assert_awaited_once()
        return
    if scenario == "no-history":
        values["forge:observations:run:BUG-1"] = []
        expected_error = "No Jira issue baseline"
    elif scenario == "wrong-observation":
        args.expected_observation = "another-observation"
        expected_error = "expected-observation"
    elif scenario == "locked":
        values["forge:queue:ticket-lock:BUG-1"] = "active-worker"
        expected_error = "quiesce workers"
    elif scenario == "changed-baseline":
        original_get = redis.get.side_effect
        reads = 0

        def changing_get(key):
            nonlocal reads
            if key == resource_key:
                reads += 1
                if reads == 2:
                    return "{}"
            return original_get(key)

        redis.get.side_effect = changing_get
        expected_error = "Baseline changed"
    elif scenario in {"wrong-ticket", "unsupported-event", "conflicting-created"}:
        payload = json.loads(original.read_text())
        if scenario == "wrong-ticket":
            payload["issue"]["key"] = "BUG-2"
            expected_error = "issue key"
        elif scenario == "unsupported-event":
            payload["webhookEvent"] = "comment_deleted"
            expected_error = "Evidence must be"
        else:
            payload["issue"]["fields"]["updated"] = TIMESTAMP
            expected_error = "conflicting issue.updated"
        original.write_text(json.dumps(payload))
    elif scenario == "replay-order":
        replay.write_text(json.dumps(comment("2", "2026-09-17T08:00:00.000+0000")))
        expected_error = "Installed Forge would reject"
    elif scenario == "changelog-mismatch":
        changed = json.loads(original.read_text())
        changed["changelog"]["items"][0]["fromString"] = "different-label"
        assert observe(changed).facts == baseline.observation.facts
        original.write_text(json.dumps(changed))
        expected_error = "known provider revision identity"
    if expected_error:
        before = copy.deepcopy(values)
        with pytest.raises(ValueError, match=expected_error):
            asyncio.run(repair["run"](args))
        assert values == before
        assert not backup.exists()
        pipeline.execute.assert_not_awaited()
        redis.aclose.assert_awaited_once()
        return
    asyncio.run(repair["run"](args))
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is apply
    if apply:
        assert (
            json.loads(values[resource_key])["latest"]["resource_revision"]
            == observe(json.loads(original.read_text())).resource_revision
        )
        assert "forge:observations:delivery:" + rejected.delivery_identity not in values
        assert values[history_key] == history
        assert backup.exists()
        pipeline.execute.assert_awaited_once()
        enriched = ReconciledResource.model_validate_json(values[resource_key])
        marker = ObservationDecision.model_validate_json(
            values[report["baseline_acceptance_marker"]]
        )
        assert enriched.latest_delivery_identity == marker.delivery_identity
        assert marker.disposition.value == "accepted"
        assert marker.observation == enriched.latest
        original_key = "forge:observations:delivery:" + baseline.delivery_identity
        if original_key != report["baseline_acceptance_marker"]:
            assert values[original_key] == before[original_key]
        else:
            assert marker.observation.revision_order is not None

        async def replay_after_repair():
            ledger = InMemoryObservationLedger()
            from forge.reconciliation.ledger import resource_identity

            ledger._resources[resource_identity(enriched.latest)] = enriched
            ledger._deliveries[marker.delivery_identity] = marker
            assert (await ledger.record(observe(comment()))).disposition.value == "accepted"
            delayed_baseline = json.loads(original.read_text())
            assert (await ledger.record(observe(delayed_baseline))).disposition.value == "duplicate"
            if delayed_baseline["webhookEvent"] == "comment_created":
                delayed_baseline["issue"]["fields"]["updated"] = "2026-09-17T09:00:00.000+0000"
                assert (
                    await ledger.record(observe(delayed_baseline))
                ).disposition.value == "conflict"

        asyncio.run(replay_after_repair())
    else:
        assert values == before
        assert not backup.exists()
        pipeline.execute.assert_not_awaited()
    redis.aclose.assert_awaited_once()


def test_operator_script_apply_requires_explicit_safety_flags():
    repair = runpy.run_path(ROOT / "scripts" / "repair_jira_ledger.py")
    with pytest.raises(SystemExit) as exc:
        repair["main"](
            [
                "--ticket",
                "BUG-1",
                "--expected-observation",
                "id",
                "--baseline-payload",
                "baseline.json",
                "--apply",
            ]
        )
    assert exc.value.code == 2
