"""Checks for the provider identity fields Forge normalizes from poller payloads."""

from poller import forwarder, payloads


def test_synthetic_github_review_preserves_provider_review_id() -> None:
    payload = payloads.pr_review_submitted(
        repo="acme/api",
        branch="feature",
        pr_number=42,
        pr_title="Change",
        pr_url="https://github.com/acme/api/pull/42",
        review_state="APPROVED",
        review_body="Looks good",
        reviewer_login="octocat",
        review_id=1234,
        head_sha="abc123",
        base_branch="main",
    )

    assert payload["review"]["id"] == 1234
    assert payload["review"]["user"]["login"] == "octocat"
    assert payload["pull_request"]["head"]["sha"] == "abc123"
    assert payload["pull_request"]["base"]["ref"] == "main"
    assert forwarder.github_delivery_id("pull_request_review", "acme/api", 42, 1234)


def test_synthetic_github_comment_preserves_provider_comment_id() -> None:
    payload = payloads.issue_comment(
        repo="acme/api",
        pr_number=42,
        comment_body="Please update this",
        sender_login="octocat",
        comment_id=5678,
        issue_title="Change",
        issue_url="https://github.com/acme/api/pull/42",
    )

    assert payload["comment"]["id"] == 5678
    assert payload["issue"]["pull_request"]["html_url"].endswith("/42")


def test_synthetic_github_merge_preserves_provider_head_revision() -> None:
    payload = payloads.pr_merged(
        repo="acme/api",
        branch="feature",
        pr_number=42,
        pr_title="Change",
        pr_url="https://github.com/acme/api/pull/42",
        head_sha="abc123",
        base_branch="main",
    )

    assert payload["pull_request"]["head"]["sha"] == "abc123"
    assert payload["pull_request"]["base"]["ref"] == "main"


def test_provider_delivery_ids_are_replay_stable_but_event_specific() -> None:
    first = forwarder.github_delivery_id("issue_comment", "acme/api", 42, 5678)
    replay = forwarder.github_delivery_id("issue_comment", "acme/api", 42, 5678)
    other = forwarder.github_delivery_id("issue_comment", "acme/api", 42, 5679)

    assert first == replay
    assert first != other


def test_synthetic_jira_comment_preserves_provider_comment_identity() -> None:
    payload = payloads.comment_created(
        ticket_key="FORGE-17",
        issue_type="Bug",
        status="In Progress",
        summary="A bug",
        labels={"forge:managed"},
        body="Please investigate",
        author_account_id="alice",
        author_display_name="Alice",
        comment_id="10042",
        issue_updated="2026-08-27T10:01:00.000+0000",
        comment_created="2026-08-27T10:00:59.000+0000",
    )

    assert payload["comment"]["id"] == "10042"
    assert payload["issue"]["fields"]["updated"].startswith("2026-08-27")
    assert forwarder.jira_delivery_id(payload) == forwarder.jira_delivery_id(payload)


def test_synthetic_jira_label_revision_is_replay_stable() -> None:
    payload = payloads.label_changed(
        ticket_key="FORGE-17",
        issue_type="Bug",
        status="In Progress",
        summary="A bug",
        old_labels={"forge:managed"},
        new_labels={"forge:managed", "forge:approved"},
        updated="2026-08-27T10:02:00.000+0000",
    )

    assert payload["issue"]["fields"]["updated"] == "2026-08-27T10:02:00.000+0000"
    assert forwarder.jira_delivery_id(payload) == forwarder.jira_delivery_id(payload)
