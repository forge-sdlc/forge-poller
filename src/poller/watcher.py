import asyncio
import logging
from dataclasses import dataclass, field

from poller import forwarder, payloads
from poller.config import get_settings
from poller.github import GitHubClient, latest_check_conclusion, latest_review
from poller.jira import JiraClient, extract_pr_info

logger = logging.getLogger(__name__)


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
    pr_title: str | None
    pr_url: str | None
    last_check_status: str | None
    last_check_conclusion: str | None
    last_review_id: int | None


class TicketWatcher:
    def __init__(self) -> None:
        self._state: dict[str, TicketState] = {}
        self._lock = asyncio.Lock()

    async def add(self, ticket_key: str) -> None:
        async with self._lock:
            if ticket_key in self._state:
                return
        state = await self._snapshot(ticket_key)
        async with self._lock:
            self._state[ticket_key] = state
        logger.info(f"Watching {ticket_key} (labels={state.labels})")

    async def remove(self, ticket_key: str) -> bool:
        async with self._lock:
            if ticket_key not in self._state:
                return False
            del self._state[ticket_key]
        logger.info(f"Stopped watching {ticket_key}")
        return True

    def list(self) -> list[dict]:
        return [
            {
                "ticket_key": s.ticket_key,
                "issue_type": s.issue_type,
                "labels": sorted(s.labels),
                "pr": f"{s.repo}#{s.pr_number}" if s.pr_number else None,
            }
            for s in self._state.values()
        ]

    async def run(self) -> None:
        interval = get_settings().poll_interval
        logger.info(f"Polling loop started (interval={interval}s)")
        while True:
            await asyncio.sleep(interval)
            async with self._lock:
                keys = list(self._state.keys())
            for key in keys:
                try:
                    await self._poll(key)
                except Exception as e:
                    logger.warning(f"Poll failed for {key}: {e}")

    async def _snapshot(self, ticket_key: str) -> TicketState:
        jira = JiraClient()
        issue = await jira.get_issue(ticket_key)
        fields = issue.get("fields", {})

        labels = set(fields.get("labels", []))
        comments = fields.get("comment", {}).get("comments", [])
        last_comment_id = comments[-1]["id"] if comments else None

        issue_type = fields.get("issuetype", {}).get("name", "")
        status = fields.get("status", {}).get("name", "")
        summary = fields.get("summary", "")

        repo = pr_number = branch = pr_title = pr_url = None
        try:
            remote_links = await jira.get_remote_links(ticket_key)
            info = extract_pr_info(remote_links)
            if info:
                repo, pr_number, _ = info
                gh = GitHubClient()
                pr = await gh.get_pr(repo, pr_number)
                branch = pr.get("head", {}).get("ref", "")
                pr_title = pr.get("title", "")
                pr_url = pr.get("html_url", "")
        except Exception as e:
            logger.debug(f"Could not fetch PR info for {ticket_key}: {e}")

        last_check_status = last_check_conclusion = None
        last_review_id = None

        if repo and pr_number:
            try:
                gh = GitHubClient()
                check_runs = await gh.get_check_runs(repo, pr_number)
                result = latest_check_conclusion(check_runs)
                if result:
                    last_check_status, last_check_conclusion = result
                reviews = await gh.get_reviews(repo, pr_number)
                rev = latest_review(reviews)
                if rev:
                    last_review_id = rev.get("id")
            except Exception as e:
                logger.debug(f"Could not fetch GitHub state for {ticket_key}: {e}")

        return TicketState(
            ticket_key=ticket_key,
            issue_type=issue_type,
            status=status,
            summary=summary,
            labels=labels,
            last_comment_id=last_comment_id,
            repo=repo,
            pr_number=pr_number,
            branch=branch,
            pr_title=pr_title,
            pr_url=pr_url,
            last_check_status=last_check_status,
            last_check_conclusion=last_check_conclusion,
            last_review_id=last_review_id,
        )

    async def _poll(self, ticket_key: str) -> None:
        async with self._lock:
            if ticket_key not in self._state:
                return
            state = self._state[ticket_key]

        jira = JiraClient()
        issue = await jira.get_issue(ticket_key)
        fields = issue.get("fields", {})

        new_labels = set(fields.get("labels", []))
        new_status = fields.get("status", {}).get("name", "")
        new_summary = fields.get("summary", "")
        comments = fields.get("comment", {}).get("comments", [])
        new_last_comment_id = comments[-1]["id"] if comments else None

        # Label change
        if new_labels != state.labels:
            logger.info(f"{ticket_key}: labels changed {state.labels} → {new_labels}")
            await forwarder.forward_jira(
                payloads.label_changed(
                    ticket_key=ticket_key,
                    issue_type=state.issue_type,
                    status=new_status,
                    summary=new_summary,
                    old_labels=state.labels,
                    new_labels=new_labels,
                )
            )

        # New comment
        if new_last_comment_id and new_last_comment_id != state.last_comment_id:
            comment = comments[-1]
            body = _extract_comment_body(comment.get("body"))
            author = comment.get("author", {})
            logger.info(f"{ticket_key}: new comment from {author.get('displayName')}")
            await forwarder.forward_jira(
                payloads.comment_created(
                    ticket_key=ticket_key,
                    issue_type=state.issue_type,
                    status=new_status,
                    summary=new_summary,
                    labels=new_labels,
                    body=body,
                    author_account_id=author.get("accountId", ""),
                    author_display_name=author.get("displayName", ""),
                )
            )

        # Discover PR if not yet known
        repo, pr_number, branch = state.repo, state.pr_number, state.branch
        pr_title, pr_url = state.pr_title, state.pr_url
        if not pr_number:
            try:
                remote_links = await jira.get_remote_links(ticket_key)
                info = extract_pr_info(remote_links)
                if info:
                    repo, pr_number, _ = info
                    gh = GitHubClient()
                    pr = await gh.get_pr(repo, pr_number)
                    branch = pr.get("head", {}).get("ref", "")
                    pr_title = pr.get("title", "")
                    pr_url = pr.get("html_url", "")
                    logger.info(f"{ticket_key}: discovered PR {repo}#{pr_number}")
            except Exception as e:
                logger.debug(f"PR discovery failed for {ticket_key}: {e}")

        last_check_status = state.last_check_status
        last_check_conclusion = state.last_check_conclusion
        last_review_id = state.last_review_id

        if repo and pr_number:
            gh = GitHubClient()

            # Check merged
            try:
                pr_data = await gh.get_pr(repo, pr_number)
                if pr_data.get("merged"):
                    logger.info(f"{ticket_key}: PR merged")
                    await forwarder.forward_github(
                        payloads.pr_merged(
                            repo=repo,
                            branch=branch or "",
                            pr_number=pr_number,
                            pr_title=pr_title or "",
                            pr_url=pr_url or "",
                        ),
                        event_type="pull_request",
                    )
                    await self.remove(ticket_key)
                    return
            except Exception as e:
                logger.debug(f"PR merge check failed for {ticket_key}: {e}")

            # CI status
            try:
                check_runs = await gh.get_check_runs(repo, pr_number)
                result = latest_check_conclusion(check_runs)
                if result:
                    new_check_status, new_check_conclusion = result
                    if (new_check_status, new_check_conclusion) != (last_check_status, last_check_conclusion):
                        logger.info(f"{ticket_key}: CI {new_check_status}/{new_check_conclusion}")
                        await forwarder.forward_github(
                            payloads.check_suite_completed(
                                repo=repo,
                                branch=branch or "",
                                pr_number=pr_number,
                                conclusion=new_check_conclusion,
                            ),
                            event_type="check_suite",
                        )
                        last_check_status = new_check_status
                        last_check_conclusion = new_check_conclusion
            except Exception as e:
                logger.debug(f"CI check failed for {ticket_key}: {e}")

            # Reviews
            try:
                reviews = await gh.get_reviews(repo, pr_number)
                rev = latest_review(reviews)
                if rev and rev.get("id") != last_review_id:
                    reviewer = rev.get("user", {}).get("login", "")
                    logger.info(f"{ticket_key}: new review by {reviewer} ({rev.get('state')})")
                    await forwarder.forward_github(
                        payloads.pr_review_submitted(
                            repo=repo,
                            branch=branch or "",
                            pr_number=pr_number,
                            pr_title=pr_title or "",
                            pr_url=pr_url or "",
                            review_state=rev.get("state", ""),
                            review_body=rev.get("body", "") or "",
                            reviewer_login=reviewer,
                        ),
                        event_type="pull_request_review",
                    )
                    last_review_id = rev.get("id")
            except Exception as e:
                logger.debug(f"Review check failed for {ticket_key}: {e}")

        async with self._lock:
            if ticket_key in self._state:
                self._state[ticket_key] = TicketState(
                    ticket_key=ticket_key,
                    issue_type=state.issue_type,
                    status=new_status,
                    summary=new_summary,
                    labels=new_labels,
                    last_comment_id=new_last_comment_id,
                    repo=repo,
                    pr_number=pr_number,
                    branch=branch,
                    pr_title=pr_title,
                    pr_url=pr_url,
                    last_check_status=last_check_status,
                    last_check_conclusion=last_check_conclusion,
                    last_review_id=last_review_id,
                )


def _extract_comment_body(body: object) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        # ADF: flatten all text nodes
        parts: list[str] = []
        _walk_adf(body, parts)
        return " ".join(parts)
    return str(body)


def _walk_adf(node: dict, out: list[str]) -> None:
    if node.get("type") == "text":
        out.append(node.get("text", ""))
    for child in node.get("content", []):
        _walk_adf(child, out)
