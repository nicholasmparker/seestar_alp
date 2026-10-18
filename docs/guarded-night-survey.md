# Guarded nighttime visibility survey

This is a local operator tool for the reviewed S30 Pro alt-az setup, not a
generic unattended observatory or a continuous skyline mapper. Use the normal
ALP virtual environment from the repository root. Do not enable device-native
Alpaca or change SSH permissions to use it.

```sh
venv/bin/python -m device.night_survey preflight
venv/bin/python -m device.night_survey start --clearance-confirmed --max-cells 1 --wait-seconds 14400
venv/bin/python -m device.night_survey status
venv/bin/python -m device.night_survey stop
```

After the live one-cell smoke test is verified, the full coarse survey is:

```sh
venv/bin/python -m device.night_survey start --clearance-confirmed --max-cells 36 --wait-seconds 14400
```

The returned run id identifies the run; use `--run-id ID` with status/stop.
`start` means a worker was launched, **not** that hardware moved or a survey
completed. `status` reports phase and measured/expected sample counts. If the
worker disappeared, its state is interrupted/unconfirmed, never successful.
Do not reuse a run id or blindly retry an uncertain arm/motion command.

## Authority and prerequisites

`--clearance-confirmed` represents a real, current user confirmation that the
ground-based tripod is stable, the body/arm is clear, and the scope is free for
this run. Never add it merely to bypass a refusal. It does not cover a moved
setup. Existing idle star preview may be used/replaced; other observing work
is refused. Keep the phone and other controllers from issuing commands during
the run. The runner's per-device lock excludes other copies of this runner;
it cannot lock out the manufacturer's phone app.
Close unused live image viewers: a pre-existing or newly opened viewer retains
its stream, which may congest a slow control link and cause a safe timeout.

The runner checks authentication, synchronized host/device clocks, private
device site, mount state, scheduler state, battery/thermal warnings, level,
main-camera geometry, and Sun altitude. Firmware 9.75 does not expose an
undervoltage flag in the observed state; if present and asserted, it is refused.
No gate has a force/bypass flag. Sun must be strictly below -18 degrees
(astronomical night), checked before opening and every movement. This is a
darkness criterion for the stellar survey, not a requirement for 30-degree
angular separation from a Sun already occluded by Earth. The safety argument
requires the confirmed ground-based tripod setup, including during unfolding;
it does not authorize daytime, airborne, or a moved setup. This replaces the
unnecessarily restrictive -40-degree rule. A four-hour wait may expire without
movement. Active survey has a 90-minute deadline.

## What happens

1. Open once using the native horizon command and verify a new completion
   event plus fresh mount state. Unsupported `master_cli` is not attempted.
2. Establish finite-pulse axis directions; each pulse expires in at most two
   seconds on the device. Verify fresh state and displacement after every pulse.
   Native speed is measured in 15-arcsecond steps/second, not arcseconds/second:
   calibration uses 120 for 1 second (nominal 0.5 degrees); survey speed never
   exceeds 240 (nominal 1 degree/second). Retain the 4-degree measured-displacement
   abort. Private
   manifest motion evidence counts attempted versus bounded/idle-verified pulses
   and retains the first three measured deltas; it is not stellar calibration.
3. At two high-altitude headings, require fresh stellar solutions and consistent
   pointing offsets. Calibration failure stops the run; no guessed north,
   fabricated coordinates, or silent mount synchronization.
4. Visit the requested part of a 12-heading by 3-altitude grid. At each position,
   settle, require two new preview frames, bracket capture with pointing reads,
   and collect a fresh plate-solve outcome. Save results incrementally.
   Allow up to 45 seconds for two newly received full-resolution preview frames;
   this remains bounded and does not relax freshness or motion checks.
   Release a temporary image-stream subscription after capture or failure, so
   full-resolution frames do not congest the control link between samples.
   Preserve existing image viewers and any viewer that takes over during capture;
   this subscription cleanup does not stop the telescope's observing mode.
   A known subscription-send failure fails the capture as `camera_stream_uncertain`
   and refuses further captures until an approved, idle-state ALP restart. Socket
   send success is not a firmware acknowledgement; check live traffic cessation.
5. Leave the scope stationary, tracking off. Do not automatically park on error.
   A network failure means physical state may be unconfirmed even after the
   finite pulse should have expired. Inspect status before any subsequent move.
   Do not stop preview as cleanup: firmware provides no ownership token, so an
   apparently identical preview may belong to another controller by then.

## Evidence and limits

Artifacts are private under `~/.local/state/seestar-survey/RUN_ID/`:
`manifest.json`, actual camera PNGs, and a bounded worker log. Do not post raw
images, site coordinates, full device state, or credentials into conversations.
The manifest contains sky directions, frame identity/checksum, timestamps,
calibration provenance, and these conservative labels:

- `stars_detected_not_all_clear`: a fresh plate solve detected stars in the
  sampled field. Trees may still obscure part of it.
- `unknown`: no verified stellar solution; trees, clouds, focus, exposure, and
  other causes are not distinguished automatically.

The bridge verifies two newly **received** frames, not device exposure times:
this protocol does not expose a verified acquisition timestamp. Buffered images
cannot independently prove the sampled direction or tree location. The labels
above come from the separate, fresh stellar solve at verified stationary pointing;
do not turn a saved image alone into an obstruction measurement.

Survey commands use a dedicated loopback action that disables socket replay.
Completion evidence is retained independently of the UI event stream, scoped to
the current connection, and anchored after the accepted command's device timestamp.
Plate solves must also show a new working/start event. Reconnection invalidates
pending proof; it never restarts a movement automatically.

Unmeasured directions remain unknown. The camera frames can support a private
visual review of tree silhouettes, but no automatic tree classification is
claimed. A one-cell smoke test is not a 36-cell survey. A complete 36-cell run
is still a coarse visibility sampling, not a continuous obstruction mask.
No K2 memories, observing schedules, telescope firmware, or external issues
are modified by the runner.

## Deployment and rollback

Deploy a reviewed Git revision and restart ALP only with interruption authority.
The snapshot action is loopback-only and does not log image bodies. Preserve
`device/config.toml` and the interoperability key. Stop the survey worker and
confirm no active imaging/motion before rolling back ALP. Existing commands and
the standard UI remain available; this tool adds no network listener.
