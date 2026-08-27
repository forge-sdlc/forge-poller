from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    jira_base_url: str = Field(description="Jira instance URL")
    jira_user_email: str = Field(description="Jira user email")
    jira_api_token: str = Field(description="Jira API token")

    github_token: str = Field(description="GitHub personal access token")

    forge_gateway_url: str = Field(
        default="http://localhost:8000",
        description="Base URL of the Forge API gateway",
    )
    poll_interval: int = Field(
        default=30,
        description="Base polling interval in seconds",
    )
    poller_max_concurrency: int = Field(
        default=8,
        ge=1,
        description="Maximum number of tickets to poll concurrently",
    )
    poller_max_poll_interval: int = Field(
        default=300,
        ge=1,
        description="Maximum adaptive polling interval in seconds",
    )
    poller_jitter_ratio: float = Field(
        default=0.2,
        ge=0,
        le=1,
        description="Random jitter ratio applied when rescheduling tickets",
    )
    forge_bot_account_id: str = Field(
        default="",
        description=(
            "Deprecated: unused. Jira ticket comments are always forwarded; "
            "Forge ignores non-command bodies."
        ),
    )
    forge_bot_github_login: str = Field(
        default="",
        description="GitHub login of the Forge bot — PR comments from this account are ignored",
    )
    beta_invite_code: str = Field(
        default="",
        description="Shared invite code for beta access — leave empty to disable the check",
    )
    poller_state_file: str = Field(
        default="",
        description="Path to JSON state file for persistence — leave empty to disable",
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
