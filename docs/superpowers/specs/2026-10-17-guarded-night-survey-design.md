# Guarded nighttime visibility survey

## Intent and authority

Give K2 a reusable local command on Watcher to open the cleared Seestar and
collect an evidence-backed, coarse sky-visibility survey. The user approved
drafting, testing, review, deployment, and live testing without further approval
rounds, and reconfirmed that the telescope is outside and clear for movement.
This is a new control feature. Implementation is inline with independent review.

## Scope and honest output

The first pass samples 12 azimuths at altitudes 60, 40, and 20 degrees. It is
not a continuous panorama or a leaf-edge horizon mask. Each sample contains a
fresh private camera image, measured pointing, and a fresh plate-solve outcome.
Successful solving means stars were detected in that field; it does not prove
the entire field is unobstructed. Failed solving means unknown, not trees.
Unsampled areas remain unknown. No automatic interpolation or observing
scheduler edits. A human/vision review can subsequently identify visible trees
in the retained frames; the runner does not fabricate this classification.

## Architecture

- A local Python CLI uses ALP's existing loopback action endpoint. No new
  listener, device-native Alpaca enablement, authentication extraction, or cloud
  image service. All command replies require native integer id, code zero,
  matching method, and the expected result shape.
- A small loopback-only ALP snapshot action returns actual raw preview frames
  and freshness metadata, never loading placeholders or annotated MJPEG frames.
  Image bodies are never logged. A process instance id plus frame sequence
  distinguishes restarts; a frame must arrive after the capture request.
- The runner has read-only `preflight`, bounded `run`, background `start`,
  `status`, and cooperative `stop`. It owns a private run directory, process
  lock, manifest, and images. No raw native-state dumps, credentials, or site
  coordinates are written. Output permission is 0700/0600. Stop does not park.

## Safety and motion

Fail closed on absent/invalid site, unsynchronized host clock, device clock
skew over 10 seconds, EQ mode, non-idle mount/scheduler, active observing job,
invalid/unknown optics, low battery/temperature warning, missing physical
clearance, or failed native authentication. Existing preview may be replaced
under the user's explicit approval; unrelated observing work may not.

No daytime survey. Require the Sun below -40 degrees and an alt-az mount
with fresh level error <=1 degree; the accepted hardware optical-axis envelope
is -5 through 90 degrees. Primary-camera half-diagonal must be <=3 degrees.
This provides at least 32 degrees center-to-optical-field clearance over the
entire accepted envelope, independent of heading calibration. Recheck current
Sun altitude before every motion and periodically while waiting. No bypass.
If the device/model cannot support that envelope, reject rather than assume.

Open exactly once using `scope_move_to_horizon`; verify fresh native close=false
and idle plus a new matching completion event. A timeout is unconfirmed and
must not be blindly retried. After opening, movement uses only finite
`scope_speed_move` pulses (speed <=4000, duration <=2 seconds), never an
unbounded goto. Calibrate axis signs with one-second pulses. Check displacement,
cross-axis motion, idle state, and progress after each pulse. Reject >4 degrees
per pulse, wrong direction, stalled motion, or unexpected position/state.
Targets stay 20..65 degrees altitude. No park on error; leave stationary with
tracking off and report any inability to confirm that state. Final cleanup may
stop only preview/tracking that this run started. Unknown external activity
causes a stop, not a takeover.

## Pointing calibration and image evidence

Use native horizon list order [altitude, azimuth], documented in seestarpy
status.py, and native equatorial coordinates. Convert coordinates using current
private device site and UTC (FK5 equinox-of-date for native RA/Dec). Internal
coordinate agreement is not calibration. Require two fresh stellar plate solves
at high altitude, at separated headings, to derive/check a small pointing
offset (agreement <=1 degree); require fresh level <=1 degree. Do not silently
sync the mount or use guessed pointing. A failed calibration aborts collection.
Check later successful solves against expected pointing and abort on drift.

For each capture: verify arrival twice, stop tracking, wait for settling, require
a newly received camera frame, then request a fresh plate solve and correlate
the new PlateSolve event by its timestamp. Cached events cannot pass. Record
actual coordinates and FOV, not just requested coordinates. Save incremental
results so cancellation leaves a truthful partial run.

## Limits and recovery

Waiting for safe darkness is bounded to four hours; active survey <=90 minutes.
Native calls have HTTP deadlines; every poll, pulse, solve, and capture is
bounded. No retries of uncertain motion. One run per telescope. SIGTERM and a
stop file request cooperative cancellation; hardware pulse expiry bounds motion
even if the process dies. Restart creates a new run, never replays a motion.

## Verification and rollout

Unit tests cover malformed/NaN/bool payloads, stale events/frames, Sun boundaries,
trajectory envelope, wrong-axis motion, timeouts, duplicate run exclusion,
permissions, partial results, and no unsafe calls on preflight failures.
Exercise the real CLI against a controlled loopback fake transport. Run the
existing full Linux suite and Ruff. Independent review must explicitly challenge
physical safety assumptions, stale calibration, external-control races, and
false claims of completion. Deploy reviewed commits only; preserve private ALP
configuration and existing key. K2 performs preflight and a live smoke run,
then the full coarse survey only after all safety checks pass.

## Alternatives considered

A manual phone panorama is faster but is not repeatable by K2 alone. Enabling
device-native Alpaca would add another control interface and is unnecessary.
Continuous daylight skyline mapping is intentionally outside this night-only
feature. The remaining limitation is that darkness/clouds can prevent useful
tree silhouettes, and failed solving cannot identify their cause.
