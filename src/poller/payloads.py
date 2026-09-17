from typing import Any

from poller.jira_revisions import revision_time


def label_changed(
    ticket_key: str,
    issue_type: str,
    status: str,
    summary: str,
    old_labels: set[str],
    new_labels: set[str],
    *,
    updated: str | None = None,
) -> dict[str, Any]:
    issue_fields: dict[str, Any] = {
        "issuetype": {"name": issue_type},
        "status": {"name": status},
        "summary": summary,
        "labels": sorted(new_labels),
    }
    if updated is not None:
        revision_time(updated, "issue.updated")
        issue_fields["updated"] = updated

    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": ticket_key,
            "fields": issue_fields,
        },
        "changelog": {
            "items": [
                {
                    "field": "labels",
                    "fromString": ", ".join(sorted(old_labels)),
                    "toString": ", ".join(sorted(new_labels)),
                }
            ]
        },
        "user": {"accountId": "poller", "displayName": "Forge Poller"},
    }


def comment_created(
    ticket_key: str,
    issue_type: str,
    status: str,
    summary: str,
    labels: set[str],
    body: str,
    author_account_id: str,
    author_display_name: str,
    author_email: str = "",  # ignored — always blank so Forge gateway won't self-filter
    *,
    comment_id: str | None = None,
    created: str | None = None,
    updated: str | None = None,
) -> dict[str, Any]:
    # Leave emailAddress empty: Forge skips comment_created when email equals
    # JIRA_USER_EMAIL. Local single-account setups share that address with humans.
    _ = author_email
    comment: dict[str, Any] = {
        "body": body,
        "author": {
            "accountId": author_account_id,
            "displayName": author_display_name,
            "emailAddress": "",
        },
    }
    # Legacy callers may omit metadata; polling always supplies a complete revision.
    if any(value is not None for value in (comment_id, created, updated)):
        if not isinstance(comment_id, str) or not comment_id.strip():
            raise ValueError("Jira comment.id must be a non-empty string")
        created_time = revision_time(created, "comment.created")
        comment["id"] = comment_id
        comment["created"] = created
        if updated is not None:
            if revision_time(updated, "comment.updated") < created_time:
                raise ValueError("Jira comment.updated precedes comment.created")
            comment["updated"] = updated

    return {
        "webhookEvent": "comment_created",
        "issue": {
            "key": ticket_key,
            "fields": {
                "issuetype": {"name": issue_type},
                "status": {"name": status},
                "summary": summary,
                "labels": sorted(labels),
            },
        },
        "comment": comment,
        "user": {"accountId": author_account_id, "displayName": author_display_name},
    }


def check_suite_completed(
    repo: str,
    branch: str,
    pr_number: int,
    conclusion: str,
    head_sha: str,
) -> dict[str, Any]:
    return {
        "action": "completed",
        "check_suite": {
            "status": "completed",
            "conclusion": conclusion,
            "head_branch": branch,
            "head_sha": head_sha,
            "pull_requests": [{"number": pr_number}],
        },
        "repository": {"full_name": repo},
    }


def pr_review_submitted(
    repo: str,
    branch: str,
    pr_number: int,
    pr_title: str,
    pr_url: str,
    review_state: str,
    review_body: str,
    reviewer_login: str,
) -> dict[str, Any]:
    return {
        "action": "submitted",
        "review": {
            "state": review_state.lower(),
            "body": review_body,
        },
        "pull_request": {
            "number": pr_number,
            "title": pr_title,
            "state": "open",
            "html_url": pr_url,
            "head": {"ref": branch},
        },
        "repository": {"full_name": repo},
        "sender": {"login": reviewer_login},
    }


def issue_comment(
    repo: str,
    pr_number: int,
    comment_body: str,
    sender_login: str,
) -> dict[str, Any]:
    return {
        "action": "created",
        "issue": {"number": pr_number},
        "comment": {
            "body": comment_body,
        },
        "repository": {"full_name": repo},
        "sender": {"login": sender_login},
    }


def pr_merged(
    repo: str,
    branch: str,
    pr_number: int,
    pr_title: str,
    pr_url: str,
) -> dict[str, Any]:
    return {
        "action": "closed",
        "pull_request": {
            "number": pr_number,
            "merged": True,
            "title": pr_title,
            "state": "closed",
            "html_url": pr_url,
            "head": {"ref": branch},
        },
        "repository": {"full_name": repo},
        "sender": {"login": "poller"},
    }
