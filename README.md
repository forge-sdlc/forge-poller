# Forge Poller

A lightweight development tool that watches Jira tickets and automatically forwards events to the [Forge](https://github.com/forge-sdlc/forge) webhook gateway — no public endpoint or webhook configuration required.

## The Problem

Forge is driven by Jira and GitHub webhooks. In production these are delivered automatically, but during local development you either need to expose a public endpoint (ngrok, etc.) or manually `curl` test payloads every time you want to advance a workflow step.

The poller eliminates both. Tell it which tickets to watch, and it polls Jira and GitHub directly, constructing and forwarding the exact webhook payloads Forge expects whenever something changes.

## What It Detects

| Event | Source | What triggers it |
|-------|--------|-----------------|
| Label change | Jira | A `forge:*` label is added or removed (approvals, rejections, retries) |
| New comment | Jira | Any comment posted on the ticket (feedback, Q&A) |
| CI completed | GitHub | Check runs finish on the PR (pass or fail) |
| PR review | GitHub | A review is submitted on the PR |
| PR merged | GitHub | The PR is merged — ticket is automatically removed from the watch list |

GitHub PR discovery is automatic: the poller reads the PR link from Jira remote links once Forge creates the PR, so you never need to provide it manually.

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
cd poller
cp .env.example .env   # fill in your credentials
uv sync
```

## Configuration

Edit `.env`:

```env
JIRA_BASE_URL=https://company.atlassian.net
JIRA_USER_EMAIL=you@example.com
JIRA_API_TOKEN=your-jira-api-token

GITHUB_TOKEN=your-github-token

FORGE_GATEWAY_URL=http://localhost:8000   # where Forge is running
POLL_INTERVAL=30                          # seconds between polls
```

Set `POLL_INTERVAL` lower (e.g. `10`) for faster iteration, higher for quieter logs.

### Beta access (optional)

```env
BETA_INVITE_CODE=your-secret-code
```

When set, the `/watch` endpoint requires an `X-Invite-Code` header matching this value. Requests without a valid code receive a 403. Leave empty or unset to disable the check entirely.

Use `forge-cli register` (see below) instead of raw `curl` to handle the code prompt automatically.

### Persistence (optional)

```env
POLLER_STATE_FILE=./poller-state.json
```

When set, the poller saves the full ticket state (labels, last comment, PR info, CI status) to this JSON file after every change and restores it automatically on startup — so the watch list and all state survive a restart. Leave empty or unset to disable; the watch list starts fresh each run.

## Running

```bash
uv run uvicorn poller.main:app --port 8001
```

## Usage

### forge-cli (beta)

If you have been given a beta invite code, install the CLI and use it instead of raw `curl`:

```bash
pip install git+https://github.com/forge-sdlc/forge-poller-plugin.git
```

Then register a ticket:

```bash
forge-cli register AISOS-123
# Invite code: (hidden prompt)
# Welcome to the Forge beta!
# Ticket AISOS-123 is now being watched.
```

By default the CLI talks to `http://localhost:8001`. Set `FORGE_POLLER_URL` to override.

### Watch a ticket (curl)

```bash
curl -X POST http://localhost:8001/watch \
  -H "Content-Type: application/json" \
  -d '{"tickets": ["AISOS-123"]}'
```

Multiple tickets at once:

```bash
curl -X POST http://localhost:8001/watch \
  -H "Content-Type: application/json" \
  -d '{"tickets": ["AISOS-123", "AISOS-124"]}'
```

### List watched tickets

```bash
curl http://localhost:8001/watch
```

### Stop watching a ticket

```bash
curl -X DELETE http://localhost:8001/watch/AISOS-123
```

Tickets are also automatically removed when their PR is merged.

## Typical Development Workflow

1. Start the Forge worker: `uv run forge worker`
2. Start the poller: `uv run uvicorn poller.main:app --port 8001`
3. Register the ticket you're testing: `POST /watch`
4. Interact normally in Jira (approve, comment, add labels)
5. The poller detects the change within `POLL_INTERVAL` seconds and forwards it to Forge
6. Watch the Forge worker logs to see the workflow advance

## Notes

- The poller only forwards changes detected *after* it starts watching. It snapshots the current state on registration and only fires events for things that change from that point on.
- Signature validation must be disabled in Forge for poller-forwarded events to be accepted. Set `JIRA_WEBHOOK_SECRET=` and `GITHUB_WEBHOOK_SECRET=` (empty) in Forge's `.env`.
- This tool is intended for development only. In production, use real Jira and GitHub webhooks.
