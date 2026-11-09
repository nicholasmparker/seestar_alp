from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from ssalp_api_client import SSAlpConnectionError

from seestar_mcp.plan_schedule import PlanTarget
from seestar_mcp.tools import view_plan
from seestar_mcp.tools.view_plan import (
    cancel_plan_tool,
    get_plan_status_tool,
    plan_night_tool,
)

# 17:00 MST on 2026-11-08 is 00:00 UTC on 2026-11-09.
NOW = datetime(2026, 11, 9, 0, 0, 0, tzinfo=UTC)
SCOPE_TIME = {
    "year": 2026,
    "mon": 11,
    "day": 8,
    "hour": 17,
    "min": 0,
    "sec": 0,
    "time_zone": "America/Denver",
}
TARGETS = [
    {
        "name": "IC 1396A",
        "ra": "21:36:33",
        "dec": "+57:30:00",
        "start": "18:25",
        "end": "01:00",
        "lp_filter": True,
    },
    {
        "name": "IC 434",
        "ra": 5.6814,
        "dec": -2.4583,
        "start": "01:00",
        "end": "05:05",
        "lp_filter": False,
    },
]


def _ok(method, result=0):
    return {"jsonrpc": "2.0", "method": method, "result": result, "code": 0, "id": 1}


class FakeScope:
    """Answers method_sync the way ALP relays the scope's JSON-RPC replies."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.settings = {
            "plan_target_af": False,
            "viewplan_gohome": False,
            "exp_ms": {"stack_l": 10000, "continuous": 500},
        }
        self.app_state: dict = {"View": {"state": "idle"}}
        self.time = dict(SCOPE_TIME)
        self.replies: dict[str, object] = {}  # method -> canned reply override
        self.ignore_settings: set[str] = set()
        self.events: dict = {}

    async def method_sync(self, method, params=None):
        self.calls.append((method, params))
        if method in self.replies:
            reply = self.replies[method]
            if isinstance(reply, Exception):
                raise reply
            return reply
        if method == "pi_get_time":
            return _ok(method, self.time)
        if method == "iscope_get_app_state":
            return _ok(method, self.app_state)
        if method == "get_setting":
            return _ok(method, self.settings)
        if method == "set_setting":
            for key, value in params.items():
                if key in self.ignore_settings:
                    continue
                if isinstance(value, dict) and isinstance(self.settings.get(key), dict):
                    self.settings[key].update(value)
                else:
                    self.settings[key] = value
            return _ok(method)
        if method == "set_view_plan":
            self.app_state["ViewPlan"] = {
                "state": "working",
                "lapse_ms": 5,
                "plan": {
                    "plan_name": params["plan_name"],
                    "plan_target_af": self.settings["plan_target_af"],
                    "viewplan_gohome": self.settings["viewplan_gohome"],
                    "list": [
                        {**t, "state": "idle", "lapse_ms": 0} for t in params["list"]
                    ],
                },
            }
            return _ok(method)
        if method == "clear_view_plan":
            node = self.app_state.pop("ViewPlan", None)
            if node:
                self.app_state["ViewPlan"] = {**node, "state": "cancel"}
            return _ok(method)
        raise AssertionError(f"unexpected method {method}")

    async def action(self, action, params=None):
        self.calls.append((action, params))
        assert action == "get_event_state"
        return {"result": self.events.get(params["event_name"], {})}

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture
def scope(monkeypatch):
    fake = FakeScope()
    monkeypatch.setattr("seestar_mcp.server.make_client", MagicMock(return_value=fake))
    monkeypatch.setattr(view_plan, "_utcnow", lambda: NOW)
    return fake


def _targets():
    return [PlanTarget.model_validate(t) for t in TARGETS]


# --- plan_night ------------------------------------------------------------------


async def test_plan_night_without_confirm_previews_and_changes_nothing(scope):
    out = await plan_night_tool(_targets(), exposure_s=20)
    assert out["ok"] is False and out["requires_confirmation"] is True
    assert scope.methods() == ["pi_get_time"]
    assert [t["start"] for t in out["targets"]] == [
        "2026-11-08T18:25:00-07:00",
        "2026-11-09T01:00:00-07:00",
    ]


async def test_plan_night_sends_settings_then_plan(scope):
    out = await plan_night_tool(_targets(), exposure_s=20, confirm=True)
    assert out["ok"] is True, out
    assert out["status"] == "accepted, not done"
    assert out["confirmed"] is True
    sent = [(m, p) for m, p in scope.calls if m in ("set_setting", "set_view_plan")]
    assert sent[0] == ("set_setting", {"exp_ms": {"stack_l": 20000}})
    assert ("set_setting", {"plan_target_af": True}) in sent
    assert ("set_setting", {"viewplan_gohome": True}) in sent
    assert sent[-1][0] == "set_view_plan"
    plan = sent[-1][1]
    assert [(t["start_min"], t["duration_min"]) for t in plan["list"]] == [
        (1105, 395),
        (1500, 245),
    ]
    assert [t["lp_filter"] for t in plan["list"]] == [True, False]
    assert plan["plan_name"] == "Night of 2026-11-08"
    assert out["settings"] == {
        "plan_target_af": True,
        "viewplan_gohome": True,
        "exposure_s": 20,
    }
    assert scope.settings["exp_ms"] == {"stack_l": 20000, "continuous": 500}


async def test_plan_night_leaves_exposure_alone_by_default(scope):
    out = await plan_night_tool(
        _targets(), af_on_switch=False, close_arm=False, confirm=True
    )
    assert out["ok"] is True, out
    assert not any(m == "set_setting" and "exp_ms" in p for m, p in scope.calls if p), (
        scope.calls
    )
    assert out["settings"]["exposure_s"] is None
    assert scope.settings["plan_target_af"] is False


async def test_plan_night_validation_error_sends_nothing(scope):
    bad = [*_targets(), PlanTarget.model_validate({**TARGETS[1], "start": "04:00"})]
    out = await plan_night_tool(bad, confirm=True)
    assert out["ok"] is False
    assert "overlap" in out["error"]
    assert scope.methods() == ["pi_get_time"]


async def test_plan_night_refuses_when_a_plan_is_running(scope):
    scope.app_state["ViewPlan"] = {"state": "working", "plan": {"plan_name": "Old"}}
    out = await plan_night_tool(_targets(), confirm=True)
    assert out["ok"] is False and "cancel_plan" in out["error"]
    assert "set_setting" not in scope.methods()


async def test_plan_night_refuses_when_view_is_running(scope):
    scope.app_state["View"] = {"state": "working", "stage": "Stack"}
    out = await plan_night_tool(_targets(), confirm=True)
    assert out["ok"] is False and "stop_view" in out["error"]
    assert "set_setting" not in scope.methods()


async def test_plan_night_refuses_when_app_state_unreadable(scope):
    scope.replies["iscope_get_app_state"] = SSAlpConnectionError("timed out")
    out = await plan_night_tool(_targets(), confirm=True)
    assert out["ok"] is False and "timed out" in out["error"]
    assert "set_setting" not in scope.methods()


async def test_plan_night_refuses_when_scope_time_unreadable(scope):
    scope.replies["pi_get_time"] = {
        "method": "pi_get_time",
        "error": "fail to operate",
        "code": 207,
    }
    out = await plan_night_tool(_targets(), confirm=True)
    assert out["ok"] is False and "207" in out["error"]
    assert scope.methods() == ["pi_get_time"]


async def test_plan_night_alp_timeout_reply_is_an_error(scope):
    # ALP's method_sync hands back the request with this marker when the scope
    # never answers within its wait.
    scope.replies["pi_get_time"] = {
        "method": "pi_get_time",
        "id": 9,
        "result": "Error: Exceeded allotted wait time for result",
    }
    out = await plan_night_tool(_targets(), confirm=True)
    assert out["ok"] is False and "Exceeded" in out["error"]


async def test_plan_night_falls_back_to_legacy_setting_names(scope):
    scope.ignore_settings = {"plan_target_af", "viewplan_gohome"}
    out = await plan_night_tool(_targets(), confirm=True)
    assert out["ok"] is True, out
    assert ("set_setting", {"plan": {"target_af": True}}) in scope.calls
    assert ("set_setting", {"viewplan_go_home": True}) in scope.calls


async def test_plan_night_does_not_send_plan_when_settings_unconfirmed(scope):
    async def stubborn(method, params=None):
        scope.calls.append((method, params))
        if method == "get_setting":
            return _ok(method, {"plan_target_af": False, "viewplan_gohome": True})
        if method == "set_setting":
            return _ok(method)
        return await FakeScope.method_sync(scope, method, params)

    scope.method_sync = stubborn
    out = await plan_night_tool(_targets(), confirm=True)
    assert out["ok"] is False
    assert "plan_target_af" in out["error"]
    assert "set_view_plan" not in scope.methods()


async def test_plan_night_reports_set_view_plan_rejection(scope):
    scope.replies["set_view_plan"] = {
        "method": "set_view_plan",
        "error": "view plan is already running",
        "code": 536,
    }
    out = await plan_night_tool(_targets(), exposure_s=20, confirm=True)
    assert out["ok"] is False
    assert "536" in out["error"] and "cancel_plan" in out["hint"]
    assert "exposure_s" in out["changed"]


async def test_plan_night_unconfirmed_when_readback_differs(scope):
    original = scope.method_sync

    async def stale(method, params=None):
        if method == "set_view_plan":
            scope.calls.append((method, params))
            return _ok(method)
        return await original(method, params)

    scope.method_sync = stale
    out = await plan_night_tool(_targets(), confirm=True)
    assert out["ok"] is True
    assert out["confirmed"] is False
    assert out["warnings"]


async def test_plan_night_refuses_all_scopes(scope):
    out = await plan_night_tool(_targets(), confirm=True, telescope_id=0)
    assert out["ok"] is False and scope.calls == []


@pytest.mark.parametrize("exposure_s", [0, -5, 601])
async def test_plan_night_refuses_bad_exposure(scope, exposure_s):
    out = await plan_night_tool(_targets(), exposure_s=exposure_s, confirm=True)
    assert out["ok"] is False and scope.calls == []


# --- get_plan_status ---------------------------------------------------------------


async def test_get_plan_status_reports_each_target(scope):
    scope.app_state["ViewPlan"] = {
        "state": "complete",
        "lapse_ms": 1000,
        "plan": {
            "plan_name": "Night",
            "plan_target_af": True,
            "viewplan_gohome": True,
            "list": [
                {
                    "target_id": 1,
                    "target_name": "IC 1396A",
                    "state": "complete",
                    "start_min": 1105,
                    "duration_min": 395,
                    "lp_filter": True,
                    "stack_total_sec": 18000.0,
                },
                {
                    "target_id": 2,
                    "target_name": "IC 434",
                    "state": "fail",
                    "code": 513,
                    "error": "target time out",
                    "start_min": 1500,
                    "duration_min": 245,
                    "lp_filter": False,
                },
            ],
        },
    }
    out = await get_plan_status_tool()
    assert out["ok"] is True and out["source"] == "app_state"
    assert out["state"] == "complete"
    assert out["all_targets_complete"] is False
    assert out["counts"] == {"complete": 1, "fail": 1}
    second = out["targets"][1]
    assert second["name"] == "IC 434" and second["code"] == 513
    assert "time out" in second["code_meaning"]
    assert "does not mean" in out["note"]


async def test_get_plan_status_without_plan(scope):
    out = await get_plan_status_tool()
    assert out["ok"] is True and out["state"] == "none"
    assert out["targets"] == []


async def test_get_plan_status_falls_back_to_cached_event(scope):
    scope.replies["iscope_get_app_state"] = SSAlpConnectionError("timed out")
    scope.events["ViewPlan"] = {
        "Event": "ViewPlan",
        "state": "working",
        "plan": {"plan_name": "Night", "list": [{"target_id": 1, "state": "idle"}]},
    }
    out = await get_plan_status_tool()
    assert out["ok"] is True and out["source"] == "event_cache"
    assert out["state"] == "working"
    assert "timed out" in out["warnings"][0]


async def test_get_plan_status_error_when_nothing_readable(scope):
    scope.replies["iscope_get_app_state"] = SSAlpConnectionError("refused")
    out = await get_plan_status_tool()
    assert out["ok"] is False and "refused" in out["error"]


# --- cancel_plan ---------------------------------------------------------------------


async def test_cancel_plan_clears_and_reports(scope):
    await plan_night_tool(_targets(), confirm=True)
    out = await cancel_plan_tool()
    assert out["ok"] is True
    assert "clear_view_plan" in scope.methods()
    assert out["status"]["state"] == "cancel"
    assert "plan_night" in out["note"]


async def test_cancel_plan_reports_device_error(scope):
    scope.replies["clear_view_plan"] = {
        "method": "clear_view_plan",
        "error": "fail to write file",
        "code": 218,
    }
    out = await cancel_plan_tool()
    assert out["ok"] is False and "218" in out["error"]


async def test_tools_register():
    registered = []

    class Recorder:
        def tool(self, name):
            registered.append(name)
            return lambda fn: fn

    view_plan.register(Recorder())
    assert registered == ["plan_night", "get_plan_status", "cancel_plan"]


async def test_plan_night_through_fastmcp_with_json_targets(scope):
    from seestar_mcp.server import mcp

    content, structured = await mcp.call_tool(
        "plan_night", {"targets": TARGETS, "exposure_s": 20, "confirm": True}
    )
    assert structured["ok"] is True, structured
    assert [t["start_min"] for t in structured["targets"]] == [1105, 1500]
    assert "set_view_plan" in scope.methods()
