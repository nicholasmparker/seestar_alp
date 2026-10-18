# Guarded Night Survey Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give K2 a bounded, independently verified nighttime visibility survey.

**Architecture:** Pure safety/protocol helpers, a loopback-only fresh-frame bridge,
and a local runner with private incremental artifacts. Existing native ALP
commands are used; no device-native service or extra credential handling.

**Tech Stack:** Python 3.12+, existing astropy/numpy/OpenCV, urllib, pytest.

**Spec:** `docs/superpowers/specs/2026-10-17-guarded-night-survey-design.md`

## Global Constraints

- Sun strictly below -18 degrees for the confirmed ground-based tripod setup
  (user-approved 2026-10-17 correction from -40); primary half-FOV <=3 degrees;
  level <=1 degree. Earth occlusion, not angular clearance, protects unfolding.
- Native integer id/code=0/method validation; finite numbers exclude booleans.
- Pulse speed <=240 native 15-arcsecond steps/second and duration <=2 seconds;
  calibration 120 for 1 second, fine speed 60; displacement <=4 degrees.
- Targets 20..65 degrees; 12 headings x 3 altitude rows; all gaps unknown.
- No raw state/site/credentials/image bodies in logs; private files 0600/0700.
- Wait <=4 hours, active run <=90 minutes; no uncertain motion retries.

## Review Focus

- Wrong or stale device/host clock: reject before motion; test malformed/skewed timestamps.
- A valid but stale completion/frame: reject across old timestamps and process restarts.
- Network failure after accepted movement: no replay; pulse expiry plus partial manifest.
- Another controller or imaging job: refuse takeover and stop further commands.
- Stars in a partially blocked field: report star evidence only, never all-clear/tree classification.

### Task 1: Protocol and safety kernel

**Files:** Create `device/survey_safety.py`; test `tests/test_survey_safety.py`.

**Interfaces:** `SurveyError(code)`, `native_result(reply, method)`,
`horizon(result)->(alt,az)`, `preflight_state(state, schedule, view)`,
`sun_gate(sun_alt, half_fov, level)`, `pulse_for(current,target,axes)`.

- [ ] Write failing tests for malformed replies, shape/order, preflight failures,
  Sun/FOV boundaries, pulse bounds/direction and coordinate wrap.
- [ ] Run `.venv/bin/python -m pytest tests/test_survey_safety.py -q`; expect failure on missing implementation.
- [ ] Implement the named helpers and independent coordinate conversion helpers.
- [ ] Run that test command; expect all pass. Commit the kernel and tests.

### Task 2: Fresh, private camera snapshots

**Files:** Create `device/survey_snapshot.py`; modify `device/telescope.py`,
`device/protocols/imager.py`; test `tests/test_survey_snapshot.py`.

**Interfaces:** `snapshot(imager, timeout)->dict` with instance id, sequence,
frame age, dimensions, base64 PNG. Loopback action `survey_snapshot` bypasses
generic response logging and refuses non-preview/nonlocal calls.

- [ ] Write failing tests for fresh-only frames, no-image/stale/timeout rejection,
  process identity, loopback restriction and absence of image bodies in logs.
- [ ] Run snapshot tests; expect failure for missing behavior.
- [ ] Add atomic frame freshness metadata and the bounded snapshot action.
- [ ] Run snapshot plus telescope/protocol tests; expect all pass. Commit.

### Task 3: Runner, CLI, artifacts, and K2 operations

**Files:** Create `device/night_survey.py`, `tests/test_night_survey.py`,
`docs/guarded-night-survey.md`.

**Interfaces:** `SurveyClient.native/action`, `SurveyRunner.run`, CLI
`python -m device.night_survey preflight|run|start|status|stop`.

- [ ] Write failing tests using controlled transport/state/clock for refused
  preflights (zero motion), one-shot arm opening, stale solve rejection,
  axis calibration, cancelled/timeout pulse, private manifests and run lock.
- [ ] Run runner tests; expect failure on absent feature.
- [ ] Implement CLI, bounded state machine, two-field calibration, capture grid,
  failure cleanup, background lifecycle, and operational documentation.
- [ ] Run all survey tests and CLI integration tests; expect all pass. Commit.
- [ ] Run full Linux suite and Ruff; require all pass; obtain independent review.
- [ ] Fix important findings with reproducing tests, merge reviewed PR, deploy,
  preserve private configuration, update K2 recipe and test K2's actual behavior.
- [ ] Run live preflight/smoke/full survey only when all runtime gates pass;
  report measured coverage and unresolved cells, not a fabricated horizon mask.
