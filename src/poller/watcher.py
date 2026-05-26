import asyncio
import logging

from poller import forwarder, payloads
from poller.config import get_settings
from poller.github import GitHubClient, check_suites_conclusion, latest_review
from poller.jira import JiraClient, extract_pr_info
from poller.models import TicketState
from poller.persistence import load_state, save_state

logger = logging.getLogger(__name__)


class TicketWatcher:
    def __init__(self) -> None:
        self._state_file = get_settings().poller_state_file
        if self._state_file:
            self._state = load_state(self._state_file)
            logger.info(f"Restored {len(self._state)} ticket(s) from {self._state_file!r}")
        else:
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
        if self._state_file:
            save_state(self._state_file, self._state)

    async def remove(self, ticket_key: str) -> bool:
        async with self._lock:
            if ticket_key not in self._state:
                return False
            del self._state[ticket_key]
        logger.info(f"Stopped watching {ticket_key}")
        if self._state_file:
            save_state(self._state_file, self._state)
        return True

    def list(self) -> list[dict]:
        def _entry(s: TicketState, children: list[dict] | None = None) -> dict:
            return {
                "ticket_key": s.ticket_key,
                "issue_type": s.issue_type,
                "labels": sorted(s.labels),
                "pr": f"{s.repo}#{s.pr_number}" if s.pr_number else None,
                "children": children if children is not None else [],
            }

        # Build parent → children mapping from forge:parent: labels
        children_of: dict[str, list[dict]] = {}
        child_keys: set[str] = set()
        for s in self._state.values():
            for label in s.labels:
                if label.startswith("forge:parent:"):
                    parent_key = label[len("forge:parent:"):]
                    if parent_key in self._state:
                        children_of.setdefault(parent_key, []).append(_entry(s))
                        child_keys.add(s.ticket_key)

        return [
            _entry(s, children_of.get(s.ticket_key, []))
            for s in self._state.values()
            if s.ticket_key not in child_keys
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

        repo = pr_number = branch = head_sha = pr_title = pr_url = None
        try:
            remote_links = await jira.get_remote_links(ticket_key)
            info = extract_pr_info(remote_links)
            if info:
                repo, pr_number, _ = info
                gh = GitHubClient()
                pr = await gh.get_pr(repo, pr_number)
                branch = pr.get("head", {}).get("ref", "")
                head_sha = pr.get("head", {}).get("sha", "")
                pr_title = pr.get("title", "")
                pr_url = pr.get("html_url", "")
        except Exception as e:
            logger.debug(f"Could not fetch PR info for {ticket_key}: {e}")

        last_check_status = last_check_conclusion = None
        last_completed_count = None
        last_review_id = None
        last_pr_comment_id = None

        if repo and pr_number:
            try:
                gh = GitHubClient()
                comments = await gh.get_issue_comments(repo, pr_number)
                if comments:
                    last_pr_comment_id = comments[0].get("id")
            except Exception as e:
                logger.debug(f"Could not fetch PR comments for {ticket_key}: {e}")

        if repo and head_sha:
            try:
                gh = GitHubClient()
                suites = await gh.get_check_suites(repo, head_sha)
                # Do NOT capture CI conclusion as baseline — always leave as None so the
                # first poll after registration fires even if CI already completed before
                # the ticket was registered (or the poller was restarted).
                last_completed_count = sum(1 for s in suites if s.get("status") == "completed")
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
            head_sha=head_sha,
            pr_title=pr_title,
            pr_url=pr_url,
            last_check_status=last_check_status,
            last_check_conclusion=last_check_conclusion,
            last_completed_count=last_completed_count,
            last_review_id=last_review_id,
            last_pr_comment_id=last_pr_comment_id,
        )

    async def _sync_epics(self, feature_key: str) -> None:
        """Auto-add/remove child Epic and Task watches for a Feature ticket."""
        try:
            jira = JiraClient()
            active_keys = set(await jira.search_children(feature_key))
        except Exception as e:
            logger.warning(f"Epic sync failed for {feature_key}: {e}")
            return

        parent_label = f"forge:parent:{feature_key}"
        async with self._lock:
            watched_children = {
                key for key, state in self._state.items()
                if parent_label in state.labels
            }

        for key in active_keys - watched_children:
            logger.info(f"Auto-watching child {key} of {feature_key}")
            await self.add(key)

        for key in watched_children - active_keys:
            logger.info(f"Auto-unwatching archived child {key} of {feature_key}")
            await self.remove(key)

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
            bot_id = get_settings().forge_bot_account_id
            if bot_id and author.get("accountId") == bot_id:
                logger.debug(f"{ticket_key}: skipping comment from Forge bot")
            else:
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
                        author_email=author.get("emailAddress", ""),
                    )
                )

        # Discover PR if not yet known
        repo, pr_number, branch = state.repo, state.pr_number, state.branch
        head_sha, pr_title, pr_url = state.head_sha, state.pr_title, state.pr_url
        if not pr_number:
            try:
                remote_links = await jira.get_remote_links(ticket_key)
                info = extract_pr_info(remote_links)
                if info:
                    repo, pr_number, _ = info
                    gh = GitHubClient()
                    pr = await gh.get_pr(repo, pr_number)
                    branch = pr.get("head", {}).get("ref", "")
                    head_sha = pr.get("head", {}).get("sha", "")
                    pr_title = pr.get("title", "")
                    pr_url = pr.get("html_url", "")
                    logger.info(f"{ticket_key}: discovered PR {repo}#{pr_number}")
            except Exception as e:
                logger.debug(f"PR discovery failed for {ticket_key}: {e}")

        last_check_status = state.last_check_status
        last_check_conclusion = state.last_check_conclusion
        last_completed_count = state.last_completed_count
        last_review_id = state.last_review_id
        last_pr_comment_id = state.last_pr_comment_id

        if repo and pr_number:
            gh = GitHubClient()

            # Check merged — also refresh head_sha in case new commits were pushed
            try:
                pr_data = await gh.get_pr(repo, pr_number)
                head_sha = pr_data.get("head", {}).get("sha", head_sha)
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

            # CI status — use check_suites, not individual check_runs.
            # GitHub marks a suite status="completed" only when ALL its child
            # check_runs are done. Checking the suite is authoritative; counting
            # individual check_runs is not because GitHub registers them lazily.
            if head_sha:
                try:
                    suites = await gh.get_check_suites(repo, head_sha)
                    total_suites = len(suites)
                    new_completed_count = sum(1 for s in suites if s.get("status") == "completed")
                    result = check_suites_conclusion(suites)
                    if result:
                        new_check_status, new_check_conclusion = result
                        conclusion_changed = (new_check_status, new_check_conclusion) != (last_check_status, last_check_conclusion)
                        if conclusion_changed:
                            logger.info(
                                f"{ticket_key}: CI all suites done — {new_check_conclusion} "
                                f"({new_completed_count}/{total_suites} suites complete)"
                            )
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
                    else:
                        logger.debug(
                            f"{ticket_key}: CI suites still running "
                            f"({new_completed_count}/{total_suites} suites complete)"
                        )
                    last_completed_count = new_completed_count
                except Exception as e:
                    logger.warning(f"CI check failed for {ticket_key}: {e}")
            else:
                logger.debug(f"{ticket_key}: skipping CI check — head_sha not yet known")

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

            # PR comments (issue_comment events — needed for /forge skip-gate)
            try:
                pr_comments = await gh.get_issue_comments(repo, pr_number)
                if pr_comments and pr_comments[0].get("id") != state.last_pr_comment_id:
                    # Comments are sorted desc (newest first). Collect all new
                    # ones, then forward in chronological order.
                    new_comments = []
                    for c in pr_comments:
                        if c.get("id") == state.last_pr_comment_id:
                            break
                        new_comments.append(c)
                    for c in reversed(new_comments):
                        sender = c.get("user", {}).get("login", "")
                        body = c.get("body", "")
                        bot_login = get_settings().forge_bot_github_login
                        if bot_login and sender == bot_login:
                            logger.debug(f"{ticket_key}: skipping own PR comment")
                            continue
                        logger.info(f"{ticket_key}: new PR comment by {sender}")
                        await forwarder.forward_github(
                            payloads.issue_comment(
                                repo=repo,
                                pr_number=pr_number,
                                comment_body=body,
                                sender_login=sender,
                            ),
                            event_type="issue_comment",
                        )
                    last_pr_comment_id = pr_comments[0].get("id")
            except Exception as e:
                logger.warning(f"PR comment check failed for {ticket_key}: {e}")

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
                    head_sha=head_sha,
                    pr_title=pr_title,
                    pr_url=pr_url,
                    last_check_status=last_check_status,
                    last_check_conclusion=last_check_conclusion,
                    last_completed_count=last_completed_count,
                    last_review_id=last_review_id,
                    last_pr_comment_id=last_pr_comment_id,
                )
        if self._state_file:
            save_state(self._state_file, self._state)

        if state.issue_type in ("Feature", "Story"):
            await self._sync_epics(ticket_key)


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
