import os


def pytest_configure(config):
    # Provide minimal env vars so poller.main can be imported (module-level
    # TicketWatcher.__init__ calls get_settings()) in environments without a .env file.
    os.environ.setdefault("JIRA_BASE_URL", "http://jira.example.com")
    os.environ.setdefault("JIRA_USER_EMAIL", "test@example.com")
    os.environ.setdefault("JIRA_API_TOKEN", "token")
    os.environ.setdefault("GITHUB_TOKEN", "ghtoken")
