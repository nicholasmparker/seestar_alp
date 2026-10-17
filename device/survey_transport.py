"""Loopback survey transport: no replay, immutable connection-scoped receipts."""

from collections import deque
import copy
import ipaddress
import threading
import uuid

from device.survey_safety import SurveyError, native_result


class SurveyReceipts:
    def __init__(self):
        self.lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.generation = uuid.uuid4().hex
        self.sequence = 0
        self.events = deque(maxlen=64)

    def reset(self):
        with self.lock:
            self.generation = uuid.uuid4().hex
            self.sequence = 0
            self.events.clear()

    def cursor(self):
        with self.lock:
            return {"generation": self.generation, "after_sequence": self.sequence}

    def record(self, event, generation):
        if event.get("Event") not in ("PlateSolve", "ScopeMoveToHorizon"):
            return
        with self.lock:
            if generation != self.generation:
                return
            self.sequence += 1
            self.events.append(
                {"sequence": self.sequence, "event": copy.deepcopy(event)}
            )

    def read(self, params):
        with self.lock:
            if params.get("generation") != self.generation:
                raise SurveyError("connection_changed")
            after = params.get("after_sequence")
            if type(after) is not int or not 0 <= after <= self.sequence:
                raise SurveyError("invalid_event_cursor")
            if self.events and after < self.events[0]["sequence"] - 1:
                raise SurveyError("event_receipt_gap")
            return {
                "generation": self.generation,
                "events": copy.deepcopy(
                    [
                        item
                        for item in self.events
                        if item["sequence"] > after
                        and item["event"].get("Event") == params.get("event_name")
                    ]
                ),
            }


METHODS = frozenset(
    {
        "get_device_state",
        "get_view_state",
        "pi_get_time",
        "pi_is_verified",
        "scope_get_horiz_coord",
        "scope_move_to_horizon",
        "scope_speed_move",
        "scope_set_track_state",
        "iscope_start_view",
        "start_solve",
    }
)


def command(device, params):
    if (
        not isinstance(params, dict)
        or set(params) - {"method", "params"}
        or params.get("method") not in METHODS
    ):
        raise SurveyError("unsupported_survey_method")
    method = params["method"]
    value = params.get("params")
    if method == "scope_speed_move" and (
        not isinstance(value, dict)
        or set(value) != {"speed", "angle", "dur_sec"}
        or type(value["speed"]) is not int
        or not 1 <= value["speed"] <= 4000
        or type(value["dur_sec"]) is not int
        or not 1 <= value["dur_sec"] <= 2
        or type(value["angle"]) is not int
        or value["angle"] not in (0, 90, 180, 270)
    ):
        raise SurveyError("invalid_motion_pulse")
    if method == "scope_set_track_state" and value is not False:
        raise SurveyError("survey_tracking_must_be_off")
    if method == "iscope_start_view" and value != {"mode": "star"}:
        raise SurveyError("survey_requires_star_preview")
    receipts = device.survey_receipts
    if not receipts.command_lock.acquire(blocking=False):
        raise SurveyError("survey_command_busy")
    try:
        before = receipts.cursor()
        reply = device.send_message_param_sync(params, replay_on_error=False)
        if receipts.cursor()["generation"] != before["generation"]:
            raise SurveyError("connection_changed")
        native_result(reply, method)
        return {**before, "reply": reply}
    finally:
        receipts.command_lock.release()


def local_action(remote_addr, device, action, params):
    try:
        local = ipaddress.ip_address(remote_addr).is_loopback
    except (TypeError, ValueError):
        local = False
    if not local:
        raise SurveyError("survey_loopback_only")
    if action == "survey_native":
        return command(device, params)
    if not isinstance(params, dict):
        raise SurveyError("invalid_event_cursor")
    return device.survey_receipts.read(params)
