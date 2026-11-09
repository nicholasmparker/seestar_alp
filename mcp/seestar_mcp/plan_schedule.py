"""Pure scheduling for on-device night plans (firmware ViewPlans). No device I/O.

How the firmware times a plan (seestar-spec, spec/control/plan-scheduling.md):

- R14: when the plan worker starts it reads the scope's local clock once, as
  seconds since local midnight ``n``, then derives every deadline from ``n``
  and a monotonic clock. A target starts ``max(60*start_min - n, 0)`` seconds
  after the worker starts and ends ``60*(start_min + duration_min) - n``
  seconds after it.
- R15-R17: a target that starts before the previous one ends is moved a day
  later (a second such overlap aborts the plan); a plan whose last end has
  passed runs tomorrow; a plan whose last end is more than a day away is read
  as last night's.
- R22: a target whose end is within 60 s of now fails at once (code 513).

So this module turns the person's wall-clock times into absolute instants and
then into ``start_min`` relative to the scope's own ``n`` (from ``pi_get_time``).
That keeps the schedule right whatever the scope's time zone, clock error or a
daylight-saving change, provided the plan ends within 24 hours and no target
overlaps the one before it. ``firmware_offsets`` replays R15 so the schedule
returned to the caller is the one the firmware will run.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

DAY_S = 86400
END_LEAD_S = 60  # R22: the firmware fails a target whose end is this close
SEND_MARGIN_S = 30  # time for the settings writes and set_view_plan to land
MIDNIGHT_GUARD_S = 120  # refuse when the scope's local midnight is this close
MIN_TARGET = timedelta(minutes=5)
MAX_AHEAD = timedelta(hours=24)
ZONE_SKEW_LIMIT = timedelta(minutes=10)

# Keys people reach for per target that the firmware only has globally.
GLOBAL_ONLY_KEYS = frozenset(
    {"exposure", "exposure_s", "exposure_ms", "exp_ms", "stack_l", "gain"}
)

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_RA_SEXA = re.compile(
    r"^\s*(\d{1,2})\s*[h:\s]\s*(\d{1,2})\s*(?:[m:\s]\s*(\d{1,2}(?:\.\d+)?)\s*s?)?\s*m?\s*$"
)
_DEC_SEXA = re.compile(
    r"^\s*([+-]?)\s*(\d{1,2})\s*[d°:\s]\s*(\d{1,2})\s*"
    r"(?:['m:\s]\s*(\d{1,2}(?:\.\d+)?)\s*(?:\"|s)?)?\s*['m]?\s*$"
)


class PlanError(ValueError):
    """The plan cannot be scheduled. ``errors`` lists every reason found."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_ra(value: Any) -> float:
    """J2000 right ascension in decimal hours, from hours or 'HH:MM:SS'/'21h36m33s'."""
    if isinstance(value, bool):
        raise ValueError("ra must be hours or 'HH:MM:SS', not a boolean")
    hours = _number(value)
    if hours is None and isinstance(value, str):
        m = _RA_SEXA.match(value)
        if m:
            h, mi, s = int(m[1]), int(m[2]), float(m[3] or 0)
            if mi >= 60 or s >= 60:
                raise ValueError(f"ra {value!r}: minutes and seconds must be below 60")
            hours = h + mi / 60 + s / 3600
    if hours is None:
        raise ValueError(f"ra {value!r}: use decimal hours or 'HH:MM:SS'")
    if not 0 <= hours < 24:
        hint = (
            f" ({hours} looks like degrees; divide by 15)" if 24 <= hours < 360 else ""
        )
        raise ValueError(f"ra must be in hours, 0 to 24{hint}")
    return hours


def parse_dec(value: Any) -> float:
    """J2000 declination in decimal degrees, from degrees or '+DD:MM:SS'."""
    if isinstance(value, bool):
        raise ValueError("dec must be degrees or '+DD:MM:SS', not a boolean")
    degrees = _number(value)
    if degrees is None and isinstance(value, str):
        m = _DEC_SEXA.match(value)
        if m:
            d, mi, s = int(m[2]), int(m[3]), float(m[4] or 0)
            if mi >= 60 or s >= 60:
                raise ValueError(f"dec {value!r}: minutes and seconds must be below 60")
            degrees = (d + mi / 60 + s / 3600) * (-1 if m[1] == "-" else 1)
    if degrees is None:
        raise ValueError(f"dec {value!r}: use decimal degrees or '+DD:MM:SS'")
    if not -90 <= degrees <= 90:
        raise ValueError("dec must be in degrees, -90 to 90")
    return degrees


class PlanTarget(BaseModel):
    """One target of a night plan. Exposure and gain are global, not per target."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        description="Target name, shown in the app and used for saved images.",
    )
    ra: float | str = Field(
        description="J2000 right ascension: decimal hours (0-24) or 'HH:MM:SS'."
    )
    dec: float | str = Field(
        description="J2000 declination: decimal degrees (-90 to 90) or '+DD:MM:SS'."
    )
    start: str = Field(
        description="Local start time, 24-hour 'HH:MM', in the plan's timezone."
    )
    end: str = Field(
        description="Local end time, 'HH:MM'. Earlier than start means after midnight."
    )
    lp_filter: bool = Field(
        description="True: light-pollution filter after the goto. False: IR-cut."
    )
    camera_mode: Literal["tele"] = Field(
        default="tele", description="Only the telephoto camera is supported."
    )

    @model_validator(mode="before")
    @classmethod
    def _refuse_global_settings(cls, data: Any) -> Any:
        if isinstance(data, dict):
            bad = sorted(k for k in data if k in GLOBAL_ONLY_KEYS)
            if bad:
                raise ValueError(
                    f"{', '.join(bad)} cannot be set per target: exposure and gain "
                    "are global on the scope. Pass exposure_s to plan_night for the "
                    "whole plan; gain follows the scope's own setting."
                )
        return data

    @field_validator("name")
    @classmethod
    def _plain_name(cls, v: str) -> str:
        if any(c in v for c in "/\\") or any(ord(c) < 32 for c in v):
            raise ValueError("name must not contain slashes or control characters")
        return v

    @field_validator("ra", mode="before")
    @classmethod
    def _ra(cls, v: Any) -> float:
        return parse_ra(v)

    @field_validator("dec", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> float:
        return parse_dec(v)

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"{v!r} is not a 24-hour 'HH:MM' time")
        return v

    def wall(self, which: Literal["start", "end"]) -> time:
        h, m = getattr(self, which).split(":")
        return time(int(h), int(m))


@dataclass(frozen=True)
class ScopeClock:
    """The scope's local calendar time (``pi_get_time``) and when it was read."""

    wall: datetime  # naive: the scope's own local wall clock
    time_zone: str  # as the scope reports it; may be empty or not an IANA name
    read_at: datetime  # aware UTC instant on this host when the reply arrived

    @classmethod
    def from_pi_get_time(cls, result: Any, read_at: datetime) -> "ScopeClock":
        if not isinstance(result, dict):
            raise PlanError([f"pi_get_time returned {result!r}, not a time"])
        try:
            wall = datetime(
                *(int(result[k]) for k in ("year", "mon", "day", "hour", "min", "sec"))
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanError([f"pi_get_time reply is malformed: {result!r}"]) from exc
        return cls(wall, str(result.get("time_zone") or "").strip(), read_at)

    @property
    def seconds_since_midnight(self) -> int:
        w = self.wall
        return w.hour * 3600 + w.minute * 60 + w.second


@dataclass(frozen=True)
class ScheduledTarget:
    target_id: int
    target: PlanTarget
    start: datetime  # when the firmware will start it, in the plan's timezone
    end: datetime
    starts_now: bool
    start_min: int
    duration_min: int


@dataclass(frozen=True)
class Schedule:
    timezone: str
    clock: ScopeClock
    targets: list[ScheduledTarget]
    warnings: list[str] = field(default_factory=list)


def firmware_offsets(
    entries: list[tuple[int, int]], n: int
) -> tuple[int, int, list[tuple[int, int]]]:
    """Replay the firmware's day normalisation (R15) for (start_min, duration_min).

    Returns (carry, shift, [(start_s, end_s)]) with offsets in seconds from the
    worker's start. An end at or below zero means the target has already timed out.
    """
    carry = 0
    prev_end: int | None = None
    s: list[int] = []
    for start_min, duration_min in entries:
        si = start_min + carry
        if prev_end is not None and si < prev_end:
            if carry:
                raise PlanError(
                    ["the firmware would abort this plan: a second overlap (R17)"]
                )
            carry = 1440
            si = start_min + carry
        s.append(si)
        prev_end = si + duration_min
    first_start = 60 * s[0]
    last_end = 60 * (s[-1] + entries[-1][1])
    if n >= last_end:
        shift = 1440
    elif n >= first_start:
        shift = 0
    elif n + DAY_S < last_end:
        shift = -1440
    else:
        shift = 0
    offsets = [
        (max(60 * (si + shift) - n, 0), 60 * (si + shift + dur) - n)
        for si, (_, dur) in zip(s, entries, strict=True)
    ]
    return carry, shift, offsets


def _zone(name: str) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _is_utc(zone: ZoneInfo) -> bool:
    probe = datetime(2026, 1, 1, tzinfo=zone), datetime(2026, 7, 1, tzinfo=zone)
    return all(p.utcoffset() == timedelta(0) for p in probe)


def _wall_instant(d: date, hm: time, zone: ZoneInfo, label: str) -> datetime:
    """The UTC instant of wall time ``hm`` on ``d`` in ``zone``; refuses DST gaps."""
    instant = datetime.combine(d, hm, tzinfo=zone).astimezone(UTC)
    if instant.astimezone(zone).time() != hm:
        raise PlanError(
            [f"{label} {hm:%H:%M} does not exist on {d} in {zone.key} (clocks jump)"]
        )
    return instant


def _next_at(
    after: datetime, hm: time, zone: ZoneInfo, label: str, inclusive: bool
) -> datetime:
    """Earliest wall time ``hm`` in ``zone`` after (or at, if inclusive) ``after``."""
    day = after.astimezone(zone).date()
    for k in range(-1, 3):
        d = day + timedelta(days=k)
        candidate = datetime.combine(d, hm, tzinfo=zone).astimezone(UTC)
        if candidate > after or (inclusive and candidate == after):
            return _wall_instant(d, hm, zone, label)
    raise AssertionError("unreachable: a wall time recurs within two days")


def _latest_before(before: datetime, hm: time, zone: ZoneInfo, label: str) -> datetime:
    """Latest wall time ``hm`` in ``zone`` strictly before ``before``."""
    day = before.astimezone(zone).date()
    for k in range(1, -3, -1):
        d = day + timedelta(days=k)
        candidate = datetime.combine(d, hm, tzinfo=zone).astimezone(UTC)
        if candidate < before:
            return _wall_instant(d, hm, zone, label)
    raise AssertionError("unreachable: a wall time recurs within two days")


def _check_clock(clock: ScopeClock, warnings: list[str]) -> list[str]:
    scope_zone = _zone(clock.time_zone)
    if scope_zone is None:
        warnings.append(
            f"the scope reports time zone {clock.time_zone!r}, which is not a known "
            "zone, so its clock could not be cross-checked"
        )
        return []
    expected = clock.read_at.astimezone(scope_zone).replace(tzinfo=None)
    skew = clock.wall - expected
    if abs(skew) > ZONE_SKEW_LIMIT:
        return [
            f"the scope's clock ({clock.wall:%Y-%m-%d %H:%M}) disagrees with its own "
            f"zone {clock.time_zone} ({expected:%Y-%m-%d %H:%M}) by "
            f"{skew.total_seconds() / 60:+.0f} min, so the plan worker could read "
            "either; set the scope's time (the ZWO app sets it on connect) and retry"
        ]
    return []


def build_schedule(
    targets: list[PlanTarget], clock: ScopeClock, timezone: str | None
) -> Schedule:
    """Resolve wall-clock targets into the firmware schedule the scope will run.

    Rules: targets run in list order. The first target's end is its next
    occurrence after now and its start the latest one before that end, so a
    target already under way starts now. Each later target starts at the first
    occurrence of its start time at or after the previous target's start, must
    not begin before the previous one ends, and ends at the next occurrence of
    its end time. The whole plan must end within 24 hours.
    """
    if not targets:
        raise PlanError(["a plan needs at least one target"])
    warnings: list[str] = []
    zone_name = timezone or clock.time_zone
    zone = _zone(zone_name)
    if zone is None:
        if timezone:
            raise PlanError([f"timezone {timezone!r} is not a known IANA zone"])
        raise PlanError(
            [
                f"the scope's time zone {clock.time_zone!r} is not a known zone; pass "
                "timezone (for example 'America/Denver') for the times you wrote"
            ]
        )
    if timezone is None and _is_utc(zone):
        warnings.append(
            f"times were read as UTC, the scope's own zone ({zone_name}); pass "
            "timezone if you meant local time"
        )
    errors = _check_clock(clock, warnings)
    n = clock.seconds_since_midnight
    if n > DAY_S - MIDNIGHT_GUARD_S:
        errors.append(
            "it is less than 2 minutes to midnight on the scope's clock; retry just "
            "after midnight"
        )
    if errors:
        raise PlanError(errors)

    now = clock.read_at
    windows: list[tuple[datetime, datetime]] = []
    for i, t in enumerate(targets):
        if i == 0:
            end = _next_at(now, t.wall("end"), zone, f"{t.name} end", False)
            start = _latest_before(end, t.wall("start"), zone, f"{t.name} start")
        else:
            prev_start, prev_end = windows[-1]
            start = _next_at(prev_start, t.wall("start"), zone, f"{t.name} start", True)
            if start < prev_end:
                errors.append(
                    f"{t.name} starts at {t.start}, before {targets[i - 1].name} ends "
                    f"at {targets[i - 1].end}: targets must not overlap"
                )
            end = _next_at(start, t.wall("end"), zone, f"{t.name} end", False)
        length = end - start
        if length < MIN_TARGET:
            minutes = length.total_seconds() / 60
            errors.append(f"{t.name} runs {minutes:.0f} minutes; the minimum is 5")
        elif length >= MAX_AHEAD:
            errors.append(f"{t.name} must run less than 24 hours")
        windows.append((start, end))
    if errors:
        raise PlanError(errors)

    last_end = windows[-1][1]
    if last_end - now > MAX_AHEAD:
        raise PlanError(
            [
                f"the plan would end at {last_end.astimezone(zone):%Y-%m-%d %H:%M} "
                f"{zone_name}, more than 24 hours from now, which the firmware "
                "cannot schedule. Targets run in list order; if the first targets "
                "are already over tonight, leave them out"
            ]
        )

    def minutes_at(instant: datetime) -> int:
        return math.floor((n + (instant - now).total_seconds()) / 60 + 0.5)

    entries: list[tuple[int, int]] = []
    for start, end in windows:
        start_min = n // 60 if start <= now else minutes_at(start)
        entries.append((start_min, minutes_at(end) - start_min))
    carry, shift, offsets = firmware_offsets(entries, n)
    if carry or shift:
        raise PlanError(
            [
                f"internal check failed: the firmware would shift this plan ({carry=}, "
                f"{shift=})"
            ]
        )
    first_end = offsets[0][1]
    if first_end <= END_LEAD_S + SEND_MARGIN_S:
        raise PlanError(
            [
                f"{targets[0].name} ends {max(first_end, 0)} s from now; the firmware "
                f"fails a target that ends within {END_LEAD_S} s, so it must end more "
                f"than {END_LEAD_S + SEND_MARGIN_S} s ahead"
            ]
        )

    scheduled = [
        ScheduledTarget(
            target_id=i + 1,
            target=t,
            start=(now + timedelta(seconds=start_s)).astimezone(zone),
            end=(now + timedelta(seconds=end_s)).astimezone(zone),
            starts_now=start_s == 0,
            start_min=start_min,
            duration_min=duration_min,
        )
        for i, (t, (start_s, end_s), (start_min, duration_min)) in enumerate(
            zip(targets, offsets, entries, strict=True)
        )
    ]
    return Schedule(zone_name, clock, scheduled, warnings)


def view_plan_params(
    schedule: Schedule, plan_name: str, af_on_switch: bool, close_arm: bool
) -> dict[str, Any]:
    """The ``set_view_plan`` params for a full inline plan (seestar-spec set_view_plan).

    ``plan_target_af`` and ``viewplan_gohome`` here are held with the plan, but the
    firmware acts on the persistent settings of the same names, which the caller
    must also set with ``set_setting``.
    """
    edited = schedule.clock.read_at.astimezone(_zone(schedule.timezone))
    return {
        "plan_name": plan_name,
        "update_time_seestar": f"{edited:%Y.%m.%d}",
        "plan_target_af": af_on_switch,
        "viewplan_gohome": close_arm,
        "list": [
            {
                "target_id": s.target_id,
                "target_name": s.target.name,
                "target_ra_dec": [s.target.ra, s.target.dec],
                "start_min": s.start_min,
                "duration_min": s.duration_min,
                "camera_mode": s.target.camera_mode,
                "lp_filter": s.target.lp_filter,
                "skip": False,
            }
            for s in schedule.targets
        ],
    }
