import base64
import json
import logging
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from device import exceptions, survey_snapshot, telescope
from device.protocols.imager import SeestarImagerProtocol
from device.shr import set_shr_logger
from device.survey_safety import SurveyError
from tests.test_telescope_action import DummyReq, DummyResp, FakeDevice


@pytest.fixture(autouse=True)
def exception_logger(monkeypatch):
    monkeypatch.setattr(exceptions, "logger", logging.getLogger("snapshot-errors"))


def test_protocol_records_actual_frame_freshness_atomically(monkeypatch):
    proto = SeestarImagerProtocol(
        logging.getLogger("snapshot"), "test", 1, "127.0.0.1", 1
    )
    monkeypatch.setattr(proto, "is_connected", lambda: True)
    raw = np.arange(32 * 32, dtype=np.uint16).tobytes()
    packets = [b"header", raw]
    monkeypatch.setattr(proto, "recv_exact", lambda n: packets.pop(0))
    monkeypatch.setattr(proto, "parse_header", lambda h: (len(raw), 21, 32, 32))
    proto._run_receive_message()
    frame = proto.get_survey_frame()
    assert frame["sequence"] == 1
    assert frame["received_monotonic"] > 0
    assert frame["instance"]
    assert frame["image"].shape == (32, 32, 3)
    frame["image"][:] = 0
    assert proto.get_survey_frame()["image"].max() > 0


class Camera(SeestarImagerProtocol):
    def __init__(self):
        super().__init__(logging.getLogger("snapshot-test"), "test", 1, "127.0.0.1", 1)
        self.seq = 4
        self.messages = []

    def send_message(self, message):
        self.messages.append(message)
        return True

    def get_survey_frame(self):
        return {
            "instance": "test-instance",
            "sequence": self.seq,
            "received_monotonic": float(self.seq),
            "kind": self.exposure_mode,
            "image": np.full((16, 16, 3), self.seq, dtype=np.uint8),
        }


def test_snapshot_discards_inflight_frame_then_returns_new_pixels(monkeypatch):
    camera = Camera()
    clock = [4.0]
    monkeypatch.setattr(survey_snapshot.time, "monotonic", lambda: clock[0])

    def advance(seconds):
        clock[0] += 1
        camera.seq += 1

    monkeypatch.setattr(survey_snapshot.time, "sleep", advance)
    result = survey_snapshot.snapshot(SimpleNamespace(comm=camera), timeout=5)
    assert result["sequence"] == 6
    assert result["instance"] == "test-instance"
    decoded = cv2.imdecode(
        np.frombuffer(base64.b64decode(result["png_base64"]), np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    assert decoded.shape == (16, 16, 3)
    assert int(decoded[0, 0, 0]) == 6
    assert camera.exposure_mode is None
    assert [json.loads(x)["method"] for x in camera.messages] == [
        "begin_streaming",
        "stop_streaming",
    ]


def test_snapshot_does_not_serve_cached_or_missing_frame(monkeypatch):
    camera = Camera()
    clock = [4.0]
    monkeypatch.setattr(survey_snapshot.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        survey_snapshot.time, "sleep", lambda _: clock.__setitem__(0, clock[0] + 1)
    )
    with pytest.raises(SurveyError, match="fresh_frame_timeout"):
        survey_snapshot.snapshot(SimpleNamespace(comm=camera), timeout=2)
    assert camera.exposure_mode is None
    assert [json.loads(x)["method"] for x in camera.messages] == [
        "begin_streaming",
        "stop_streaming",
    ]


@pytest.mark.parametrize("takeover_mode", ["preview", "stream"])
def test_snapshot_cleanup_does_not_stop_a_new_viewer(monkeypatch, takeover_mode):
    camera = Camera()
    clock = [4.0]
    monkeypatch.setattr(survey_snapshot.time, "monotonic", lambda: clock[0])

    def advance(_):
        clock[0] += 1
        camera.seq += 1
        camera.set_exposure_mode(takeover_mode)

    monkeypatch.setattr(survey_snapshot.time, "sleep", advance)
    if takeover_mode == "stream":
        with pytest.raises(SurveyError, match="fresh_frame_timeout"):
            survey_snapshot.snapshot(SimpleNamespace(comm=camera), timeout=5)
    else:
        survey_snapshot.snapshot(SimpleNamespace(comm=camera), timeout=5)
    assert camera.exposure_mode == takeover_mode


def test_snapshot_does_not_release_existing_preview(monkeypatch):
    camera = Camera()
    camera.set_exposure_mode("preview")
    clock = [4.0]
    monkeypatch.setattr(survey_snapshot.time, "monotonic", lambda: clock[0])

    def advance(_):
        clock[0] += 1
        camera.seq += 1

    monkeypatch.setattr(survey_snapshot.time, "sleep", advance)
    survey_snapshot.snapshot(SimpleNamespace(comm=camera), timeout=5)
    assert camera.exposure_mode == "preview"
    assert len(camera.messages) == 1


@pytest.mark.parametrize("mode", ["stream", "stack"])
def test_snapshot_refuses_to_replace_another_stream_mode(mode):
    camera = Camera()
    camera.set_exposure_mode(mode)
    before = list(camera.messages)
    with pytest.raises(SurveyError, match="camera_stream_busy"):
        survey_snapshot.snapshot(SimpleNamespace(comm=camera), timeout=1)
    assert camera.exposure_mode == mode
    assert camera.messages == before


def test_concurrent_snapshot_cannot_borrow_another_snapshots_temporary_stream(
    monkeypatch,
):
    camera = Camera()
    clock = [4.0]
    monkeypatch.setattr(survey_snapshot.time, "monotonic", lambda: clock[0])
    attempted = []

    def advance(_):
        if not attempted:
            attempted.append(True)
            with pytest.raises(SurveyError, match="camera_snapshot_busy"):
                survey_snapshot.snapshot(SimpleNamespace(comm=camera), timeout=1)
        clock[0] += 1
        camera.seq += 1

    monkeypatch.setattr(survey_snapshot.time, "sleep", advance)
    survey_snapshot.snapshot(SimpleNamespace(comm=camera), timeout=5)
    assert camera.exposure_mode is None


def test_two_slow_preview_frames_fit_bounded_capture_window(monkeypatch):
    camera = Camera()
    clock = [0.0]
    monkeypatch.setattr(survey_snapshot.time, "monotonic", lambda: clock[0])

    def advance(seconds):
        clock[0] += seconds
        camera.seq = 4 + int(clock[0] / 15)

    monkeypatch.setattr(survey_snapshot.time, "sleep", advance)
    original = camera.get_survey_frame

    def frame():
        result = original()
        result["received_monotonic"] = (camera.seq - 4) * 15.0
        return result

    camera.get_survey_frame = frame
    image = survey_snapshot.snapshot(SimpleNamespace(comm=camera))
    assert image["sequence"] == 6
    assert 30 <= clock[0] < 45


@pytest.mark.parametrize("address", ["192.0.2.1", "localhost", "", None])
def test_route_refuses_nonloopback_even_when_forwarded_headers_claim_local(
    address, monkeypatch
):
    set_shr_logger(logging.getLogger("snapshot-route"))
    dev = FakeDevice()
    monkeypatch.setitem(telescope.seestar_dev, 1, dev)
    req = DummyReq("survey_snapshot", {})
    req.remote_addr = address
    resp = DummyResp()
    telescope.action().on_put(req, resp, 1)
    import json

    assert json.loads(resp.text)["ErrorNumber"] != 0
    assert dev.calls == []


def test_route_image_payload_is_not_logged(monkeypatch, caplog):
    set_shr_logger(logging.getLogger("snapshot-route"))
    dev = FakeDevice()
    dev.logger = logging.getLogger("snapshot-device")
    monkeypatch.setitem(telescope.seestar_dev, 1, dev)
    dev.send_message_param_sync = lambda params: {
        "id": 1,
        "code": 0,
        "method": "get_view_state",
        "result": {
            "View": {"mode": "star", "state": "working", "stage": "ContinuousExposure"}
        },
    }
    monkeypatch.setitem(telescope.seestar_imager, 1, object())
    monkeypatch.setattr(
        survey_snapshot,
        "snapshot",
        lambda *_args, **_kwargs: {"png_base64": "PRIVATE_IMAGE_SENTINEL"},
    )
    with caplog.at_level(logging.DEBUG):
        resp = DummyResp()
        telescope.action().on_put(DummyReq("survey_snapshot", {}), resp, 1)
    assert "PRIVATE_IMAGE_SENTINEL" in resp.text
    assert "PRIVATE_IMAGE_SENTINEL" not in caplog.text
