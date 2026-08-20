# Migrating usb4vc from the Pi 3B to a Pi 5

Written 2026-08-20. Plan only — nothing here has been executed.

**Verdict: closer to drop-in than this project has been assuming, but not
drop-in.** The blocker everyone cited is already retired. Four real items
remain, three of which are one-line changes and one of which is a physical
question no amount of software inspection can answer.

---

## 1. The blocker everyone cited is already gone

Two sessions, independently, told the operator that a Pi 5 breaks USB4VC
because `rpi_app` imports `RPi.GPIO`, which does not work on the RP1
southbridge. Both of us were describing the package list rather than reading
it. On the running Pi 3, right now:

    $ dpkg -l | grep python3-rpi
    ii  python3-rpi-lgpio  0.6-0~rpt1

    $ python3 -c "import RPi.GPIO as G; print(G.__file__, G.VERSION)"
    /usr/lib/python3/dist-packages/RPi/GPIO/__init__.py 0.7.2

`python3-rpi.gpio` is **not installed**. The only provider of the `RPi.GPIO`
module name is `rpi-lgpio`, the lgpio-backed shim — which reports `0.7.2` on
purpose, to impersonate the API it replaces. That version string is why a
casual check says "RPi.GPIO 0.7.2, this will break".

**USB4VC has therefore been running through lgpio in production on this rig,
for as long as this image has existed.** The entire GPIO surface — 27
`setup`, 16 `output`, 6 `input`, the pull configuration, the single
`add_event_detect`, and three `spi.xfer` — is already exercised through the
code path a Pi 5 would use. The migration does not have to prove that shim
works. It already works, here, on the hardware in the rack.

That does not make `add_event_detect` and SPI clocking risk-free (sec. 5),
but it moves them from "unknown, may require a port" to "known-good on one
platform, verify on the other".

## 2. Nothing on the Pi side is compiled

    $ find /home/pi/usb4vc -type f \( -name "*.so" -o -perm -111 \) ...
    /home/pi/usb4vc/firmware/PBFW_LISA_MAC_ADB_PBID3_V0_1_0.hex

One non-Python file, and it is an STM32 image that is *flashed over SPI*, not
executed on the Pi. There are no `.so` files, no compiled extensions, no
vendored binaries.

So "USB4VC is 32-bit only" is a statement about the **stock OS image**, not
about the application. `rpi_app` is pure Python 3 against packages that all
ship for `arm64` in the same Bookworm release the Pi 3 is already running:
`evdev`, `luma`, `PIL`, `requests`, `spidev`, `RPi` (via rpi-lgpio). Moving to
64-bit costs a reinstall, not a port.

The Pi 3 card cannot simply be cloned — an `armhf` rootfs will not boot a Pi 5
— so a fresh 64-bit build is required regardless. That is also the agreed
fallback strategy, so it costs nothing extra.

---

## 3. What actually changes

| | Pi 3B (now) | Pi 5 | Consequence |
|---|---|---|---|
| Arch | armhf, 32-bit, kernel `6.6.74+rpt-rpi-v7` | arm64 only | Fresh install; no clone path |
| GPIO | rpi-lgpio shim | rpi-lgpio shim | **No change** — already the same code path |
| Ethernet | `smsc95xx` **on the USB bus** | Native, off the USB bus | Frees the shared bus (see below) |
| USB | 4× USB2 behind one shared hub | 2× USB3 + 2× USB2 | The real win |
| Composite out | `enable_tvout=1`, 720×480 fb | **Does not exist** | Vestigial here — strip the config |
| Analog audio | ALSA card 0 `bcm2835 Headphones` | **Does not exist** | **Card indices shift — see 4.1** |
| Power | micro-USB, own supply | USB-C, wants 5V/5A | New PSU needed |
| Cooling | passive | active required | Fan/heatsink needed |

**Why the USB bus is the point.** Every peripheral on this rig currently
shares one USB 2.0 bus behind the LAN9514 hub, *including the Ethernet
controller*:

    Dev 3  smsc95xx        Ethernet
    Dev 4  MACROSILICON    2× uvcvideo + 2× snd-usb-audio + HID   <- capture stick
    Dev 5  Genesys Logic   mass storage                            <- CF reader

The capture stick carries **both** video and audio (it is one dongle
presenting five interfaces), and it contends with Ethernet and the CF reader
for a single 480 Mbit bus. That contention is the origin of the ~16k
interrupts/sec measured during capture, and it is exactly what a Pi 5 removes:
Ethernet leaves the bus entirely and the stick gets USB 3.

## 4. The four real items

### 4.1 ALSA card index will shift — and `hw:1,0` is the default

Today:

    0 [Headphones ]  bcm2835 Headphones      <- disappears on a Pi 5
    1 [U0x010xff02]  MACROSILICON            <- the capture stick, what we want
    2 [vc4hdmi    ]  vc4-hdmi

`daemon/vcctrld.py` line 1579:

    DEVICE = os.environ.get("VCCTRL_ALSA", "hw:1,0")

With card 0 gone, the stick will not be card 1. `hw:1,0` will then point at
whatever *is* card 1, or fail. The env override exists, which is the saving
grace, but the default is an index and indices are not stable across hardware.

**Fix, and do it on the Pi 3 first:** switch to a by-name reference, which is
stable across both machines —

    hw:CARD=U0x010xff02,DEV=0

Change it on the running Pi 3, confirm audio still captures, *then* migrate.
Changing code and changing hardware in the same step is how a working system
becomes two unknowns.

### 4.2 `/dev/video0` is hardcoded with no override

`daemon/vcctrld.py` line 869:

    DEVICE = "/dev/video0"

Unlike the ALSA path, there is no env escape hatch. The Pi 3 registers
`video10`–`video31` for the `bcm2835-codec` platform devices; the Pi 5 has a
different codec complement (notably no hardware H.264 encoder), so the
numbering the UVC stick lands on is not guaranteed.

**Fix, also on the Pi 3 first:** use a stable path —
`/dev/v4l/by-id/usb-MACROSILICON_*-video-index0` — and add a `VCCTRL_VIDEO`
override to match the audio one. Note `bin/vcctrl-capcheck` also hardcodes
`/dev/video0` in an ffmpeg line and will need the same treatment.

### 4.3 Composite config must be stripped

Confirmed vestigial by the operator — nothing is attached. But it is not inert:

    enable_tvout=1
    sdtv_mode=0 / sdtv_aspect=1 / overscan_*
    framebuffer_width=720 / framebuffer_height=480
    hdmi_ignore_hotplug=1
    dtoverlay=vc4-kms-v3d,composite

On a Pi 5 `enable_tvout` and the `composite` overlay parameter have no target.
Carry only what is load-bearing:

    dtparam=i2c_arm=on     # OLED
    dtparam=spi=on         # STM32 link + firmware flashing
    dtparam=audio=on       # (optional; the Pi's own audio is unused)

`gpu_freq=250`, `disable_splash`, `boot_delay=0` are Pi-3-era tuning and should
not be carried forward without a reason.

### 4.4 Take the console fix for free

This rig spent today's session losing its ssh control path for minutes at a
time because `systemd-journald` blocked writing to `/dev/console`, which is
`console=tty1` with `ixon` enabled — and the vcctrl virtual keyboard is a
keyboard to *the Pi*, so a `Ctrl-S` aimed at the DOS box is XOFF on the Pi's
own console. See FINDINGS sec. 28.

The mitigation now in place (`ForwardToConsole=no`, `ForwardToWall=no`, both
`MaxLevel`s at `emerg`) removes journald from the blast radius but leaves the
root cause: anything else writing to that console still hangs forever.

**A fresh build is the moment to fix it properly rather than inherit it.** On
the new card, before anything else:

- carry the journald settings over from the start
- `-ixon` on the console tty at boot, via a unit ordered before `getty`
- consider `console=` pointing somewhere a stray keystroke cannot stop
- keep `pi/install.sh`'s `ctrl-alt-del.target` mask — same class of bug, and
  the reason to keep it is now much better documented

---

## 5. Risks, in the order I would test them

1. **Mechanical fit.** The 40-pin header is in the same position on every
   model, so the HAT mates electrically. What is unverified is whether the
   USB4VC board, standoffs or enclosure clear a Pi 5's PCIe connector, fan
   header, power button and relocated ports. **This is a physical question and
   nobody should plan around a guess.** Verify with the board in hand before
   anything else.
2. **SPI clock rate.** `flash_fw.py` writes STM32 firmware over SPI. RP1's
   clock divisors differ from BCM2835's, so confirm `max_speed_hz` is actually
   honoured before flashing anything. Failure mode here is the worst on the
   list: a half-flashed STM32 on the board that is the whole point of the rig.
   Test SPI *reads* first; do not flash as the first SPI operation.
3. **`add_event_detect`.** Exactly one call. lgpio's debounce and
   callback-thread semantics differ from the original library's, and it cannot
   watch a pin that is also being driven. Already exercised on the Pi 3 through
   the same shim, so this is verification rather than discovery — but verify it
   *fires*, do not infer it from the app starting.
4. **Power.** Own supply today, so no back-powering concern through the HAT.
   Source a 27 W (5 V/5 A) USB-C PD adapter; a 5 V/3 A supply boots but limits
   the USB current budget, and this rig runs three USB devices with a video
   capture stick among them.
5. **Tailnet identity.** `vcctrl-pi.example.ts.net` belongs to the *node*, not
   the rig. It is baked into `pi/deploy.sh` defaults, the TLS cert, the
   `tailscale serve` config, the monitoring, and every bookmark. A new machine
   needs the old node removed and the name claimed, or all of that moves.
6. **Thermals.** Passive today, active required on a Pi 5. Not optional under
   continuous capture load.

## 6. Sequence

**Phase 0 — on the Pi 3, before touching any new hardware.** Fix 4.1 and 4.2
in place: by-name ALSA and video references, plus the `VCCTRL_VIDEO` override.
Verify capture still works. Commit. This means the Pi 5 build inherits code
that is already hardware-agnostic, and any breakage is attributable to one
change at a time.

**Phase 1 — off-rig, no downtime.** Fresh 64-bit Bookworm on a **new** SD card.
The Pi 3's card is not written to at any point and stays the revert path.
Install `python3-rpi-lgpio`, `python3-spidev`, `python3-luma.*`, `python3-evdev`,
`python3-pil`, `python3-requests`, `ffmpeg`. Minimal `config.txt` per 4.3.
Console hardening per 4.4.

**Phase 2 — USB4VC alone, nothing else connected.** Physical fit check first
(risk 1). Then prove the board in isolation: OLED lights, `rpi_app` starts from
`/etc/rc.local`, a keystroke reaches a target. SPI *read* before any flash.
**If this fails, stop** — swap the Pi 3 back and the rig is as it was.

**Phase 3 — vcctrl.** `pi/install.sh`, capture stick, CF reader, the Kasa plug
at 192.0.2.46. Confirm `caps` reports all six, a frame locks, audio floor
looks analog rather than digital-silent.

**Phase 4 — tailnet.** Remove the old node, claim the name, re-issue the cert,
re-point `tailscale serve`. Verify the web KVM from a phone, since that is the
path with the most surface.

**Phase 5 — a real sweep.** Not a smoke test. A full sweep with collect,
compared against a banked result on the same hardware profile. The Pi is in the
timing path for every keystroke, and nothing short of a measured run proves it
did not change.

## 7. Rollback

Power down, swap the Pi, power up. The Pi 3's SD card is never written to, so
the revert is the physical swap plus the tailnet name moving back. Minutes, not
hours — provided phase 4 is the *last* irreversible step, which is why it sits
after everything else.

## 8. Open items

- Mechanical clearance — physical inspection required (risk 1)
- Whether the CF reader and capture stick want USB2 or USB3 ports on the Pi 5;
  cheap USB3 capture dongles are occasionally happier on a USB2 port, and this
  one is a `0001:ff02` "Fry's Electronics" no-name
- Whether anything besides the OLED uses `/dev/i2c-2`
