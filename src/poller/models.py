from dataclasses import dataclass, field


@dataclass
class PrState:
    repo: str
    pr_number: int
    branch: str
    head_sha: str
    pr_title: str
    pr_url: str
    merged: bool = False
    last_check_status: str | None = None
    last_check_conclusion: str | None = None
    last_reported_head_sha: str | None = None
    last_completed_count: int | None = None
    last_review_id: int | None = None
    last_pr_comment_id: int | None = None


@dataclass
class TicketState:
    ticket_key: str
    issue_type: str
    status: str
    summary: str
    labels: set[str]
    last_comment_id: str | None
    prs: list[PrState] = field(default_factory=list)
    # PRD proposals PR tracking
    prd_pr_repo: str | None = None
    prd_pr_number: int | None = None
    prd_last_review_id: int | None = None
    prd_last_pr_comment_id: int | None = None
    prd_pr_merged: bool = False
    # Spec proposals PR tracking
    spec_pr_repo: str | None = None
    spec_pr_number: int | None = None
    spec_last_review_id: int | None = None
    spec_last_pr_comment_id: int | None = None
    spec_pr_merged: bool = False
    poll_interval_seconds: int | None = None
    last_polled_at: float | None = None
    next_poll_at: float | None = None
    failure_count: int = 0
