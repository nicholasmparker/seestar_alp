# seestar-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant
control a Seestar telescope — point at targets, image, read/change settings,
and build and run schedules — by wrapping the seestar_alp
[`ssalp_api_client`](../cli). It talks to a **running seestar_alp device
server** over its Alpaca HTTP API; it does not talk to the telescope directly.

```
AI assistant (MCP client)
        │  MCP stdio
   seestar-mcp            ← this package (thin async tool layer)
        │  ssalp_api_client
seestar_alp device server ──HTTP──> Seestar
```

Two ways to use it:

- **As a Claude Code plugin** (recommended) — see [`plugin/README.md`](plugin/README.md)
  for marketplace/plugin install. The plugin also ships skills that teach the
  assistant how to plan sessions, build schedules, handle settings
  compatibility, and stay safe.
- **Standalone** — as an MCP server for Claude Desktop or any other MCP client,
  described below.

## Requirements

- Python 3.13+
- A reachable seestar_alp device server (the normal seestar_alp install,
  default `http://localhost:5555`).

## Install

`ssalp-api-client` is not on PyPI — it lives in this repo at [`cli/`](../cli) —
so install both from a clone, into the same environment:

```bash
python -m venv ~/.venvs/seestar
~/.venvs/seestar/bin/pip install -e ./cli -e ./mcp
```

This puts the `seestar-mcp` console script in `~/.venvs/seestar/bin`. Make sure
that directory is on the PATH of whatever launches the MCP client, or symlink
the script somewhere that is (e.g. `ln -sf ~/.venvs/seestar/bin/seestar-mcp ~/.local/bin/seestar-mcp`).

## Configure

The device-server URL is resolved by `ssalp_api_client`'s own config, so the
MCP server honors the same settings as the CLI:

| Source | Example | Notes |
|---|---|---|
| `SEESTAR_BASE_URL` env | `http://192.168.1.51:5555` | Explicit override; wins if set |
| `SSALP_HOST` / `SSALP_PORT` env | `192.168.1.51` / `5555` | |
| `ssalp.toml` / `~/.config/ssalp/config.toml` | | |
| default | `http://localhost:5555` | |

Every tool also takes a `telescope_id` argument (default `1`; use `0` for a
federated/all-devices call).

## Run

```bash
seestar-mcp          # stdio MCP server
# or
python -m seestar_mcp
```

To reach the server from an agent on another host, run it over streamable
HTTP on loopback and forward the port with SSH:

```bash
seestar-mcp --transport streamable-http --port 8769   # serves http://127.0.0.1:8769/mcp
```

The HTTP transport has no authentication, so `--host` accepts only loopback
addresses (`127.0.0.1`, `::1`, `localhost`); requests whose `Host` header is
not loopback are rejected (DNS-rebinding protection).

### Register with an MCP client

Point your client at the command. Example client config entry:

```json
{
  "mcpServers": {
    "seestar": { "command": "seestar-mcp" }
  }
}
```

(The Claude Code plugin ships this wiring in `plugin/.mcp.json` — you don't
write it by hand when installing via the plugin.)

## Tools

Each returns the device's response, or `{"ok": false, "error": "..."}` on a
connection/API error.

| Group | Tools |
|---|---|
| **State** | `get_device_state`, `get_view_state` |
| **Pointing** | `goto_target`, `scope_goto`, `start_solve`, `get_solve_result`, `is_goto_completed_ok`, `stop_goto_target` |
| **Alignment** | `polar_align`, `get_pa_error`, `set_3ppa_calibration` |
| **Focuser** | `get_focuser_position`, `adjust_focus`, `start_auto_focus`, `stop_auto_focus` |
| **Filter wheel** | `get_wheel_position`, `get_wheel_state`, `get_wheel_setting`, `set_wheel_position`, `set_lp_filter` |
| **Imaging** | `start_stack`, `stop_view`, `set_gain`, `set_exposure`, `get_last_image`, `create_dark` |
| **Settings** | `read_settings`, `write_setting` |
| **Scheduling** | `get_schedule`, `create_schedule`, `add_schedule_item`, `schedule_mosaic`, `start_mosaic`, `start_scheduler`, `stop_scheduler`, `export_schedule`, `import_schedule` |
| **Night plans (on the scope)** | `plan_night`, `get_plan_status`, `cancel_plan` |
| **Startup / system** | `startup_sequence`, `set_heater` |
| **System (physical, gated)** | `scope_park`, `scope_move_to_horizon`, `reboot`, `shutdown` |

### Settings

- `read_settings` merges both device read paths (`get_setting` and
  `get_stack_setting`), returning what each provides and listing any that
  failed under `errors` (older firmware may not support one).
- `write_setting(values, variant="auto")` supports all three save variants
  (`set_setting` with a stack payload, `set_stack_setting`, `set_stack_settings`).
  `auto` uses `set_stack_settings` for multiple keys, else `set_stack_setting`;
  pass an explicit `variant` to force one. An inline device rejection is
  returned as `{"ok": false, ...}` rather than reported as success.

### Night plans (on the scope)

`plan_night` sends a whole multi-target night to the scope as a firmware
**ViewPlan**, the same mechanism as the ZWO app's *Plan*. The scope runs it on
its own, so this server, seestar_alp and the network can all go away once it is
accepted. ALP's scheduler (`start_scheduler`) is different: it runs inside
seestar_alp, which has to stay up all night, and its `wait_until` times use the
ALP host's clock.

```python
plan_night(
    targets=[
        {"name": "IC 1396A", "ra": "21:36:33", "dec": "+57:30:00",
         "start": "18:25", "end": "01:00", "lp_filter": True},
        {"name": "IC 434", "ra": "05:41:00", "dec": "-02:27:30",
         "start": "01:00", "end": "05:05", "lp_filter": False},
    ],
    timezone="America/Denver",   # the zone the HH:MM times are in
    exposure_s=20, af_on_switch=True, close_arm=True, confirm=True,
)
```

- **Targets** take J2000 `ra` (hours or `HH:MM:SS`) and `dec` (degrees or
  `+DD:MM:SS`), local `start`/`end` as `HH:MM`, and `lp_filter`. They run in
  list order and must not overlap; an end earlier than its start is after
  midnight. The plan must end within 24 hours. A target already under way starts
  now. `timezone` defaults to the scope's own zone.
- **Timing.** The firmware counts a target's start in minutes after the scope's
  local midnight and turns that into a countdown when the plan starts. The tool
  reads the scope's clock (`pi_get_time`) and computes those minutes from it, so
  the plan runs at the times you wrote even if the scope's zone is wrong (for
  example UTC) or its clock is a few minutes off. It refuses a plan the firmware
  would silently move to another day, a first target that ends within 90 s,
  the two minutes before the scope's midnight, and a scope clock that disagrees
  with its own zone name.
- **Exposure and gain are global.** A plan target has no exposure or gain: every
  target stacks at the scope's exposure setting and its own gain. Per-target
  exposure or gain is refused; `exposure_s` sets the stack exposure once, before
  the plan.
- **Autofocus and closing the arm** are the scope's persistent `plan_target_af`
  and `viewplan_gohome` settings. The tool writes and reads back both before it
  sends the plan (the same-named members of the plan request are ignored by the
  firmware). Autofocus at each switch is likely but not guaranteed.
- **Filter.** Each target's goto puts the wheel on IR-cut, then a target with
  `lp_filter: true` moves it to LP.
- **Confirmation.** Without `confirm=true`, `plan_night` only previews the
  normalised schedule and changes nothing. With it, the reply means *accepted,
  not done*, and includes a read-back of the plan the scope holds.
- **Status.** `get_plan_status` reports the plan and each target's state and
  code. `complete` means the plan's loop finished, not that every target
  succeeded: a target whose end passed before it could run shows code 513.
- **Cancel.** `cancel_plan` sends `clear_view_plan`. On firmware 9.75 that also
  deletes the stored plan, so send it again to rerun it. While a plan runs, the
  scope refuses to start another view or plan.

### Safety

There is no raw command passthrough — only curated tools. Physical and
destructive actions require explicit confirmation: `scope_park`,
`scope_move_to_horizon`, `reboot`, and `shutdown` each refuse unless called with
`confirm=true`, and making no device call when refused. Because the scheduler
can execute physical actions too, `add_schedule_item` (for a physical action)
and `start_scheduler` likewise require `confirm=true`, as does `plan_night`,
which arms an unattended night on the scope.

## Development

```bash
. ~/.venvs/seestar/bin/activate   # or your editable-install env
cd mcp && python -m pytest tests/ -q          # unit tests
ruff check . && ruff format --check .
```

End-to-end tests (MCP tools → device server → simulator) live at
[`../tests/integration/test_mcp_simulator_e2e.py`](../tests/integration/test_mcp_simulator_e2e.py)
and run under the `integration` marker.
