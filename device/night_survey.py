"""Local, bounded Seestar visibility sampling. No daytime or blind survey."""

import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from zoneinfo import ZoneInfo

import astropy.units as u
from astropy.coordinates import AltAz, EarthLocation, FK5, SkyCoord, get_sun
from astropy.time import Time
from astropy.utils import iers

from device.survey_safety import (
    SurveyError,
    az_delta,
    fresh_terminal,
    horizon,
    native_result,
    number,
    preflight_state,
    pulse_for,
    sun_gate,
    validate_step,
)

iers.conf.auto_download = False
iers.conf.auto_max_age = None


class Clock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

    @staticmethod
    def now():
        return datetime.now(timezone.utc)


def site_from(state):
    value = state.get("location_lon_lat")
    if (
        not isinstance(value, list)
        or len(value) < 2
        or not all(number(x) for x in value[:2])
        or not -180 <= value[0] <= 180
        or not -90 < value[1] < 90
        or value[:2] == [0, 0]
    ):
        raise SurveyError("invalid_site")
    return tuple(value[:2])


def frame_for(site, now):
    return AltAz(
        obstime=Time(now),
        location=EarthLocation.from_geodetic(site[0] * u.deg, site[1] * u.deg),
        pressure=0 * u.hPa,
    )


def to_horizontal(radec, site, now):
    if (
        not isinstance(radec, (list, tuple))
        or len(radec) != 2
        or not all(number(x) for x in radec)
        or not 0 <= radec[0] <= 24
        or not -90 <= radec[1] <= 90
    ):
        raise SurveyError("invalid_solved_coordinates")
    position = SkyCoord(
        ra=radec[0] * u.hourangle, dec=radec[1] * u.deg, frame=FK5(equinox=Time(now))
    ).transform_to(frame_for(site, now))
    return float(position.alt.deg), float(position.az.deg)


def to_equatorial(altaz, site, now):
    position = SkyCoord(
        alt=altaz[0] * u.deg, az=altaz[1] * u.deg, frame=frame_for(site, now)
    ).transform_to(FK5(equinox=Time(now)))
    return float(position.ra.hour), float(position.dec.deg)


def solar_altitude(site, now):
    return float(get_sun(Time(now)).transform_to(frame_for(site, now)).alt.deg)


def camera_half_fov(state):
    camera, setting = state.get("camera", {}), state.get("setting", {})
    model = state.get("device", {}).get("product_model", "")
    if "s30pro" not in str(model).lower().replace(" ", ""):
        raise SurveyError("unreviewed_mount_model")
    if setting.get("wide_cam") is not False:
        raise SurveyError("primary_camera_required")
    size, pixel, focal = (
        camera.get("chip_size"),
        camera.get("pixel_size_um"),
        camera.get("focal_len"),
    )
    if (
        not isinstance(size, list)
        or len(size) != 2
        or not all(number(x) and 100 <= x <= 10000 for x in size)
        or not number(pixel)
        or not 0 < pixel <= 10
        or not number(focal)
        or not 100 <= focal <= 300
    ):
        raise SurveyError("unknown_camera_geometry")
    return math.degrees(math.atan(math.hypot(*size) * pixel / 1000 / (2 * focal)))


def check_device_clock(value, now):
    try:
        keys = ("year", "mon", "day", "hour", "min", "sec")
        if not all(type(value[k]) is int for k in keys):
            raise ValueError
        dt = datetime(*(value[k] for k in keys), tzinfo=ZoneInfo(value["time_zone"]))
        if abs((dt - now).total_seconds()) > 10:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SurveyError("device_clock_invalid_or_skewed") from None


def ntp_synchronized():
    try:
        return (
            subprocess.check_output(
                ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).strip()
            == "yes"
        )
    except (OSError, subprocess.SubprocessError):
        return False


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SurveyError("unexpected_http_redirect")


class SurveyClient:
    def __init__(self, port=5555, device=1):
        if (
            type(port) is not int
            or not 1024 <= port <= 65535
            or type(device) is not int
            or not 1 <= device <= 10
        ):
            raise SurveyError("invalid_local_endpoint")
        self.url = f"http://127.0.0.1:{port}/api/v1/telescope/{device}/action"
        self.lock_root = (
            Path(tempfile.gettempdir())
            / f"seestar-survey-{os.getuid()}-{port}-{device}"
        )
        self.transaction = 0
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def action(self, name, params=None):
        self.transaction += 1
        data = urllib.parse.urlencode(
            {
                "Action": name,
                "Parameters": json.dumps(params or {}),
                "ClientID": 97,
                "ClientTransactionID": self.transaction,
            }
        ).encode()
        try:
            request = urllib.request.Request(self.url, data=data, method="PUT")
            with self.opener.open(request, timeout=18) as response:
                body = response.read(64 * 1024 * 1024 + 1)
            if len(body) > 64 * 1024 * 1024:
                raise SurveyError("response_too_large")
            outer = json.loads(body)
            if type(outer.get("ErrorNumber")) is not int or outer["ErrorNumber"] != 0:
                raise SurveyError("alp_action_rejected")
            value = outer.get("Value")
            return json.loads(value) if isinstance(value, str) else value
        except SurveyError:
            raise
        except Exception:
            raise SurveyError("transport_uncertain") from None

    def native(self, method, params=None):
        request = {"method": method}
        if params is not None:
            request["params"] = params
        return native_result(self.action("method_sync", request), method)


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = path.lstat()
    if path.is_symlink() or not path.is_dir() or stat.st_uid != os.getuid():
        raise SurveyError("unsafe_output_directory")
    path.chmod(0o700)
    return path


def private_write(path, data):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temp, path)


@contextmanager
def run_lock(root, inherited_fd=None):
    root = private_directory(root)
    path = root / "survey.lock"
    if inherited_fd is not None:
        try:
            expected, actual = path.stat(), os.fstat(inherited_fd)
            if (expected.st_dev, expected.st_ino, expected.st_uid) != (
                actual.st_dev,
                actual.st_ino,
                os.getuid(),
            ):
                raise ValueError
        except (OSError, ValueError):
            raise SurveyError("invalid_inherited_lock") from None
        fd = os.dup(inherited_fd)
        os.close(inherited_fd)
    else:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SurveyError("survey_already_running") from None
        yield fd
    finally:
        os.close(fd)


class SurveyRunner:
    def __init__(self, client, directory, *, clock=None, ntp_check=ntp_synchronized):
        self.client, self.directory = client, private_directory(directory)
        self.lock_root = getattr(client, "lock_root", self.directory.parent)
        self.clock, self.ntp_check = clock or Clock(), ntp_check
        self.cancelled = False
        self.deadline = None
        self.context = None
        self.site = None
        self.axes = {}
        self.offset = (0.0, 0.0)
        self.preview_started = False
        self.last_clock_check = None
        self.manifest = {
            "version": 1,
            "status": "created",
            "samples": [],
            "coverage": "coarse samples only; gaps unknown; no continuous horizon mask",
        }

    def checkpoint(self):
        if self.cancelled or (self.directory / "stop").exists():
            raise SurveyError("cancelled")
        if self.deadline is not None and self.clock.monotonic() >= self.deadline:
            raise SurveyError("survey_deadline_exceeded")

    def sleep(self, seconds):
        deadline = self.clock.monotonic() + seconds
        while self.clock.monotonic() < deadline:
            self.checkpoint()
            self.clock.sleep(min(1, deadline - self.clock.monotonic()))

    def save(self, **changes):
        self.manifest.update(changes)
        self.manifest["updated_at"] = self.clock.now().isoformat()
        private_write(
            self.directory / "manifest.json",
            json.dumps(self.manifest, indent=2, allow_nan=False).encode(),
        )

    def event(self, name):
        response = self.client.action("get_event_state", {"event_name": name})
        if (
            not isinstance(response, dict)
            or type(response.get("code")) is not int
            or response.get("code") != 0
            or not isinstance(response.get("result"), dict)
        ):
            raise SurveyError("invalid_event_state")
        return response["result"]

    def operating_state(self):
        self.checkpoint()
        state = self.client.native(
            "get_device_state",
            {
                "keys": [
                    "device",
                    "mount",
                    "pi_status",
                    "balance_sensor",
                    "camera",
                    "setting",
                    "location_lon_lat",
                ]
            },
        )
        view = self.client.native("get_view_state")
        schedule = self.client.action("get_schedule")
        preflight_state(state, schedule, view, allow_preview=True)
        if self.site is not None and site_from(state) != self.site:
            raise SurveyError("site_changed")
        return state, view

    def preflight(self):
        self.checkpoint()
        if not self.ntp_check():
            raise SurveyError("host_clock_not_synchronized")
        verified = self.client.native("pi_is_verified")
        if not (
            verified is True
            or isinstance(verified, dict)
            and verified.get("is_verified") is True
        ):
            raise SurveyError("authentication_required")
        state, view = self.operating_state()
        check_device_clock(self.client.native("pi_get_time"), self.clock.now())
        self.last_clock_check = self.clock.monotonic()
        site = site_from(state)
        half = camera_half_fov(state)
        sensor = state.get("balance_sensor", {})
        level = sensor.get("data", {}).get("angle")
        if sensor.get("code") != 0 or not number(level):
            raise SurveyError("unknown_level")
        altitude = solar_altitude(site, self.clock.now())
        reason = None
        try:
            sun_gate(altitude, half, level)
        except SurveyError as error:
            if str(error) != "waiting_for_safe_darkness":
                raise
            reason = str(error)
        self.site = site
        result = {
            "ready": reason is None,
            "reason": reason,
            "arm_closed": state["mount"]["close"],
            "sun_altitude_deg": round(altitude, 2),
            "half_fov_deg": half,
            "level_deg": level,
            "existing_preview": view["View"].get("stage") == "ContinuousExposure",
        }
        self.context = result
        return result

    def motion_gate(self):
        if (
            self.last_clock_check is None
            or self.clock.monotonic() - self.last_clock_check >= 30
        ):
            if not self.ntp_check():
                raise SurveyError("host_clock_not_synchronized")
            check_device_clock(self.client.native("pi_get_time"), self.clock.now())
            self.last_clock_check = self.clock.monotonic()
        state, _ = self.operating_state()
        sensor = state.get("balance_sensor", {})
        if sensor.get("code") != 0:
            raise SurveyError("unknown_level")
        sun_gate(
            solar_altitude(self.site, self.clock.now()),
            camera_half_fov(state),
            sensor.get("data", {}).get("angle"),
        )
        if state["mount"]["close"]:
            raise SurveyError("arm_closed")
        return state

    def position(self):
        return horizon(self.client.native("scope_get_horiz_coord"))

    def open_arm(self):
        self.preflight()
        if not self.context["ready"]:
            raise SurveyError("waiting_for_safe_darkness")
        state, _ = self.operating_state()
        if not state["mount"]["close"]:
            return
        previous = self.event("ScopeMoveToHorizon")
        self.client.native("scope_move_to_horizon")
        deadline = self.clock.monotonic() + 60
        while self.clock.monotonic() < deadline:
            self.sleep(1)
            state = self.client.native("get_device_state", {"keys": ["mount"]})
            mount = state.get("mount", {})
            event = self.event("ScopeMoveToHorizon")
            outcome = fresh_terminal(event, previous)
            if outcome in ("fail", "cancel"):
                raise SurveyError("arm_open_failed")
            if (
                outcome == "complete"
                and mount.get("close") is False
                and mount.get("move_type") == "none"
            ):
                if mount.get("tracking") is True:
                    self.client.native("scope_set_track_state", False)
                self.motion_gate()
                return
        raise SurveyError("arm_open_unconfirmed")

    def pulse(self, params):
        if (
            params.get("angle") not in (0, 90, 180, 270)
            or type(params.get("dur_sec")) is not int
            or not 1 <= params["dur_sec"] <= 2
            or type(params.get("speed")) is not int
            or not 1 <= params["speed"] <= 4000
        ):
            raise SurveyError("invalid_motion_pulse")
        self.motion_gate()
        before = self.position()
        try:
            self.client.native("scope_speed_move", params)
        finally:
            # Even an uncertain reply must not cause immediate replay. This
            # hardware-duration wait also runs on cancellation/transport error.
            self.clock.sleep(params["dur_sec"] + 0.5)
        self.motion_gate()
        after = self.position()
        if abs(after[0] - before[0]) > 4 or abs(az_delta(before[1], after[1])) > 4:
            raise SurveyError("pulse_exceeded_envelope")
        return before, after

    def calibrate_axes(self):
        for angle in (90, 270):
            before, after = self.pulse({"speed": 4000, "angle": angle, "dur_sec": 1})
            delta = after[0] - before[0]
            if abs(az_delta(before[1], after[1])) > 0.3:
                raise SurveyError("unexpected_axis_coupling")
            if delta > 0.03:
                self.axes["alt_positive"] = angle
                break
        else:
            raise SurveyError("altitude_axis_did_not_respond")
        before, after = self.pulse({"speed": 4000, "angle": 0, "dur_sec": 1})
        delta = az_delta(before[1], after[1])
        if abs(after[0] - before[0]) > 0.3 or abs(delta) < 0.03:
            raise SurveyError("azimuth_axis_did_not_respond")
        self.axes["az_positive"] = 0 if delta > 0 else 180

    def move_to(self, target):
        for _ in range(400):
            self.checkpoint()
            current = self.position()
            pulse = pulse_for(current, target, self.axes)
            if pulse is None:
                self.sleep(1)
                check = self.position()
                if (
                    abs(check[0] - target[0]) <= 0.6
                    and abs(az_delta(check[1], target[1])) <= 0.6
                ):
                    self.motion_gate()
                    return check
                raise SurveyError("position_changed_while_settling")
            axis = "alt" if pulse["angle"] in (90, 270) else "az"
            positive = self.axes[axis + "_positive"]
            before, after = self.pulse(pulse)
            validate_step(before, after, axis, 1 if pulse["angle"] == positive else -1)
        raise SurveyError("movement_budget_exhausted")

    def solve(self):
        previous = self.event("PlateSolve")
        self.client.native("start_solve")
        deadline = self.clock.monotonic() + 25
        while self.clock.monotonic() < deadline:
            self.sleep(1)
            event = self.event("PlateSolve")
            outcome = fresh_terminal(event, previous)
            if outcome in ("fail", "cancel"):
                return None
            result = event.get("result", {})
            if (
                outcome == "complete"
                and isinstance(result, dict)
                and result.get("ra_dec") is not None
            ):
                position = to_horizontal(result["ra_dec"], self.site, self.clock.now())
                return {
                    "position": position,
                    "stars": result.get("star_number"),
                    "timestamp": event["Timestamp"],
                }
        return None

    def calibrate_pointing(self):
        base = self.position()[1]
        offsets = []
        for heading in (base, (base + 60) % 360):
            measured = self.move_to((60, heading))
            solved = self.solve()
            if solved is None:
                raise SurveyError("fresh_stellar_calibration_failed")
            actual = solved["position"]
            offset = (actual[0] - measured[0], az_delta(measured[1], actual[1]))
            if abs(offset[0]) > 1:
                raise SurveyError("altitude_calibration_disagrees")
            offsets.append(offset)
        if (
            abs(offsets[0][0] - offsets[1][0]) > 1
            or abs(az_delta(offsets[0][1], offsets[1][1])) > 1
        ):
            raise SurveyError("pointing_calibration_inconsistent")
        self.offset = (
            (offsets[0][0] + offsets[1][0]) / 2,
            (offsets[0][1] + az_delta(offsets[0][1], offsets[1][1]) / 2) % 360,
        )
        self.save(
            calibration={
                "stellar_references": 2,
                "uncertainty_deg": 2,
                "verified_at": self.clock.now().isoformat(),
            }
        )

    def capture(self, index, target):
        measured = self.move_to((target[0], (target[1] - self.offset[1]) % 360))
        image = self.client.action("survey_snapshot", {"timeout": 12})
        after = self.position()
        if (
            abs(after[0] - measured[0]) > 0.3
            or abs(az_delta(measured[1], after[1])) > 0.3
        ):
            raise SurveyError("position_changed_during_capture")
        self.motion_gate()
        if (
            not isinstance(image, dict)
            or type(image.get("sequence")) is not int
            or not isinstance(image.get("instance"), str)
            or not number(image.get("age_s"))
            or not 0 <= image["age_s"] <= 2
        ):
            raise SurveyError("invalid_snapshot_metadata")
        try:
            data = base64.b64decode(image["png_base64"], validate=True)
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise SurveyError("invalid_snapshot_image") from None
        name = f"sample-{index:03d}.png"
        private_write(self.directory / name, data)
        solved = self.solve()
        after_solve = self.position()
        if (
            abs(after_solve[0] - measured[0]) > 0.3
            or abs(az_delta(measured[1], after_solve[1])) > 0.3
        ):
            raise SurveyError("position_changed_during_solve")
        self.motion_gate()
        expected = (measured[0] + self.offset[0], (measured[1] + self.offset[1]) % 360)
        if solved and (
            abs(solved["position"][0] - expected[0]) > 2
            or abs(az_delta(expected[1], solved["position"][1])) > 2
        ):
            raise SurveyError("pointing_calibration_drift")
        cell = {
            "index": index,
            "requested_alt_az": target,
            "measured_alt_az": solved["position"] if solved else expected,
            "pointing_source": "fresh_stellar_solve"
            if solved
            else "two_reference_calibrated_encoders",
            "uncertainty_deg": 2,
            "image": name,
            "image_sha256": hashlib.sha256(data).hexdigest(),
            "frame_instance": image["instance"],
            "frame_sequence": image["sequence"],
            "classification": "stars_detected_not_all_clear" if solved else "unknown",
            "captured_at": self.clock.now().isoformat(),
        }
        self.manifest["samples"].append(cell)
        self.save(phase="sampling", sampled_cells=len(self.manifest["samples"]))

    def run(
        self,
        *,
        clearance_confirmed=False,
        max_cells=36,
        wait_seconds=0,
        inherited_fd=None,
    ):
        if not clearance_confirmed:
            raise SurveyError("physical_clearance_required")
        if (
            type(max_cells) is not int
            or not 1 <= max_cells <= 36
            or not number(wait_seconds)
            or not 0 <= wait_seconds <= 14400
        ):
            raise SurveyError("invalid_run_budget")
        self.checkpoint()
        with run_lock(self.lock_root, inherited_fd=inherited_fd):
            private_write(
                self.directory.parent / "latest", self.directory.name.encode()
            )
            self.save(
                status="running",
                phase="preflight",
                pid=os.getpid(),
                planned_cells=max_cells,
                started_at=self.clock.now().isoformat(),
                sampled_cells=0,
            )
            try:
                waiting_deadline = self.clock.monotonic() + wait_seconds
                while not self.preflight()["ready"]:
                    self.save(
                        phase="waiting_for_safe_darkness",
                        sun_altitude_deg=self.context["sun_altitude_deg"],
                    )
                    if self.clock.monotonic() >= waiting_deadline:
                        raise SurveyError("waiting_for_safe_darkness")
                    self.sleep(min(30, waiting_deadline - self.clock.monotonic()))
                self.deadline = self.clock.monotonic() + 5400
                self.save(phase="opening", half_fov_deg=self.context["half_fov_deg"])
                self.open_arm()
                if not self.context["existing_preview"]:
                    self.client.native("iscope_start_view", {"mode": "star"})
                    self.preview_started = True
                self.save(phase="axis_calibration")
                self.calibrate_axes()
                self.save(phase="stellar_calibration")
                self.calibrate_pointing()
                targets = [
                    (alt, float(az))
                    for alt in (60.0, 40.0, 20.0)
                    for az in range(0, 360, 30)
                ]
                for index, target in enumerate(targets[:max_cells]):
                    self.capture(index, target)
                self.motion_gate()
                self.save(
                    status="complete",
                    phase="stationary",
                    finished_at=self.clock.now().isoformat(),
                )
            except SurveyError as error:
                self.save(
                    status="cancelled" if str(error) == "cancelled" else "failed",
                    reason=str(error),
                )
                raise
            except Exception:
                self.save(status="failed", reason="unexpected_runner_error")
                raise SurveyError("unexpected_runner_error") from None
            finally:
                # No park or blind stop-slew. Every speed command expires in
                # hardware. Do not stop a preview this run did not start.
                if self.preview_started:
                    try:
                        view = self.client.native("get_view_state").get("View", {})
                        if (
                            view.get("stage") == "ContinuousExposure"
                            and view.get("mode") == "star"
                        ):
                            self.client.native("iscope_stop_view")
                    except Exception:
                        self.save(cleanup="preview_stop_unconfirmed")
            return summary(self.manifest)


def summary(manifest):
    return {
        key: manifest[key]
        for key in (
            "status",
            "phase",
            "reason",
            "planned_cells",
            "sampled_cells",
            "started_at",
            "updated_at",
            "finished_at",
            "cleanup",
        )
        if key in manifest
    }


def run_directory(root, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise SurveyError("invalid_run_id")
    directory = root / run_id
    if directory.is_symlink():
        raise SurveyError("unsafe_run_path")
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("preflight", "run", "start", "status", "stop")
    )
    parser.add_argument(
        "--root", type=Path, default=Path.home() / ".local/state/seestar-survey"
    )
    parser.add_argument("--run-id")
    parser.add_argument("--port", type=int, default=5555)
    parser.add_argument("--device", type=int, default=1)
    parser.add_argument("--clearance-confirmed", action="store_true")
    parser.add_argument("--max-cells", type=int, default=36)
    parser.add_argument("--wait-seconds", type=float, default=0)
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        root = private_directory(args.root)
        if args.command in ("status", "stop"):
            run_id = args.run_id or (root / "latest").read_text().strip()
            directory = run_directory(root, run_id)
            if args.command == "stop":
                private_write(directory / "stop", b"stop requested\n")
                print(json.dumps({"run_id": run_id, "stop_requested": True}))
            else:
                manifest = json.loads((directory / "manifest.json").read_text())
                result = summary(manifest)
                if result.get("status") in ("running", "starting"):
                    try:
                        pid = manifest.get("pid") or int(
                            (directory / "pid").read_text()
                        )
                        os.kill(pid, 0)
                    except (OSError, KeyError, ValueError):
                        result["status"] = "interrupted_unconfirmed"
                print(json.dumps({"run_id": run_id, **result}))
            return 0
        run_id = args.run_id or uuid.uuid4().hex
        directory = private_directory(run_directory(root, run_id))
        client = SurveyClient(args.port, args.device)
        if args.command == "start":
            if not args.clearance_confirmed:
                raise SurveyError("physical_clearance_required")
            if (
                not 1 <= args.max_cells <= 36
                or not number(args.wait_seconds)
                or not 0 <= args.wait_seconds <= 14400
            ):
                raise SurveyError("invalid_run_budget")
            with run_lock(client.lock_root) as lock_fd:
                if (directory / "manifest.json").exists():
                    raise SurveyError("run_id_already_used")
                private_write(root / "latest", run_id.encode())
                private_write(
                    directory / "manifest.json",
                    json.dumps(
                        {"status": "starting", "planned_cells": args.max_cells}
                    ).encode(),
                )
                fd = os.open(
                    directory / "worker.log",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(fd, "wb") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "device.night_survey",
                            "run",
                            "--root",
                            str(root),
                            "--run-id",
                            run_id,
                            "--port",
                            str(args.port),
                            "--device",
                            str(args.device),
                            "--max-cells",
                            str(args.max_cells),
                            "--wait-seconds",
                            str(args.wait_seconds),
                            "--clearance-confirmed",
                            "--lock-fd",
                            str(lock_fd),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        start_new_session=True,
                        pass_fds=(lock_fd,),
                        cwd=Path(__file__).resolve().parents[1],
                    )
                private_write(directory / "pid", str(process.pid).encode())
            print(
                json.dumps({"run_id": run_id, "pid": process.pid, "status": "starting"})
            )
            return 0
        runner = SurveyRunner(client, directory)
        if args.command == "preflight":
            print(json.dumps(runner.preflight()))
            return 0
        if (directory / "manifest.json").exists() and args.lock_fd is None:
            raise SurveyError("run_id_already_used")
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: setattr(runner, "cancelled", True))
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    **runner.run(
                        clearance_confirmed=args.clearance_confirmed,
                        max_cells=args.max_cells,
                        wait_seconds=args.wait_seconds,
                        inherited_fd=args.lock_fd,
                    ),
                }
            )
        )
        return 0
    except (SurveyError, OSError, ValueError) as error:
        code = (
            str(error)
            if isinstance(error, SurveyError)
            else "local_artifact_or_argument_error"
        )
        print(json.dumps({"status": "failed", "reason": code}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
