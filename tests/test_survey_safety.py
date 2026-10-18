"""Failures here catch unsafe acceptance, not wording or private structure."""

import math
import pytest

from device import survey_safety as safety


def native(result=None, **changes):
    return {"id": 23, "code": 0, "method": "read", "result": result, **changes}


def state():
    return {
        "mount": {
            "close": True,
            "move_type": "none",
            "tracking": False,
            "equ_mode": False,
        },
        "pi_status": {
            "battery_capacity": 70,
            "is_overtemp": False,
            "is_undervolt": False,
        },
        "balance_sensor": {"code": 0, "data": {"angle": 0.4}},
    }


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "timeout",
        {},
        native(code=False),
        native(id=True),
        native(id=0),
        native(code=210),
        native(error={"code": 210}),
        native(method="other"),
    ],
)
def test_native_rejects_invalid_or_wrong_reply(reply):
    with pytest.raises(safety.SurveyError):
        safety.native_result(reply, "read")


def test_native_preserves_successful_false_result():
    assert safety.native_result(native(False), "read") is False


@pytest.mark.parametrize(
    "value",
    [[True, 12], [math.nan, 12], [0, math.inf], [0], [-91, 12], [20, 361], "timeout"],
)
def test_horizon_rejects_invalid_coordinates(value):
    with pytest.raises(safety.SurveyError):
        safety.horizon(value)


def test_horizon_uses_altitude_first_and_accepts_named_fields():
    assert safety.horizon([25, 270]) == (25, 270)
    assert safety.horizon({"alt": 25, "az": 270}) == (25, 270)


@pytest.mark.parametrize(
    "sun,fov,level",
    [
        (-18, 2, 0),
        (-17.99, 2, 0),
        (-0.1, 2, 0),
        (30, 2, 0),
        (-90.1, 2, 0),
        (-50, 3.1, 0),
        (-50, 2, 1.1),
        (math.nan, 2, 0),
        (math.inf, 2, 0),
        (-math.inf, 2, 0),
        (False, 2, 0),
        (-50, 0, 0),
        (-50, 2, -1),
        (-50, 2, True),
    ],
)
def test_sun_gate_rejects_unsafe_unknown_and_boundary(sun, fov, level):
    with pytest.raises(safety.SurveyError):
        safety.sun_gate(sun, fov, level)


@pytest.mark.parametrize("sun", [-18.01, -24.36, -40, -90])
def test_sun_gate_accepts_astronomical_night_for_ground_based_setup(sun):
    safety.sun_gate(sun, 3, 1)


@pytest.mark.parametrize(
    "part,key,value",
    [
        ("mount", "tracking", True),
        ("mount", "equ_mode", True),
        ("mount", "move_type", "goto"),
        ("mount", "close", None),
        ("pi_status", "battery_capacity", 19),
        ("pi_status", "is_overtemp", True),
        ("pi_status", "is_undervolt", True),
    ],
)
def test_preflight_does_not_take_over_unsafe_mount(part, key, value):
    sample = state()
    sample[part][key] = value
    with pytest.raises(safety.SurveyError):
        safety.preflight_state(
            sample, {"state": "stopped"}, {"View": {"state": "idle"}}
        )


def test_preflight_rejects_missing_state_and_active_scheduler_or_stack():
    for sample, schedule, view in [
        ({}, {"state": "stopped"}, {"View": {"state": "idle"}}),
        (state(), {"state": "working"}, {"View": {"state": "idle"}}),
        (
            state(),
            {"state": "stopped"},
            {"View": {"state": "working", "stage": "Stack"}},
        ),
    ]:
        with pytest.raises(safety.SurveyError):
            safety.preflight_state(sample, schedule, view)


def test_preflight_accepts_explicit_idle_or_authorized_preview():
    safety.preflight_state(state(), {"state": "stopped"}, {"View": {"state": "idle"}})
    view = {"View": {"state": "working", "stage": "ContinuousExposure", "mode": "star"}}
    with pytest.raises(safety.SurveyError):
        safety.preflight_state(state(), {"state": "stopped"}, view)
    safety.preflight_state(state(), {"state": "stopped"}, view, allow_preview=True)


def test_absent_optional_undervoltage_flag_is_not_invented_as_an_alarm():
    sample = state()
    del sample["pi_status"]["is_undervolt"]
    safety.preflight_state(sample, {"state": "stopped"}, {"View": {"state": "idle"}})


def test_pulse_uses_shortest_azimuth_direction_and_is_hardware_bounded():
    axes = {"alt_positive": 90, "az_positive": 180}
    assert safety.pulse_for((40, 359), (40, 1), axes) == {
        "speed": 4000,
        "angle": 180,
        "dur_sec": 1,
    }
    assert safety.pulse_for((40, 1), (40, 359), axes) == {
        "speed": 4000,
        "angle": 0,
        "dur_sec": 1,
    }
    pulse = safety.pulse_for((20, 0), (60, 270), axes)
    assert pulse == {"speed": 4000, "angle": 90, "dur_sec": 2}
    assert safety.pulse_for((40, 1), (40.2, 1.2), axes) is None


@pytest.mark.parametrize("target", [(19, 10), (66, 10), (20, math.nan), (20, 361)])
def test_pulse_rejects_out_of_envelope_targets(target):
    with pytest.raises(safety.SurveyError):
        safety.pulse_for((40, 90), target, {"alt_positive": 90, "az_positive": 180})


def test_displacement_rejects_runaway_and_wrong_axis():
    safety.validate_step((40, 359), (40, 1), "az", 1)
    for end in [(40, 10), (43, 1), (40, 358)]:
        with pytest.raises(safety.SurveyError):
            safety.validate_step((40, 359), end, "az", 1)


def test_fresh_event_requires_new_timestamp_and_success():
    old = {"Timestamp": "42", "state": "complete", "ra_dec": [12, 30]}
    assert safety.fresh_terminal(old, 42.0) is None
    assert safety.fresh_terminal({**old, "Timestamp": "43"}, 42.0) == "complete"
    assert (
        safety.fresh_terminal({**old, "Timestamp": "44", "state": "fail"}, 42.0)
        == "fail"
    )
    assert safety.fresh_terminal({**old, "Timestamp": None}, 42.0) is None


@pytest.mark.parametrize("baseline", [None, {}, "nan", float("nan"), True, -1])
def test_missing_or_malformed_command_anchor_never_accepts_a_late_event(baseline):
    assert (
        safety.fresh_terminal({"Timestamp": "1", "state": "complete"}, baseline) is None
    )
