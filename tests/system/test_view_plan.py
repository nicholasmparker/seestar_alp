"""The on-device ViewPlan tools (mcp/seestar_mcp/tools/view_plan.py) against real
firmware in the emulator: MCP tool -> ssalp_api_client -> ALP -> firmware.

Emulator only. A plan changes persistent scope settings (plan_target_af,
viewplan_gohome, the stack exposure), and cancelling one deletes the scope's
stored plan on firmware 9.75, so these tests never run against a real Seestar.

Marked smoke, so the per-PR emulator lane (firmware 3.4.0, 32- and 64-bit)
runs it. Targets are scheduled hours ahead and cancelled straight away: the
plan worker only sleeps, so no goto or capture happens.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

pytest.importorskip("seestar_mcp")

from seestar_mcp.plan_schedule import (  # noqa: E402
    PlanTarget,
    ScopeClock,
    build_schedule,
    view_plan_params,
)
from seestar_mcp.server import make_client  # noqa: E402
from seestar_mcp.tools import imaging, view_plan  # noqa: E402

pytestmark = pytest.mark.system

DEVICE_NUM = 1


def _run(coro, timeout=120):
    async def _bounded():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_bounded())


def _method(method, params=None):
    async def _go():
        return await make_client(telescope_id=DEVICE_NUM).method_sync(method, params)

    return _run(_go())


def _wait_view_idle(timeout_s=90):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = _method("iscope_get_app_state")
        view = (last.get("result") or {}).get("View") or {}
        if view.get("state") != "working":
            return
        time.sleep(2)
    pytest.fail(f"the scope's View never went idle: {last}")


def _scope_clock():
    """The scope's clock from pi_get_time, or (None, error) if it cannot be read."""
    reply = _method("pi_get_time")
    if not isinstance(reply, dict) or reply.get("code") not in (None, 0):
        return None, reply
    result = reply.get("result")
    try:
        return ScopeClock.from_pi_get_time(result, datetime.now(UTC)), None
    except ValueError:
        return None, reply


def _zone(name):
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _targets(zone, now, first_start_min=120):
    def hhmm(minutes):
        return (now + timedelta(minutes=minutes)).astimezone(zone).strftime("%H:%M")

    s = first_start_min
    return [
        {
            "name": "M42",
            "ra": "05:35:17",
            "dec": "-05:23:28",
            "start": hhmm(s),
            "end": hhmm(s + 60),
            "lp_filter": True,
        },
        {
            "name": "M45",
            "ra": 3.7833,
            "dec": 24.1167,
            "start": hhmm(s + 60),
            "end": hhmm(s + 120),
            "lp_filter": False,
        },
    ]


@pytest.fixture(scope="module")
def alp(target, running_app):
    if target.kind != "emulator":
        pytest.skip(
            "ViewPlan tests change persistent scope settings and delete the "
            "stored plan; emulator only"
        )
    proc = running_app("classic")
    mp = pytest.MonkeyPatch()
    mp.setenv("SEESTAR_BASE_URL", f"http://127.0.0.1:{proc.alpaca_port}")
    mp.setenv("SSALP_TIMEOUT", "30")  # the emulator answers slowly under QEMU
    _run(view_plan.cancel_plan_tool())  # nothing left over from an earlier run
    _run(imaging.stop_view_tool())
    _wait_view_idle()
    yield proc
    _run(view_plan.cancel_plan_tool())
    mp.undo()


@pytest.mark.smoke
def test_firmware_accepts_view_plan_payload(alp):
    """The set_view_plan payload view_plan_params builds is accepted and held."""
    clock, _ = _scope_clock()
    if clock is None or _zone(clock.time_zone) is None:
        # Still check the payload contract, timed by the host's UTC clock.
        now = datetime.now(UTC)
        clock = ScopeClock(now.replace(tzinfo=None), "Etc/UTC", now)
    zone = _zone(clock.time_zone)
    targets = [
        PlanTarget.model_validate(t)
        for t in _targets(zone, clock.read_at, first_start_min=180)
    ]
    schedule = build_schedule(targets, clock, zone.key)
    params = view_plan_params(schedule, "System test payload", True, True)

    reply = _method("set_view_plan", params)
    assert reply.get("code") == 0, reply

    status = _run(view_plan.get_plan_status_tool())
    assert status["ok"] is True and status["state"] == "working", status
    assert status["plan_name"] == "System test payload", status
    assert [(t["start_min"], t["duration_min"]) for t in status["targets"]] == [
        (t["start_min"], t["duration_min"]) for t in params["list"]
    ], status

    cancelled = _run(view_plan.cancel_plan_tool())
    assert cancelled["ok"] is True, cancelled
    assert cancelled["status"]["state"] != "working", cancelled


@pytest.mark.smoke
def test_plan_night_end_to_end(alp):
    clock, error = _scope_clock()
    if clock is None:
        pytest.skip(f"the emulator cannot report its clock (pi_get_time: {error})")
    if clock.seconds_since_midnight > 86400 - 600:
        pytest.skip("too close to the scope's midnight for a stable test plan")
    zone = _zone(clock.time_zone)
    tz_arg = None if zone is not None else "Etc/UTC"
    targets = _targets(zone or ZoneInfo("Etc/UTC"), clock.read_at)

    preview = _run(view_plan.plan_night_tool(targets, timezone=tz_arg))
    assert preview.get("requires_confirmation") is True, preview

    sent = _run(
        view_plan.plan_night_tool(targets, exposure_s=10, timezone=tz_arg, confirm=True)
    )
    assert sent["ok"] is True, sent
    assert sent["status"] == "accepted, not done"
    assert sent["confirmed"] is True, sent

    status = _run(view_plan.get_plan_status_tool())
    assert status["state"] == "working", status
    assert [t["name"] for t in status["targets"]] == ["M42", "M45"], status

    again = _run(view_plan.plan_night_tool(targets, timezone=tz_arg, confirm=True))
    assert again["ok"] is False and "already running" in again["error"], again

    cancelled = _run(view_plan.cancel_plan_tool())
    assert cancelled["ok"] is True, cancelled
    assert cancelled["status"]["state"] != "working", cancelled
