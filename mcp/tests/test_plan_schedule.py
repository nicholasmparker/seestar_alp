from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from seestar_mcp.plan_schedule import (
    PlanError,
    PlanTarget,
    ScopeClock,
    build_schedule,
    firmware_offsets,
    parse_dec,
    parse_ra,
    view_plan_params,
)

TRUNK = {
    "name": "IC 1396A",
    "ra": "21:36:33",
    "dec": "+57:30:00",
    "start": "18:25",
    "end": "01:00",
    "lp_filter": True,
}
HORSEHEAD = {
    "name": "IC 434",
    "ra": 5.6814,
    "dec": -2.4583,
    "start": "01:00",
    "end": "05:05",
    "lp_filter": False,
}


def _targets(*raw):
    return [PlanTarget.model_validate(t) for t in raw]


def _clock(wall: str, zone: str, utc: str) -> ScopeClock:
    """A scope reading `wall` (its local calendar time) in `zone`, read at host `utc`."""
    return ScopeClock(
        wall=datetime.fromisoformat(wall),
        time_zone=zone,
        read_at=datetime.fromisoformat(utc).replace(tzinfo=UTC),
    )


# 17:00 MST on 2026-11-08 is 00:00 UTC on 2026-11-09.
DENVER_5PM = _clock("2026-11-08T17:00:00", "America/Denver", "2026-11-09T00:00:00")


# --- coordinates -----------------------------------------------------------


@pytest.mark.parametrize(
    "value, hours",
    [
        ("21:36:33", 21 + 36 / 60 + 33 / 3600),
        ("21h36m33s", 21 + 36 / 60 + 33 / 3600),
        ("21 36 33.5", 21 + 36 / 60 + 33.5 / 3600),
        ("05:41", 5 + 41 / 60),
        (5.6814, 5.6814),
        ("5.6814", 5.6814),
        (0, 0.0),
    ],
)
def test_parse_ra(value, hours):
    assert parse_ra(value) == pytest.approx(hours)


@pytest.mark.parametrize("value", [24, -1, "25:00:00", "21:61:00", "abc", True])
def test_parse_ra_rejects(value):
    with pytest.raises(ValueError):
        parse_ra(value)


def test_parse_ra_names_degrees_mistake():
    with pytest.raises(ValueError, match="degrees"):
        parse_ra(309.1)


@pytest.mark.parametrize(
    "value, degrees",
    [
        ("+57:30:00", 57.5),
        ("-00:30:00", -0.5),
        ("57d30m", 57.5),
        ("-02°27'30\"", -(2 + 27.5 / 60)),
        (-2.4583, -2.4583),
        ("89.5", 89.5),
    ],
)
def test_parse_dec(value, degrees):
    assert parse_dec(value) == pytest.approx(degrees)


@pytest.mark.parametrize("value", [91, -90.5, "+91:00:00", "45:60:00", "x", False])
def test_parse_dec_rejects(value):
    with pytest.raises(ValueError):
        parse_dec(value)


# --- target model ------------------------------------------------------------


def test_target_normalises_coordinates():
    t = PlanTarget.model_validate(TRUNK)
    assert t.ra == pytest.approx(21.609166, abs=1e-5)
    assert t.dec == pytest.approx(57.5)
    assert t.camera_mode == "tele"


@pytest.mark.parametrize("key", ["exposure_s", "exp_ms", "gain", "exposure"])
def test_target_refuses_per_target_exposure_and_gain(key):
    with pytest.raises(ValidationError, match="global"):
        PlanTarget.model_validate({**TRUNK, key: 20})


@pytest.mark.parametrize(
    "patch",
    [
        {"start": "25:00"},
        {"end": "1:5"},
        {"start": "18.25"},
        {"camera_mode": "wide"},
        {"name": ""},
        {"name": "a/b"},
        {"bogus": 1},
    ],
)
def test_target_rejects_bad_fields(patch):
    with pytest.raises(ValidationError):
        PlanTarget.model_validate({**TRUNK, **patch})


def test_target_requires_lp_filter():
    raw = dict(TRUNK)
    del raw["lp_filter"]
    with pytest.raises(ValidationError):
        PlanTarget.model_validate(raw)


# --- firmware day normalisation (seestar-spec plan-scheduling R15-R17) --------


def test_firmware_offsets_tonight():
    carry, shift, offsets = firmware_offsets([(1105, 395), (1500, 245)], 61200)
    assert (carry, shift) == (0, 0)
    assert offsets == [(66300 - 61200, 90000 - 61200), (90000 - 61200, 104700 - 61200)]


def test_firmware_offsets_underway_starts_now():
    carry, shift, offsets = firmware_offsets([(1105, 395)], 72000)
    assert shift == 0
    assert offsets[0] == (0, 90000 - 72000)


def test_firmware_offsets_carry_reads_overlap_as_after_midnight():
    carry, shift, _ = firmware_offsets([(1380, 120), (60, 60)], 0)
    assert carry == 1440


def test_firmware_offsets_whole_plan_over_runs_tomorrow():
    _, shift, offsets = firmware_offsets([(600, 60)], 40000)
    assert shift == 1440
    assert offsets[0][0] == 60 * (600 + 1440) - 40000


def test_firmware_offsets_reads_far_end_as_yesterday():
    # The R15 trap: at 02:00 a plan written as 18:25 then 01:00 (+1440) is read
    # as last night's, so its first target has already timed out.
    _, shift, offsets = firmware_offsets([(1105, 395), (1500, 245)], 7200)
    assert shift == -1440
    assert offsets[0][1] < 0


def test_firmware_offsets_second_overlap_aborts():
    with pytest.raises(PlanError, match="abort"):
        firmware_offsets([(1380, 60), (0, 60), (30, 60)], 0)


# --- schedule ------------------------------------------------------------------


def test_build_schedule_for_tonight():
    s = build_schedule(_targets(TRUNK, HORSEHEAD), DENVER_5PM, "America/Denver")
    a, b = s.targets
    assert (a.start_min, a.duration_min) == (1105, 395)
    assert (b.start_min, b.duration_min) == (1500, 245)
    assert a.start.isoformat() == "2026-11-08T18:25:00-07:00"
    assert a.end.isoformat() == "2026-11-09T01:00:00-07:00"
    assert b.start.isoformat() == "2026-11-09T01:00:00-07:00"
    assert b.end.isoformat() == "2026-11-09T05:05:00-07:00"
    assert not a.starts_now and not b.starts_now
    assert [t.target_id for t in s.targets] == [1, 2]
    assert s.timezone == "America/Denver"
    assert s.night_of == date(2026, 11, 8)
    assert s.warnings == []


def test_build_schedule_defaults_to_scope_zone():
    s = build_schedule(_targets(TRUNK), DENVER_5PM, None)
    assert s.timezone == "America/Denver"
    assert s.targets[0].start_min == 1105


def test_build_schedule_target_under_way_starts_now():
    clock = _clock("2026-11-08T20:00:00", "America/Denver", "2026-11-09T03:00:00")
    a, b = build_schedule(_targets(TRUNK, HORSEHEAD), clock, None).targets
    assert a.starts_now
    assert (a.start_min, a.duration_min) == (1200, 300)
    assert a.start.isoformat() == "2026-11-08T20:00:00-07:00"
    assert (b.start_min, b.duration_min) == (1500, 245)


def test_build_schedule_after_midnight():
    clock = _clock("2026-11-09T00:30:00", "America/Denver", "2026-11-09T07:30:00")
    a, b = build_schedule(_targets(TRUNK, HORSEHEAD), clock, None).targets
    assert a.starts_now and (a.start_min, a.duration_min) == (30, 30)
    assert (b.start_min, b.duration_min) == (60, 245)


def test_build_schedule_converts_into_scope_clock():
    # ALP's startup routine sets the scope's zone from its host, which is UTC on
    # watcher. Times written in Denver still land at the right instants.
    clock = _clock("2026-11-09T00:00:00", "Etc/UTC", "2026-11-09T00:00:00")
    a, b = build_schedule(_targets(TRUNK, HORSEHEAD), clock, "America/Denver").targets
    assert (a.start_min, a.duration_min) == (85, 395)  # 18:25 MST is 01:25 UTC
    assert (b.start_min, b.duration_min) == (480, 245)
    assert a.start.isoformat() == "2026-11-08T18:25:00-07:00"


def test_build_schedule_is_immune_to_scope_clock_error():
    # Scope clock 3 minutes fast: start_min shifts so the start instant does not.
    clock = _clock("2026-11-08T17:03:00", "America/Denver", "2026-11-09T00:00:00")
    a = build_schedule(_targets(TRUNK), clock, None).targets[0]
    assert a.start_min == 1108
    assert a.start.isoformat() == "2026-11-08T18:25:00-07:00"


def test_build_schedule_counts_real_minutes_across_dst_end():
    # US DST ends 2026-11-01 02:00: 18:00 MDT to 05:00 MST is 12 real hours.
    clock = _clock("2026-10-31T17:00:00", "America/Denver", "2026-10-31T23:00:00")
    t = _targets({**TRUNK, "start": "18:00", "end": "05:00"})
    a = build_schedule(t, clock, None).targets[0]
    assert (a.start_min, a.duration_min) == (1080, 720)
    assert a.end.isoformat() == "2026-11-01T05:00:00-07:00"


def test_build_schedule_refuses_time_missing_on_dst_start():
    # 2027-03-14 02:00 MST jumps to 03:00 MDT, so 02:30 never happens.
    clock = _clock("2027-03-13T20:00:00", "America/Denver", "2027-03-14T03:00:00")
    t = _targets({**TRUNK, "start": "22:00", "end": "02:30"})
    with pytest.raises(PlanError, match="does not exist"):
        build_schedule(t, clock, None)


def test_build_schedule_refuses_overlap():
    t = _targets(TRUNK, {**HORSEHEAD, "start": "00:30"})
    with pytest.raises(PlanError, match="overlap"):
        build_schedule(t, DENVER_5PM, None)


def test_build_schedule_allows_gap():
    t = _targets(
        {**TRUNK, "start": "19:00", "end": "21:00"},
        {**HORSEHEAD, "start": "22:00", "end": "23:00"},
    )
    a, b = build_schedule(t, DENVER_5PM, None).targets
    assert (a.start_min, a.duration_min, b.start_min, b.duration_min) == (
        1140,
        120,
        1320,
        60,
    )


def test_build_schedule_refuses_target_already_over_instead_of_moving_it():
    # At 02:00 the first target ended at 01:00. It is refused, never moved to
    # tomorrow night, and the message says which night was assumed.
    clock = _clock("2026-11-09T02:00:00", "America/Denver", "2026-11-09T09:00:00")
    with pytest.raises(PlanError, match="already over.*2026-11-08.*night_of"):
        build_schedule(_targets(TRUNK, HORSEHEAD), clock, None)
    rest = build_schedule(_targets(HORSEHEAD), clock, None).targets[0]
    assert rest.starts_now and rest.end.isoformat() == "2026-11-09T05:05:00-07:00"


def test_build_schedule_refuses_window_ended_a_minute_ago():
    clock = _clock("2026-11-08T23:01:00", "America/Denver", "2026-11-09T06:01:00")
    t = _targets({**TRUNK, "start": "21:00", "end": "23:00"})
    with pytest.raises(PlanError, match="already over"):
        build_schedule(t, clock, None)


def test_build_schedule_refuses_night_more_than_24h_ahead():
    clock = _clock("2026-11-09T02:00:00", "America/Denver", "2026-11-09T09:00:00")
    with pytest.raises(PlanError, match="24 hours"):
        build_schedule(_targets(TRUNK, HORSEHEAD), clock, None, date(2026, 11, 9))


def test_build_schedule_plans_tonight_in_the_morning_with_night_of():
    clock = _clock("2026-11-09T06:00:00", "America/Denver", "2026-11-09T13:00:00")
    with pytest.raises(PlanError, match="already over"):
        build_schedule(_targets(TRUNK, HORSEHEAD), clock, None)  # assumes last night
    s = build_schedule(_targets(TRUNK, HORSEHEAD), clock, None, date(2026, 11, 9))
    assert s.night_of == date(2026, 11, 9)
    assert s.targets[0].start.isoformat() == "2026-11-09T18:25:00-07:00"
    assert s.targets[1].end.isoformat() == "2026-11-10T05:05:00-07:00"


@pytest.mark.parametrize(
    "first, second",
    [
        (TRUNK, HORSEHEAD),  # listed backwards below
        (
            {**TRUNK, "start": "18:00", "end": "19:00"},
            {**HORSEHEAD, "start": "17:00", "end": "17:30"},
        ),
        (
            {**TRUNK, "start": "23:00", "end": "23:30"},
            {**HORSEHEAD, "start": "22:00", "end": "22:30"},
        ),
    ],
    ids=["reversed", "earlier-evening", "earlier-hour"],
)
def test_build_schedule_refuses_targets_out_of_order(first, second):
    targets = _targets(second, first) if first is TRUNK else _targets(first, second)
    with pytest.raises(PlanError, match="time order"):
        build_schedule(targets, DENVER_5PM, None)


def test_build_schedule_refuses_end_too_close():
    clock = _clock("2026-11-09T00:59:30", "America/Denver", "2026-11-09T07:59:30")
    with pytest.raises(PlanError, match="left"):
        build_schedule(_targets(TRUNK, HORSEHEAD), clock, None)


def test_build_schedule_refuses_target_with_under_five_minutes_left():
    clock = _clock("2026-11-08T22:58:00", "America/Denver", "2026-11-09T05:58:00")
    t = _targets({**TRUNK, "start": "21:00", "end": "23:00"})
    with pytest.raises(PlanError, match="only 2 minutes left"):
        build_schedule(t, clock, None)


def test_build_schedule_refuses_rounding_past_24h_with_the_24h_message():
    # Scope clock 40 s fast: the last end rounds to just over a day of the
    # scope's clock, which the firmware would read as last night's plan.
    clock = _clock("2026-11-08T06:00:50", "America/Denver", "2026-11-08T13:00:10")
    t = _targets({**TRUNK, "start": "05:50", "end": "06:00"})
    with pytest.raises(PlanError, match="24 hours"):
        build_schedule(t, clock, None, date(2026, 11, 8))


def test_build_schedule_warns_on_time_that_happens_twice():
    # US DST ends 2026-11-01 02:00: 01:30 happens in MDT and again in MST.
    clock = _clock("2026-10-31T20:00:00", "America/Denver", "2026-11-01T02:00:00")
    t = _targets({**TRUNK, "start": "22:00", "end": "01:30"})
    s = build_schedule(t, clock, None)
    assert s.targets[0].end.isoformat() == "2026-11-01T01:30:00-06:00"
    assert any("twice" in w for w in s.warnings)


def test_build_schedule_refuses_near_scope_midnight():
    clock = _clock("2026-11-08T23:59:00", "America/Denver", "2026-11-09T06:59:00")
    with pytest.raises(PlanError, match="midnight"):
        build_schedule(_targets(HORSEHEAD), clock, None)


@pytest.mark.parametrize(
    "start, end, message",
    [
        ("18:25", "18:27", "minimum is 5"),
        ("18:25", "18:25", "not after its start"),
        ("11:00", "13:00", "noon to noon"),
    ],
    ids=["short", "equal", "spans-noon"],
)
def test_build_schedule_refuses_bad_durations(start, end, message):
    t = _targets({**TRUNK, "start": start, "end": end})
    with pytest.raises(PlanError, match=message):
        build_schedule(t, DENVER_5PM, None)


def test_build_schedule_refuses_scope_clock_that_disagrees_with_its_zone():
    # Calendar says 17:00 but the zone name says it should be 00:00: the plan
    # worker could read either, so refuse rather than guess.
    clock = _clock("2026-11-08T17:00:00", "Etc/UTC", "2026-11-09T00:00:00")
    with pytest.raises(PlanError, match="disagrees"):
        build_schedule(_targets(TRUNK), clock, "America/Denver")


def test_build_schedule_needs_a_zone_when_scope_zone_unknown():
    clock = _clock("2026-11-08T17:00:00", "UTC+0", "2026-11-09T00:00:00")
    with pytest.raises(PlanError, match="timezone"):
        build_schedule(_targets(TRUNK), clock, None)


def test_build_schedule_warns_when_scope_zone_unknown_but_zone_given():
    clock = _clock("2026-11-08T17:00:00", "", "2026-11-09T00:00:00")
    s = build_schedule(_targets(TRUNK), clock, "America/Denver")
    assert s.targets[0].start_min == 1105
    assert any("zone" in w for w in s.warnings)


def test_build_schedule_will_not_guess_a_utc_scope_zone():
    # ALP's startup routine sets the scope's zone from its host (UTC on
    # watcher); the ZWO app sets the phone's. A UTC default is not trusted.
    clock = _clock("2026-11-09T00:00:00", "Etc/UTC", "2026-11-09T00:00:00")
    t = _targets({**TRUNK, "start": "01:25", "end": "08:00"})
    with pytest.raises(PlanError, match="pass timezone"):
        build_schedule(t, clock, None)
    assert build_schedule(t, clock, "UTC").timezone == "UTC"


def test_build_schedule_rejects_unknown_timezone():
    with pytest.raises(PlanError, match="timezone"):
        build_schedule(_targets(TRUNK), DENVER_5PM, "Mars/Olympus")


@pytest.mark.parametrize(
    "wall, utc, night",
    [
        ("2026-11-08T12:00:00", "2026-11-08T19:00:00", date(2026, 11, 8)),
        ("2026-11-08T23:00:00", "2026-11-09T06:00:00", date(2026, 11, 8)),
        ("2026-11-09T11:59:00", "2026-11-09T18:59:00", date(2026, 11, 8)),
    ],
)
def test_default_night_runs_noon_to_noon(wall, utc, night):
    from zoneinfo import ZoneInfo

    from seestar_mcp.plan_schedule import default_night

    clock = _clock(wall, "America/Denver", utc)
    assert default_night(clock.read_at, ZoneInfo("America/Denver")) == night


def test_build_schedule_needs_targets():
    with pytest.raises(PlanError):
        build_schedule([], DENVER_5PM, None)


def test_scope_clock_from_pi_get_time():
    result = {
        "year": 2026,
        "mon": 11,
        "day": 8,
        "hour": 17,
        "min": 0,
        "sec": 30,
        "time_zone": "America/Denver",
    }
    read_at = datetime(2026, 11, 9, 0, 0, 30, tzinfo=UTC)
    clock = ScopeClock.from_pi_get_time(result, read_at)
    assert clock.wall == datetime(2026, 11, 8, 17, 0, 30)
    assert clock.seconds_since_midnight == 61230
    assert clock.time_zone == "America/Denver"


@pytest.mark.parametrize("result", [None, 0, {"year": 2026}, {"hour": "x"}])
def test_scope_clock_rejects_malformed_reply(result):
    with pytest.raises(PlanError):
        ScopeClock.from_pi_get_time(result, DENVER_5PM.read_at)


# --- request payload -------------------------------------------------------------


def test_view_plan_params_shape():
    s = build_schedule(_targets(TRUNK, HORSEHEAD), DENVER_5PM, None)
    params = view_plan_params(s, "Trunk then Horsehead", True, True)
    assert params["plan_name"] == "Trunk then Horsehead"
    assert params["plan_target_af"] is True and params["viewplan_gohome"] is True
    assert params["update_time_seestar"] == "2026.11.08"
    first = params["list"][0]
    assert first == {
        "target_id": 1,
        "target_name": "IC 1396A",
        "target_ra_dec": [pytest.approx(21.609166, abs=1e-5), 57.5],
        "start_min": 1105,
        "duration_min": 395,
        "camera_mode": "tele",
        "lp_filter": True,
        "skip": False,
    }
    assert params["list"][1]["lp_filter"] is False
