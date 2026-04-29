from typing import Any


def label_changed(
    ticket_key: str,
    issue_type: str,
    status: str,
    summary: str,
    old_labels: set[str],
    new_labels: set[str],
) -> dict[str, Any]:
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": ticket_key,
            "fields": {
                "issuetype": {"name": issue_type},
                "status": {"name": status},
                "summary": summary,
                "labels": sorted(new_labels),
            },
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
) -> dict[str, Any]:
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
        "comment": {
            "body": body,
            "author": {
                "accountId": author_account_id,
                "displayName": author_display_name,
            },
        },
        "user": {"accountId": author_account_id, "displayName": author_display_name},
    }


def check_suite_completed(
    repo: str,
    branch: str,
    pr_number: int,
    conclusion: str,
) -> dict[str, Any]:
    return {
        "action": "completed",
        "check_suite": {
            "status": "completed",
            "conclusion": conclusion,
            "head_branch": branch,
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
