# vcctrl

Remote control of a real, physical retro/legacy machine — keyboard, mouse,
screen, audio, power and file transfer — driven by a CLI, an MCP server (so
an agent like Claude Code or Codex can operate it directly), or a browser
KVM. Built for automated testing of software ports on hardware that
predates automated testing: DOS boxes, classic Macs, and anything else you
can wire up a capture device and an input path to.

**[docs/HARNESS-STANDARD.md](./docs/HARNESS-STANDARD.md)** is the
target-agnostic contract for running measured tests on it.
**[docs/MCP-SERVER.md](./docs/MCP-SERVER.md)** hooks an agent up to vcctrl
directly over MCP. **[docs/SKILLS.md](./docs/SKILLS.md)** carries
rig/repo domain knowledge to any of those same agents as portable
`SKILL.md` files. **[docs/PROFILES.md](./docs/PROFILES.md)** covers driving
more than one target machine from one daemon.

## What it actually is

Three roles, usually three separate machines, connected over SSH/HTTP(S)
and a physical wire:

```
 control host                 daemon host (a Raspberry Pi)         target
 ───────────────              ─────────────────────────            ──────
 bin/vcctrl (CLI)     ssh     daemon/vcctrld.py                    the DOS/
 agent/vcctrl_mcp.py  ─────►  owns the input device(s),   PS/2 or  Mac/Linux
 the web browser       http   the capture device(s),      USB/HDMI machine
                              the smart plug, the file     ───────► under
                              server; vcctrld also serves           test
                              the web KVM directly
```

All timing-sensitive work (key dwell, event pacing, capture) happens on
the daemon host, never on the control host — the whole point of the split
is that network latency to the control host never enters the input path.
A minimal setup runs the daemon and the control tools on the same box; the
common one is a Pi doing the daemon's job and a separate dev machine (or
your laptop) doing the control host's.

## Hardware reference: what this project's own rig actually runs

You do not need this exact hardware — `profile-kinds/` below covers three
different shapes, and the `shell` power backend and `hid-gadget` input
backend exist specifically so cheaper/different substitutes work. This is
what one working, continuously-tested build looks like, so you have a
concrete parts list to start from or diverge from on purpose:

| role | what this rig uses | notes |
|---|---|---|
| Daemon host | **Raspberry Pi 5** (4 GB), Debian 13 (Trixie) arm64 | A Pi 3B also worked (see `docs/PI5-MIGRATION.md`) but is measurably slower under load; the Pi's own USB-C port doubles as the OTG connection for the `hid-gadget` input backend if you use that instead of USB4VC |
| PS/2 input + board identity | **[USB4VC](https://github.com/dekuNukem/USB4VC)** HAT (STM32-based), with its swappable **IBM PC** protocol board (PS/2 keyboard+mouse) or **Lisa/Mac/ADB** protocol board | One USB4VC drives either target; the board is swapped by hand and identified over SPI at runtime (`docs/BOARD-IDENTITY.md`) |
| Video + audio capture | A **MACROSILICON-chipset** USB2.0 VGA/HDMI-to-USB capture dongle (USB ID `1b80:e309`/similar, sold generically on Amazon/AliExpress/eBay as a "USB video capture card") | Locks onto both text-mode and low-res graphics modes; carries line-in audio on the same dongle. Pin it by `/dev/v4l/by-id/...`, never `/dev/videoN` — index drift across UVC devices is real (`docs/FINDINGS.md` #44) |
| Optional second camera | An **Innomaker U20CAM-1080p** UVC camera, pointed at the physical machine (not its video signal) — for a hardware-level view when the primary capture is dark or frozen | Genuinely optional; `capabilities.camera.backend` defaults to not-installed |
| Mains power control | A **TP-Link Kasa** smart plug (works with both the legacy LAN protocol and the newer KLAP one) or a **Wemo Insight**, switched between rigs over time | `shell` backend exists for a relay board, a Zigbee bridge, or a person with a switch |
| Target boot media | A CF card + generic **USB CF card reader** (Genesys Logic chipset) on the daemon host, for pushing files onto/pulling logs off of a DOS target that has no other network path | Not needed once a NIC + FTP client is working on the target; see `docs/FILE-TRANSFER.md` |
| Target machine(s) proven so far | A Gateway 2000-class 486/Pentium-era PC (PS/2, DOS 6.22) over USB4VC; a Linux box over HDMI capture + the Pi's own USB gadget port (no USB4VC needed for this shape) | A classic Macintosh over USB4VC's ADB board + RGB2HDMI capture is scaffolded (`profile-kinds/rgb2hdmi-usb4vc.yaml`) but unmeasured — no such hardware has run against this project's own rig yet |

### What every shape needs, generically

`profile-kinds/*.yaml` documents three reusable hardware shapes in detail;
pick the one closest to your target and use it as your starting config
(`tools/new-profile.py --kind <kind>` scaffolds one). In all three:

- **A Linux daemon host** with `/dev/uinput` (for USB4VC) or a USB
  peripheral-capable port with `configfs`/`libcomposite` (for the
  `hid-gadget` backend), root, and systemd.
- **A capture device** that emits MJPEG natively at a fixed resolution —
  any UVC device that does this over V4L2 works, not just the one above.
- **Some way to cut power to the target on command** — a smart plug is the
  common case; `backend: shell` runs your own on/off/state commands
  instead, for a relay, a PDU, or a GPIO pin.
- **ffmpeg** (with libx264 and libopus if you want the optional H.264
  video transport and Opus audio side-stream) on the daemon host.
- Python ≥ 3.7 on the daemon and control hosts; ≥ 3.10 if you run the MCP
  server (`agent/`, needs `mcp>=2.0`).

## Quickstart

This gets a Pi from a blank OS install to answering `vcctrl status`. It
assumes the USB4VC/PS/2 shape (`profile-kinds/vga-ps2.yaml`); swap in the
HDMI/HID-gadget steps from `profile-kinds/hdmi-usb.yaml` if that's your
target instead.

**1. Flash the daemon host.** Raspberry Pi OS or plain Debian, arm64,
Trixie (13) or newer, with SSH enabled. Note its hostname or IP.

**2. Install USB4VC** on the Pi per [its own
instructions](https://github.com/dekuNukem/USB4VC), then apply this
repo's two local patches (needed on 64-bit userland and to publish board
identity — see each file's own docstring for why):

```sh
python3 tools/patch-usb4vc-64bit.py --check   # then without --check to apply
python3 tools/patch-usb4vc-board.py --check
```

**3. Clone this repo on your control host** (your laptop, a dev VM —
anywhere that can reach the Pi over SSH) and set up the config:

```sh
git clone <this repo's URL> && cd vcctrl
cp vcctrl.example.yaml vcctrl.yaml       # untracked, never commit this
$EDITOR vcctrl.yaml                      # daemon_host, plug, devices --
                                          # see the file's own comments
```

**4. Deploy the daemon:**

```sh
VCCTRL_HOST=<pi-hostname-or-ip> pi/deploy.sh
```

(Or set `control.daemon_host` in `vcctrl.yaml` instead of the env var —
either works, and `deploy.sh` refuses clearly if neither is set. It ships
`daemon/ bin/ pi/ tools/ common/ harness/ vendor/ agent/` and your
`vcctrl.yaml` if it exists, then runs `pi/install.sh` on the Pi over SSH.)

**5. Install the CLI on the control host** and confirm the daemon answers:

```sh
sudo cp bin/vcctrl-client /usr/local/bin/vcctrl   # or: pi/deploy.sh --client
vcctrl status
vcctrl preflight            # one gate: caps, board, power, video, input
```

**6. Drive it:**

```sh
vcctrl type 'CD \'
vcctrl key enter
vcctrl shot                 # a frame from the capture device, as a file
vcctrl keymap                # key names, aliases, chord order
```

**7. Connect an agent** (Claude Code, Codex, …) instead of typing verbs by
hand — see the section right below.

**8. Making your own machine's profile**, once the above works against
default settings: `tools/new-profile.py --kind <vga-ps2|hdmi-usb|rgb2hdmi-usb4vc> --name <yours>`
scaffolds a complete `vcctrl-<yours>.yaml`; see `docs/PROFILES.md` for
running more than one target off one daemon.

## Connect an agent: skills vs. MCP

Two different things, on purpose. **Skills** are portable knowledge --
copying them into another repo costs nothing and grants nothing. **MCP**
is real ability to drive physical hardware -- treat registering it as a
hardware-access decision, not a documentation one, and never bake a rig's
real hostname into a tracked/committed file.

**Skills** (Claude Code, Codex, Cursor, ...), no vcctrl checkout needed:

```sh
npx skills add <this repo's URL> \
  --full-depth -a claude-code -y \
  -s vcctrl-mcp-workflows -s vcctrl-common-workflows \
  -s vcctrl-rig-hazards -s vcctrl-camera   # --agent codex for Codex
```

Installs the four hardware-portable skills into `.agents/skills/`
(symlinked into `.claude/skills/` for Claude Code). `--full-depth` is
required -- there's no `SKILL.md` at the repo root. (Two more skills,
`vcctrl-repo-conventions` and `vcctrl-webkvm-copy`, exist for contributing
to *this* repo rather than driving a rig; they're intentionally left out of
the line above since they describe this repo's own conventions, not yours
— see `docs/SKILLS.md` if you want them anyway.) See
[docs/SKILLS.md](./docs/SKILLS.md) sec. 8 for the same-machine symlink
alternative and sec. 5 for verifying a skill actually loaded (Claude Code
needs a session restart for a brand-new directory; Codex picks it up
live).

**MCP** (drives the real rig):

```sh
# daemon mode -- once deployed (docs/MCP-SERVER.md sec. 4), no local checkout
claude mcp add --transport http vcctrl-mcp-daemon https://<your-daemon-host>/mcp
codex mcp add vcctrl-mcp-daemon --url https://<your-daemon-host>/mcp

# control mode -- needs a local clone + venv
cd vcctrl
python3 -m venv agent/.venv && agent/.venv/bin/pip install -r agent/requirements.txt
claude mcp add vcctrl-mcp -- "$(pwd)/agent/.venv/bin/python3" "$(pwd)/agent/vcctrl_mcp.py"
```

Restart the session afterward -- `/mcp` doesn't pick up a freshly
registered server live. Full reasoning, the safety model (shared input
lock, named `confirm=` arguments, board-scoped power), and Codex config-file
syntax: [docs/MCP-SERVER.md](./docs/MCP-SERVER.md).

## Layout

```
bin/vcctrl              control-host CLI -- ssh's to the daemon, holds no logic
bin/vcctrl-client       the real CLI; installed on the daemon host as /usr/local/bin/vcctrl
bin/vcctrl-audio        one-off audio capture/level check from the control host
bin/vcctrl-capcheck     one-off video capture/lock check from the control host
bin/vcctrl-cardid       identifies the installed video card from an SDL3-DOS backend log
bin/vcctrl-cfclean      clears a target's log directory before a run
bin/vcctrl-uvconfig     drives UniVBE's video-mode configurator on the target
daemon/vcctrld.py       input/video/audio/power/file server; owns the devices, listens on a socket
daemon/vcweb.py         the private control web KVM
daemon/vcweb_public.py  the optional public, read-only mirror
daemon/kvm.html         the control KVM's page
common/vcconfig.py      shared config-file loader (control host and daemon host both import it)
agent/vcctrl_mcp.py     MCP server -- the CLI's tools, exposed to an MCP client
harness/vcctrl-cell     runs one attempt of a target program, for automated test sweeps
harness/vcctrl-sweep    orchestrates many cells into a named sweep
harness/vcctrl-collect  pulls results/logs back from the target after a sweep
profile-kinds/*.yaml    reusable hardware-shape templates (read by tools/new-profile.py only)
pi/install.sh           systemd units, deps, udev/config.txt edits on the daemon host
pi/deploy.sh            push from the control host and install (whole, or --page/--client/--mcp/--public/--hid-gadget/--profile <name>)
vendor/                 third-party code this repo carries (see THIRD-PARTY.md)
tests/                  the test suite -- pytest tests/, no hardware required
```

## Status, by target shape

| shape | proven | notes |
|---|---|---|
| PS/2 DOS/Windows PC over USB4VC (`vga-ps2`) | **working end to end** | keyboard, mouse, video, audio, power, file transfer, and the web KVM all proven on real hardware; see `docs/FINDINGS.md` |
| HDMI-out Linux box over the Pi's own USB gadget (`hdmi-usb`) | **working end to end** | no USB4VC required for this shape; multi-profile (one daemon, several targets) proven the same way |
| Classic Macintosh over USB4VC's ADB board + RGB2HDMI (`rgb2hdmi-usb4vc`) | **scaffolded, unmeasured** | `profile-kinds/rgb2hdmi-usb4vc.yaml` and `vcctrl-macintosh.example.yaml` exist; no such hardware has run against this project's rig yet |

Known gaps, honestly: no hardware reset line yet for a target that ignores
Ctrl-Alt-Del from within a program (GPIO to the reset header is the
documented plan, not yet built); H.264 video transport and the on-screen
keyboard's full per-key coverage are measured on exactly one board so far.
See `docs/OPEN-FAULTS.md` for the complete, current list.

## Contributing

See `CONTRIBUTING.md` for this repo's own working conventions (where
planning drafts and measurements go, what a commit message should say).
Run the test suite with `pytest tests/` -- no hardware required; it needs
Python, PyYAML, node, a Chromium/Chrome binary, and ffmpeg.

## Licence

MIT — see `LICENSE`. Third-party code under `vendor/` keeps its own
licence; see `THIRD-PARTY.md`.
