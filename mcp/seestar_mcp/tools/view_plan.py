"""On-device night plans: the firmware ViewPlan, which the ZWO app calls a "Plan".

``plan_night`` hands a whole night to the scope. The scope runs it on its own
worker thread, so nothing has to stay connected overnight: this server, ALP and
the network can all go away once the plan is accepted. This is a different
mechanism from ALP's own scheduler (``start_scheduler``), which runs inside ALP.

Firmware behaviour relied on here is from seestar-spec
(spec/control/plan-scheduling.md and spec/control/focus-and-filter.md):

- Exposure and gain are global. A plan target has no exposure or gain member;
  each stack uses the scope's exposure setting at that moment, and gain follows
  the scope's own setting. ``plan_night`` can set the exposure once beforehand.
- "AF on target switch" and "close arm at the end" are the persistent settings
  ``plan_target_af`` and ``viewplan_gohome``. The same-named members of the
  ``set_view_plan`` request are not what the firmware acts on (R9, R29), so
  both settings are written with ``set_setting`` and read back first.
- During a plan, each target's goto leaves the filter wheel on IR-cut and then
  moves it to LP when that target's ``lp_filter`` is true (R6a, R22d).
- The scope sends ViewPlan events only when a plan starts and ends, so status
  is read from ``iscope_get_app_state``. A ``complete`` plan means its loop
  finished, not that every target succeeded (R26).
"""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from seestar_mcp.plan_schedule import (
    PlanError,
    PlanTarget,
    Schedule,
    ScopeClock,
    build_schedule,
    view_plan_params,
)
from seestar_mcp.tools._util import call

MAX_EXPOSURE_S = 600

# Where each plan setting lives, canonical first. The fallbacks mirror the
# compatibility variants front/app.py uses to save the same settings.
SETTING_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "exposure_s": (("exp_ms", "stack_l"),),
    "plan_target_af": (("plan_target_af",), ("plan", "target_af")),
    "viewplan_gohome": (
        ("viewplan_gohome",),
        ("viewplan_go_home",),
        ("viewplan", "go_home"),
    ),
}

TARGET_CODES = {
    204: "skipped",
    253: "cancelled before it ran",
    513: "target time out: its end had passed before it could run",
    537: "cancelled by shutdown",
}

STATE_NOTES = {
    "working": "Running. Targets still 'idle' have not started; the scope sends no "
    "progress events while a plan runs.",
    "complete": "The plan's loop finished. That does not mean every target "
    "succeeded: check all_targets_complete and each target's state and code.",
    "fail": "The plan failed (see code and error).",
    "cancel": "The plan was cancelled; targets it never reached show code 253.",
    "idle": "The scope holds this plan but it is not running.",
}

REJECTION_HINTS = {
    536: "A plan is already running: check get_plan_status, and cancel_plan first "
    "if it should be replaced.",
    268: "The scope's view is running: stop it with stop_view, then retry.",
    212: "The scope stored the plan but could not start it: check get_plan_status.",
}
PARSE_FAILURE_HINT = (
    "The scope rejected the plan's contents and may now hold part of it: run "
    "cancel_plan before retrying."
)

ACCEPTED_NOTE = (
    "The scope now runs this plan by itself; nothing needs to stay connected. Times "
    "are nominal: each target's goto and autofocus come out of its own window, and "
    "a target that overruns makes the next one start late. Every target stacks at "
    "the scope's global exposure and gain. Check get_plan_status: 'complete' does "
    "not mean every target succeeded."
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Reply:
    result: Any = None
    error: str | None = None
    code: int | None = None


def _reply(raw: Any) -> Reply:
    """Unwrap a scope JSON-RPC reply as ALP's method_sync relays it."""
    if not isinstance(raw, dict):
        return Reply(error=f"unexpected reply {raw!r}")
    if raw.get("ok") is False:  # client/transport error mapped by server.call
        return Reply(error=str(raw.get("error")))
    code = raw.get("code")
    if raw.get("error"):
        return Reply(error=f"{raw['error']} (code {code})", code=code)
    if code not in (None, 0):
        return Reply(error=f"device code {code}", code=code)
    if "result" not in raw:
        return Reply(error=f"reply has no result: {raw!r}")
    result = raw["result"]
    if isinstance(result, str) and result.startswith("Error:"):
        return Reply(error=result)  # ALP gave up waiting for the scope
    return Reply(result=result)


async def _sync(telescope_id: int, method: str, params: Any = None) -> Reply:
    return _reply(await call(telescope_id, lambda c: c.method_sync(method, params)))


def _dig(obj: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return None
        obj = obj[key]
    return obj


def _nest(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    out: Any = value
    for key in reversed(path):
        out = {key: out}
    return out


def _refuse(error: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": error, **extra}


async def _write_setting(telescope_id: int, name: str, value: Any) -> str | None:
    """Write a setting, trying each known variant until get_setting reads it back."""
    tried: list[str] = []
    for path in SETTING_PATHS[name]:
        written = await _sync(telescope_id, "set_setting", _nest(path, value))
        readback = await _sync(telescope_id, "get_setting")
        if readback.error is None and _dig(readback.result, path) == value:
            return None
        tried.append(".".join(path) + (f" ({written.error})" if written.error else ""))
    return f"the scope did not keep {name}={value!r} (tried {', '.join(tried)})"


def _busy(app_state: Any) -> str | None:
    if not isinstance(app_state, dict):
        return f"unexpected app state {app_state!r}"
    plan = app_state.get("ViewPlan")
    if isinstance(plan, dict) and plan.get("state") == "working":
        name = _dig(plan, ("plan", "plan_name"))
        return (
            f"a plan is already running ({name!r}); cancel_plan first if it should "
            "be replaced"
        )
    view = app_state.get("View")
    if isinstance(view, dict) and view.get("state") == "working":
        stage = view.get("stage") or "working"
        return f"the scope is busy (View: {stage}); stop it with stop_view first"
    return None


def _summary(
    schedule: Schedule,
    plan_name: str,
    af_on_switch: bool,
    close_arm: bool,
    exposure_s: int | None,
) -> dict[str, Any]:
    clock = schedule.clock
    return {
        "plan_name": plan_name,
        "timezone": schedule.timezone,
        "scope_clock": {"local": clock.wall.isoformat(), "time_zone": clock.time_zone},
        "settings": {
            "plan_target_af": af_on_switch,
            "viewplan_gohome": close_arm,
            "exposure_s": exposure_s,
        },
        "targets": [
            {
                "target_id": s.target_id,
                "name": s.target.name,
                "start": s.start.isoformat(),
                "end": s.end.isoformat(),
                "starts_now": s.starts_now,
                "lp_filter": s.target.lp_filter,
                "ra_hours": round(s.target.ra, 5),
                "dec_deg": round(s.target.dec, 5),
                "start_min": s.start_min,
                "duration_min": s.duration_min,
            }
            for s in schedule.targets
        ],
        "warnings": list(schedule.warnings),
    }


async def _confirm(
    telescope_id: int, params: dict[str, Any]
) -> tuple[bool, str | None, list[str]]:
    """Read the plan back and compare it with what was sent."""
    state = await _sync(telescope_id, "iscope_get_app_state")
    if state.error is not None:
        return (
            False,
            None,
            [f"accepted, but the plan could not be read back: {state.error}"],
        )
    node = state.result.get("ViewPlan") if isinstance(state.result, dict) else None
    if not isinstance(node, dict):
        return False, None, ["accepted, but the scope reports no plan when read back"]
    held = [
        (t.get("start_min"), t.get("duration_min"))
        for t in _dig(node, ("plan", "list")) or []
        if isinstance(t, dict)
    ]
    sent = [(t["start_min"], t["duration_min"]) for t in params["list"]]
    same = (
        node.get("state") == "working"
        and held == sent
        and _dig(node, ("plan", "plan_name")) == params["plan_name"]
    )
    warnings = (
        []
        if same
        else [
            f"read-back differs from what was sent: state {node.get('state')!r}, "
            f"held {held}, sent {sent}"
        ]
    )
    return same, node.get("state"), warnings


async def plan_night_tool(
    targets: list[PlanTarget],
    af_on_switch: bool = True,
    close_arm: bool = True,
    exposure_s: int | None = None,
    timezone: str | None = None,
    plan_name: str | None = None,
    confirm: bool = False,
    telescope_id: int = 1,
) -> dict[str, Any]:
    """Schedule a multi-target night that runs on the scope itself (the app's Plan).

    Each target: name, J2000 ra (hours or 'HH:MM:SS') and dec (degrees or
    '+DD:MM:SS'), start and end as 'HH:MM' in `timezone` (an IANA name such as
    'America/Denver'; default: the scope's own zone), and lp_filter. Targets run
    in list order, must not overlap, and the plan must end within 24 hours; a
    target already under way starts now. Exposure and gain are global, not per
    target: exposure_s (1-600) sets the stack exposure for the whole plan, and
    gain follows the scope's own setting. af_on_switch autofocuses at each target
    and close_arm parks at the end; both are persistent scope settings.

    Without confirm=true nothing is changed and the reply previews the schedule.
    With confirm=true the reply means accepted, not done: check get_plan_status.
    """
    if telescope_id == 0:
        return _refuse(
            "plan_night addresses one scope; telescope_id 0 is not supported"
        )
    if exposure_s is not None and not 1 <= exposure_s <= MAX_EXPOSURE_S:
        return _refuse(f"exposure_s must be 1 to {MAX_EXPOSURE_S} seconds")
    if plan_name is not None and not 1 <= len(plan_name) <= 64:
        return _refuse("plan_name must be 1 to 64 characters")
    try:
        parsed = [
            t if isinstance(t, PlanTarget) else PlanTarget.model_validate(t)
            for t in targets
        ]
    except ValidationError as exc:
        return _refuse(f"plan not sent: {exc}")

    clock_reply = await _sync(telescope_id, "pi_get_time")
    if clock_reply.error is not None:
        return _refuse(
            f"plan not sent: could not read the scope's clock: {clock_reply.error}"
        )
    try:
        clock = ScopeClock.from_pi_get_time(clock_reply.result, _utcnow())
        schedule = build_schedule(parsed, clock, timezone)
    except PlanError as exc:
        return _refuse(f"plan not sent: {exc}", errors=exc.errors)

    # Name the night by the evening it belongs to, even if it starts after midnight.
    evening = schedule.targets[0].start - timedelta(hours=12)
    name = plan_name or f"Night of {evening:%Y-%m-%d}"
    summary = _summary(schedule, name, af_on_switch, close_arm, exposure_s)
    if not confirm:
        return _refuse(
            "plan_night arms an unattended night; review this schedule, then pass "
            "confirm=true after the user confirms. Nothing was changed.",
            requires_confirmation=True,
            **summary,
        )

    app_state = await _sync(telescope_id, "iscope_get_app_state")
    if app_state.error is not None:
        return _refuse(
            f"plan not sent: could not read the scope's state: {app_state.error}",
            **summary,
        )
    busy = _busy(app_state.result)
    if busy:
        return _refuse(f"plan not sent: {busy}", **summary)

    writes: list[tuple[str, Any]] = []
    if exposure_s is not None:
        writes.append(("exposure_s", exposure_s * 1000))
    writes += [("plan_target_af", af_on_switch), ("viewplan_gohome", close_arm)]
    changed: list[str] = []
    for setting, value in writes:
        error = await _write_setting(telescope_id, setting, value)
        if error:
            return _refuse(f"plan not sent: {error}", changed=changed, **summary)
        changed.append(setting)

    params = view_plan_params(schedule, name, af_on_switch, close_arm)
    sent = await _sync(telescope_id, "set_view_plan", params)
    if sent.error is not None:
        hint = REJECTION_HINTS.get(sent.code or 0)
        if hint is None:
            hint = PARSE_FAILURE_HINT if sent.code else "Check get_plan_status."
        return _refuse(
            f"set_view_plan refused: {sent.error}",
            hint=hint,
            changed=changed,
            **summary,
        )

    confirmed, plan_state, warnings = await _confirm(telescope_id, params)
    summary["warnings"] += warnings
    return {
        "ok": True,
        "status": "accepted, not done",
        "confirmed": confirmed,
        "plan_state": plan_state,
        "changed": changed,
        **summary,
        "note": ACCEPTED_NOTE,
    }


def _target_status(t: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "target_id": t.get("target_id"),
        "name": t.get("target_name") or t.get("alias_name"),
        "state": t.get("state", "idle"),
    }
    for key in ("stage", "start_min", "duration_min", "lp_filter", "stack_total_sec"):
        if key in t:
            out[key] = t[key]
    if t.get("code") is not None:
        out["code"] = t["code"]
        out["error"] = t.get("error")
        if t["code"] in TARGET_CODES:
            out["code_meaning"] = TARGET_CODES[t["code"]]
    return out


def _plan_status(
    node: dict[str, Any] | None, source: str, warnings: list[str]
) -> dict[str, Any]:
    if node is None:
        return {
            "ok": True,
            "state": "none",
            "source": source,
            "targets": [],
            "counts": {},
            "all_targets_complete": None,
            "note": "The scope holds no plan.",
            "warnings": warnings,
        }
    plan = node.get("plan") if isinstance(node.get("plan"), dict) else {}
    targets = [_target_status(t) for t in plan.get("list") or [] if isinstance(t, dict)]
    state = node.get("state", "idle")
    out: dict[str, Any] = {
        "ok": True,
        "state": state,
        "source": source,
        "plan_name": plan.get("plan_name"),
        "lapse_ms": node.get("lapse_ms"),
        "plan_target_af": plan.get("plan_target_af"),
        "viewplan_gohome": plan.get("viewplan_gohome"),
        "targets": targets,
        "counts": dict(Counter(t["state"] for t in targets)),
        "all_targets_complete": bool(targets)
        and all(t["state"] == "complete" for t in targets),
        "note": STATE_NOTES.get(state, ""),
        "warnings": warnings,
    }
    if node.get("code") is not None:
        out["code"] = node["code"]
        out["error"] = node.get("error")
    return out


async def _cached_plan_event(telescope_id: int) -> dict[str, Any] | None:
    raw = await call(
        telescope_id,
        lambda c: c.action("get_event_state", {"event_name": "ViewPlan"}),
    )
    event = raw.get("result") if isinstance(raw, dict) else None
    return event if isinstance(event, dict) and event.get("state") else None


async def get_plan_status_tool(telescope_id: int = 1) -> dict[str, Any]:
    """State of the plan on the scope, with each target's state and code.

    'complete' means the plan's loop finished, not that every target succeeded:
    check all_targets_complete and each target's code (513: its end passed before
    it could run; 253: cancelled before it ran; 204: skipped).
    """
    if telescope_id == 0:
        return _refuse(
            "get_plan_status addresses one scope; telescope_id 0 is not supported"
        )
    state = await _sync(telescope_id, "iscope_get_app_state")
    if state.error is None and isinstance(state.result, dict):
        node = state.result.get("ViewPlan")
        return _plan_status(node if isinstance(node, dict) else None, "app_state", [])
    reason = state.error or f"unexpected app state {state.result!r}"
    event = await _cached_plan_event(telescope_id)
    if event is None:
        return _refuse(f"could not read the plan state: {reason}")
    warning = (
        f"live state unavailable ({reason}); this is ALP's last cached ViewPlan "
        "event, and the scope sends those only when a plan starts and ends"
    )
    return _plan_status(event, "event_cache", [warning])


async def cancel_plan_tool(telescope_id: int = 1) -> dict[str, Any]:
    """Stop the plan running on the scope (clear_view_plan).

    On firmware 9.75 this also deletes the stored plan, so it cannot be resumed:
    send it again with plan_night. Whether the arm closes after a cancel is not
    established; check get_device_state and use scope_park if needed.
    """
    if telescope_id == 0:
        return _refuse(
            "cancel_plan addresses one scope; telescope_id 0 is not supported"
        )
    cleared = await _sync(telescope_id, "clear_view_plan")
    if cleared.error is not None:
        return _refuse(
            f"clear_view_plan failed: {cleared.error}",
            hint="Stopping a target mid-capture can take a while; check "
            "get_plan_status before retrying.",
        )
    return {
        "ok": True,
        "result": "cancel requested",
        "status": await get_plan_status_tool(telescope_id),
        "note": "On firmware 9.75 the stored plan is deleted too: send it again "
        "with plan_night. The arm may stay open; use scope_park if needed.",
    }


def register(mcp: Any) -> None:
    for fn in (plan_night_tool, get_plan_status_tool, cancel_plan_tool):
        mcp.tool(name=fn.__name__.removesuffix("_tool"))(fn)
