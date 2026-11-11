from unittest.mock import AsyncMock, MagicMock

import pytest

from seestar_mcp.tools.filter_wheel import (
    get_wheel_position_tool,
    set_lp_filter_tool,
    set_wheel_position_tool,
)


@pytest.fixture
def mock_client():
    return AsyncMock()


@pytest.fixture
def make_client_spy(monkeypatch, mock_client):
    spy = MagicMock(return_value=mock_client)
    monkeypatch.setattr("seestar_mcp.server.make_client", spy)
    return spy


async def test_get_wheel_position(make_client_spy, mock_client):
    await get_wheel_position_tool(telescope_id=0)
    make_client_spy.assert_called_once_with(telescope_id=0)
    mock_client.get_wheel_position.assert_awaited_once_with()


async def test_set_wheel_position(make_client_spy, mock_client):
    await set_wheel_position_tool(2)
    mock_client.set_wheel_position.assert_awaited_once_with(2)


async def test_set_lp_filter(make_client_spy, mock_client):
    await set_lp_filter_tool(True)
    mock_client.set_lp_filter.assert_awaited_once_with(True)


async def test_get_wheel_position_names_the_filter(make_client_spy, mock_client):
    mock_client.get_wheel_position.return_value = {"result": 2, "code": 0}
    reply = await get_wheel_position_tool()
    assert reply == {"result": 2, "code": 0, "filter": "LP"}


@pytest.mark.parametrize(
    ("position", "name"),
    [
        (0, "dark"),
        (1, "IR-cut"),
        (2, "LP"),
        (5, "unknown"),
        (None, "unknown"),
        (True, "unknown"),
    ],
)
async def test_get_wheel_position_filter_names(
    make_client_spy, mock_client, position, name
):
    mock_client.get_wheel_position.return_value = {"result": position}
    assert (await get_wheel_position_tool())["filter"] == name


async def test_get_wheel_position_passes_errors_through(make_client_spy, mock_client):
    mock_client.get_wheel_position.return_value = {"ok": False, "error": "timeout"}
    assert await get_wheel_position_tool() == {"ok": False, "error": "timeout"}
