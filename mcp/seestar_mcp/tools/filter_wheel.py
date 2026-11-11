"""Filter-wheel tools."""

from typing import Any

from seestar_mcp.tools._util import call

# Wheel positions as the firmware reports them. Observed 2026-11-10 on an S30 Pro:
# position 2 while every sub was tagged _LP_ with FITS FILTER = 'LP'; the scope turns
# the wheel to 0 while it builds a dark frame and to 1 for IR-cut.
WHEEL_FILTERS = {0: "dark", 1: "IR-cut", 2: "LP"}


def filter_name(position: Any) -> str:
    """Name of the filter at a wheel position, or "unknown"."""
    if isinstance(position, int) and not isinstance(position, bool):
        return WHEEL_FILTERS.get(position, "unknown")
    return "unknown"


async def get_wheel_position_tool(telescope_id: int = 1) -> Any:
    """Which filter is in the light path now. This is the check for "is the LP filter on".

    Positions: 0 = dark (only while the scope builds a dark frame), 1 = IR-cut, 2 = LP.
    The reply's `filter` field names the position. get_view_state does not report the
    filter.
    """
    reply = await call(telescope_id, lambda c: c.get_wheel_position())
    if isinstance(reply, dict) and "result" in reply:
        return {**reply, "filter": filter_name(reply["result"])}
    return reply


async def get_wheel_state_tool(telescope_id: int = 1) -> Any:
    """Get the filter-wheel state (for which filter is in place, use get_wheel_position)."""
    return await call(telescope_id, lambda c: c.get_wheel_state())


async def get_wheel_setting_tool(telescope_id: int = 1) -> Any:
    """Get the filter-wheel setting."""
    return await call(telescope_id, lambda c: c.get_wheel_setting())


async def set_wheel_position_tool(position: int, telescope_id: int = 1) -> Any:
    """Move the filter wheel directly: 0 = dark, 1 = IR-cut, 2 = LP.

    For imaging, use set_lp_filter or a schedule item's is_use_lp_filter instead. Moving
    the wheel during a stack changes the filter of the frames being taken.
    """
    return await call(telescope_id, lambda c: c.set_wheel_position(position))


async def set_lp_filter_tool(enabled: bool, telescope_id: int = 1) -> Any:
    """Turn the LP (light-pollution / duo-band) filter on or off for stacking.

    Sets the scope's stack_lenhance setting; the wheel turns to 2 (LP) or 1 (IR-cut).
    A schedule item with is_use_lp_filter already sets and checks this before it stacks,
    so read get_wheel_position first: when it already says 2 / LP, nothing needs doing.
    """
    return await call(telescope_id, lambda c: c.set_lp_filter(enabled))


def register(mcp: Any) -> None:
    for fn in (
        get_wheel_position_tool,
        get_wheel_state_tool,
        get_wheel_setting_tool,
        set_wheel_position_tool,
        set_lp_filter_tool,
    ):
        mcp.tool(name=fn.__name__.removesuffix("_tool"))(fn)
