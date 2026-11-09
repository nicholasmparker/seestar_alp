"""End-to-end test of the seestar-mcp tools against the simulator.

Path under test: MCP tool -> ssalp_api_client -> real HTTP -> seestar_alp Alpaca
device server (served on a real TCP port) -> simulator.
"""

import asyncio
import logging
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("seestar_mcp")
pytest.importorskip("falcon")

import device.app as device_app  # noqa: E402
from device.config import Config  # noqa: E402
from seestar_mcp.tools import (  # noqa: E402
    pointing,
    scheduling,
    settings,
    state,
    system,
    view_plan,
)

SIMULATOR_SRC = Path(__file__).resolve().parents[2] / "simulator" / "src"
if str(SIMULATOR_SRC) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_SRC))

from listener import SocketListener  # noqa: E402

pytestmark = pytest.mark.integration

HOST = "127.0.0.1"


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def _wait_for_tcp(host, port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(0.05)
    raise TimeoutError(f"{host}:{port} did not open in time")


class _NoDiscovery:
    def __init__(self, *args, **kwargs):
        pass


@pytest.fixture(scope="module")
def device_server():
    """Boot the simulator, then the real Alpaca device server pointed at it."""
    sim_tcp, sim_udp, api_port = _find_free_port(), _find_free_port(), _find_free_port()
    logger = logging.getLogger("mcp-simulator-e2e")
    listener = SocketListener(logger, host=HOST, tcp_port=sim_tcp, udp_port=sim_udp)
    sim_thread = threading.Thread(target=listener._start_socket_listener, daemon=True)
    sim_thread.start()
    _wait_for_tcp(HOST, sim_tcp)

    mp = pytest.MonkeyPatch()
    # Don't read/write the developer's config.toml; configure in memory.
    mp.setattr(Config, "load_toml", lambda *a, **k: None)
    mp.setattr(Config, "ip_address", HOST)
    mp.setattr(Config, "port", api_port)
    mp.setattr(
        Config,
        "seestars",
        [
            {
                "device_num": 1,
                "name": "Seestar Sim",
                "ip_address": HOST,
                "simulator": True,
            }
        ],
    )
    # DeviceMain hardcodes the seestar port 4700; redirect to the simulator.
    real_start = device_app.telescope.start_seestar_device
    mp.setattr(
        device_app.telescope,
        "start_seestar_device",
        lambda logger, name, ip, port, num: real_start(logger, name, ip, sim_tcp, num),
    )
    mp.setattr(device_app, "DiscoveryResponder", _NoDiscovery)

    main = device_app.DeviceMain()
    api_thread = threading.Thread(target=main.start, daemon=True)
    api_thread.start()
    try:
        _wait_for_tcp(HOST, api_port, timeout=30.0)
    except TimeoutError:
        mp.undo()
        listener.shutdown_event.set()
        pytest.skip("Alpaca device server could not be booted in this environment")

    yield {"base_url": f"http://{HOST}:{api_port}", "listener": listener}

    main.stop()
    api_thread.join(timeout=5)
    mp.undo()
    listener.shutdown_event.set()
    for sock in (listener.tcp_socket, listener.udp_socket):
        if sock:
            sock.close()
    sim_thread.join(timeout=2)


@pytest.fixture
def tools_env(monkeypatch, device_server):
    monkeypatch.setenv("SEESTAR_BASE_URL", device_server["base_url"])
    return device_server


def _run(coro, timeout=30):
    async def _bounded():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(_bounded())


def _assert_ok(result):
    assert result is not None, "tool returned None"
    assert not (isinstance(result, dict) and result.get("ok") is False), result


def _find_key(obj, key):
    """Return the value of the first occurrence of key in nested dicts/lists."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


def test_get_device_state_returns_dict(tools_env):
    result = _run(state.get_device_state_tool())
    _assert_ok(result)
    assert isinstance(result, dict)
    assert result.get("method") == "get_device_state", result
    assert result["result"]["device"]["sn"] == "simulated123", result


def test_goto_target_round_trip(tools_env):
    goto = _run(pointing.goto_target_tool("M31", 0.712, 41.27))
    _assert_ok(goto)
    done = _run(pointing.is_goto_completed_ok_tool())
    _assert_ok(done)
    assert isinstance(done, bool), done


def test_read_and_write_settings(tools_env):
    before = _run(settings.read_settings_tool())
    assert before.get("setting") is not None or before.get("stack_setting") is not None
    write = _run(settings.write_setting_tool({"stack_lenhance": True}))
    _assert_ok(write)
    after = _run(settings.read_settings_tool())
    # The simulator persists the field; confirm the write is reflected on read.
    # Check the field in each read path's own result payload (either may carry it).
    seen = [
        _find_key(after[k].get("result"), "stack_lenhance")
        for k in ("setting", "stack_setting")
        if isinstance(after.get(k), dict)
    ]
    assert True in seen, (seen, after)


def test_schedule_flow(tools_env):
    created = _run(scheduling.create_schedule_tool())
    assert not (isinstance(created, dict) and created.get("ok") is False), created
    added = _run(scheduling.add_schedule_item_tool("wait_for", {"timer_sec": 1}))
    assert not (isinstance(added, dict) and added.get("ok") is False), added
    schedule = _run(scheduling.get_schedule_tool())
    assert "wait_for" in repr(schedule)
    refused = _run(scheduling.start_scheduler_tool())
    assert refused["ok"] is False and refused["requires_confirmation"] is True
    for fn in (
        lambda: scheduling.start_scheduler_tool(confirm=True),
        scheduling.stop_scheduler_tool,
    ):
        res = _run(fn())
        assert not (isinstance(res, dict) and res.get("ok") is False), res


def test_shutdown_without_confirm_makes_no_device_call(tools_env, monkeypatch):
    import seestar_mcp.tools.system as system_mod

    def _boom(*a, **k):
        raise AssertionError("device call made for unconfirmed shutdown")

    monkeypatch.setattr(system_mod, "call", _boom)
    result = _run(system.shutdown_tool(confirm=False))
    assert result["requires_confirmation"] is True
    assert result["ok"] is False


def test_view_plan_round_trip(tools_env):
    """plan_night -> get_plan_status -> cancel_plan through ALP to the simulator."""
    from seestar_mcp.plan_schedule import default_night

    now = datetime.now(UTC)
    if now.hour == 23 and now.minute >= 55:
        pytest.skip("plan_night refuses the minutes before the scope's midnight")
    # A night runs noon to noon, so keep the 2-hour plan clear of noon (UTC,
    # the simulator's zone).
    base = now + timedelta(minutes=30)
    noon = base.replace(hour=12, minute=0, second=0, microsecond=0)
    if base < noon < base + timedelta(minutes=125):
        base = noon + timedelta(minutes=5)
    night = default_night(base, UTC).isoformat()

    def hhmm(minutes):
        return (base + timedelta(minutes=minutes - 30)).strftime("%H:%M")

    targets = [
        {
            "name": "M42",
            "ra": "05:35:17",
            "dec": "-05:23:28",
            "start": hhmm(30),
            "end": hhmm(90),
            "lp_filter": True,
        },
        {
            "name": "M45",
            "ra": 3.7833,
            "dec": 24.1167,
            "start": hhmm(90),
            "end": hhmm(150),
            "lp_filter": False,
        },
    ]

    preview = _run(
        view_plan.plan_night_tool(targets, timezone="Etc/UTC", night_of=night)
    )
    assert preview["requires_confirmation"] is True, preview
    assert _run(view_plan.get_plan_status_tool())["state"] == "none"

    sent = _run(
        view_plan.plan_night_tool(
            targets, exposure_s=20, timezone="Etc/UTC", night_of=night, confirm=True
        )
    )
    assert sent["ok"] is True, sent
    assert sent["status"] == "accepted, not done"
    assert sent["confirmed"] is True, sent
    assert [t["duration_min"] for t in sent["targets"]] == [60, 60]

    status = _run(view_plan.get_plan_status_tool())
    assert status["state"] == "working", status
    assert [t["name"] for t in status["targets"]] == ["M42", "M45"]
    assert status["plan_target_af"] is True and status["viewplan_gohome"] is True

    written = _run(settings.read_settings_tool())["setting"]["result"]
    assert written["exp_ms"]["stack_l"] == 20000, written

    again = _run(
        view_plan.plan_night_tool(
            targets, timezone="Etc/UTC", night_of=night, confirm=True
        )
    )
    assert again["ok"] is False and "already running" in again["error"]

    cancelled = _run(view_plan.cancel_plan_tool())
    assert cancelled["ok"] is True, cancelled
    assert cancelled["status"]["state"] == "cancel"
    assert {t["code"] for t in cancelled["status"]["targets"]} == {253}
