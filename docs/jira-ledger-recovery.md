# Jira revision delivery and legacy-ledger recovery

## What the poller guarantees

The poller forwards the original Jira comment ID and creation timestamp, plus
the edit timestamp when present. Label snapshots and registration events carry
the issue's `updated` timestamp. Polling rejects missing, malformed, or
timezone-free revision metadata before forwarding the affected Jira batch.
Timestamp strings are preserved: changing their spelling changes the revision
token in current Forge. The old payload helper signatures remain compatible;
only legacy callers that omit all revision arguments can build unversioned
payloads. The watcher never intentionally uses that compatibility path.

Recovered comments are validated and sent chronologically before the current
label snapshot. Incomplete pagination, a missing cursor, duplicate IDs, or
ambiguous comment ordering prevent the cursor and labels from advancing. A
truncated embedded page is paginated even when it contains the saved cursor.
Pagination also requires an unchanged issue revision before and after fetching
the pages; a stable comment count alone cannot detect concurrent replacement.

Each outgoing Jira event is saved as `pending_jira_delivery` before sending, with
its exact payload and transport delivery ID. Each gateway acknowledgement saves
the cursor or labels independently, so a later batch failure does not replay
already acknowledged comments. An HTTP error, timeout, or missing acknowledgement
retains the pending event. The next poll retries it before reading fresh Jira
facts, using the same payload and delivery ID, including after a process restart
when `POLLER_STATE_FILE` is configured. Failed registration delivery is also
retained and scheduled for retry. Delivery remains at least once: an ambiguous
timeout or a crash before saving an acknowledgement can repeat a request.
Without a state file, these guarantees do not survive restarting the process.
Do not downgrade while an event is pending: older pollers ignore this field.
State files are atomically replaced with owner-only permissions; both the file
and its directory are synced before sending. They contain pending comment text,
so protect their directory and backups too. A failed state write prevents sending.
An initial registration write failure rolls back the in-memory registration, so
registration can be retried after storage recovers.

Known gateway policy skips (`missing forge:managed label`, `self-comment`, and
`Sub-task must have forge:parent label`) are terminal for that frozen event, not
successful queueing. They are logged with their reason and clear the pending
event, advancing its cursor/labels so later Jira changes can still be read.
Skipped comments are not replayed automatically if the labels later change.
Unknown skip reasons remain pending and require investigation.

Current Forge treats comments and label snapshots as revisions of the same issue.
If a label timestamp is equal to or older than the latest observed cursor
comment, the label event is deferred and the old labels are retained. A later
poll may send it when a genuinely newer issue revision exists. Distinct comments
at the same timestamp defer the entire new comment batch and its labels. These
conditions produce explicit warnings, not fabricated timestamps or silent
cursor advancement. They can require the combined Forge/poller follow-up;
do not manufacture a Jira update just to get past a collision.

`TicketState.updated` is the last fetched issue snapshot timestamp, not proof of
delivery or a comment watermark. It is optional for older state files. The
poller reads the live issue type for routing and payloads; this does not change
an existing Forge workflow's pinned definition.

Logs distinguish forwarding, gateway queue acknowledgement, and cursor movement.
Only Forge's execution timeline and worker logs can establish that an observation
was accepted and its command handled. A `queued` response is not that proof.

## When legacy repair is applicable

There is no database schema upgrade. However, an issue already recorded without
`revision_order` can remain blocked after upgrading the poller: Forge cannot
compare the new versioned event with its old baseline. Typical worker reasons are
`unversioned observations cannot be ordered safely` and
`opaque revisions cannot be ordered safely`.

This is distinct from an ordinary workflow failure carrying `forge:blocked`.
Adding `forge:retry` does not repair the observation ledger; that event passes
through the same ledger before a workflow command is considered.

The optional `scripts/repair_jira_ledger.py` operates on one ticket. It imports
the installed Forge adapter and ledger from the **Forge Python environment**;
the normal poller does not gain a Redis dependency or access to Forge storage.
The tool supports an unordered current baseline whose accepted historical event
can be identified exactly. If that evidence cannot be established, stop and
ask the maintainer to investigate. A current issue snapshot cannot substitute
for the original accepted event.

## Operator procedure

### 1. Prepare and pause

Use a maintenance window approved by the deployment owner. Stop the poller and
other event producers for this ticket, account for queued events and retry
entries, let active processing settle, and pause workers before editing Redis.
For a shared deployment without ticket-scoped controls, coordinate a service
maintenance window. Preserve pending messages; do not flush queues.

Inspect the ticket's execution timeline, checkpoint, and effect records to
identify which commands actually executed. Take the normal Redis backup as
well as the tool's targeted backup. Keep the upgraded poller stopped until
recovery is verified. Do not unregister/re-register the ticket as a substitute
for pausing: registration changes its polling baseline.

Run the following from the Forge checkout with the same configuration as the
affected worker. Replace the checkout path and example ticket with your values:

```bash
POLLER_CHECKOUT=/path/to/forge-poller
install -d -m 700 .tmp/ledger-recovery
uv run python "$POLLER_CHECKOUT/scripts/repair_jira_ledger.py" \
  --ticket BUG-1 --inspect
```

Inspect mode is read-only. Record the current `latest.observation_id`, facts,
resource revision, and ordering metadata. The output may contain ticket text;
treat it and subsequent backups as sensitive local artifacts.

### 2. Reconstruct verified evidence

Prepare `baseline.json` from the original accepted ingress payload. Restore only
the missing provider metadata using the original Jira comment or change history.
The issue key, issue type, status, labels, event type, and normalized comment text
must match the saved observation's facts. Preserve the poller's original body
representation; substituting raw ADF can change those facts.

For a comment, verify its real `id` and timezone-qualified `created`, and retain
`updated` when available. Do not put today's `issue.fields.updated` into this
historical comment payload. The tool rejects comment evidence whose selected
ordering differs from its creation timestamp, evidence that changes an already
known provider identity, and baseline timestamps later than the original receipt.
For a saved `jira-changelog:` token, the original changelog items must reproduce
that exact fingerprint through the installed Forge adapter before enrichment;
matching canonical issue facts alone is not sufficient.
These checks cannot prove the identity of a completely unversioned event; the
operator must establish it from the original ingress and Jira evidence.
For a historical label event,
establish the actual issue revision timestamp from the corresponding change;
if that cannot be proven, this tool cannot safely repair it.

Prepare one payload file per missed event that should still be handled. Use
original provider identities, timestamps, and facts. Review these events against
the current workflow position: do not blindly replay every informational comment
or superseded command. List selected payloads in strictly increasing provider
timestamp order, after the baseline. Equal-time or previously accepted events
are refused. Identical facts matching an earlier accepted event are also refused
as ambiguous, even if the new comment ID is different.

### 3. Review a dry run

```bash
uv run python "$POLLER_CHECKOUT/scripts/repair_jira_ledger.py" \
  --ticket BUG-1 \
  --expected-observation 'observation:REPLACE_WITH_INSPECTED_ID' \
  --baseline-payload .tmp/ledger-recovery/baseline.json \
  --replay-payload .tmp/ledger-recovery/missed-comment.json
```

Repeat `--replay-payload` for multiple selected events. It may be omitted when
only the baseline needs repair. The dry run performs no Redis writes and creates
no backup file. It validates the baseline against the accepted history, checks
replay ordering with the installed Forge classifier, and reports the proposed
baseline, its promoted acceptance marker, and any rejected delivery markers
that must be released.

Only markers for the explicitly supplied replay revisions, rejected specifically
for missing ordering, are eligible. Accepted, stale, differently shaped, or
policy-conflicting markers are refused. A rejected marker may lack an order if
the corrected evidence preserves its provider identity and facts. Historical
decisions are never deleted.
The tool cannot prove from Jira content alone that an old command never executed;
the operator's checkpoint/effect review in step 1 remains necessary.

### 4. Apply the reviewed repair

```bash
uv run python "$POLLER_CHECKOUT/scripts/repair_jira_ledger.py" \
  --ticket BUG-1 \
  --expected-observation 'observation:REPLACE_WITH_INSPECTED_ID' \
  --baseline-payload .tmp/ledger-recovery/baseline.json \
  --replay-payload .tmp/ledger-recovery/missed-comment.json \
  --backup .tmp/ledger-recovery/BUG-1-before-repair.json \
  --apply --quiesced --replays-reviewed
```

The flags confirm that producers/workers are paused and the selected replay
commands were reviewed. They are not automatic pause controls. The tool also
refuses an active ticket lock.

Before modifying Redis, the tool exclusively creates a mode-0600 backup and
flushes both the file and its parent directory to disk. An existing backup path
is never overwritten. The backup
contains raw resource/delivery values, ledger histories, the proposed repair,
and recovery delivery IDs. A compare-and-set transaction checks the captured
resource, histories, delivery keys, and absence of a ticket lock before updating
the current projection, protecting the baseline's promoted delivery identity,
and deleting the selected rejected markers together. Concurrent changes abort
the transaction. The result is read back and verified.

Only the current observation's revision token and order are enriched; its facts
and original observation ID remain intact. The projection references an accepted
marker for the enriched identity so that a delayed baseline delivery cannot run
the old command again. The original marker is retained when the identity changes;
when it stays the same, its ordering metadata is enriched too. A different
pre-existing marker at the promoted identity is refused. Historical records retain
their original form as audit evidence. Workflow
checkpoints, effects, queue entries, gateway deduplication keys, and poller state
are not modified. The script never publishes a replay or calls Jira.

### 5. Replay and verify

Keep ordinary ingress paused. Start the gateway and worker under operator
control, then submit each reviewed payload using the recovery delivery ID from
the **apply output or backup**, in the displayed order:

```bash
curl --fail-with-body http://localhost:8000/api/v1/webhooks/jira \
  -H 'Content-Type: application/json' \
  -H 'X-Atlassian-Webhook-Identifier: ledger-recovery-ID_FROM_APPLY_OUTPUT' \
  --data-binary @.tmp/ledger-recovery/missed-comment.json
```

Use the deployment's normal authentication/signing requirements. The example is
for the local unsigned gateway already used by the development poller.

For each event, verify the gateway returns `queued`, the ledger records
`accepted`, and the intended command is handled at the current workflow position.
Wait for the result before submitting the next event. Investigate a duplicate,
conflict, stale decision, or separate workflow error before continuing; do not
keep generating new delivery IDs to force the event through. An HTTP retry of
the same recovery operation should reuse its recorded delivery ID.

Resume the poller only after verifying recovery. Its previously advanced cursor
will not automatically resend missed events, which is why explicit replay is
part of this procedure. If its cursor is behind, already accepted provider
revisions may be redelivered and deduplicated normally. Investigate changed facts
or ordering conflicts rather than resetting the cursor blindly.

Before any replay/processing, a failed repair can be investigated using the raw
backup values and restored in a controlled maintenance window. After processing
resumes, restoring an old ledger snapshot can permit duplicate external writes;
do not treat it as a general workflow rollback. This script has no automatic
rollback or global reset mode.

## Remaining combined Forge/poller work

- Jira comment events and issue snapshots currently compete for one issue
  revision sequence. Equal-time distinct events and previously unseen backlogs
  older than an accepted revision need a consumer-side design.
- Webhooks may order a comment using `issue.updated`, while the poller uses
  `comment.created`. The same comment can therefore produce conflicting orders.
- Timestamp spellings and ADF flattening differ across ingress paths. Unifying
  them only in the poller could conflict with existing or webhook evidence.
- Failed observation delivery markers require explicit recovery today. A
  supported Forge recovery API should distinguish rejected from executed work.
- Registration observes current state; it is not a command to restart an
  already completed workflow. Comment edits and historical issue-type changes
  also need an explicit shared event contract.

These limitations are exercised as characterization tests in `contract_tests/`.
Those tests pass by asserting the current conflicts; they do not claim the
combined fix is implemented. The operator script addresses only the unordered
baseline case, not arbitrary conflicts from this list.

## Verification

In the poller checkout:

```bash
uv run pytest
```

Run consumer contract tests using a compatible Forge checkout's environment,
with the poller source importable, from the poller checkout:

```bash
FORGE_CHECKOUT=/path/to/forge
PYTHONPATH="$PWD/src" "$FORGE_CHECKOUT/.venv/bin/python" -m pytest contract_tests -q
```

The contract tests use actual Forge adapters/models/ledger logic with in-memory
storage and simulated Redis. They never contact live Jira, Redis, or the gateway.
If the installed Forge API changes, revalidate the tool before using it.
