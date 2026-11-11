"""Device/view state tools."""

from typing import Any

from seestar_mcp.tools._util import call


async def get_device_state_tool(telescope_id: int = 1) -> Any:
    """Get the telescope's device state."""
    return await call(telescope_id, lambda c: c.get_device_state())


async def get_view_state_tool(telescope_id: int = 1) -> Any:
    """Get the telescope's current view state: stage, target, stacking progress.

    It does not say which filter is in use. The firmware's View.lp_filter only echoes the
    request that started the view, and ALP always starts a target with it false (the
    scheduler turns the wheel separately), so that field is removed here. For the
    filter, call get_wheel_position: 2 = LP, 1 = IR-cut.
    """
    reply = await call(telescope_id, lambda c: c.get_view_state())
    result = reply.get("result") if isinstance(reply, dict) else None
    view = result.get("View") if isinstance(result, dict) else None
    if isinstance(view, dict) and "lp_filter" in view:
        view = {k: v for k, v in view.items() if k != "lp_filter"}
        reply = {**reply, "result": {**result, "View": view}}
    return reply


def register(mcp: Any) -> None:
    mcp.tool(name="get_device_state")(get_device_state_tool)
    mcp.tool(name="get_view_state")(get_view_state_tool)
