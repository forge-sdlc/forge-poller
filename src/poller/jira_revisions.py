"""Validate provider revisions without inventing timestamps or changing tokens."""

from datetime import UTC, datetime


def revision_time(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Jira {field} must be a non-empty timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Jira {field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Jira {field} must include a timezone")
    return parsed.astimezone(UTC)
