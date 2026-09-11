# Known limitations, for a stranger with different hardware

This lists what does NOT yet adapt to a different system, separately from
`docs/OPEN-FAULTS.md` (which tracks bugs and workarounds on this project's
own hardware). Everything here works correctly on this project's own system;
the limitation is that another system's shape isn't yet a configuration
choice, either because the value is still a literal in the code, or
because the mechanism itself was designed against one specific setup.

## The DOS-side contract (the `vga-ps2` profile kind)

A `vga-ps2`-kind target (PS/2 keyboard/mouse over USB4VC, the shape this
project's own system runs) needs the following facts about its DOS
installation, and every one of them is currently a literal in
`daemon/vcctrld.py` or `harness/*`, not a config key:

- **The CONFIG.SYS boot-menu item number and its timeout window.** This
  project's target has its network-capable boot profile at item `"5"`
  with a 5-second selection window; both are hardcoded
  (`daemon/vcctrld.py`'s boot-menu handling, `harness/vcctrl-collect`'s
  `NET_MENU_ITEM`). A target with a differently-numbered menu, or no menu
  at all, cannot be described in config yet.
- **The BLASTER-environment-variable-to-boot-profile-name map.** Used to
  identify which sound configuration actually booted, by matching the
  live `BLASTER` string against a small hardcoded table
  (`daemon/vcctrld.py`). A target with different sound hardware, or none,
  has no way to add its own entries.
- **`mTCP`'s installation path and batch-file names** (`C:\MTCP`,
  `GET.BAT`/`PUT.BAT`/`CHK.BAT`/`VCGET.BAT`/`VCCHK.BAT`/`VCLIST.BAT`), and
  the packet-driver-detection markers (`"name:"`, `"odipkt"`,
  `"no packet drivers found"`). A target using a different TCP/IP stack or
  packet driver isn't recognized.
- **`RDYPULSE.COM`'s installation path** (`C:\DRIVERS\RDYPULSE.COM`) and
  its Scroll-Lock-as-readiness-signal protocol. This is the ONLY
  boot-readiness witness this project implements; a target that can't run
  a custom TSR, or doesn't use PS/2 LEDs, has no readiness signal at all
  (see "Pluggable readiness witness" below).
- **The VGA mode-restore COM files** (`C:\VGACAP\MODE12`, `MODE03`) and
  **UniVBE's path** (`C:\UNIVBE\UVCONFIG.EXE`, `UNIVBE.DRV`), used to force
  the console back into a capturable video mode and to configure VESA
  timings per video card.

None of `dos/keywit.asm`, `dos/rdypulse.asm` or the mode-restore COMs are
shipped with an install script — putting them on a target's boot media is
a manual, undocumented step today.

## A pluggable readiness witness

The Scroll-Lock-over-PS/2-LEDs signal above is the only mechanism this
project has for "the target finished booting." An `hdmi-usb`-kind target
(no USB4VC, no PS/2 LEDs) has no equivalent, and every reboot-and-wait
operation that depends on this signal simply doesn't work for that shape
yet. This needs an actual abstraction (a `readiness: {kind, ...}` block in
a profile) with at least a second implementation before it's a real
choice rather than one hardcoded path.

## Timing constants that are this system's own physics, not configuration

`RegistryDriver`'s `BOOT_TIMEOUT_S`/`MENU_TIMEOUT_S`/`PROMPT_TIMEOUT_S`/
`TRANSFER_TIMEOUT_S`/`RESEND_TIMEOUT_S` (`daemon/vcctrld.py`), the harness's
own wait constants (`harness/vcctrl-collect`, `bin/vcctrl_common.py`), and
the ring/transfer size caps (`RING_BYTES`, `LARGEST_VERIFIED_BYTES`,
`REFUSE_BYTES`) are all literals measured against this project's own
target hardware. A much slower (or faster) real machine needs these
changed in the source, not in a config file.

## Capabilities read once from the primary profile

`AudioCapability.DEVICE`, `CameraCapability.DEVICE`, `BoardCapability.FILE`,
`LED_BOARDS` and `POWER_BOARDS` are all set once, from the PRIMARY
profile's config, when the daemon process starts (`daemon/vcctrld.py`).
A second profile (see `docs/PROFILES.md`) sharing the same daemon process
gets the primary's values for these specifically, not its own — this is
already documented as a known gap in `docs/lab/OPEN-FAULTS.md` #21, not
new here, but it belongs on this list too since it's exactly the kind of
thing a stranger adding a second target would trip on.

## Install-time assumptions

- **`/opt/vcctrl`, `/usr/local/bin/vcctrl`, and the `pi` user/group** are
  literals throughout `pi/install.sh`, `pi/deploy.sh`, and the systemd
  unit files under `pi/files/` — not an install prefix, bindir, or
  service-user setting.
- **`daemon.web.tls.provider: tailscale | files | none`** is validated in
  `vcctrl.example.yaml` but only the `tailscale` path is actually wired
  into `pi/install.sh`; choosing `files` or `none` there doesn't change
  what the installer does.
- **The HID gadget** (`pi/files/vcctrl-hid-gadget-setup.sh`) uses a fixed
  gadget name, VID:PID and USB strings, and unconditionally unbinds
  whatever else is using the Pi's UDC — there's no way to pick a different
  identity or coexist with another gadget function.

## Layout and text handling

- **`type`'s character map is US-QWERTY only** (`daemon/vcctrld.py`); a
  non-US keyboard layout on the target will type the wrong characters for
  its own accented/shifted keys.
- **The harness's OCR digit-confusion folds** (`harness/vcctrl-cell`,
  `bin/vcctrl_common.py`) are tuned to this project's own DOS console font
  and are not documented as font-specific, though they clearly are.

## The bootstrap file-transfer path

Before a target has a NIC and mTCP working, this project's own bootstrap
path for getting the FIRST files onto a CF card runs through
`~/doskutsu-netiter/server.py`/`serve.sh` on the control host — a script
that exists outside this repository by design (see `CONTRIBUTING.md`), so
a stranger's clone has no equivalent and no documented way to build one.
Once a target has its own NIC and the daemon's embedded FTP server
running (`FilesCapability` in `daemon/vcctrld.py`), this isn't needed —
but getting to that point on a target with nothing on it yet has no
documented path today.

---

None of the above is a security or identifier problem — everything on
this list is a **generality** gap: a real system can be built and driven
today by editing the source in the specific places named, but cannot yet
express the difference purely in `vcctrl.yaml` or a profile file the way
the rest of this project's configuration already does. Turning each of
these into a real, tested configuration point is real, separate,
substantial work, tracked here rather than silently implied to be done.
