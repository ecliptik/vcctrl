# Migrating usb4vc from the Pi 3B to a Pi 5

Written 2026-08-20, revised the same day once the hardware was in hand, and
updated again once it was executed.

## STATUS: phases 0-2 and 5 done. Phases 3, 4 and 6 wait on the hardware swap.

| Phase | State |
|---|---|
| 0. Device-name fixes on the old Pi | **Superseded.** `VCCTRL_VIDEO` landed on the new machine instead (the webkvm session added it); the Pi 3 was already down. Cost: the change ships untested against a real device, mitigated by the default being byte-identical to the old behaviour |
| 1. Base system | **Done.** Trixie arm64, apt-only (no pip, so PEP 668 never applies), SPI+I2C enabled, console hardened at build time, tuning applied |
| 2. USB4VC on the bench | **Done.** Board answers over SPI through rpi-lgpio, OLED live, `PB INFO` frame read, PBID 3 |
| 3. Move the Gateway | **Pending** — the commitment point |
| 4. Peripherals + vcctrl | **Partly done.** vcctrl installed and all seven capabilities up; video/audio report `unavailable` because the sticks are not attached yet |
| 5. Rename | **Done.** Pi 3 is `usb4vc-old` and powered down; the Pi 5 holds `usb4vc`, cert reissued, `tailscale serve` re-pointed |
| 6. A real sweep | **Pending** — and see the revised comparability rule below |

**Two things went differently from the plan.**

The first SD card was **failing, not just unexpanded**: `mmc0: Card stuck being
busy` four times and two read errors on first boot, which killed the firstboot
resize partway and left the filesystem `clean with errors`. Replaced rather
than repaired — this rig's output is measurements, and an hour saved is not
worth building them on storage already known to drop writes.

**The RPi.GPIO blocker never existed.** `python3-rpi-lgpio` was already
installed on the Pi 3 and `python3-rpi.gpio` was not, so USB4VC had been running
through the lgpio shim in production all along. Two sessions reported that
blocker to the operator from the package list rather than from the machine.

**Phase 6 no longer compares absolute fps.** The benchmarking session measured
the same configuration one day apart at 30.4 and 29.6 — 0.85 across sessions
against a 0.2 within-session band — so nothing the Pi 5 plausibly does could be
resolved that way. Compare the **control-pair spread** and the **PUMP arm delta
(+1.30)** instead: both are within-session differences and therefore measured
against the tight band.

## 0. What the operator settled, and what it changes

| Decision | Consequence |
|---|---|
| **Raspberry Pi OS Trixie Lite** (Debian 13, Python 3.13) | Not Bookworm. Newer than the Pi 3's 12/3.11 — see sec. 3a for the package deltas that actually matter |
| **A second USB4VC board** | The board, GPIO, SPI, OLED and `rpi_app` are all provable in parallel with **zero risk to the running rig** |
| **Capture stick and CF reader move at cutover** | Only one set exists. Video/audio cannot be tested in parallel; they are the *last* thing proven, not the first |
| **The Gateway moves to the Pi 5 early** | **This is the sharpest edge.** The moment the g2k's PS/2 lead moves, the Pi 3 stops being a working fallback — it keeps its SD card and its software, but it has nothing to drive. Rollback stops being "swap the Pi" and becomes "swap the Pi *and move the target back*" |
| **`usb4vc-new` now, renamed to `usb4vc` at cutover** | The old node must be renamed FIRST to free the name (sec. 3c). Renaming is the cutover switch: everything pointing at `usb4vc` moves in one step |
| **Pi 3 retired once the Pi 5 is proven** | Single-rig end state, so nothing needs to learn to handle two permanently |

Because rollback degrades after the target moves, the sequence below front-loads
everything that is reversible and defers the target move until the board itself
is proven.

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

## 3a. Trixie changes the dependency picture, and mostly for the better

Verified against the live archives, not assumed.

**The shim survives the jump.** `python3-rpi-lgpio 0.6-0~rpt1+trixie` exists in
the Raspberry Pi archive for `arm64` — the same 0.6 the Pi 3 runs — alongside
`python3-lgpio 0.2.2-1~rpt1+trixie`. `python3-rpi.gpio` is not there at all,
which is consistent with it being unusable on RP1.

**The Pi 3 is a mix of apt and pip, and that is the part to clean up.** These
live in `/usr/local/lib/python3.11/dist-packages`, i.e. pip-installed over a
PEP 668 `EXTERNALLY-MANAGED` marker that is already present on Bookworm:

    evdev==1.7.1   luma.core==2.4.2   luma.oled==3.13.0   Pillow==9.4.0
    pyusb==1.2.1   smbus2==0.4.2      spidev==3.5         pyserial==3.5

**Every one of them is an apt package in Debian Trixie**, so the Pi 5 build
should use apt and avoid pip entirely:

| Package | Pi 3 (pip) | Trixie (apt) | Direction |
|---|---|---|---|
| `python3-evdev` | 1.7.1 | 1.9.1-1 | forward, API stable |
| `python3-luma.core` | 2.4.2 | 2.4.2-1 | identical |
| `python3-luma.oled` | 3.13.0 | 3.10.0-1 | **backward** — see below |
| `python3-pil` | 9.4.0 | 11.1.0 | forward, big jump |
| `python3-usb` | 1.2.1 | 1.2.1-2 | identical |
| `python3-smbus2` | 0.4.2 | 0.4.3-1 | forward |

Two of those needed checking rather than assuming:

- **The `luma.oled` downgrade is safe here.** USB4VC drives an `ssd1306` —
  `--display ssd1306 --interface spi --spi-port 0 --spi-device 1 --gpio-reset 6
  --gpio-data-command 5 --spi-bus-speed 2000000`. ssd1306 is the oldest and
  most stable device class luma ships; nothing about it arrived after 3.10.
- **The Pillow 9.4 → 11.1 jump is safe here.** Pillow 10 removed
  `Image.ANTIALIAS` and the `textsize`/`getsize` font methods, which is the
  usual way this breaks. `grep` across `rpi_app` finds none of them in use.

**Note the OLED is on SPI, not I2C** — `spidev0.1`, while the STM32 is on
`spidev0.0`. SPI0 therefore carries both, which sharpens risk 2: a clock-rate
problem would affect the display and the microcontroller, and only one of them
fails visibly.

`python3-pil` is currently installed as `:armhf`; on arm64 it is simply the
native package. `gpiozero` and `pigpio` are present on the Pi 3 but nothing in
either codebase imports them — do not carry them without a reason.

## 3c. The rename is the cutover switch

`usb4vc` is a tailnet node name, and it is load-bearing well beyond ssh: it is
`pi/deploy.sh`'s default host, the TLS cert subject
(`vcctrl-pi.example.ts.net`, valid to Nov 2026), the `tailscale serve` target,
the web KVM URL in bookmarks, and the host in this session's monitoring.

Because exactly one node can hold the name, the rename is not a tidying step —
**it is the moment the whole toolchain changes machines.** Order matters:

1. Old node `usb4vc` → `usb4vc-pi3`. Everything pointing at `usb4vc` breaks
   *immediately*, including `deploy.sh` and the KVM URL. Do this only when
   ready to complete the cutover.
2. New node `usb4vc-new` → `usb4vc`.
3. Re-issue the cert on the new node and re-point `tailscale serve`. The old
   cert cannot move; it is issued to the node that held the name.

Until step 1, the Pi 5 is reachable as `usb4vc-new` and nothing that assumes
`usb4vc` needs to change. That is deliberate: it keeps the rename as a single
reversible switch rather than a scattered edit.

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

Ordered so that everything reversible happens before anything that degrades the
fallback. The target move (phase 3) is the point of no easy return, so nothing
speculative happens after it.

**Phase 0 — on the Pi 3, before the Pi 5 is touched.** Fix 4.1 and 4.2 in
place: by-name ALSA reference, `by-id` video path, `VCCTRL_VIDEO` override, and
the same treatment in `bin/vcctrl-capcheck`. Verify capture still works on
hardware that is known good. Commit. The Pi 5 then inherits code that is
already hardware-agnostic, and any later breakage has one candidate cause
instead of two.

**Phase 1 — Pi 5 base system, `usb4vc-new`, no rig impact.** Trixie Lite is
already imaged with a `claude` user and key. Bring up: apt package set per 3a
(no pip), minimal `config.txt` per 4.3, console hardening per 4.4 *before*
anything else writes to a console, tailscale join as `usb4vc-new`. Nothing here
touches the running rig.

**Phase 2 — the second USB4VC board, bench only.** Mechanical fit first (risk
1) — this is the one that cannot be planned around. Then prove the board in
isolation: OLED lights (ssd1306 over SPI0 CE1), `rpi_app` starts from
`/etc/rc.local`, SPI **reads** succeed. Check the new board's STM32 firmware
version rather than assuming it matches the Pi 3's
`PBFW_LISA_MAC_ADB_PBID3_V0_1_0.hex`, and **do not flash as the first SPI
operation**. If any of this fails, stop — the running rig is untouched and
nothing has been lost.

**Phase 3 — move the Gateway. This is the commitment point.** PS/2 lead from
the old USB4VC to the new one. Prove a keystroke reaches DOS and comes back on
the LED channel. From here the Pi 3 has no target and is no longer a working
fallback, only a shelf spare.

**Phase 4 — move the peripherals and install vcctrl.** Capture stick and CF
reader across, `pi/install.sh`, Kasa plug at 192.0.2.46 unchanged (it is a
network device and does not care). Confirm `caps` reports all six, a frame
locks, and the audio floor reads analog rather than digital-silent. This is the
first time video and audio can be tested at all, which is why it is late.

**Phase 5 — the rename.** Per 3c, in that order, then re-issue the cert and
re-point `tailscale serve`. Verify the web KVM from a phone, since that path
has the most surface.

**Phase 6 — a real sweep.** Not a smoke test. A full sweep with collect,
compared against a banked result on the same hardware profile. The Pi sits in
the timing path for every keystroke and every LED poll, and the harness has
already produced four separate timing bugs on the *old* hardware. Nothing short
of a measured run proves the new machine did not move something.

### 6a. Acceptance criteria, fixed before the run

**Written down before any Pi 5 number existed**, which is the only time a
threshold can be chosen honestly. Supplied by the benchmarking session from
their profile sec. 3.2b rather than invented here.

The instrument is PUMP on machine 1, declared `VIRGE PICOGUS`, against round P
banked at `~/doskutsu-netiter/banked/roundP-pi3-2026-08-20/`:

| cell | arm | Pi 3 |
|---|---|---|
| GPU0 | control | 29.6 |
| GPU0B | control, repeat | 29.5 |
| GPUA | no_input_poll | 30.9 |
| GPUAB | no_input_poll, repeat | 30.8 |

Assumed per-cell sigma is **0.10** — the profile's declared 0.2 within-session
band read as ~2 sigma. The three pair ranges actually observed (0.0, 0.1, 0.1)
imply sigma ~0.06, but they are quantised to 0.1 and do not determine it, and a
tight sigma makes the test fire on noise.

**Control-pair and arm-pair spread.** Threshold 0.2, because the profile
declared 0.2 before this test existed.

    both pairs <= 0.2       pass
    exactly one over 0.2    INCONCLUSIVE -- re-run, do not diagnose
    both over 0.2           fail: the harness has added variance

The middle state is not squeamishness. **A pair spread is a 2-sample range,
which is a poor variance estimator**: at sigma 0.10 the difference of two cells
has SD 0.14, so one pair exceeds 0.2 by chance about one time in six with
nothing wrong, and with two pairs per run, seeing one wide pair on healthy
hardware is roughly a one-in-three event. Treating that as failure would fail
good hardware most of the time it was tested.

**Arm delta.** Centre +1.30, and its band is **not** tighter than a single
cell's, which is the counterintuitive part:

    Var(delta) = sigma^2/2 + sigma^2/2 = sigma^2      SD(delta) = 0.10

Averaging two cells halves the variance, but that is done for both arms and
then differenced, which adds it straight back. Four cells buy no more precision
than one.

    1.10 - 1.50             pass
    1.00-1.10 or 1.50-1.60  INCONCLUSIVE -- repeat before concluding
    outside 1.00 - 1.60     real change, investigate the harness

**The two verdicts are not independent, and are not combined symmetrically.**
The delta window rests on sigma = 0.10, and the pair spread is exactly what
estimates sigma — so a run whose spreads have widened has refuted the premise
its own delta threshold stands on, and a delta that drifted in that run is
*explained by* the widened spread rather than being separate evidence.

    spread FAIL    -> overall FAIL; delta reported but UNSCORED
    spread INCONC  -> overall INCONCLUSIVE; delta scored but flagged
    spread PASS    -> sigma confirmed by the data, delta stands on its own

"This run cannot tell you" is a different statement from "the delta is bad",
and collapsing them counts one fault as two. Both quantities are printed
whatever the verdict, because they test different hypotheses — spread asks
whether the apparatus added variance, delta asks whether it shifted the
measurement — and one exit code cannot say which moved.

**Do not attempt to resolve a delta difference below ~0.2 at all** — reporting
resolution is 0.1 and the statistical band is 0.1, so below that you are
reading quantisation. If a tighter verdict is ever needed the lever is more
repeats per arm, not a cleverer statistic.

**Why the delta is the right instrument, checked rather than assumed.** The
obvious objection is that it only cancels an *additive* session confound. If
whatever moved 30.4 to 29.6 is proportional instead, it is 2.8% at ~30 fps, and
2.8% of a 1.30 delta is **0.04** — an order of magnitude inside the window. So
it survives the confound either way.

### 6b. Two hazards closed before launching

**Tag collision.** PUMP writes GPU0/GPUA/GPU0B/GPUAB on every run, so a second
run overwrites the first — on the card and, through `--collect`, in
`incoming/`. Round P was archived first. **Archive before re-running any sweep
whose tags you are about to reuse**, which is every sweep.

**Stale logs read as fresh.** The four tags were deleted from
`C:\DOSKUTSU\LOGS` before launch, confirmed by a `DIR` returning `File not
found`. A cell that fails to run now leaves a hole that `--collect` reports
loudly, instead of handing back the previous run's log with nothing to say it
was old. PLAN sec. 5.2 states this rule; nothing enforced it.

### 6c. Result: PASS  [measured 2026-08-20, 14:41-14:47]

    cell    Pi 3    Pi 5
    GPU0    29.6    29.8     control
    GPU0B   29.5    29.6     control, repeat
    GPUA    30.9    31.1     no_input_poll
    GPUAB   30.8    30.9     no_input_poll, repeat

    control-pair spread   0.1 -> 0.2    pass (threshold 0.2)
    arm-pair spread       0.1 -> 0.2    pass
    ARM DELTA            +1.30 -> +1.30  pass (window 1.10-1.50)

**The arm delta reproduces exactly.** That is the quantity the whole test was
built around, and the one robust to a session confound in either direction.
The migration did not move the timing path.

Sweep ran 11.5 min against a 23 min budget, four cells clean, one transient
blind poll at t+4.3 which the three-state stall detector counted as blind
rather than scoring as movement. All nine artifacts collected.

**Two honest qualifications, neither of which changes the verdict.**

*The spreads doubled, 0.1 to 0.2 on both pairs.* Both pass, and both sit ON
the threshold rather than comfortably inside it. At sigma 0.10 the expected
range of two samples is ~0.11, so 0.2 twice is the upper end of normal rather
than a signal — but it is not nothing, and it is the direction a harness that
had added variance would move. **Do not read this run as evidence the Pi 5 is
quieter than the Pi 3.** The next PUMP run is worth watching for the same
thing; two runs at 0.2 would be worth a look, three would not be noise.

*`irq_source` differed.* Round P declared `NONE`, this run `UNDECLARED`,
because the QA line was given `--video` and `--sound` but no `--irq`. That is
an omission on the harness side and the only difference in the entire
manifest — every other field, including both PicoGUS mode readbacks, is
byte-identical. It is a declaration rather than a measurement, and the
configuration it describes is separately proven by the readbacks and by
cardid, so the comparison stands. **Pass `--irq NONE` next time** so the two
manifests are identical rather than merely equivalent.

**Card attested rather than declared.** All four SDL logs read S3 ViRGE/DX at
8/8 signature points (vram 4096 KB, lfb 0x78000000, 320x240 present, S3 probe
positive), against ATI Mach64 1/6. Declared VIRGE and detected ViRGE agree,
which is worth having on the first run from new hardware — that is exactly
when a silently different configuration is most plausible.

**Phase 6 is complete, and with it the migration.**

## 7. Rollback

**Before phase 3:** free. Power down, put the Pi 3 back in service, done — its
SD card was never written to and it still owns the tailnet name.

**After phase 3:** physical and slower. Move the PS/2 lead back, move the
capture stick and CF reader back, and if phase 5 has happened, rename the nodes
back and re-issue the cert. Minutes rather than seconds, and it needs someone
at the rack.

This is the direct cost of moving the target early, and it is worth stating
plainly rather than discovering it: the plan trades a cheap rollback for an
earlier end-to-end test. That is a reasonable trade *because* phase 2 proves
the board before the target moves — but it means phase 2 must not be rushed.

## 8. Open items

- **Mechanical clearance** around the Pi 5's PCIe connector, fan header and
  relocated ports. Physical inspection, risk 1, blocks phase 2.
- **STM32 firmware version on the new board** — unknown until it is read. May
  differ from the Pi 3's board.
- **Whether the capture stick prefers a USB2 or USB3 port** on the Pi 5. It is
  a no-name `0001:ff02` and cheap capture dongles are occasionally happier on
  USB2. Worth trying both before concluding anything about frame rates.
- **Whether anything besides the OLED uses `/dev/i2c-2`.**
- **`snd_bcm2835` cmdline parameters** (`enable_headphones`, `enable_hdmi`) are
  Pi-3-specific and should not be carried forward blindly.
