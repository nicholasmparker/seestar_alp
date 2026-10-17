import base64
import copy
import json
import math
import os
from datetime import datetime, timezone

import cv2
import numpy as np
import pytest

from device import night_survey as survey
from device.survey_safety import SurveyError


def device_state():
    return {
        "device": {"product_model": "Seestar S30 Pro"},
        "mount": {
            "close": True,
            "move_type": "none",
            "tracking": False,
            "equ_mode": False,
        },
        "pi_status": {"battery_capacity": 90, "is_overtemp": False},
        "balance_sensor": {"code": 0, "data": {"angle": 0.5}},
        "camera": {"chip_size": [2160, 3840], "pixel_size_um": 2.9, "focal_len": 160.0},
        "setting": {"wide_cam": False},
        "location_lon_lat": [-105.0, 35.0],
    }


class Clock:
    def __init__(self):
        self.elapsed = 0.0

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds

    def now(self):
        from datetime import timedelta

        return datetime(2026, 10, 18, 6, tzinfo=timezone.utc) + timedelta(
            seconds=self.elapsed
        )


class Scope:
    """A timed protocol fixture, not a replacement for runner behavior."""

    def __init__(self, clock):
        self.clock = clock
        self.state = device_state()
        self.position = [0.0, 180.0]
        self.view = {
            "View": {"state": "working", "mode": "star", "stage": "ContinuousExposure"}
        }
        self.events = {}
        self.writes = []
        self.fail_motion = False
        self.solve_success = True

    def native(self, method, params=None):
        self.clock.sleep(0.05)
        if method == "get_device_state":
            return copy.deepcopy(self.state)
        if method == "pi_is_verified":
            return True
        if method == "pi_get_time":
            dt = self.clock.now()
            return {
                "year": dt.year,
                "mon": dt.month,
                "day": dt.day,
                "hour": dt.hour,
                "min": dt.minute,
                "sec": dt.second,
                "time_zone": "UTC",
            }
        if method == "scope_get_horiz_coord":
            return list(self.position)
        if method == "get_view_state":
            return copy.deepcopy(self.view)
        self.writes.append((method, params))
        if method == "scope_move_to_horizon":
            self.state["mount"]["close"] = False
            self.events["ScopeMoveToHorizon"] = {
                "Timestamp": str(self.clock.elapsed),
                "state": "complete",
            }
        elif method == "scope_speed_move":
            if self.fail_motion:
                raise SurveyError("transport_uncertain")
            speed = params["speed"] / 3600 * params["dur_sec"]
            if params["angle"] == 90:
                self.position[0] += speed
            elif params["angle"] == 270:
                self.position[0] = max(0, self.position[0] - speed)
            elif params["angle"] == 180:
                self.position[1] = (self.position[1] + speed) % 360
            elif params["angle"] == 0:
                self.position[1] = (self.position[1] - speed) % 360
        elif method == "start_solve":
            ra, dec = survey.to_equatorial(
                tuple(self.position), (-105, 35), self.clock.now()
            )
            self.events["PlateSolve"] = {
                "Timestamp": str(self.clock.elapsed),
                "state": "complete" if self.solve_success else "fail",
                "result": {"ra_dec": [ra, dec], "star_number": 40, "fov": [4.0, 2.25]},
            }
        elif method == "iscope_start_view":
            pass
        elif method == "iscope_stop_view":
            self.view = {"View": {"state": "idle"}}
        elif method == "scope_set_track_state":
            self.state["mount"]["tracking"] = params
        else:
            raise AssertionError("Unexpected mutation: " + method)
        return 0

    def action(self, name, params=None):
        if name == "get_schedule":
            return {"state": "stopped"}
        if name == "get_event_state":
            return {
                "code": 0,
                "result": copy.deepcopy(self.events.get(params["event_name"], {})),
            }
        if name == "survey_snapshot":
            data = cv2.imencode(".png", np.full((16, 16, 3), 100, np.uint16))[
                1
            ].tobytes()
            return {
                "instance": "fixture",
                "sequence": int(self.clock.elapsed * 100),
                "age_s": 0,
                "width": 16,
                "height": 16,
                "png_base64": base64.b64encode(data).decode(),
            }
        raise AssertionError(name)


def runner(tmp_path, **kwargs):
    clock = Clock()
    scope = Scope(clock)
    run = survey.SurveyRunner(
        scope, tmp_path, clock=clock, ntp_check=lambda: True, **kwargs
    )
    return run, scope, clock


def test_camera_geometry_is_derived_not_guessed():
    assert 2.2 < survey.camera_half_fov(device_state()) < 2.4
    s = device_state()
    s["setting"]["wide_cam"] = True
    with pytest.raises(SurveyError):
        survey.camera_half_fov(s)
    s = device_state()
    s["camera"]["pixel_size_um"] = math.nan
    with pytest.raises(SurveyError):
        survey.camera_half_fov(s)


def test_equatorial_north_pole_has_observer_latitude_altitude():
    alt, az = survey.to_horizontal(
        (0, 90), (-105, 35), datetime(2026, 10, 18, 6, tzinfo=timezone.utc)
    )
    assert alt == pytest.approx(35, abs=0.05)
    assert min(az, 360 - az) < 0.1


def test_clock_skew_and_invalid_zone_fail_closed():
    now = datetime(2026, 10, 18, 6, tzinfo=timezone.utc)
    good = {
        "year": 2026,
        "mon": 10,
        "day": 18,
        "hour": 6,
        "min": 0,
        "sec": 0,
        "time_zone": "UTC",
    }
    survey.check_device_clock(good, now)
    for bad in (
        {**good, "hour": 5},
        {**good, "time_zone": "NOT_A_ZONE"},
        {**good, "sec": False},
    ):
        with pytest.raises(SurveyError):
            survey.check_device_clock(bad, now)


def test_preflight_and_missing_clearance_never_send_motion(tmp_path):
    run, scope, _ = runner(tmp_path)
    assert run.preflight()["ready"] is True
    assert scope.writes == []
    with pytest.raises(SurveyError, match="physical_clearance_required"):
        run.run(clearance_confirmed=False, max_cells=1)
    assert scope.writes == []


def test_unsafe_state_never_sends_motion(tmp_path):
    run, scope, _ = runner(tmp_path)
    scope.state["mount"]["tracking"] = True
    with pytest.raises(SurveyError):
        run.run(clearance_confirmed=True, max_cells=1)
    assert scope.writes == []


def test_arm_timeout_is_not_retried(tmp_path):
    run, scope, clock = runner(tmp_path)
    original = scope.native

    def native(method, params=None):
        result = original(method, params)
        if method == "scope_move_to_horizon":
            scope.events.clear()
        return result

    scope.native = native
    with pytest.raises(SurveyError, match="arm_open_unconfirmed"):
        run.open_arm()
    assert sum(m == "scope_move_to_horizon" for m, p in scope.writes) == 1
    assert clock.elapsed <= 65


def test_stale_plate_solve_cannot_calibrate(tmp_path):
    run, scope, _ = runner(tmp_path)
    scope.events["PlateSolve"] = {
        "Timestamp": "1",
        "state": "complete",
        "result": {"ra_dec": [12, 30]},
    }
    original = scope.native
    scope.native = lambda m, p=None: 0 if m == "start_solve" else original(m, p)
    assert run.solve() is None


def test_uncertain_pulse_never_repeats_and_expires_before_error(tmp_path):
    run, scope, clock = runner(tmp_path)
    run.context = run.preflight()
    scope.state["mount"]["close"] = False
    scope.fail_motion = True
    with pytest.raises(SurveyError, match="transport_uncertain"):
        run.pulse({"speed": 4000, "angle": 90, "dur_sec": 1})
    assert sum(m == "scope_speed_move" for m, p in scope.writes) == 1
    assert clock.elapsed >= 1.5


def test_live_workflow_saves_private_partial_grid_without_claiming_clear_sky(tmp_path):
    run, scope, _ = runner(tmp_path)
    result = run.run(clearance_confirmed=True, max_cells=1)
    assert result["status"] == "complete"
    assert result["sampled_cells"] == 1 and result["planned_cells"] == 1
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    cell = manifest["samples"][0]
    assert cell["classification"] == "stars_detected_not_all_clear"
    assert (tmp_path / cell["image"]).is_file()
    assert not (os.stat(tmp_path / "manifest.json").st_mode & 0o077)
    assert not (os.stat(tmp_path / cell["image"]).st_mode & 0o077)
    assert "location_lon_lat" not in (tmp_path / "manifest.json").read_text()
    assert sum(m == "scope_move_to_horizon" for m, p in scope.writes) == 1
    assert not any(
        m in ("scope_goto", "scope_park", "set_setting", "scope_sync")
        for m, p in scope.writes
    )


def test_lock_excludes_second_run_and_stop_is_cooperative(tmp_path):
    with survey.run_lock(tmp_path):
        with pytest.raises(SurveyError, match="survey_already_running"):
            with survey.run_lock(tmp_path):
                pass
    run, scope, _ = runner(tmp_path)
    (tmp_path / "stop").touch()
    with pytest.raises(SurveyError, match="cancelled"):
        run.run(clearance_confirmed=True, max_cells=1)
    assert scope.writes == []


def test_capture_rejects_motion_during_image_read(tmp_path):
    run, scope, _ = runner(tmp_path)
    run.preflight()
    scope.state["mount"]["close"] = False
    scope.position = [40.0, 180.0]
    run.axes = {"alt_positive": 90, "az_positive": 180}
    original = scope.action

    def action(name, params=None):
        result = original(name, params)
        if name == "survey_snapshot":
            scope.position[0] += 3
        return result

    scope.action = action
    with pytest.raises(SurveyError, match="position_changed_during_capture"):
        run.capture(0, (40, 180))
    assert run.manifest["samples"] == []


def test_failed_solve_is_unknown_not_tree_or_clear(tmp_path):
    run, scope, _ = runner(tmp_path)
    run.preflight()
    scope.state["mount"]["close"] = False
    scope.position = [40.0, 180.0]
    run.axes = {"alt_positive": 90, "az_positive": 180}
    scope.solve_success = False
    run.capture(0, (40, 180))
    assert run.manifest["samples"][0]["classification"] == "unknown"


def test_different_output_directories_share_live_controller_lock(tmp_path):
    first = survey.SurveyRunner(survey.SurveyClient(55001), tmp_path / "a")
    second = survey.SurveyRunner(survey.SurveyClient(55001), tmp_path / "b")
    assert first.lock_root == second.lock_root
    assert first.lock_root != tmp_path
    with survey.run_lock(first.lock_root):
        with pytest.raises(SurveyError, match="survey_already_running"):
            with survey.run_lock(second.lock_root):
                pass


def test_inherited_lock_cannot_be_an_unrelated_file(tmp_path):
    other = tmp_path / "other"
    other.touch(mode=0o600)
    with other.open() as handle:
        with pytest.raises(SurveyError, match="invalid_inherited_lock"):
            with survey.run_lock(tmp_path, inherited_fd=handle.fileno()):
                pass


def test_capture_rejects_motion_during_failed_solve(tmp_path):
    run, scope, _ = runner(tmp_path)
    run.preflight()
    scope.state["mount"]["close"] = False
    scope.position = [40.0, 180.0]
    run.axes = {"alt_positive": 90, "az_positive": 180}
    original = scope.native

    def native(method, params=None):
        result = original(method, params)
        if method == "start_solve":
            scope.position[0] += 3
            scope.events["PlateSolve"]["state"] = "fail"
        return result

    scope.native = native
    with pytest.raises(SurveyError, match="position_changed_during_solve"):
        run.capture(0, (40, 180))
    assert run.manifest["samples"] == []
