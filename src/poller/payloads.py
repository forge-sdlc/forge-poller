from typing import Any


def label_changed(
    ticket_key: str,
    issue_type: str,
    status: str,
    summary: str,
    old_labels: set[str],
    new_labels: set[str],
    updated: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "issuetype": {"name": issue_type},
        "status": {"name": status},
        "summary": summary,
        "labels": sorted(new_labels),
    }
    if updated:
        # Jira's issue updated timestamp is the native revision available to
        # the REST poller for label/status changes.
        fields["updated"] = updated
    return {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": ticket_key,
            "fields": fields,
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
    author_email: str = "",
    comment_id: str | int | None = None,
    issue_updated: str | None = None,
    comment_created: str | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "issuetype": {"name": issue_type},
        "status": {"name": status},
        "summary": summary,
        "labels": sorted(labels),
    }
    if issue_updated:
        fields["updated"] = issue_updated
    comment: dict[str, Any] = {
        "body": body,
        "author": {
            "accountId": author_account_id,
            "displayName": author_display_name,
            "emailAddress": author_email,
        },
    }
    if comment_id is not None:
        comment["id"] = comment_id
    if comment_created:
        comment["created"] = comment_created
    return {
        "webhookEvent": "comment_created",
        "issue": {
            "key": ticket_key,
            "fields": fields,
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
    review_id: object | None = None,
    head_sha: str = "",
    base_branch: str = "",
    pr_body: str = "",
    pr_state: str = "open",
    draft: bool = False,
) -> dict[str, Any]:
    review: dict[str, Any] = {
        "state": review_state.lower(),
        "body": review_body,
        "user": {"login": reviewer_login},
    }
    if review_id is not None:
        review["id"] = review_id
    return {
        "action": "submitted",
        "review": review,
        "pull_request": {
            "number": pr_number,
            "title": pr_title,
            "body": pr_body,
            "state": pr_state,
            "html_url": pr_url,
            "head": {"ref": branch, "sha": head_sha},
            "base": {"ref": base_branch},
            "draft": draft,
        },
        "repository": {"full_name": repo},
        "sender": {"login": reviewer_login},
    }


def issue_comment(
    repo: str,
    pr_number: int,
    comment_body: str,
    sender_login: str,
    comment_id: object | None = None,
    issue_title: str = "",
    issue_body: str = "",
    issue_state: str = "open",
    issue_url: str = "",
) -> dict[str, Any]:
    comment: dict[str, Any] = {"body": comment_body}
    if comment_id is not None:
        comment["id"] = comment_id
    issue: dict[str, Any] = {
        "number": pr_number,
        "title": issue_title,
        "body": issue_body,
        "state": issue_state,
        "html_url": issue_url,
    }
    if issue_url:
        issue["pull_request"] = {"html_url": issue_url}
    return {
        "action": "created",
        "issue": issue,
        "comment": comment,
        "repository": {"full_name": repo},
        "sender": {"login": sender_login},
    }


def pr_merged(
    repo: str,
    branch: str,
    pr_number: int,
    pr_title: str,
    pr_url: str,
    head_sha: str = "",
    pr_body: str = "",
    base_branch: str = "",
) -> dict[str, Any]:
    return {
        "action": "closed",
        "pull_request": {
            "number": pr_number,
            "merged": True,
            "title": pr_title,
            "body": pr_body,
            "state": "closed",
            "html_url": pr_url,
            "head": {"ref": branch, "sha": head_sha},
            "base": {"ref": base_branch},
        },
        "repository": {"full_name": repo},
        "sender": {"login": "poller"},
    }
