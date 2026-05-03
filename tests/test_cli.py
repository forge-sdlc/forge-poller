from unittest.mock import MagicMock, patch
from poller.cli import register


def test_register_returns_0_and_prints_success_on_202(capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    with patch("poller.cli.httpx.post", return_value=mock_resp) as mock_post:
        result = register("aisos-123", "http://localhost:8001", "secret")
    assert result == 0
    mock_post.assert_called_once_with(
        "http://localhost:8001/watch",
        json={"tickets": ["AISOS-123"]},
        headers={"X-Invite-Code": "secret"},
    )
    out = capsys.readouterr().out
    assert "Welcome to the Forge beta!" in out
    assert "AISOS-123" in out


def test_register_returns_1_and_prints_failure_on_403(capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    with patch("poller.cli.httpx.post", return_value=mock_resp):
        result = register("AISOS-123", "http://localhost:8001", "wrong")
    assert result == 1
    out = capsys.readouterr().out
    assert "Wrong password" in out
    assert "#forge-sdlc" in out


def test_register_upcases_ticket_id():
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    with patch("poller.cli.httpx.post", return_value=mock_resp) as mock_post:
        register("aisos-123", "http://localhost:8001", "secret")
    assert mock_post.call_args.kwargs["json"] == {"tickets": ["AISOS-123"]}


def test_register_returns_1_on_unexpected_status(capsys):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    with patch("poller.cli.httpx.post", return_value=mock_resp):
        result = register("AISOS-1", "http://localhost:8001", "secret")
    assert result == 1
