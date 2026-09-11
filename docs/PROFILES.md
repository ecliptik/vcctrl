# Profiles: one vcctrld, multiple targets

Written 2026-09-02 at the end of the session that built this, for
whoever (human or agent) picks the work up next.

## What a profile is

A **profile** is one target machine this rig can drive: its own input
backend, its own capture device, its own capability set. `gateway2000`
(the original DOS/RetroPC target, VGA capture + PS/2 via USB4VC) and
`modernpc` (a Linux server, HDMI capture + a USB HID gadget on the Pi's
own USB-C port) are both profiles today, addressed identically
everywhere — CLI, MCP, and the web UI. Neither is structurally "the
primary" in the code; `gateway2000` is simply whichever profile's
config file is found via the normal `VCCTRL_CONFIG`/search-path
resolution (see `common/vcconfig.py`'s `find_config_file`), and it
keeps a backward-compatible `/run/vcctrl.sock` alias alongside its own
`/run/vcctrl-gateway2000.sock` so nothing that predates profiles broke.

**One `vcctrld` process serves every profile.** This was NOT the
original design — `modernpc` first shipped as a second, independent
`vcctrld` process (commit `24efb7e`) and was later folded into the
primary's process (commits `7bdede9`, `3a973c6`) once a live
measurement (`docs/FINDINGS.md` #46) showed sharing the process/GIL
cost the primary's timing-sensitive PS/2 emission under a millisecond
— safely inside the margin, not a real risk. Read that commit sequence
if you need the reasoning, not just the result.

**Hardware is only ever active on one profile at a time, by policy, not
by code enforcement** — except for one specific case that IS enforced:
`capabilities.input.backend: usb4vc-uinput` (the one physical USB4VC/
SPI bridge) is refused for any profile after the first to claim it
(`main()`'s "EXCLUSIVE HARDWARE GUARD", `daemon/vcctrld.py`). Every
other potential collision (two profiles pointed at the same camera or
audio device, say) is prevented by the operator's own config discipline
— see the "Not yet fixed properly" note in `OPEN-FAULTS.md` #21 for
exactly what that means in practice.

## How to add a new profile

1. Pick a **kind** — `profile-kinds/vga-ps2.yaml` (a retro PC over VGA/
   PS2, USB4VC), `profile-kinds/hdmi-usb.yaml` (a modern target over
   HDMI capture + the Pi's own USB HID gadget), or
   `profile-kinds/rgb2hdmi-usb4vc.yaml` (a classic Macintosh over
   RGB2HDMI capture + USB4VC/ADB — unmeasured, no such hardware has run
   against this rig yet; see `vcctrl-macintosh.example.yaml`, its placeholder
   scaffold).
2. Run `tools/new-profile.py --kind <kind> --name <name>` to scaffold a
   complete, self-contained `vcctrl-<name>.yaml` (this is a
   repo-authoring-time tool; it never runs on the daemon host and
   `vcctrld`/`vcconfig.py` never read a `profile-kinds/*.yaml` file at
   runtime — see that directory's own comments for why: a deployed
   profile's config must stay fully self-evident on its own).
3. Fill in the `REPLACE_ME` placeholders (device by-id paths especially
   — always by-id, never `/dev/videoN`, see `docs/FINDINGS.md` #44's
   port-topology note on why index drift is a real hazard with three-
   plus UVC devices on one Pi) — including the `machine:` block's own
   `label`/`os`/`keyboard`, added 2026-09-02. `keyboard` matters most for
   an `hdmi-usb` profile: it has no protocol board and no `targets:` row,
   so `machine.keyboard` is the *only* place a layout can come from (see
   `OPEN-FAULTS.md` #21's "modernpc had no keyboard layout at all").
4. Deploy: `pi/deploy.sh --profile <name>` (or `pi/install.sh
   --profile-only <name>` on the Pi directly). This is currently
   **transitional** — it installs a second systemd unit
   (`vcctrld-<name>.service`) even though the single-process daemon
   would auto-discover the new sibling config file on its own restart.
   That second-unit step will become unnecessary once the install
   tooling itself catches up to the single-process design; for now it's
   how a new profile actually gets started without restarting (and
   thereby momentarily dropping) the primary's own already-running
   process. See `pi/install.sh`'s `install_profile()` for the exact
   caveat in its own words.

## Where things live

- `daemon/vcctrld.py`: `discover_profiles()`/`Instance`/`_build_instance()`
  build one `Devices`/`Registry`/`Arbiter` per discovered profile;
  `_profile_scope()`/`_CFGProxy` make the existing `CFG.xxx` call sites
  (there are dozens) resolve against whichever profile is bound on the
  calling thread, without having threaded an explicit parameter through
  all of them.
- `daemon/vcweb.py`: one `WebCapability`, one port, routing on a leading
  `/p/<name>/` path prefix (`Handler._route_profile()`) — no prefix
  means the profile aliased to `None`, which `_start_web()` in
  `vcctrld.py` always binds to whichever instance owns the shared port
  (normally `gateway2000`).
- `bin/vcctrl-client`: `--profile <name>` resolves to
  `/run/vcctrl-<name>.sock` by convention (no registry file — the
  socket directory listing IS the registry, see `profile_socket_path()`'s
  own comment); `profiles` lists what's actually running.
- `agent/vcctrl_mcp.py`: every profile-aware tool takes an optional
  `profile` argument; `vcctrl_profiles`/`vcctrl_profile_set` for
  discovery and session-default switching. Each profile gets its own
  `LockManager` — acquiring one profile's lock never blocks another's.
- `daemon/kvm.html`: a small toolbar switcher (`PROFILES`/
  `switchProfile()`/`apiPath()`) changes which profile's `/p/<name>/`
  prefix the page's WebSocket/fetch calls use, in place, no reload.

## What's actually running right now (2026-09-02)

One `vcctrld.service` on `usb4vc` serving both `gateway2000` and
`modernpc`. `vcctrld-modernpc.service` (the old second-process unit) is
gone. The VGA capture stick and the room camera were unplugged
2026-09-01 to diagnose a real undervoltage event (see
`docs/FINDINGS.md` #44's power-path caveat and `OPEN-FAULTS.md` #21) —
`gateway2000`'s own video/camera capabilities were still reporting
`device_present: false` as of this writing. `modernpc` is fully working:
HID-gadget keyboard/mouse, HDMI capture at its native 1920x1080
(`docs/FINDINGS.md` region around commit `cc57c0e`), profile switching
in the web UI confirmed live in both directions.

See `OPEN-FAULTS.md` #21 for the specific things still worth watching
before trusting this further, and the commit range `fe15c51..3ca61dd`
for the full, in-order history of how this was built, measured, and
debugged.
