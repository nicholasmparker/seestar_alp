"""Fail-closed, side-effect-free contracts for a coarse nighttime survey."""

import math

SNAPSHOT_TIMEOUT_SECONDS = 45
ASTRONOMICAL_NIGHT_SUN_ALTITUDE_DEG = -18
# Native speed is 15-arcsecond steps/sec: 240 ~= 1 degree/sec, not 240 arcsec/sec.
SURVEY_MAX_SPEED = 240
SURVEY_CALIBRATION_SPEED = 120
SURVEY_FINE_SPEED = 60


class SurveyError(RuntimeError):
    """A stable, sanitized failure code; never pass raw device payloads here."""


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def native_result(reply, method):
    if (
        not isinstance(reply, dict)
        or type(reply.get("id")) is not int
        or reply["id"] <= 0
        or type(reply.get("code")) is not int
        or reply["code"] != 0
        or reply.get("error")
        or reply.get("method") != method
        or "result" not in reply
    ):
        raise SurveyError("invalid_native_reply")
    return reply["result"]


def horizon(result):
    if isinstance(result, dict):
        values = result.get("alt"), result.get("az")
    elif isinstance(result, list) and len(result) == 2:
        values = result
    else:
        raise SurveyError("invalid_horizon_reply")
    alt, az = values
    if not number(alt) or not number(az) or not -90 <= alt <= 90 or not 0 <= az <= 360:
        raise SurveyError("invalid_horizon_reply")
    return float(alt), float(az) % 360


def sun_gate(sun_alt, half_fov, level):
    """Night-only gate for the explicitly cleared, ground-based tripod setup.

    Earth occludes direct sunlight even during unfolding. This is an
    astronomical-darkness criterion, not an angular Sun-avoidance envelope;
    it must not be reused to authorize daytime or airborne operation.
    """
    if not all(number(v) for v in (sun_alt, half_fov, level)):
        raise SurveyError("unknown_sun_envelope")
    if not -90 <= sun_alt < ASTRONOMICAL_NIGHT_SUN_ALTITUDE_DEG:
        raise SurveyError("waiting_for_safe_darkness")
    if not 0 < half_fov <= 3 or not 0 <= level <= 1:
        raise SurveyError("unsafe_optics_or_level")


def preflight_state(state, schedule, view, *, allow_preview=False):
    if not all(isinstance(v, dict) for v in (state, schedule, view)):
        raise SurveyError("unknown_operating_state")
    mount, power = state.get("mount", {}), state.get("pi_status", {})
    if not isinstance(mount, dict) or not isinstance(power, dict):
        raise SurveyError("unknown_operating_state")
    if (
        type(mount.get("close")) is not bool
        or mount.get("equ_mode") is not False
        or mount.get("tracking") is not False
        or mount.get("move_type") != "none"
    ):
        raise SurveyError("mount_not_idle_altaz")
    battery = power.get("battery_capacity")
    if (
        not number(battery)
        or not 20 <= battery <= 100
        or power.get("is_overtemp") is not False
        or power.get("is_undervolt") not in (None, False)
    ):
        raise SurveyError("unsafe_or_unknown_power")
    if schedule.get("state") not in ("stopped", "complete", "idle"):
        raise SurveyError("scheduler_not_idle")
    current = view.get("View")
    if not isinstance(current, dict):
        raise SurveyError("unknown_view_state")
    if current.get("state") in ("idle", "complete", "stopped") and not current.get(
        "error"
    ):
        return
    if (
        allow_preview
        and current.get("state") == "working"
        and current.get("stage") == "ContinuousExposure"
        and current.get("mode") == "star"
        and not current.get("error")
    ):
        return
    raise SurveyError("active_or_unknown_view")


def az_delta(start, end):
    return (end - start + 180) % 360 - 180


def pulse_for(current, target, axes):
    alt, az = horizon(list(current))
    target_alt, target_az = horizon(list(target))
    if not 20 <= target_alt <= 65 or not -5 <= alt <= 90:
        raise SurveyError("target_outside_envelope")
    if axes.get("alt_positive") not in (90, 270) or axes.get("az_positive") not in (
        0,
        180,
    ):
        raise SurveyError("axes_not_calibrated")
    alt_error, az_error = target_alt - alt, az_delta(az, target_az)
    if abs(alt_error) > 0.6:
        error, angle = alt_error, axes["alt_positive"]
    elif abs(az_error) > 0.6:
        error, angle = az_error, axes["az_positive"]
    else:
        return None
    if error < 0:
        angle = (angle + 180) % 360
    return {
        "speed": SURVEY_MAX_SPEED if abs(error) >= 1.5 else SURVEY_FINE_SPEED,
        "angle": angle,
        "dur_sec": 2 if abs(error) > 3 else 1,
    }


def validate_step(before, after, axis, direction):
    before, after = horizon(list(before)), horizon(list(after))
    da, dz = after[0] - before[0], az_delta(before[1], after[1])
    major, minor = (da, dz) if axis == "alt" else (dz, da)
    if (
        axis not in ("alt", "az")
        or direction not in (-1, 1)
        or not -5 <= after[0] <= 90
        or abs(major) > 4
        or abs(minor) > 0.3
        or major * direction < 0.03
    ):
        raise SurveyError("unexpected_pulse_motion")


def fresh_terminal(event, command_timestamp):
    if (
        not isinstance(event, dict)
        or not number(command_timestamp)
        or command_timestamp < 0
    ):
        return None
    stamp = event.get("Timestamp")
    try:
        stamp_value = float(stamp)
    except (TypeError, ValueError):
        return None
    if (
        type(stamp) is bool
        or not math.isfinite(stamp_value)
        or stamp_value <= command_timestamp
    ):
        return None
    state = event.get("state")
    return state if state in ("complete", "fail", "cancel") else None
