"""Opt-in repair of one unordered Jira baseline, using the installed Forge adapter.

Run from the Forge checkout with its Python environment and Redis configuration.
See docs/jira-ledger-recovery.md. This script never sends events or edits checkpoints.
"""

import argparse
import asyncio
import copy
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4


ORDERING_REJECTIONS = {
    "unversioned observations cannot be ordered safely",
    "opaque revisions cannot be ordered safely",
}


def _ordered(observation):
    order = observation.get("revision_order")
    revision = observation.get("resource_revision")
    if (
        not isinstance(order, int)
        or isinstance(order, bool)
        or order < 0
        or not isinstance(revision, str)
        or not revision
    ):
        raise ValueError("Evidence must contain a provider revision and numeric revision order")


def _same_resource(left, right):
    if left.get("source_system") != "jira" or right.get("source_system") != "jira":
        raise ValueError("Only Jira resources can be repaired")
    if left.get("resource") != right.get("resource"):
        raise ValueError("Evidence refers to a different resource")


def build_repair(resource, history, baseline, replays, *, baseline_fingerprint=None):
    """Return a metadata-only projection and explicitly eligible marker deletions."""
    latest = resource["latest"]
    if latest.get("revision_order") is not None:
        raise ValueError("Baseline is already ordered; refusing to overwrite it")
    _same_resource(latest, baseline)
    _ordered(baseline)
    original_revision = latest.get("resource_revision")
    if (
        original_revision
        and original_revision != baseline["resource_revision"]
        and not (
            original_revision.startswith("jira-changelog:")
            and baseline_fingerprint == original_revision
        )
    ):
        raise ValueError("Evidence cannot change a known provider revision identity")
    received = datetime.fromisoformat(latest["received_at"].replace("Z", "+00:00"))
    if received.tzinfo is None or baseline["revision_order"] > int(
        received.timestamp() * 1_000_000
    ):
        raise ValueError("Baseline revision cannot postdate its original receipt")
    if baseline.get("facts") != latest.get("facts"):
        raise ValueError("Baseline evidence facts do not match the saved observation")
    accepted = [d for d in history if d.get("disposition") == "accepted"]
    if not any(
        d.get("observation") == latest
        and d.get("delivery_identity") == resource.get("latest_delivery_identity")
        for d in accepted
    ):
        raise ValueError("Current baseline does not match an accepted historical decision")

    repaired = copy.deepcopy(resource)
    repaired["latest"].update(
        resource_revision=baseline["resource_revision"],
        revision_order=baseline["revision_order"],
    )
    previous = baseline
    seen_revisions = {baseline["resource_revision"]}
    deletions = []
    for key, replay, marker in replays:
        _same_resource(latest, replay)
        _ordered(replay)
        if (
            replay["revision_order"] <= previous["revision_order"]
            or replay["resource_revision"] in seen_revisions
        ):
            raise ValueError(
                "Every replay must have a distinct revision strictly newer than the baseline and preceding replay"
            )
        for decision in accepted:
            observation = decision["observation"]
            if observation.get("resource") == latest["resource"] and (
                observation.get("resource_revision") == replay["resource_revision"]
                or observation.get("facts") == replay.get("facts")
            ):
                raise ValueError(
                    "Replay matches already accepted evidence; investigate execution first"
                )
        if marker is not None:
            if (
                marker.get("disposition") != "conflict"
                or marker.get("reason") not in ORDERING_REJECTIONS
            ):
                raise ValueError("Only a delivery rejected for missing ordering may be removed")
            stored = marker["observation"]
            _same_resource(latest, stored)
            if stored.get("facts") != replay.get("facts"):
                raise ValueError("Replay facts differ from the rejected delivery")
            if stored.get("resource_revision") != replay["resource_revision"] or stored.get(
                "revision_order"
            ) not in (None, replay["revision_order"]):
                raise ValueError("Replay revision differs from the rejected delivery")
            deletions.append(key)
        seen_revisions.add(replay["resource_revision"])
        previous = replay
    return repaired, deletions


def promote_delivery(resource, history, delivery_identity, existing):
    """Protect the enriched baseline from being executed again under its new identity."""
    original = next(
        d
        for d in history
        if d.get("disposition") == "accepted"
        and d.get("delivery_identity") == resource["latest_delivery_identity"]
        and d["observation"]["observation_id"] == resource["latest"]["observation_id"]
    )
    if existing is not None and (
        delivery_identity != original["delivery_identity"] or existing != original
    ):
        raise ValueError(
            "Promoted baseline identity was already observed; investigate before repair"
        )
    promoted = copy.deepcopy(original)
    promoted.update(
        observation=copy.deepcopy(resource["latest"]),
        delivery_identity=delivery_identity,
        reason="operator enriched metadata for an already accepted baseline; no command executed",
        decided_at=datetime.now(UTC).isoformat(),
    )
    resource = copy.deepcopy(resource)
    resource["latest_delivery_identity"] = delivery_identity
    return resource, promoted


async def _read(redis, key, kind):
    return await redis.lrange(key, 0, -1) if kind == "list" else await redis.get(key)


async def apply_repair(
    redis,
    expected,
    resource_key,
    replacement,
    deletions,
    backup,
    audit,
    *,
    marker_updates=None,
):
    """Back up exclusively, compare all observed keys, then mutate in one transaction."""
    backup = Path(backup)
    descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"before": expected, "audit": audit}, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(backup.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    marker_updates = marker_updates or {}
    async with redis.pipeline(transaction=True) as pipeline:
        await pipeline.watch(*expected)
        for key, snapshot in expected.items():
            if await _read(pipeline, key, snapshot["kind"]) != snapshot["value"]:
                raise ValueError(f"Ledger changed during repair at {key}; nothing applied")
        pipeline.multi()
        pipeline.set(resource_key, replacement)
        for key, value in marker_updates.items():
            pipeline.set(key, value)
        for key in deletions:
            pipeline.delete(key)
        await pipeline.execute()
    writes = {resource_key: replacement, **marker_updates, **dict.fromkeys(deletions)}
    if any([await redis.get(key) != value for key, value in writes.items()]):
        raise ValueError("Post-repair verification failed; keep ingress paused and inspect backup")


def load_forge():
    # Deliberately import the operator's installed Forge, not a copied ledger implementation.
    from forge.orchestrator.checkpointer import get_redis_client
    from forge.orchestrator.event_adapters.jira import JiraEventAdapter, _jira_revision_order
    from forge.reconciliation.ledger import (
        _DELIVERY_PREFIX,
        _RUN_HISTORY_PREFIX,
        RedisObservationLedger,
        classify_observation,
    )

    return SimpleNamespace(
        connect=get_redis_client,
        adapter=JiraEventAdapter(),
        order=_jira_revision_order,
        ledger=RedisObservationLedger,
        classify=classify_observation,
        delivery_prefix=_DELIVERY_PREFIX,
        run_prefix=_RUN_HISTORY_PREFIX,
    )


def adapt_payload(forge, path, current):
    with Path(path).open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if payload.get("issue", {}).get("key") != current.resource.external_id:
        raise ValueError("Payload issue key differs from the selected ticket")
    event_type = payload.get("webhookEvent")
    if event_type not in {"comment_created", "jira:issue_updated"}:
        raise ValueError("Evidence must be a comment_created or jira:issue_updated payload")
    message = SimpleNamespace(
        event_id=f"ledger-recovery-{uuid4()}",
        event_type=event_type,
        ticket_key=current.resource.external_id,
        payload=payload,
        timestamp=current.received_at,
        source="jira",
        normalized_event=None,
    )
    observation = forge.adapter.adapt(message).observation
    if event_type == "comment_created":
        comment = payload.get("comment", {})
        # The live adapter prioritizes issue.updated. Do not let a current issue
        # snapshot silently substitute for the original comment's creation time.
        created_order = forge.order({"comment": {"created": comment.get("created")}})
        if (
            not comment.get("id")
            or created_order is None
            or observation.revision_order != created_order
        ):
            raise ValueError(
                "Comment evidence needs id and created, with no conflicting issue.updated"
            )
    original_payload = copy.deepcopy(payload)
    original_payload["issue"].get("fields", {}).pop("updated", None)
    message.payload = original_payload
    fingerprint = forge.adapter.adapt(message).observation.resource_revision
    return observation, message.event_id, fingerprint


async def run(args):
    forge = load_forge()
    redis = await forge.connect()
    try:
        ledger = forge.ledger(redis)
        history = list(await ledger.history_for_run(args.ticket))
        seeds = [
            d.observation
            for d in history
            if d.observation.source_system == "jira"
            and d.observation.resource.resource_type == "issue"
            and d.observation.resource.external_id == args.ticket
            and d.observation.resource.namespace is None
        ]
        current = await ledger.latest(seeds[-1]) if seeds else None
        if current is None:
            raise ValueError("No Jira issue baseline found for this ticket")
        resource_key = ledger._resource_key(current.latest)
        if args.inspect:
            print(
                json.dumps(
                    {
                        "ticket": args.ticket,
                        "resource_key": resource_key,
                        "current": current.model_dump(mode="json"),
                    },
                    indent=2,
                )
            )
            return
        if current.latest.observation_id != args.expected_observation:
            raise ValueError("Current observation differs from --expected-observation")
        lock_key = f"forge:queue:ticket-lock:{args.ticket}"
        if await redis.get(lock_key):
            raise ValueError("Ticket is being processed; quiesce workers before repair")

        history_key = ledger._history_key(current.latest)
        run_key = f"{forge.run_prefix}{args.ticket}"
        expected = {lock_key: {"kind": "string", "value": None}}
        for key, kind in ((resource_key, "string"), (history_key, "list"), (run_key, "list")):
            expected[key] = {"kind": kind, "value": await _read(redis, key, kind)}
        if json.loads(expected[resource_key]["value"]) != current.model_dump(mode="json"):
            raise ValueError("Baseline changed while preparing repair; inspect again")
        # Plan from the same history snapshot that the apply transaction will watch.
        history_data = [json.loads(value) for value in expected[history_key]["value"]]
        for decision in history_data:
            key = forge.delivery_prefix + decision["delivery_identity"]
            expected[key] = {"kind": "string", "value": await redis.get(key)}

        baseline, _, fingerprint = adapt_payload(forge, args.baseline_payload, current.latest)
        replays = []
        replay_report = []
        previous = baseline
        for path in args.replay_payload:
            observation, recovery_id, _ = adapt_payload(forge, path, current.latest)
            key = forge.delivery_prefix + observation.delivery_identity
            raw = await redis.get(key)
            expected[key] = {"kind": "string", "value": raw}
            replays.append(
                (key, observation.model_dump(mode="json"), json.loads(raw) if raw else None)
            )
            decision, _, reason = forge.classify(previous, observation)
            if decision.value != "accepted":
                raise ValueError(f"Installed Forge would reject replay: {reason}")
            replay_report.append(
                {
                    "payload": str(Path(path).resolve()),
                    "recovery_delivery_id": recovery_id,
                    "revision": observation.resource_revision,
                }
            )
            previous = observation
        repaired, deletions = build_repair(
            current.model_dump(mode="json"),
            history_data,
            baseline.model_dump(mode="json"),
            replays,
            baseline_fingerprint=fingerprint,
        )
        baseline_key = forge.delivery_prefix + baseline.delivery_identity
        baseline_raw = await redis.get(baseline_key)
        expected[baseline_key] = {"kind": "string", "value": baseline_raw}
        repaired, acceptance_marker = promote_delivery(
            repaired,
            history_data,
            baseline.delivery_identity,
            json.loads(baseline_raw) if baseline_raw else None,
        )
        report = {
            "ticket": args.ticket,
            "resource_key": resource_key,
            "before_revision": current.latest.resource_revision,
            "after_revision": baseline.resource_revision,
            "after_order": baseline.revision_order,
            "baseline_acceptance_marker": baseline_key,
            "delete_rejected_deliveries": deletions,
            "replays_to_send_manually": replay_report,
            "prepared_at": datetime.now(UTC).isoformat(),
            "applied": False,
        }
        if args.apply:
            await apply_repair(
                redis,
                expected,
                resource_key,
                json.dumps(repaired),
                deletions,
                args.backup,
                report,
                marker_updates={baseline_key: json.dumps(acceptance_marker)},
            )
            report["applied"] = True
            report["backup"] = str(args.backup.resolve())
        print(json.dumps(report, indent=2))
    finally:
        await redis.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--inspect", action="store_true", help="Print the baseline; no writes")
    parser.add_argument("--expected-observation", help="Exact observation_id from --inspect")
    parser.add_argument(
        "--baseline-payload", type=Path, help="Verified original event plus revision metadata"
    )
    parser.add_argument("--replay-payload", type=Path, action="append", default=[])
    parser.add_argument("--backup", type=Path, help="New backup file, required for --apply")
    parser.add_argument("--apply", action="store_true", help="Otherwise only print the repair plan")
    parser.add_argument(
        "--quiesced", action="store_true", help="Confirm ingress/workers are paused"
    )
    parser.add_argument(
        "--replays-reviewed", action="store_true", help="Confirm replay commands never executed"
    )
    args = parser.parse_args(argv)
    if args.inspect and (args.apply or args.baseline_payload or args.replay_payload):
        parser.error("--inspect cannot be combined with repair options")
    if not args.inspect and (not args.baseline_payload or not args.expected_observation):
        parser.error("Repair requires --baseline-payload and --expected-observation")
    if args.apply and (not args.backup or not args.quiesced):
        parser.error("--apply requires --backup and --quiesced")
    if args.apply and args.replay_payload and not args.replays_reviewed:
        parser.error("Applying replay marker changes requires --replays-reviewed")
    try:
        asyncio.run(run(args))
    except (ValueError, OSError, ImportError) as exc:
        parser.exit(1, f"Repair refused: {exc}\n")


if __name__ == "__main__":
    main()
