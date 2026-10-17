"""Fresh preview capture for the local survey runner; no payload logging."""

import base64
import ipaddress
import time

import cv2

from device.survey_safety import SurveyError, native_result, number


def snapshot(imager, timeout=12):
    if not number(timeout) or not 0 < timeout <= 12:
        raise SurveyError("invalid_snapshot_timeout")
    comm = imager.comm
    baseline = comm.get_survey_frame()
    started = time.monotonic()
    comm.set_exposure_mode("preview")
    while time.monotonic() - started < timeout:
        current = comm.get_survey_frame()
        if current["instance"] != baseline["instance"]:
            raise SurveyError("camera_restarted")
        # One frame may already be exposing when the request arrives. Require
        # two new frames so that a pre-arrival exposure cannot become evidence.
        image = current["image"]
        if (
            current.get("kind") == "preview"
            and current["sequence"] >= baseline["sequence"] + 2
            and current["received_monotonic"] > started
            and image is not None
        ):
            ok, png = cv2.imencode(".png", image)
            if not ok or len(png) > 64 * 1024 * 1024:
                raise SurveyError("invalid_camera_image")
            return {
                "instance": current["instance"],
                "sequence": current["sequence"],
                "age_s": max(0, time.monotonic() - current["received_monotonic"]),
                "width": int(image.shape[1]),
                "height": int(image.shape[0]),
                "png_base64": base64.b64encode(png).decode("ascii"),
            }
        time.sleep(0.1)
    raise SurveyError("fresh_frame_timeout")


def local_snapshot(remote_addr, device, imager, params):
    try:
        local = ipaddress.ip_address(remote_addr).is_loopback
    except (ValueError, TypeError):
        local = False
    if not local:
        raise SurveyError("snapshot_loopback_only")
    if not isinstance(params, dict) or set(params) - {"timeout"}:
        raise SurveyError("invalid_snapshot_parameters")
    result = native_result(
        device.send_message_param_sync({"method": "get_view_state"}), "get_view_state"
    )
    view = result.get("View", {}) if isinstance(result, dict) else {}
    if (
        view.get("mode") != "star"
        or view.get("stage") != "ContinuousExposure"
        or view.get("state") != "working"
        or view.get("error")
    ):
        raise SurveyError("snapshot_requires_star_preview")
    if imager is None:
        raise SurveyError("camera_unavailable")
    return snapshot(imager, params.get("timeout", 12))
