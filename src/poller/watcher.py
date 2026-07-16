import asyncio
import heapq
import logging
import random
import re
import time

from poller import forwarder, payloads
from poller.config import get_settings
from poller.github import GitHubClient, check_suites_conclusion, latest_review
from poller.jira import JiraClient, extract_pr_info
from poller.models import PrState, TicketState
from poller.persistence import load_state, save_state

logger = logging.getLogger(__name__)

_PRD_COMMENT_PATTERN = re.compile(
    r"github\.com/([^/]+/[^/]+)/pull/(\d+)", re.IGNORECASE
)
_SPEC_PUBLICATION_PATTERN = re.compile(
    r"\b(?:spec|specification)\s+published\s+for\s+review\b", re.IGNORECASE
)
_ARCHIVED_LABELS = {"archived", "forge:archived"}


class TicketWatcher:
    def __init__(self) -> None:
        settings = get_settings()
        self._state_file = settings.poller_state_file
        if self._state_file:
            self._state = load_state(self._state_file)
            logger.info(f"Restored {len(self._state)} ticket(s) from {self._state_file!r}")
        else:
            self._state: dict[str, TicketState] = {}
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._inflight: set[str] = set()
        self._poll_tasks: set[asyncio.Task] = set()
        self._schedule_heap: list[tuple[float, int, str]] = []
        self._schedule_seq = 0
        now = time.time()
        for key, state in self._state.items():
            if state.poll_interval_seconds is None:
                state.poll_interval_seconds = settings.poll_interval
            due_at = state.next_poll_at or now + self._jitter(settings.poll_interval)
            self._schedule_locked(key, due_at)

    async def add(self, ticket_key: str) -> None:
        async with self._lock:
            if ticket_key in self._state:
                return
        state = await self._snapshot(ticket_key)

        # Registration snapshots the ticket's current state, so without this
        # bootstrap event a forge:managed label added before /watch would never
        # be observed as a change and Forge would not start the workflow.
        if "forge:managed" in state.labels:
            await forwarder.forward_jira(
                payloads.label_changed(
                    ticket_key=ticket_key,
                    issue_type=state.issue_type,
                    status=state.status,
                    summary=state.summary,
                    old_labels=state.labels - {"forge:managed"},
                    new_labels=state.labels,
                )
            )
        async with self._lock:
            state.poll_interval_seconds = get_settings().poll_interval
            self._state[ticket_key] = state
            self._schedule_locked(ticket_key, time.time() + self._jitter(1))
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
                "prs": [f"{pr.repo}#{pr.pr_number}" for pr in s.prs],
                "last_polled_at": s.last_polled_at,
                "next_poll_at": s.next_poll_at,
                "poll_interval_seconds": s.poll_interval_seconds,
                "failure_count": s.failure_count,
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
        settings = get_settings()
        logger.info(
            "Polling scheduler started "
            f"(base_interval={settings.poll_interval}s, "
            f"max_interval={settings.poller_max_poll_interval}s, "
            f"max_concurrency={settings.poller_max_concurrency})"
        )
        while True:
            sleep_for = await self._launch_due_polls()
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=sleep_for)
            except TimeoutError:
                pass

    async def _launch_due_polls(self) -> float:
        settings = get_settings()
        now = time.time()
        async with self._lock:
            launched = 0
            while (
                len(self._inflight) < settings.poller_max_concurrency
                and self._schedule_heap
            ):
                due_at, _seq, key = self._schedule_heap[0]
                if key not in self._state or self._state[key].next_poll_at != due_at:
                    heapq.heappop(self._schedule_heap)
                    continue
                if due_at > now:
                    break
                heapq.heappop(self._schedule_heap)
                self._inflight.add(key)
                task = asyncio.create_task(self._poll_and_reschedule(key))
                self._poll_tasks.add(task)
                task.add_done_callback(self._poll_tasks.discard)
                launched += 1

            if launched:
                return 0

            if len(self._inflight) >= settings.poller_max_concurrency:
                return 0.5

            next_due_at = self._next_due_at_locked()
            if next_due_at is None:
                return max(1, settings.poll_interval)
            return max(0, min(next_due_at - now, settings.poll_interval))

    async def _poll_and_reschedule(self, ticket_key: str) -> None:
        success = False
        try:
            await self._poll(ticket_key)
            success = True
        except Exception as e:
            logger.warning(f"Poll failed for {ticket_key}: {e}")
        finally:
            await self._reschedule_after_poll(ticket_key, success)

    async def _reschedule_after_poll(self, ticket_key: str, success: bool) -> None:
        now = time.time()
        async with self._lock:
            self._inflight.discard(ticket_key)
            state = self._state.get(ticket_key)
            if not state:
                self._wakeup.set()
                return

            state.last_polled_at = now
            if success:
                state.failure_count = 0
                next_interval = self._next_success_interval(state)
            else:
                state.failure_count += 1
                next_interval = self._next_failure_interval(state.failure_count)
            state.poll_interval_seconds = next_interval
            self._schedule_locked(ticket_key, now + self._jitter(next_interval))

        if self._state_file:
            save_state(self._state_file, self._state)

    def _next_due_at_locked(self) -> float | None:
        while self._schedule_heap:
            due_at, _seq, key = self._schedule_heap[0]
            if key in self._state and self._state[key].next_poll_at == due_at:
                return due_at
            heapq.heappop(self._schedule_heap)
        return None

    def _schedule_locked(self, ticket_key: str, due_at: float) -> None:
        state = self._state.get(ticket_key)
        if not state:
            return
        self._schedule_seq += 1
        state.next_poll_at = due_at
        heapq.heappush(self._schedule_heap, (due_at, self._schedule_seq, ticket_key))
        self._wakeup.set()

    def _next_success_interval(self, state: TicketState) -> int:
        settings = get_settings()
        base = max(1, settings.poll_interval)
        maximum = max(base, settings.poller_max_poll_interval)

        active_prs = [pr for pr in state.prs if not pr.merged]
        if active_prs:
            if any(pr.last_check_conclusion is None for pr in active_prs):
                return base
            return min(maximum, base * 2)

        if state.prd_pr_number and not state.prd_pr_merged:
            return base

        if state.spec_pr_number and not state.spec_pr_merged:
            return base

        if state.issue_type in ("Feature", "Story"):
            return min(maximum, base * 2)

        return min(maximum, base * 4)

    def _next_failure_interval(self, failure_count: int) -> int:
        settings = get_settings()
        base = max(1, settings.poll_interval)
        maximum = max(base, settings.poller_max_poll_interval)
        return min(maximum, base * (2 ** min(failure_count, 5)))

    def _jitter(self, seconds: int | float) -> float:
        jitter_ratio = get_settings().poller_jitter_ratio
        if jitter_ratio <= 0:
            return float(seconds)
        spread = seconds * jitter_ratio
        return max(0.1, random.uniform(seconds - spread, seconds + spread))

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

        prs: list[PrState] = []
        try:
            remote_links = await jira.get_remote_links(ticket_key)
            pr_infos = extract_pr_info(remote_links)
            gh = GitHubClient()
            for repo, pr_number in pr_infos:
                try:
                    pr = await gh.get_pr(repo, pr_number)
                    pr_state = PrState(
                        repo=repo,
                        pr_number=pr_number,
                        branch=pr.get("head", {}).get("ref", ""),
                        head_sha=pr.get("head", {}).get("sha", ""),
                        pr_title=pr.get("title", ""),
                        pr_url=pr.get("html_url", ""),
                    )
                    try:
                        pr_comments = await gh.get_issue_comments(repo, pr_number)
                        if pr_comments:
                            pr_state.last_pr_comment_id = pr_comments[0].get("id")
                    except Exception as e:
                        logger.debug(f"Could not fetch PR comments for {ticket_key} {repo}#{pr_number}: {e}")
                    try:
                        suites = await gh.get_check_suites(repo, pr_state.head_sha)
                        pr_state.last_completed_count = sum(1 for s in suites if s.get("status") == "completed")
                        reviews = await gh.get_reviews(repo, pr_number)
                        rev = latest_review(reviews)
                        if rev:
                            pr_state.last_review_id = rev.get("id")
                    except Exception as e:
                        logger.debug(f"Could not fetch GitHub state for {ticket_key} {repo}#{pr_number}: {e}")
                    prs.append(pr_state)
                except Exception as e:
                    logger.debug(f"Could not fetch PR {repo}#{pr_number} for {ticket_key}: {e}")
        except Exception as e:
            logger.debug(f"Could not fetch PR info for {ticket_key}: {e}")

        return TicketState(
            ticket_key=ticket_key,
            issue_type=issue_type,
            status=status,
            summary=summary,
            labels=labels,
            last_comment_id=last_comment_id,
            prs=prs,
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

    async def _poll_prd_pr(
        self,
        ticket_key: str,
        state: TicketState,
        jira_comments: list[dict],
    ) -> dict:
        """Poll the PRD proposals PR for merge, reviews, and comments.

        Returns a dict with updated prd_pr_* fields to be merged into the
        reconstructed TicketState at the end of _poll().
        """
        prd_pr_repo = state.prd_pr_repo
        prd_pr_number = state.prd_pr_number
        prd_last_review_id = state.prd_last_review_id
        prd_last_pr_comment_id = state.prd_last_pr_comment_id
        prd_pr_merged = state.prd_pr_merged

        def _result() -> dict:
            return {
                "prd_pr_repo": prd_pr_repo,
                "prd_pr_number": prd_pr_number,
                "prd_last_review_id": prd_last_review_id,
                "prd_last_pr_comment_id": prd_last_pr_comment_id,
                "prd_pr_merged": prd_pr_merged,
            }

        # Discovery: scan Jira comments for "PRD published for review: <url>"
        if not prd_pr_number and not prd_pr_merged:
            for comment in jira_comments:
                body = _extract_comment_body(comment.get("body"))
                m = _PRD_COMMENT_PATTERN.search(body)
                if m:
                    discovered_repo = m.group(1)
                    discovered_number = int(m.group(2))
                    gh = GitHubClient()
                    try:
                        pr = await gh.get_pr(discovered_repo, discovered_number)
                        prd_pr_repo = discovered_repo
                        prd_pr_number = discovered_number
                        if pr.get("merged"):
                            logger.info(
                                f"{ticket_key}: PRD PR already merged at discovery — marking done"
                            )
                            prd_pr_merged = True
                            return _result()
                        logger.info(
                            f"{ticket_key}: discovered PRD PR {prd_pr_repo}#{prd_pr_number}"
                        )
                        break
                    except Exception as e:
                        logger.debug(f"PRD PR fetch failed for {ticket_key}: {e}")

        # Polling: only when we have a live, unmerged PR
        if not prd_pr_number or prd_pr_merged:
            return _result()

        try:
            gh = GitHubClient()
            pr_data = await gh.get_pr(prd_pr_repo, prd_pr_number)

            if pr_data.get("merged"):
                logger.info(f"{ticket_key}: PRD PR merged")
                await forwarder.forward_github(
                    payloads.pr_merged(
                        repo=prd_pr_repo,
                        branch=pr_data.get("head", {}).get("ref", ""),
                        pr_number=prd_pr_number,
                        pr_title=pr_data.get("title", ""),
                        pr_url=pr_data.get("html_url", ""),
                    ),
                    event_type="pull_request",
                )
                prd_pr_merged = True
                return _result()

            prd_branch = pr_data.get("head", {}).get("ref", "")
            prd_title = pr_data.get("title", "")
            prd_url = pr_data.get("html_url", "")

            # Reviews
            reviews = await gh.get_reviews(prd_pr_repo, prd_pr_number)
            rev = latest_review(reviews)
            if rev and rev.get("id") != prd_last_review_id:
                reviewer = rev.get("user", {}).get("login", "")
                logger.info(
                    f"{ticket_key}: new PRD PR review by {reviewer} ({rev.get('state')})"
                )
                await forwarder.forward_github(
                    payloads.pr_review_submitted(
                        repo=prd_pr_repo,
                        branch=prd_branch,
                        pr_number=prd_pr_number,
                        pr_title=prd_title,
                        pr_url=prd_url,
                        review_state=rev.get("state", ""),
                        review_body=rev.get("body", "") or "",
                        reviewer_login=reviewer,
                    ),
                    event_type="pull_request_review",
                )
                prd_last_review_id = rev.get("id")

            # Comments (get_issue_comments returns newest-first)
            pr_comments = await gh.get_issue_comments(prd_pr_repo, prd_pr_number)
            if pr_comments and pr_comments[0].get("id") != prd_last_pr_comment_id:
                new_comments = []
                for c in pr_comments:
                    if c.get("id") == prd_last_pr_comment_id:
                        break
                    new_comments.append(c)
                for c in reversed(new_comments):
                    sender = c.get("user", {}).get("login", "")
                    bot_login = get_settings().forge_bot_github_login
                    if bot_login and sender == bot_login:
                        logger.debug(f"{ticket_key}: skipping bot comment on PRD PR")
                        continue
                    logger.info(f"{ticket_key}: new PRD PR comment by {sender}")
                    await forwarder.forward_github(
                        payloads.issue_comment(
                            repo=prd_pr_repo,
                            pr_number=prd_pr_number,
                            comment_body=c.get("body", ""),
                            sender_login=sender,
                        ),
                        event_type="issue_comment",
                    )
                prd_last_pr_comment_id = pr_comments[0].get("id")

        except Exception as e:
            logger.warning(f"PRD PR polling failed for {ticket_key}: {e}")

        return _result()

    async def _poll_spec_pr(
        self,
        ticket_key: str,
        state: TicketState,
        jira_comments: list[dict],
        prd_updates: dict | None = None,
    ) -> dict:
        """Poll the spec proposals PR for merge, reviews, and comments."""
        spec_pr_repo = state.spec_pr_repo
        spec_pr_number = state.spec_pr_number
        spec_last_review_id = state.spec_last_review_id
        spec_last_pr_comment_id = state.spec_last_pr_comment_id
        spec_pr_merged = state.spec_pr_merged

        def _result() -> dict:
            return {
                "spec_pr_repo": spec_pr_repo,
                "spec_pr_number": spec_pr_number,
                "spec_last_review_id": spec_last_review_id,
                "spec_last_pr_comment_id": spec_last_pr_comment_id,
                "spec_pr_merged": spec_pr_merged,
            }

        if not spec_pr_number and not spec_pr_merged:
            prd_repo = (prd_updates or {}).get("prd_pr_repo", state.prd_pr_repo)
            prd_number = (prd_updates or {}).get("prd_pr_number", state.prd_pr_number)
            for comment in jira_comments:
                body = _extract_comment_body(comment.get("body"))
                if not _SPEC_PUBLICATION_PATTERN.search(body):
                    continue
                for m in _PRD_COMMENT_PATTERN.finditer(body):
                    discovered_repo = m.group(1)
                    discovered_number = int(m.group(2))
                    if (
                        discovered_repo == prd_repo
                        and discovered_number == prd_number
                    ):
                        continue
                    gh = GitHubClient()
                    try:
                        pr = await gh.get_pr(discovered_repo, discovered_number)
                        spec_pr_repo = discovered_repo
                        spec_pr_number = discovered_number
                        if pr.get("merged"):
                            logger.info(
                                f"{ticket_key}: spec PR already merged at discovery — marking done"
                            )
                            spec_pr_merged = True
                            return _result()
                        logger.info(
                            f"{ticket_key}: discovered spec PR {spec_pr_repo}#{spec_pr_number}"
                        )
                        break
                    except Exception as e:
                        logger.debug(f"Spec PR fetch failed for {ticket_key}: {e}")
                else:
                    continue
                break

        if not spec_pr_number or spec_pr_merged:
            return _result()

        try:
            gh = GitHubClient()
            pr_data = await gh.get_pr(spec_pr_repo, spec_pr_number)

            if pr_data.get("merged"):
                logger.info(f"{ticket_key}: spec PR merged")
                await forwarder.forward_github(
                    payloads.pr_merged(
                        repo=spec_pr_repo,
                        branch=pr_data.get("head", {}).get("ref", ""),
                        pr_number=spec_pr_number,
                        pr_title=pr_data.get("title", ""),
                        pr_url=pr_data.get("html_url", ""),
                    ),
                    event_type="pull_request",
                )
                spec_pr_merged = True
                return _result()

            spec_branch = pr_data.get("head", {}).get("ref", "")
            spec_title = pr_data.get("title", "")
            spec_url = pr_data.get("html_url", "")

            reviews = await gh.get_reviews(spec_pr_repo, spec_pr_number)
            rev = latest_review(reviews)
            if rev and rev.get("id") != spec_last_review_id:
                reviewer = rev.get("user", {}).get("login", "")
                logger.info(
                    f"{ticket_key}: new spec PR review by {reviewer} ({rev.get('state')})"
                )
                await forwarder.forward_github(
                    payloads.pr_review_submitted(
                        repo=spec_pr_repo,
                        branch=spec_branch,
                        pr_number=spec_pr_number,
                        pr_title=spec_title,
                        pr_url=spec_url,
                        review_state=rev.get("state", ""),
                        review_body=rev.get("body", "") or "",
                        reviewer_login=reviewer,
                    ),
                    event_type="pull_request_review",
                )
                spec_last_review_id = rev.get("id")

            pr_comments = await gh.get_issue_comments(spec_pr_repo, spec_pr_number)
            if pr_comments and pr_comments[0].get("id") != spec_last_pr_comment_id:
                new_comments = []
                for c in pr_comments:
                    if c.get("id") == spec_last_pr_comment_id:
                        break
                    new_comments.append(c)
                for c in reversed(new_comments):
                    sender = c.get("user", {}).get("login", "")
                    bot_login = get_settings().forge_bot_github_login
                    if bot_login and sender == bot_login:
                        logger.debug(f"{ticket_key}: skipping bot comment on spec PR")
                        continue
                    logger.info(f"{ticket_key}: new spec PR comment by {sender}")
                    await forwarder.forward_github(
                        payloads.issue_comment(
                            repo=spec_pr_repo,
                            pr_number=spec_pr_number,
                            comment_body=c.get("body", ""),
                            sender_login=sender,
                        ),
                        event_type="issue_comment",
                    )
                spec_last_pr_comment_id = pr_comments[0].get("id")

        except Exception as e:
            logger.warning(f"Spec PR polling failed for {ticket_key}: {e}")

        return _result()

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

        if _is_archived(new_labels):
            logger.info(f"{ticket_key}: archived label detected — removing from watch list")
            await self.remove(ticket_key)
            return

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

        # Discover new PRs from remote links
        prs = list(state.prs)
        existing_pr_keys = {(pr.repo, pr.pr_number) for pr in prs}
        try:
            remote_links = await jira.get_remote_links(ticket_key)
            pr_infos = extract_pr_info(remote_links)
            gh = GitHubClient()
            for repo, pr_number in pr_infos:
                if (repo, pr_number) not in existing_pr_keys:
                    try:
                        pr = await gh.get_pr(repo, pr_number)
                        prs.append(PrState(
                            repo=repo,
                            pr_number=pr_number,
                            branch=pr.get("head", {}).get("ref", ""),
                            head_sha=pr.get("head", {}).get("sha", ""),
                            pr_title=pr.get("title", ""),
                            pr_url=pr.get("html_url", ""),
                        ))
                        logger.info(f"{ticket_key}: discovered PR {repo}#{pr_number}")
                    except Exception as e:
                        logger.debug(f"PR discovery failed for {ticket_key} {repo}#{pr_number}: {e}")
        except Exception as e:
            logger.debug(f"PR discovery failed for {ticket_key}: {e}")

        # Poll each PR
        all_merged = len(prs) > 0
        for pr in prs:
            if pr.merged:
                continue

            gh = GitHubClient()

            # Check merged — also refresh head_sha
            try:
                pr_data = await gh.get_pr(pr.repo, pr.pr_number)
                previous_head_sha = pr.head_sha
                pr.head_sha = pr_data.get("head", {}).get("sha", pr.head_sha)
                if pr.head_sha != previous_head_sha:
                    pr.last_check_status = None
                    pr.last_check_conclusion = None
                if pr_data.get("merged"):
                    logger.info(f"{ticket_key}: PR {pr.repo}#{pr.pr_number} merged")
                    await forwarder.forward_github(
                        payloads.pr_merged(
                            repo=pr.repo,
                            branch=pr.branch or "",
                            pr_number=pr.pr_number,
                            pr_title=pr.pr_title or "",
                            pr_url=pr.pr_url or "",
                        ),
                        event_type="pull_request",
                    )
                    pr.merged = True
                    continue
            except Exception as e:
                logger.debug(f"PR merge check failed for {ticket_key} {pr.repo}#{pr.pr_number}: {e}")

            all_merged = False

            # CI status
            if pr.head_sha:
                try:
                    suites = await gh.get_check_suites(pr.repo, pr.head_sha)
                    total_suites = len(suites)
                    new_completed_count = sum(1 for s in suites if s.get("status") == "completed")
                    result = check_suites_conclusion(suites)
                    if result:
                        new_check_status, new_check_conclusion = result
                        should_forward = (
                            (new_check_status, new_check_conclusion)
                            != (pr.last_check_status, pr.last_check_conclusion)
                            or pr.last_reported_head_sha != pr.head_sha
                        )
                        if should_forward:
                            logger.info(
                                f"{ticket_key}: CI for {pr.repo}#{pr.pr_number} — {new_check_conclusion} "
                                f"({new_completed_count}/{total_suites} suites complete)"
                            )
                            await forwarder.forward_github(
                                payloads.check_suite_completed(
                                    repo=pr.repo,
                                    branch=pr.branch or "",
                                    pr_number=pr.pr_number,
                                    conclusion=new_check_conclusion,
                                ),
                                event_type="check_suite",
                            )
                            pr.last_check_status = new_check_status
                            pr.last_check_conclusion = new_check_conclusion
                            pr.last_reported_head_sha = pr.head_sha
                    else:
                        logger.debug(
                            f"{ticket_key}: CI suites still running for {pr.repo}#{pr.pr_number} "
                            f"({new_completed_count}/{total_suites} suites complete)"
                        )
                        pr.last_check_status = None
                        pr.last_check_conclusion = None
                    pr.last_completed_count = new_completed_count
                except Exception as e:
                    logger.warning(f"CI check failed for {ticket_key} {pr.repo}#{pr.pr_number}: {e}")

            # Reviews
            try:
                reviews = await gh.get_reviews(pr.repo, pr.pr_number)
                rev = latest_review(reviews)
                if rev and rev.get("id") != pr.last_review_id:
                    reviewer = rev.get("user", {}).get("login", "")
                    logger.info(f"{ticket_key}: new review on {pr.repo}#{pr.pr_number} by {reviewer}")
                    await forwarder.forward_github(
                        payloads.pr_review_submitted(
                            repo=pr.repo,
                            branch=pr.branch or "",
                            pr_number=pr.pr_number,
                            pr_title=pr.pr_title or "",
                            pr_url=pr.pr_url or "",
                            review_state=rev.get("state", ""),
                            review_body=rev.get("body", "") or "",
                            reviewer_login=reviewer,
                        ),
                        event_type="pull_request_review",
                    )
                    pr.last_review_id = rev.get("id")
            except Exception as e:
                logger.debug(f"Review check failed for {ticket_key} {pr.repo}#{pr.pr_number}: {e}")

            # PR comments
            try:
                pr_comments = await gh.get_issue_comments(pr.repo, pr.pr_number)
                if pr_comments and pr_comments[0].get("id") != pr.last_pr_comment_id:
                    new_comments = []
                    for c in pr_comments:
                        if c.get("id") == pr.last_pr_comment_id:
                            break
                        new_comments.append(c)
                    for c in reversed(new_comments):
                        sender = c.get("user", {}).get("login", "")
                        body = c.get("body", "")
                        bot_login = get_settings().forge_bot_github_login
                        if bot_login and sender == bot_login:
                            logger.debug(f"{ticket_key}: skipping own PR comment on {pr.repo}#{pr.pr_number}")
                            continue
                        logger.info(f"{ticket_key}: new PR comment on {pr.repo}#{pr.pr_number} by {sender}")
                        await forwarder.forward_github(
                            payloads.issue_comment(
                                repo=pr.repo,
                                pr_number=pr.pr_number,
                                comment_body=body,
                                sender_login=sender,
                            ),
                            event_type="issue_comment",
                        )
                    pr.last_pr_comment_id = pr_comments[0].get("id")
            except Exception as e:
                logger.warning(f"PR comment check failed for {ticket_key} {pr.repo}#{pr.pr_number}: {e}")

        if all_merged and prs:
            logger.info(f"{ticket_key}: all {len(prs)} PR(s) merged — removing from watch list")
            await self.remove(ticket_key)
            return

        prd_updates: dict = {}
        spec_updates: dict = {}
        if state.issue_type in ("Feature", "Story"):
            prd_updates = await self._poll_prd_pr(ticket_key, state, comments)
            spec_updates = await self._poll_spec_pr(ticket_key, state, comments, prd_updates)
            await self._sync_epics(ticket_key)

        async with self._lock:
            if ticket_key in self._state:
                self._state[ticket_key] = TicketState(
                    ticket_key=ticket_key,
                    issue_type=state.issue_type,
                    status=new_status,
                    summary=new_summary,
                    labels=new_labels,
                    last_comment_id=new_last_comment_id,
                    prs=prs,
                    prd_pr_repo=prd_updates.get("prd_pr_repo", state.prd_pr_repo),
                    prd_pr_number=prd_updates.get("prd_pr_number", state.prd_pr_number),
                    prd_last_review_id=prd_updates.get("prd_last_review_id", state.prd_last_review_id),
                    prd_last_pr_comment_id=prd_updates.get("prd_last_pr_comment_id", state.prd_last_pr_comment_id),
                    prd_pr_merged=prd_updates.get("prd_pr_merged", state.prd_pr_merged),
                    spec_pr_repo=spec_updates.get("spec_pr_repo", state.spec_pr_repo),
                    spec_pr_number=spec_updates.get("spec_pr_number", state.spec_pr_number),
                    spec_last_review_id=spec_updates.get("spec_last_review_id", state.spec_last_review_id),
                    spec_last_pr_comment_id=spec_updates.get("spec_last_pr_comment_id", state.spec_last_pr_comment_id),
                    spec_pr_merged=spec_updates.get("spec_pr_merged", state.spec_pr_merged),
                    poll_interval_seconds=state.poll_interval_seconds,
                    last_polled_at=state.last_polled_at,
                    next_poll_at=state.next_poll_at,
                    failure_count=state.failure_count,
                )
        if self._state_file:
            save_state(self._state_file, self._state)


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


def _is_archived(labels: set[str]) -> bool:
    return bool(labels & _ARCHIVED_LABELS)


def _walk_adf(node: dict, out: list[str]) -> None:
    if node.get("type") == "text":
        out.append(node.get("text", ""))
        for mark in node.get("marks", []):
            if mark.get("type") == "link":
                href = mark.get("attrs", {}).get("href", "")
                if href:
                    out.append(href)
    elif node.get("type") == "inlineCard":
        url = node.get("attrs", {}).get("url", "")
        if url:
            out.append(url)
    for child in node.get("content", []):
        _walk_adf(child, out)
