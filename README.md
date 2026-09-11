# vcctrl

Remote control of a real, physical retro or legacy machine: keyboard,
mouse, screen, audio, power and file transfer, driven by a CLI, an MCP
server, or a browser KVM. It exists to let automated tests run against
hardware that predates automation — DOS boxes, classic Macs, anything you
can wire a capture device and an input path to.

**[docs/HARNESS-STANDARD.md](./docs/HARNESS-STANDARD.md)** — the
target-agnostic contract for running measured tests. **[docs/MCP-SERVER.md](./docs/MCP-SERVER.md)**
— connecting an agent over MCP. **[docs/SKILLS.md](./docs/SKILLS.md)** —
portable domain knowledge as `SKILL.md` files. **[docs/PROFILES.md](./docs/PROFILES.md)**
— driving more than one target from one daemon.

## How it's built

Three roles, usually three machines:

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

All timing-sensitive work — key dwell, event pacing, capture — happens on
the daemon host, never the control host: network latency to the control
host must never enter the input path. A minimal setup runs both roles on
one box; the common one is a Pi as the daemon and a laptop as control.

## Hardware and configuration

Three shapes, each a `profile-kinds/*.yaml` template. Scaffold one with
`tools/new-profile.py --kind <kind> --name <yours>` and fill in the
`REPLACE_ME` placeholders — the YAML below shows only what makes each
shape what it is, not a complete config.

### Retro PC — `vga-ps2`

A DOS/Windows-era PC with PS/2 keyboard/mouse and analog VGA out.

**Hardware:** Raspberry Pi 5 · USB4VC HAT with its IBM PC protocol board ·
a VGA-to-USB capture dongle · optional UVC camera pointed at the machine.

```yaml
capabilities:
  input:
    backend: usb4vc-uinput
  video:
    backend: v4l2-ffmpeg
    settings:
      device: /dev/v4l/by-id/usb-MACROSILICON_xxxx-video-index0
  camera:
    backend: none   # or v4l2-ffmpeg, if you have the second camera
```

### Classic Macintosh — `rgb2hdmi-usb4vc`

ADB keyboard/mouse, capture via an RGB2HDMI board. **Scaffolded but
unmeasured** — no such hardware has run against this project's own build
yet, so treat the template's values as a documented guess.

**Hardware:** Raspberry Pi 5 · the same USB4VC HAT with its Lisa/Mac/ADB
board instead of the IBM PC one · RGB2HDMI feeding an HDMI-to-USB capture
dongle · optional UVC camera.

```yaml
capabilities:
  input:
    backend: usb4vc-uinput   # same backend as Retro PC -- the board swap
                              # is what changes, not this setting
  video:
    backend: v4l2-ffmpeg
    settings:
      device: /dev/v4l/by-id/usb-xxxx-video-index0   # the RGB2HDMI dongle
```

### Modern PC — `hdmi-usb`

Any machine with HDMI out and a spare USB port. No USB4VC — the Pi's own
USB-C port acts as a keyboard/mouse gadget straight to the target.

**Hardware:** Raspberry Pi 5 · an **official Raspberry Pi USB3 hub**,
upstream port into the target (which then sees the Pi as a plug-in
keyboard/mouse) · the **official Raspberry Pi power supply plugged into
the hub**, not the Pi — it feeds the Pi over the same cable. Use the
official pair specifically: an underpowered hub or charger here caused a
real undervoltage brownout on this project's own hardware. · an HDMI
capture dongle · optional UVC camera on the Pi directly.

Driving capture, the HID gadget and encoding together can throttle a
Pi 5 — one capture device per Pi for this shape.

```yaml
capabilities:
  input:
    backend: hid-gadget
    settings:
      hid_keyboard_device: /dev/hidg0
      hid_mouse_device: /dev/hidg1
  video:
    backend: v4l2-ffmpeg
    settings:
      device: /dev/v4l/by-id/usb-xxxx-video-index0
      analog: false
```

Needs `dtoverlay=dwc2,dr_mode=peripheral` under `/boot/firmware/config.txt`'s
`[pi5]` section and one reboot — once per Pi, regardless of how many
`hdmi-usb` targets it later drives.

### Power (all three, optional)

```yaml
capabilities:
  power:
    backend: kasa   # kasa | kasa-klap | wemo | shell | none
    settings:
      host: 192.0.2.20
```

Both TP-Link Kasa generations and Wemo are supported. No plug at all is
fine (`none`, or omit the block) — you lose remote power-cycling, nothing
else. `shell` runs your own on/off/state commands for a relay, a PDU, or
a GPIO pin.

### What every shape needs, regardless

A Linux daemon host (`/dev/uinput` for USB4VC, or a peripheral-capable USB
port for `hid-gadget`), root and systemd; a capture device that emits
MJPEG natively over V4L2; ffmpeg (with libx264/libopus for the optional
H.264 transport and Opus audio); Python ≥ 3.7 (≥ 3.10 for the MCP server).

## Quickstart

**1. Flash the daemon host.** Raspberry Pi OS or Debian, arm64, Trixie
(13)+, SSH enabled.

**2. Install [USB4VC](https://github.com/dekuNukem/USB4VC)**, then this
repo's two local patches:

```sh
python3 tools/patch-usb4vc-64bit.py --check   # then without --check to apply
python3 tools/patch-usb4vc-board.py --check
```

**3. Clone and configure**, from the control host:

```sh
git clone <this repo's URL> && cd vcctrl
cp vcctrl.example.yaml vcctrl.yaml       # untracked, never commit this
$EDITOR vcctrl.yaml                      # daemon_host, plug, devices
```

**4. Deploy:**

```sh
VCCTRL_HOST=<pi-hostname-or-ip> pi/deploy.sh
```

(`control.daemon_host` in `vcctrl.yaml` works instead of the env var;
`deploy.sh` refuses clearly if neither is set.)

**5. Install the CLI and confirm the daemon answers:**

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
```

**7. Connect an agent** instead of typing verbs by hand — see below.

**8. Scaffold your own profile** once the above works:
`tools/new-profile.py --kind <vga-ps2|hdmi-usb|rgb2hdmi-usb4vc> --name <yours>`.
See `docs/PROFILES.md` for running more than one target off one daemon.

## Connect an agent: skills vs. MCP

**Skills** are portable knowledge — free to copy, grant nothing. **MCP**
is real control of physical hardware — registering it is a hardware-access
decision, not a documentation one; never commit a real hostname to get it
working.

**Skills** (Claude Code, Codex, Cursor — no checkout needed):

```sh
npx skills add <this repo's URL> \
  --full-depth -a claude-code -y \
  -s vcctrl-mcp-workflows -s vcctrl-common-workflows \
  -s vcctrl-rig-hazards -s vcctrl-camera   # --agent codex for Codex
```

Installs the four hardware-portable skills. Two more
(`vcctrl-repo-conventions`, `vcctrl-webkvm-copy`) describe this repo's own
conventions and are left out on purpose — see `docs/SKILLS.md` if you
want them anyway.

**MCP** (drives the real hardware):

```sh
# daemon mode -- once deployed (docs/MCP-SERVER.md sec. 4), no local checkout
claude mcp add --transport http vcctrl-mcp-daemon https://<your-daemon-host>/mcp

# control mode -- needs a local clone + venv
cd vcctrl
python3 -m venv agent/.venv && agent/.venv/bin/pip install -r agent/requirements.txt
claude mcp add vcctrl-mcp -- "$(pwd)/agent/.venv/bin/python3" "$(pwd)/agent/vcctrl_mcp.py"
```

Restart the session after registering — `/mcp` doesn't pick up a fresh
server live. Full reasoning and the safety model: [docs/MCP-SERVER.md](./docs/MCP-SERVER.md).

## Layout

```
bin/vcctrl              control-host CLI -- ssh's to the daemon, no logic itself
bin/vcctrl-client       the real CLI; installed on the daemon host as /usr/local/bin/vcctrl
bin/vcctrl-*            one-off diagnostics run from the control host (audio, capture, card ID)
daemon/vcctrld.py       input/video/audio/power/file server; owns the devices
daemon/vcweb.py         the control web KVM; daemon/vcweb_public.py is the read-only mirror
common/vcconfig.py      shared config loader (both hosts import it)
agent/vcctrl_mcp.py     MCP server exposing the CLI's tools
harness/vcctrl-*        cell/sweep/collect -- automated test runs against a target
profile-kinds/*.yaml    hardware-shape templates (tools/new-profile.py reads these)
pi/install.sh           systemd units and setup on the daemon host
pi/deploy.sh            push + install from the control host
vendor/                 third-party code (see THIRD-PARTY.md)
tests/                  pytest tests/ -- no hardware required
```

## Status, by target shape

| shape | proven | notes |
|---|---|---|
| Retro PC (`vga-ps2`) | **working end to end** | keyboard, mouse, video, audio, power, file transfer, the web KVM — all on real hardware; `docs/lab/FINDINGS.md` |
| Modern PC (`hdmi-usb`) | **working end to end** | no USB4VC needed; multi-profile (one daemon, several targets) proven the same way |
| Classic Macintosh (`rgb2hdmi-usb4vc`) | **scaffolded, unmeasured** | templates exist; no such hardware has run against this project's own build yet |

Known gaps: no hardware reset line for a target that swallows
Ctrl-Alt-Del (GPIO to the reset header is planned, not built); H.264
transport and full on-screen-keyboard coverage are measured on one board
so far. `docs/lab/OPEN-FAULTS.md` has the complete list of what's broken;
**`docs/KNOWN-LIMITATIONS.md`** has what doesn't yet adapt to different
hardware at all — the DOS-side boot contract, timing constants, install
paths, and similar still-hardcoded pieces.

## Contributing

See `CONTRIBUTING.md` for this repo's conventions. Run the tests with
`pytest tests/` — no hardware required; needs Python, PyYAML, node, a
Chromium/Chrome binary, and ffmpeg.

## Licence

MIT — see `LICENSE`. Third-party code under `vendor/` keeps its own
licence; see `THIRD-PARTY.md`.
