from unittest.mock import AsyncMock, MagicMock

import pytest

from seestar_mcp.tools.state import get_view_state_tool


@pytest.fixture
def mock_client():
    return AsyncMock()


@pytest.fixture
def make_client_spy(monkeypatch, mock_client):
    spy = MagicMock(return_value=mock_client)
    monkeypatch.setattr("seestar_mcp.server.make_client", spy)
    return spy


async def test_view_state_drops_the_start_request_lp_flag(make_client_spy, mock_client):
    # ALP starts every target with lp_filter false, so the echo is not the filter in use.
    mock_client.get_view_state.return_value = {
        "code": 0,
        "result": {"View": {"state": "working", "lp_filter": False, "stage": "Stack"}},
    }
    reply = await get_view_state_tool()
    assert reply == {
        "code": 0,
        "result": {"View": {"state": "working", "stage": "Stack"}},
    }


async def test_view_state_without_the_flag_is_unchanged(make_client_spy, mock_client):
    raw = {"code": 0, "result": {"View": {"state": "idle"}}}
    mock_client.get_view_state.return_value = raw
    assert await get_view_state_tool() == raw


async def test_view_state_errors_pass_through(make_client_spy, mock_client):
    mock_client.get_view_state.return_value = {"ok": False, "error": "timeout"}
    assert await get_view_state_tool() == {"ok": False, "error": "timeout"}
