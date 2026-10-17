import json
import logging
import socket

import pytest

from device import survey_transport, telescope
from device.seestar_device import Seestar, _rpc_log_value
from device.shr import set_shr_logger
from device.survey_safety import SurveyError
from tests.test_telescope_action import DummyReq, DummyResp


def scope():
    return Seestar(logging.getLogger("survey-transport"), "127.0.0.1", 1, "test", 1)


def test_attempted_survey_mutation_never_replays_after_send_error(monkeypatch):
    device = scope()
    delivered = []

    class Socket:
        def sendall(self, payload):
            delivered.append(payload)
            raise socket.error("accepted bytes then connection failed")

        def close(self):
            pass

    device.s = Socket()
    device.is_watch_events = True
    reconnects = []
    monkeypatch.setattr(device, "reconnect", lambda: reconnects.append(True) or True)
    with pytest.raises(SurveyError, match="transport_uncertain"):
        survey_transport.command(device, {"method": "scope_move_to_horizon"})
    assert len(delivered) == 1
    assert reconnects == []


def test_real_receiver_retains_detailed_result_through_second_event_and_sse(
    monkeypatch,
):
    device = scope()
    baseline = device.survey_receipts.cursor()
    detail = {
        "Event": "PlateSolve",
        "Timestamp": "12.2",
        "state": "complete",
        "result": {"ra_dec": [12, 30]},
    }
    simple = {"Event": "PlateSolve", "Timestamp": "12.3", "state": "complete"}

    def receive():
        device.is_watch_events = False
        return json.dumps(detail) + "\r\n" + json.dumps(simple) + "\r\n"

    device.is_watch_events = True
    monkeypatch.setattr(device, "get_socket_msg", receive)
    monkeypatch.setattr("device.seestar_device.time.sleep", lambda _: None)
    device.receive_message_thread_fn()
    stream = device.get_events()
    next(stream)
    next(stream)
    assert "Timestamp" in device.event_state["PlateSolve"]
    receipts = device.survey_receipts.read({**baseline, "event_name": "PlateSolve"})
    assert len(receipts["events"]) == 2
    assert receipts["events"][0]["event"] == detail
    receipts["events"][0]["event"].clear()
    assert (
        device.survey_receipts.read({**baseline, "event_name": "PlateSolve"})["events"][
            0
        ]["event"]
        == detail
    )
    device.disconnect()
    with pytest.raises(SurveyError, match="connection_changed"):
        device.survey_receipts.read({**baseline, "event_name": "PlateSolve"})


def test_survey_route_and_native_projection_do_not_log_private_state(
    monkeypatch, caplog
):
    device = scope()
    device.is_connected = True
    state = {
        "location_lon_lat": ["PRIVATE_SITE_SENTINEL"],
        "private": "PRIVATE_STATE_SENTINEL",
    }
    reply = {
        "method": "get_device_state",
        "id": 12,
        "code": 0,
        "Timestamp": "1",
        "result": state,
    }
    monkeypatch.setattr(device, "send_message_param_sync", lambda *a, **k: reply)
    monkeypatch.setitem(telescope.seestar_dev, 1, device)
    set_shr_logger(logging.getLogger("survey-route"))
    with caplog.at_level(logging.DEBUG):
        device.logger.debug("native: %s", _rpc_log_value(reply))
        response = DummyResp()
        telescope.action().on_put(
            DummyReq("survey_native", {"method": "get_device_state"}), response, 1
        )
    assert "PRIVATE_SITE_SENTINEL" in response.text
    assert "PRIVATE_SITE_SENTINEL" not in caplog.text
    assert "PRIVATE_STATE_SENTINEL" not in caplog.text
