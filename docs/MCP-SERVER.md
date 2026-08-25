# The MCP server: hooking an agent up to vcctrl directly

Written 2026-08-25. `agent/vcctrl_mcp.py` exposes vcctrl over MCP (Model
Context Protocol) so an agent -- Claude Code, Codex, anything that speaks
MCP -- can drive the real hardware directly: keyboard/mouse, video/audio
capture, power, file transfer, and full harness workflows (single cells,
sweeps, log collection). Proven end to end on the real rig the same day it
was built -- see the status table in sec. 5.

**PC/DOS only for now.** The Mac Plus board is a real, physically-carriable
target on this rig (`docs/BOARD-IDENTITY.md`) and every tool here behaves
correctly per-board (three-valued supported/unsupported/unknown, never
assumed) -- it has simply not been exercised against it yet, by operator
direction.

## 1. What it wraps, and what that buys

One MCP tool per `vcctrl` verb (or per harness script), shelling out to
`bin/vcctrl` exactly as a human at a terminal would -- not a second
implementation of the daemon's socket protocol. That means every fix
already in `bin/vcctrl` (SSH connection reuse, the `--out`/`--out-dir`
host-boundary rewrite) applies here for free, and there is only one place
either can go wrong.

**59 tools**, roughly:

| group | examples | notes |
|---|---|---|
| status/read | `vcctrl_status`, `_board`, `_caps`, `_keymap`, `_activity`, `_power_state` | no lock needed |
| capture | `_shot`, `_frame`, `_burst`, `_timeline`, `_record`, `_level` | files land on whichever machine runs the MCP server |
| input | `_key`, `_type`, `_hold`, `_combo`, `_mouse_move/_click`, `_verify_input` | takes the shared hardware lock |
| power | `_power` (on/off/cycle) | board-scoped; refuses on a board the configured plug doesn't control |
| file transfer | `_stage_file`, `_send_file`, `_file_status`, `_get_file`, `_pulled`, ... | reboots the target for send/refresh/get |
| harness workflows | `_preflight`, `_run_cell`, `_run_sweep`, `_collect`, `_job_status` | long-running, launched as background jobs |

## 2. The safety model

- **A shared hardware lock, not a free-for-all.** Any tool that sends input
  acquires the daemon's own Arbiter lock first, under its own identity
  (`mcp:<host>:<pid>`). If a human or another session already holds it, the
  tool **refuses outright** -- it never forces a break. Released
  automatically after 300s idle, or explicitly via `vcctrl_lock_release`.
- **Named confirmation on anything consequential.** Power actions, a combo
  that matches the reboot chord, and anything that reboots the target
  (file send/refresh/get, `run_cell`, `run_sweep`, `collect`) all require a
  `confirm` argument that names the action (`confirm="cycle"`,
  `confirm="reboot"`, `confirm="run"`) -- not a bare boolean one accidental
  `true` turns into nothing. An agent can still confirm autonomously when
  the task calls for it; the point is that it's never the accidental shape
  of a call.
- **Board-scoped power.** `vcctrl power cycle` used to hit one configured
  smart plug regardless of which USB4VC protocol board was actually seated
  -- with the Mac Plus installed it would still have cut the g2k's mains, a
  machine nobody asked about. Fixed daemon-side and deployed before any
  power tool shipped; see `docs/BOARD-IDENTITY.md` sec. 5 and
  `docs/FINDINGS.md` sec. 41 for the related `verify_input` fix this same
  work turned up.
- **Every call is visible.** Nothing here routes around `vcctrl activity`
  or the power audit log -- a human watching the rig sees `mcp:...` show up
  as a caller exactly like anyone else.

## 3. Install

```bash
cd vcctrl   # repo root
python3 -m venv agent/.venv
agent/.venv/bin/pip install -r agent/requirements.txt
```

`agent/requirements.txt` is its own file, deliberately separate from the
top-level `requirements.txt` (which is shared with the Pi) -- `mcp` pulls in
httpx/pydantic/etc. that a Pi 3 running `vcctrld` has no business installing.

If venv creation fails with "ensurepip is not available" (Debian/Ubuntu),
`apt install python3-venv` (or the version-suffixed package it names) first.

**Needs `vcctrl.yaml` configured** exactly as the CLI does -- the MCP server
shells out to `bin/vcctrl`, which resolves the daemon host the same way it
always has (`VCCTRL_CONFIG`, `./vcctrl.yaml`, `~/.config/vcctrl/vcctrl.yaml`,
`/opt/vcctrl/vcctrl.yaml`, in that order). Nothing MCP-specific to
configure beyond that.

## 4. Setup

### Claude Code

Tested live against this build (2026-08-25): `claude mcp add`, connects
immediately.

```bash
cd vcctrl   # repo root -- paths below are resolved at registration time
claude mcp add vcctrl -- "$(pwd)/agent/.venv/bin/python3" "$(pwd)/agent/vcctrl_mcp.py"
```

That's local scope (the default) -- private to you, in this project, stored
in `~/.claude.json` rather than a repo file. This is deliberate, not a
shortcut: the command bakes in an absolute path to wherever *your* clone
lives, and this repo's own convention is that machine-specific paths stay
out of the tracked tree (the same reason `vcctrl.yaml` itself is
gitignored -- see `CLAUDE.md`). Verify with:

```bash
claude mcp list                # should show vcctrl - Connected
claude mcp get vcctrl
```

Remove with `claude mcp remove vcctrl`.

**Sharing it with a team via a committed `.mcp.json`** (`-s project`) works
the same way but writes the absolute path into a file everyone would
check out -- only worth it if everyone's clone lives at the same path
(e.g. a fixed CI/build-host layout), or if you're willing to hand-edit the
committed path per machine. For a rig with one real operator, local scope
is the right default.

### Codex

Not yet tested against a live Codex install from this session -- syntax
below is from OpenAI's own docs (`developers.openai.com/codex/mcp`),
current as of 2026-08-25.

```bash
cd vcctrl
codex mcp add vcctrl -- "$(pwd)/agent/.venv/bin/python3" "$(pwd)/agent/vcctrl_mcp.py"
```

or by hand in `~/.codex/config.toml` (or a project-scoped
`.codex/config.toml`, trusted projects only):

```toml
[mcp_servers.vcctrl]
command = "/absolute/path/to/vcctrl/agent/.venv/bin/python3"
args = ["/absolute/path/to/vcctrl/agent/vcctrl_mcp.py"]
```

Same reasoning on absolute paths as the Claude Code section above.

### Any other MCP client

`agent/vcctrl_mcp.py` is a plain stdio MCP server (`mcp.run()`, defaults to
stdio transport) -- anything that can spawn a subprocess and speak MCP over
its stdin/stdout works the same way: point it at
`agent/.venv/bin/python3 agent/vcctrl_mcp.py`.

## 5. Status, 2026-08-25

Everything below was run against the real rig (board 1, the g2k) the day
this was built, not merely unit-tested:

| capability | state |
|---|---|
| Read-only tools (status/board/caps/capture/activity/...) | **working** -- live against the real daemon |
| Input (key/type/combo/mouse) | **working** -- typed at a real DOS prompt, screenshotted to confirm |
| Lock acquire/refuse/release | **working** -- against the real Arbiter |
| Power on/off/cycle, board-scoped refusal | **on works** (booted the g2k live); board-mismatch refusal is unit-tested only -- no second board to swap in yet |
| File transfer (stage/send/status) | **working** -- byte-for-byte verified round trip |
| Single cell (`run_cell`) | **working** -- ran a real doskutsu cell (Mach64, POD-83), visually confirmed the game running mid-cell |
| Full sweep (`run_sweep`) | **working** -- `MINE`, 2 cells, 6.0 min, DOS-side completion banner confirmed |
| Log collection (`collect`) | **working** -- both cell logs + SDL logs + manifest fetched, size-verified, landed on disk |
| Mac Plus (any tool, any board-specific behavior) | **not exercised** -- deferred, PC/DOS proven out first |

One known gap: `vcctrl_run_sweep` has no `hw` passthrough the way
`vcctrl_run_cell` does, so a sweep's manifest records hardware as
"UNDECLARED" even when you know exactly what's fitted. Doesn't affect the
run, just the record -- worth adding if declared hardware in sweep
manifests matters to you.
