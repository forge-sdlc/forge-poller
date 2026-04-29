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
        description="Polling interval in seconds",
    )
    forge_bot_account_id: str = Field(
        default="",
        description="Jira account ID of the Forge service account — comments from this account are ignored",
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
