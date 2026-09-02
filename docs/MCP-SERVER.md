# The MCP server: hooking an agent up to vcctrl directly

Written 2026-08-25, extended the same day with a second deployment mode.
`agent/vcctrl_mcp.py` exposes vcctrl over MCP (Model Context Protocol) so an
agent -- Claude Code, Codex, anything that speaks MCP -- can drive the real
hardware directly: keyboard/mouse, video/audio capture, power, file
transfer, and (control mode only, sec. 1) full harness workflows. Proven end to
end on the real rig the same day it was built -- see the status table in
sec. 6.

**PC/DOS only for now.** The Mac Plus board is a real, physically-carriable
target on this rig (`docs/BOARD-IDENTITY.md`) and every tool here behaves
correctly per-board (three-valued supported/unsupported/unknown, never
assumed) -- it has simply not been exercised against it yet, by operator
direction.

## 1. Two deployment modes, one file

Chosen by `VCCTRL_MCP_ROLE`:

| | **control** (default) | **daemon** |
|---|---|---|
| Runs on | the control host | the daemon host, alongside `vcctrld` |
| Transport | stdio | streamable-http |
| Talks to the daemon via | `bin/vcctrl` over SSH | `/usr/local/bin/vcctrl` directly, no SSH |
| Reached at | a local subprocess Claude Code spawns | `https://<rig>.ts.net/mcp` |
| Tool count | 59 | 55 -- no harness workflows (below) |

Named after `vcctrl.yaml`'s own `control:`/`daemon:` sections, not after a
specific board -- the daemon host has already changed hardware once
(`docs/PI5-MIGRATION.md`), and a mode called "pi" would be a lie the next
time it does.

**Why not just build MCP into `vcctrld` itself**, which was the operator's
first question: `vcctrld` is threading-based (`daemon/vcweb.py`'s own web UI
is plain `http.server`, no asyncio). The `mcp` package's HTTP transport is
Starlette+uvicorn -- a genuinely different runtime model, not just an extra
route. And a bug in the MCP-serving code must not be able to take down the
process that owns the uinput devices and the input lock; today a crashed
daemon-mode server means "the tools stop working," built into `vcctrld` it
would mean "input control stops working." The daemon host is a Pi 5 now
(4GB RAM, `docs/PI5-MIGRATION.md`) -- the old Pi-3 memory concern that
shaped a lot of this project's caution does not apply to the dependency
footprint; the coupling/stability risk is the real reason, decided with
the operator 2026-08-25.

**Why daemon mode has no harness-workflow tools:** `harness/vcctrl-cell`,
`-sweep` and `-collect` are control-host-side orchestration scripts --
`bin/vcctrl_common.py`'s `vc()`/`vc_json()` always resolve to the
`bin/vcctrl` SSH-wrapper sitting next to them (`VCCTRL = os.path.join(HERE,
"vcctrl")`), so running them on the daemon host would mean SSHing to
itself, which nothing else in this project does. Not fixed; Phases 1-4
(status, capture, input, power, file transfer) are genuine `vcctrld`
capabilities and needed no code changes beyond the binary path and the
transport to run there.

## 2. What it wraps, and what that buys

One MCP tool per `vcctrl` verb (or, control mode only, per harness script),
shelling out to the CLI exactly as a human at a terminal would -- not a
second implementation of the daemon's socket protocol. In control mode that
means every fix already in `bin/vcctrl` (SSH connection reuse, the
`--out`/`--out-dir` host-boundary rewrite) applies for free. In daemon mode
there is no SSH hop at all -- `/usr/local/bin/vcctrl` writes straight to
the local filesystem, so the host-boundary machinery simply isn't needed.

| group | examples | notes |
|---|---|---|
| status/read | `vcctrl_status`, `_board`, `_caps`, `_keymap`, `_activity`, `_power_state` | no lock needed |
| capture | `_shot`, `_frame`, `_burst`, `_timeline`, `_record`, `_level`, `_camera_shot`, `_camera_state` | files land on whichever machine runs the MCP server; camera is a separate, ringless device -- see `vcctrl-camera` |
| input | `_key`, `_type`, `_hold`, `_combo`, `_mouse_move/_click/_down/_up/_release_all`, `_verify_input` | takes the shared hardware lock |
| power | `_power` (on/off/cycle) | board-scoped; refuses on a board the configured plug doesn't control |
| file transfer | `_stage_file`, `_send_file`, `_file_status`, `_get_file`, `_pulled`, ... | reboots the target for send/refresh/get |
| harness workflows (**vm only**) | `_preflight`, `_run_cell`, `_run_sweep`, `_collect`, `_job_status` | long-running, launched as background jobs |

## 3. The safety model

- **A shared hardware lock, not a free-for-all.** Any tool that sends input
  acquires the daemon's own Arbiter lock first, under its own identity
  (`mcp:<host>:<pid>`). If a human or another session already holds it, the
  tool **refuses outright** -- it never forces a break. As of 2026-08-31, a
  single gated call (`_key`, `_type`, `_combo`, `_verify_input`, `_mouse_*`,
  `_hold`, `_keydown`/`_keyup`, `_release_all`) releases the lock again right
  after that one action by default -- only `vcctrl_lock_acquire` creates a
  *sticky* hold that survives across later gated calls, released explicitly
  via `vcctrl_lock_release` or, failing that, automatically after 300s idle.
  `vcctrl_lock_acquire` itself refuses while a file-transfer job
  (`send_file`/`get_file`/`file_refresh`/`file_scan`) or a harness job
  (`run_cell`/`run_sweep`/`collect`) is running, since a sticky hold taken
  mid-job can block that job's own later attempt to acquire this same lock --
  see `agent/vcctrl_mcp.py`'s `_active_job_conflict` for the live incident
  (a `get_file` job's return-to-menu reboot silently refused) this closes.
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
- **daemon mode's network exposure is the one real tradeoff stdio doesn't
  have.** stdio has no listener at all -- it's a subprocess Claude Code
  spawns directly, inherently local. Reaching the Pi over
  `https://<rig>.ts.net/mcp` means it's a real network service, though it
  inherits exactly the access control `daemon/vcweb.py`'s own web UI
  already uses: bound to loopback (`127.0.0.1:8090`), TLS terminated by
  `tailscale serve`, so only devices on the tailnet can reach it at all --
  no separate app-level auth. On top of that, DNS-rebinding protection is
  explicitly enabled (the `mcp` package's own default is OFF, "for backwards
  compatibility" -- see `agent/vcctrl_mcp.py`'s `__main__`): the server only
  accepts requests whose `Host` header matches an allowlist
  (`VCCTRL_MCP_ALLOWED_HOSTS`, set automatically at install time to the
  rig's own tailnet name).

## 4. Install and deploy

### control mode (control host)

```bash
cd vcctrl   # repo root
python3 -m venv agent/.venv
agent/.venv/bin/pip install -r agent/requirements.txt
```

`agent/requirements.txt` is its own file, deliberately separate from the
top-level `requirements.txt` (which is shared with the Pi) -- `mcp` pulls in
httpx/pydantic/etc. a Pi has no business installing outside this one
service's own venv.

If venv creation fails with "ensurepip is not available" (Debian/Ubuntu),
`apt install python3-venv` (or the version-suffixed package it names) first.

**Needs `vcctrl.yaml` configured** exactly as the CLI does -- `bin/vcctrl`
resolves the daemon host the same way it always has (`VCCTRL_CONFIG`,
`./vcctrl.yaml`, `~/.config/vcctrl/vcctrl.yaml`, `/opt/vcctrl/vcctrl.yaml`,
in that order). Nothing MCP-specific to configure beyond that.

### daemon mode (daemon host)

```bash
./pi/deploy.sh --mcp
```

Narrow deploy mode, matching `--page`/`--client`'s own shape: ships
`agent/` into a throwaway `/tmp` layout and runs `pi/install.sh --mcp-only`
remotely, which sets up its own venv at `/opt/vcctrl/agent/.venv`,
`vcctrl-mcp.service` (systemd, runs as the `pi` user -- unlike `vcctrld`,
this needs no root, no uinput access), and the `tailscale serve --set-path`
mapping for `/mcp`. **`vcctrld` is never touched** -- no restart, no
uinput-device drop, so this needs none of the coordination a full deploy
does. A full `pi/deploy.sh` (no flag) also runs the same install step, for
a from-scratch Pi setup.

If `tailscale` isn't installed or not logged in yet, the service still
comes up and answers at `127.0.0.1:8090` on the Pi itself -- the tailscale
mapping is best-effort and non-fatal, re-run `./pi/deploy.sh --mcp` once
tailscale is set up to add it.

## 5. Setup in an MCP client

### Claude Code

Both modes tested live against this build (2026-08-25).

**daemon mode** -- once deployed (sec. 4), this is all it takes, from anywhere,
no local venv or checkout needed on the client side at all:

```bash
claude mcp add --transport http vcctrl-mcp-daemon https://usb4vc.example.ts.net/mcp
```

(substitute your rig's own tailnet hostname).

**control mode**:

```bash
cd vcctrl   # repo root -- paths below are resolved at registration time
claude mcp add vcctrl-mcp -- "$(pwd)/agent/.venv/bin/python3" "$(pwd)/agent/vcctrl_mcp.py"
```

Both register at local scope (the default) -- private to you, stored in
`~/.claude.json` rather than a repo file. For the control-mode command this
is deliberate, not a shortcut: it bakes in an absolute path to wherever
*your* clone lives, and this repo's own convention is that machine-specific
paths stay out of the tracked tree (the same reason `vcctrl.yaml` itself is
gitignored -- see `CLAUDE.md`). The daemon-mode URL has no such problem and
could reasonably go in a shared `-s project` `.mcp.json` if a team wants
one command for everyone.

Verify either with:

```bash
claude mcp list                     # should show ... - Connected
claude mcp get vcctrl-mcp-daemon    # or vcctrl-mcp
```

Remove with `claude mcp remove <name>`.

**If you register from a shell while a Claude Code session is already open
in the same project, that session's own `/mcp` will not show the new
server.** `claude mcp add` writes to `~/.claude.json` immediately -- `claude
mcp list` reads that file fresh every time and shows it connected right
away -- but a session that was already running read its server list once
at startup and does not reload it. Restart the session (exit and run
`claude` again in the same directory); `/mcp` picks it up from there.
Measured 2026-08-25: registered both servers from a running session's own
Bash tool, `claude mcp list` showed both Connected immediately, and that
same session's `/mcp` stayed empty until it was restarted.

**Running both at once is fine and arguably the right setup**:
`vcctrl-mcp-daemon` for device control with no SSH hop, `vcctrl-mcp`
(control mode) for the harness-workflow tools daemon mode doesn't have.
Tool names don't collide -- each server's tools are namespaced by the
client.

### Codex

Not yet tested against a live Codex install from this session -- syntax
below is from OpenAI's own docs (`developers.openai.com/codex/mcp`),
current as of 2026-08-25.

```bash
# daemon mode
codex mcp add vcctrl-mcp-daemon --url https://usb4vc.example.ts.net/mcp

# control mode
cd vcctrl
codex mcp add vcctrl-mcp -- "$(pwd)/agent/.venv/bin/python3" "$(pwd)/agent/vcctrl_mcp.py"
```

or by hand in `~/.codex/config.toml` (or a project-scoped
`.codex/config.toml`, trusted projects only):

```toml
[mcp_servers.vcctrl-mcp-daemon]
url = "https://usb4vc.example.ts.net/mcp"

[mcp_servers.vcctrl-mcp]
command = "/absolute/path/to/vcctrl/agent/.venv/bin/python3"
args = ["/absolute/path/to/vcctrl/agent/vcctrl_mcp.py"]
```

Same reasoning on absolute paths (the control-mode entry) as the Claude
Code section above.

### Any other MCP client

daemon mode is a plain streamable-http MCP endpoint -- point any MCP-capable
client at `https://<rig>.ts.net/mcp`. control mode is a plain stdio server
(`mcp.run()`) -- anything that can spawn a subprocess and speak MCP over
its stdin/stdout works: `agent/.venv/bin/python3 agent/vcctrl_mcp.py`.

## 6. Status, 2026-08-25

Everything below was run against the real rig (board 1, the g2k), not
merely unit-tested:

| capability | state |
|---|---|
| Read-only tools (status/board/caps/capture/activity/...) | **working**, both modes -- live against the real daemon |
| Input (key/type/combo/mouse) | **working**, control mode -- typed at a real DOS prompt, screenshotted to confirm |
| Lock acquire/refuse/release | **working**, control mode -- against the real Arbiter |
| Power on/off/cycle, board-scoped refusal | **on works** (booted the g2k live); board-mismatch refusal is unit-tested only -- no second board to swap in yet |
| File transfer (stage/send/status) | **working**, control mode -- byte-for-byte verified round trip |
| Single cell (`run_cell`) | **working**, control mode only -- ran a real doskutsu cell (Mach64, POD-83), visually confirmed the game running mid-cell |
| Full sweep (`run_sweep`) | **working**, control mode only -- `MINE`, 2 cells, 6.0 min, DOS-side completion banner confirmed |
| Log collection (`collect`) | **working**, control mode only -- both cell logs + SDL logs + manifest fetched, size-verified, landed on disk |
| daemon mode: local (127.0.0.1:8090 on the Pi) | **working** -- real MCP client (`mcp.ClientSession`), `vcctrl_status`/`_board` called end to end, no SSH involved |
| daemon mode: over the tailnet (`https://.../mcp`) | **working** -- real `claude mcp add --transport http`, connects; DNS-rebinding-protection allowlist configured automatically at install time |
| daemon mode + vcweb coexistence | **working** -- `/` (KVM UI) and `/mcp` on the same hostname/port, added without disturbing the existing mapping; `vcctrld` confirmed untouched (same PID) across the `--mcp` deploy |
| Mac Plus (any tool, any board-specific behavior) | **not exercised** -- deferred, PC/DOS proven out first |

Known gaps:

- `vcctrl_run_sweep` has no `hw` passthrough the way `vcctrl_run_cell` does,
  so a sweep's manifest records hardware as "UNDECLARED" even when you know
  exactly what's fitted. Doesn't affect the run, just the record.
- daemon mode's `vcctrl-mcp.service` runs as the `pi` user, following
  `usb4vc.service`'s own precedent on this rig -- not verified against a
  Pi where that user doesn't exist or has different permissions; adjust
  `User=` in `pi/install.sh`'s `install_mcp()` if so.
