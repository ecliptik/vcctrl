# vcctrl -- findings from building it

Things learned by measurement, not by reading. Each one changed the code.

## 1. USB4VC ingests uinput devices with no modification  [measured]

Its scanner uses `evdev.list_devices()` (`usb4vc_usb_scan.py:894`), which
enumerates every `/dev/input/event*` node -- not just USB ones. The
`input_device_path = '/dev/input/by-path/'` constant at line 41 is vestigial and
unused by the scan.

A uinput device is therefore indistinguishable from a real USB keyboard.
Confirmed: USB4VC logged `opened device: 0x1209 0xdea1 vcctrl virtual keyboard`
within its 0.75 s rescan.

Constraints extracted from the source, all load-bearing:

| constraint | source |
|---|---|
| name must not contain "motion" | `:896` |
| keyboard needs `KEY_ENTER` + `KEY_Y` | `:913` |
| mouse needs `BTN_LEFT` + `EV_REL` | `:911` |
| must not declare gamepad buttons | `:877` `check_is_gamepad` |
| devices must stay open (0.75 s poll) | `:946` |
| one event per device per loop pass | `:772`, 5 ms idle sleep at `:766` |

`BTN_LEFT`/`BTN_RIGHT`/`BTN_MIDDLE` are **not** in
`gamepad_event_code_name_list`, so the mouse device cannot be misclassified.

## 2. The virtual keyboard is also a keyboard to the Pi  [measured, the hard way]

`vcctrl combo ctrl alt delete` **rebooted the Raspberry Pi.**

Obvious in hindsight and easy to miss: uinput devices are real input devices to
the local kernel, so systemd's `ctrl-alt-del.target` fired on the very chord
that was meant for the DOS box. The Pi went down mid-test.

Fix, now in `pi/install.sh`:

    sudo systemctl mask ctrl-alt-del.target

Verified after masking: the same command emits `KEY_LEFTCTRL KEY_LEFTALT
KEY_DELETE` and the Pi stays up.

**Generalisation worth holding onto:** every keystroke the harness sends to the
DOS box is also delivered to the Pi's own input stack. Ctrl-Alt-Del is the only
one that currently does damage, but anything else the Pi's console or a future
desktop session binds would fire too. If the Pi ever runs a GUI, revisit this.

## 3. PS/2 has a return channel, and nothing was reading it  [Pi side measured]

PS/2 is bidirectional. When lock-key state changes, the host BIOS sends `0xED`
plus an LED bitmask back down the wire. USB4VC already relays it:
`SPI_MISO_MSG_TYPE_KB_LED_REQUEST = 129`, handled by `change_kb_led()` at
`usb4vc_usb_scan.py:694`, which writes into `/sys/class/leds`.

A uinput device declaring `EV_LED` gets its own kernel-created nodes --
`input12::capslock` and friends -- and `change_kb_led()` writes to every
`*capslock*` node it finds, ours included.

This is a **non-video, non-network witness that the DOS machine received and
processed a keystroke**, which breaks the phase 1 circularity: otherwise the
only way to check input is to read the screen, and the screen needs capture to
be working.

Still unverified: the DOS half. Standard AT/PS-2 BIOS behaviour, but not
measured on this box.

**Never send lock keys mid-sweep.** They are keystrokes and would inject into a
running cell; and once the game runs, SDL3's DOS backend owns the keyboard and
its lock-key semantics are undefined.

## 4. Argument quoting survives the ssh hop  [measured]

`vcctrl type 'CD \DOSKUTSU'` from the VM produced exactly
`SHIFT+C SHIFT+D SPACE BACKSLASH SHIFT+D SHIFT+O SHIFT+S SHIFT+K SHIFT+U
SHIFT+T SHIFT+S SHIFT+U` on the virtual device.

DOS paths are full of backslashes and colons, so this was the failure most
likely to appear only in production. `bin/vcctrl` re-quotes each argument with
`printf '%q'` before handing it to ssh; without that the backslash is eaten.

## 5. The `claude` user cannot read input devices

Not in the `input` group, so `evdev.InputDevice()` raises PermissionError.
Debugging tools that read `/dev/input/event*` need `sudo`. The daemon runs as
root anyway (uinput requires it).

## 6. USB4VC is already current -- do not update  [audited 2026-08-19]

| | version | evidence |
|---|---|---|
| installed | rpi_app **0.3.3** | `RPI_APP_VERSION_TUPLE` at `usb4vc_shared.py:63` |
| latest upstream release | **0.3.3** | GitHub releases API; highest tag in the repo |
| master HEAD | still `(0,3,3)` | commit `2e21505`; version tuple never bumped |

The installed tree is **byte-identical to the official 0.3.3 release zip** across
all 14 `.py` files -- zero local modifications. Our integration therefore relies
on stock upstream behaviour, not on anything hand-patched here.

Master has drifted ~40 commits past the tag but is untagged, unreleased and
unversioned. Its only real feature is keyboard remapping, which is **inert on
AT/PS2**: `PROTOCOL_AT_PS2_KB` has no `'mapping'` key, so the remap returns the
source code unchanged.

**All seven integration points from finding 1 are unchanged in master**,
verified by extracting each function body from both trees and diffing after
CRLF normalisation -- including `evdev.list_devices()` still unfiltered, the
`by-path` constant still vestigial, `check_is_gamepad`'s 91-entry list still
free of `BTN_LEFT`/`BTN_RIGHT`/`BTN_MIDDLE`, and `change_kb_led()` /
`SPI_MISO_MSG_TYPE_KB_LED_REQUEST = 129` byte-identical.

Protocol board firmware is **0.5.7**, which is the newest released PBID1 build.
Nothing to flash. The board self-reports this via SPI INFO, decoded at
`usb4vc_ui.py:861`.

### Two standing precautions

- **Never press Update in the USB4VC OLED UI.** `usb4vc_check_update.py:85`
  only aborts when remote < local. Remote *equals* local, so it proceeds:
  `rm -rfv /home/pi/usb4vc/rpi_app/*` followed by re-copying the same 0.3.3.
  No gain, and it deletes anything else living in that directory.
- **Keep `vcctrl` out of `/home/pi/usb4vc/rpi_app/`.** We install to
  `/opt/vcctrl` and `/usr/local/bin/vcctrl` precisely so a stray Update cannot
  remove us.

### If a future tagged release appears

The re-check is cheap and targeted -- only two things are load-bearing:

1. Does `get_input_devices()` still call `evdev.list_devices()` unfiltered?
2. Do `change_kb_led()` and `SPI_MISO_MSG_TYPE_KB_LED_REQUEST` survive?

Everything else we depend on is either cosmetic or would fail loudly.

## 7. Ctrl-Alt-Del works at the prompt, NOT in-game  [measured]

Sent with DOSKUTSU on its title screen: no reboot. Title screen still up 60 s
later, cursor still responding to arrow keys, and the operator confirmed the PC
speaker POST beep never sounded. Input was alive; the chord was swallowed --
SDL3's DOS backend hooks INT 09h and owns the keyboard.

Sent from the DOS prompt: reboot in 9 s, twice, with the beep heard.

**Consequence:** the escalation ladder had no working recovery for a hung cell,
which is the one case it exists for. Hardware reset (GPIO to the motherboard
reset header, via opto-isolator) becomes load-bearing rather than optional.

### Detecting a reboot via the LED channel

NumLock is a poor reboot detector: POST sets it to 1, and if it was already 1
the transient is invisible to a 2 s poll. **Arm Caps Lock instead** -- POST
clears it, so `caps 1 -> 0` is an unambiguous edge. Measured at t+9 s on both
reboots.

## 8. Boot-profile selection is possible but needs Enter  [measured]

The CONFIG.SYS menu is uncapturable (text mode 03h @70 Hz) and times out in 5 s
to `VIBRAUSB`, so selection is blind and timed.

- **Digit alone does not work.** Spamming `2` across a 56 s window left
  `CONFIG=VIBRAUSB`.
- **Digit + Enter works.** Same window, `2` then Enter every 2 s, produced
  `BLASTER=A220 I7 D3 P330 T3` -- the PGSB profile.

Verify afterwards with `SET`, never by assumption. Note MS-DOS 6.22 does not
expand `%var%` on the interactive command line (only in batch files), so
`ECHO %config%` prints the literal text and is not a valid check. `SET name`
without `=` is a syntax error in 6.22; plain `SET` and read the list.

`&` is not a command separator in MS-DOS 6.22 either -- send commands
individually.

## 9. Closed-loop menu navigation beats blind counting  [measured]

Driving the Cave Story menu by counting keypresses failed repeatedly: the menu
wraps, a greyed entry may or may not be skipped, and a 120 ms hold sometimes
advanced two rows. Measuring the cursor's Y position from a captured frame and
stepping until it reaches the target row worked first time, every time
(258 -> 282 -> 330 = Quit).

The general rule: **the harness has eyes, so it should look rather than count.**
Anything driven by a fixed number of keypresses is a latent bug.

## 10. Audio capture works -- and a wrong claim retracted

See PLAN.md sec. 4.2 for the full account. Summary: this document and the plan
both asserted, from the `0x0602` USB descriptor, that analog audio capture was
*structurally impossible*. It was not. The descriptor is a vendor firmware
label inherited from HDMI variants, not a description of the physical jack.
With the PGSB boot profile and the sound card's line-out cabled into the stick,
capture reads -30.8 dB mean against a -65.6 dB silence floor, spectrally tonal,
confirmed by ear as the title music.

Two independent faults had to be fixed together -- wrong boot profile (no DAC
present at all) and wrong cabling -- which is why changing one at a time kept
producing silence and appearing to confirm the false conclusion.

## 11. Audio levels are not reproducible -- a volume knob is in the path

The capture stick's audio input is fed from the **headphone output of the
powered speakers** (the speakers' line-out produced silence). That places a
variable analog gain stage upstream of every measurement.

**Never apply an absolute dB threshold.** A turned-down knob is indistinguishable
from a silent cell, and that false failure would not reproduce afterwards.
Compare only within a single capture, or between captures known to be minutes
apart with nothing touched.

Fix: tap the sound card line-out with a passive Y-splitter (fixed level,
speakers keep working). Failing that, play a known reference at session start
and normalise against it.

## 12. Mains power control via Kasa -- no wiring needed  [measured]

The g2k runs from a PicoRC (12 V brick -> picoPSU -> AT rails). A TP-Link Kasa
EP10 on the wall supersedes both the J31 reset-header plan and the PicoRC
switch-header relay: same recovery capability, no soldering, no GPIO.

**No library required.** The EP10 speaks the legacy TP-Link protocol on port
9999: a 4-byte big-endian length prefix plus an XOR-autokey cipher seeded at
171. About twenty lines of pure Python, entirely local, no cloud account. If a
firmware update ever moves it to KLAP on port 80, this breaks and needs
`python-kasa`.

    vcctrl power state|on|off|cycle [secs]

`cycle` is unconditional by design -- a wedged machine can report "on" while
being useless, so cycle means cycle rather than "on if off".

**The plug's Kasa alias is "retro-rig-plug" and is deliberately not renamed.**
Recorded in `/opt/vcctrl/config.json` because nobody looking at the Kasa app
would otherwise connect that name to a 1995 PC.

### The machine boots unaided from mains  [measured]

PicoRC is AT-style with no soft-off, so restoring mains is a complete power-on:

| event | time from `power on` |
|---|---|
| POST edge (Caps cleared, NumLock set) | **26 s** |
| DOS prompt | roughly 45 s total (operator: 15-20 s after POST) |

So `vcctrl power on` needs no button press, and the escalation ladder finally
has a working recovery step for a hung cell -- the case Ctrl-Alt-Del cannot
reach because SDL owns INT 09h (finding 7).

**Detecting the boot without video:** write `1` to the Caps Lock sysfs node
before powering on. POST clears Caps and sets NumLock, so the edge is
unambiguous and arrives ~20 s before the console is readable. Note the arming
must be a direct sysfs write, not a keystroke -- with the machine off there is
no host to process a keypress.

## 13. Mode 12h at boot makes the whole boot capturable  [measured]

Setting BIOS mode 12h early in AUTOEXEC, before the TSR loads, means every line
it prints is visible to the capture stick. Confirmed on the first cold boot:
CuteMouse, the PicoGUS firmware switch, MSCDEX, and the `[PGSB] ready.` line
all readable, where previously the console was in text mode 03h and invisible.

That `[%config%] ready.` line is also the provenance witness the g2k session
recommended -- the booted profile, printed by the machine itself, and now
machine-readable.

CONFIG.SYS output still is not capturable: it runs before AUTOEXEC, so the boot
menu and driver loading remain invisible. Fixing that would need a device
driver, and is almost certainly not worth it.

## 14. End-to-end recovery works, but boot time is not constant  [measured]

`vcctrl power cycle` -> POST edge -> prompt -> typed command echoed back, with
no human involvement. Total 90 s from command to a verified-live prompt.

**But POST timing varied significantly between the two boots:**

| boot | power applied to POST edge |
|---|---|
| cold start (plug had been off) | **26 s** |
| `power cycle` (6 s off) | **44 s** |

Same machine, same profile, 18 s apart. Most likely the 6 s outage is too short
to fully discharge the PicoRC's 12 V brick and picoPSU, so the board sees a
marginal rather than clean start. It could also be BIOS memory-test variation.

Consequences for the harness:

- **Lengthen the default off period** beyond 6 s. A supply that has not fully
  drained is the classic cause of a machine that comes back oddly or not at
  all, and this is the recovery path of last resort -- it needs to be the most
  reliable thing in the system, not the fastest.
- **Do not hard-code a readiness delay.** 45 s was right once and would have
  been 18 s short the second time. Until `RDYPULSE.COM` exists, readiness must
  be polled rather than assumed, with generous margin.

This is exactly the case the readiness pulse was designed for: the POST edge is
observable and early, the prompt is not observable at all, and the gap between
them is not a constant.

## 15. The autonomous sweep loop closes  [measured]

`vcctrl-sweep MINE 1` launched a real QA sweep unattended: preflight, QA tag,
DKTCAP, launch, banner capture, PAUSE keypress, and the sweep ran to completion
(`MINE DONE -- logs GMN and GMF`). Logs were then collected over the network and
carry real data -- `mode-tick-stat n=79 phase=mode_draw_scene`, answering the
question the sweep's own banner poses about whether patch 0312 took.

FTP throughput over the ODI stack measured at **~880 KB/s** (236 KB in 0.274 s,
296 KB in 0.333 s), so a 7.8 MB binary moves in roughly 9 seconds. The CF swap
is genuinely retired for iteration.

### Two mistakes in my own instrumentation, both worth keeping

**A glob that matched its own output.** `grab()` burst-captured to `sw*.jpg` and
then saved the selected frame as `sweep-poll.jpg` in the same directory --
which `sw*.jpg` also matches. The next poll picked its own previous output as
the "brightest frame" and tried to copy a file onto itself. Burst frames now use
a distinct `vcraw` prefix and are cleared each poll.

**Comparing the wrong frames.** A diagnostic reported an 11% pixel difference on
a screen that was provably static, which looked like capture noise defeating
change detection. It was not: it compared the *first and last raw frames of a
burst*, and the first frames of a burst are the flat-black settle frames. Against
a known-static prompt, comparing the *selected brightest* frame of each burst
gives **0.0% difference at every threshold from >8 to >48**.

So the change-detection threshold needed no adjustment; the measurement did. The
lesson generalises past this bug: when an instrument reports noise, check that it
is measuring the thing it claims to measure before tuning it to tolerate the
noise. Tuning would have masked a real signal.

### Collection is now wired, and NOT YET RUN ON HARDWARE

`bin/vcctrl-collect` reboots into NET, runs `PUT` per cell tag, and confirms
each file by watching the FTP server's `incoming/` directory. `vcctrl-sweep
--collect` hands off to it at the moment the sweep is proven complete.

**Written 2026-08-19 against a powered-off machine. Every primitive it uses is
measured -- the reboot edge (sec. 7), blind menu selection (sec. 8), RDYPULSE,
and `PUT` itself, which returned real logs by hand. The composition is not.**
First run should be watched.

Two choices in it that are not obvious:

**It reboots rather than loading the ODI stack at the prompt.** Loading at the
prompt would skip a 43 s reboot, and was the obvious optimisation. It is not
done because the stack has no proven prompt-load path and *no unload path at
all* -- so a machine that loaded it by hand could not be returned to a
measurement-clean state without the reboot being avoided. The saving was
illusory.

**Confirmation is a file appearing on this host, not a line read off the
screen.** The console is in mode 12h and `PUT`'s success line is capturable,
but reading it needs OCR, and an OCR misread would mark a missing log as
collected -- the one error mode that silently corrupts a result set. The FTP
server runs here, so arrival is directly observable, and that single check
transitively proves the reboot took, NET was selected, the ODI stack loaded,
the packet driver answered, and the log existed. Nothing else needs a check
because nothing else can be true if the file is there.

### Log tags come from the BATs, not from memory

`sweeps.json` now carries each sweep's cell tags. They were derived by grepping
`SET DOSKUTSU_LOG_TAG=%QAM%...` out of the r16 payload's BATs. Counting
`DOSKUTSU.EXE` lines to get cell counts does **not** work -- the BATs mention
the binary in comments, which inflates `TAB` to 4 cells and `DEEP` to 3.

## 16. The card can be updated over the wire  [measured 2026-08-19]

The CF was one payload behind (r15 vs r16). Rather than pull the card, the two
payloads were diffed: **the entire r15 -> r16 delta was nine BAT files, 67 KB
of text.** Binary, Organya caches, everything else byte-identical.

Pushed over FTP from a NET prompt in about four minutes, and every file pulled
straight back off the card with a small `CHK.BAT` and compared by sha256:
**10/10 byte-identical, CRLF intact.** No hardware touched.

The verification was not ceremony. Those files travel from a tarball, through a
Unix filesystem, through FTP, onto a FAT volume -- and a DOS batch file with LF
endings instead of CRLF fails in ways that read as a logic bug rather than a
formatting one. `binary` mode in `GET.BAT` is what preserves them, and that is
worth confirming rather than trusting.

The general shape: **check whether a payload difference is actually binary
before treating it as a hardware errand.** A 180 MB tarball whose real delta is
67 KB of text is a network job.

## 17. Three timing bugs, all the same bug  [measured 2026-08-19]

Before trusting `vcctrl-collect` on hardware, the LED channel was measured. It
was healthy -- `at_prompt()` returned true 4/4 -- but the measurement exposed
the thing that actually mattered:

**Every `vcctrl` call costs about 1.5 s.** It is an ssh round-trip to the Pi
(measured 1.52-2.55 s), and it dominates every loop in the harness. The PS/2
LED round-trip itself is comfortably under one poll interval; an apparent
"3.14 s LED latency" was two ssh calls' worth of polling overhead being
misread as device behaviour.

Three bugs followed from not accounting for it, and all three are the same
mistake -- a fixed sleep or a naive cadence where a closed loop belonged:

1. **The menu selection was a coin flip.** Digit and Enter were sent as
   separate calls with an LED poll between them: about 5 s per attempt, against
   a menu window that is exactly 5 s wide. Now one call (`key 5 enter`) with no
   polling during the window -- measured 1.76 s apart, ~2.8 attempts inside it.
2. **`arm_leds()` slept 0.5 s** after a toggle and then re-read. The re-read is
   itself an ssh round-trip, so it raced, and arming failed on a machine that
   had toggled correctly a moment later. Now it waits for the toggle.
3. **`wait_led` on a LEVEL is unsafe across a power cycle.** See below.

### The stale-LED trap

`/sys/class/leds` on the Pi retains the last state the host published, and a
host that is powered off publishes nothing. So after `vcctrl power on` the LEDs
still read whatever they read before the power was cut -- and if the machine's
last act was firing RDYPULSE, that is `scrolllock=1`.

A level check for "scrolllock is 1" therefore **returned ready 2.5 seconds
after power-on**, on a machine that had not begun to POST. Observed exactly
that. It is the worst class of bug: it returns the correct answer whenever the
previous run ended any way other than at a ready prompt, so it would survive
most tests anyone would think to write.

An edge pair is immune. POST clears the LEDs, so wait for `scrolllock -> 0`
(proving the reading is now this boot's) and only then for `-> 1`.

### The beeps are ours

Blanketing the menu window fills the BIOS 15-key buffer, and DOS beeps once per
rejected keystroke. The operator heard a burst of beeps after the POST beep and
asked about it. Harmless, stops when the window closes -- but on a 30-year-old
machine an unexplained beep burst reads as a fault, so it is worth expecting.
