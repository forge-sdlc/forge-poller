from dataclasses import dataclass


@dataclass
class TicketState:
    ticket_key: str
    issue_type: str
    status: str
    summary: str
    labels: set[str]
    last_comment_id: str | None
    repo: str | None
    pr_number: int | None
    branch: str | None
    head_sha: str | None
    pr_title: str | None
    pr_url: str | None
    last_check_status: str | None
    last_check_conclusion: str | None
    last_completed_count: int | None
    last_review_id: int | None
    last_pr_comment_id: int | None = None
    # PRD proposals PR tracking
    prd_pr_repo: str | None = None
    prd_pr_number: int | None = None
    prd_last_review_id: int | None = None
    prd_last_pr_comment_id: int | None = None
    prd_pr_merged: bool = False
