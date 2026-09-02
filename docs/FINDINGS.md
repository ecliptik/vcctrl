# vcctrl -- findings from building it

**This file records what happened. For what is still broken and what to
check before trusting a result, see `OPEN-FAULTS.md`.**

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

> **CORRECTED 2026-08-24, read off the card rather than from either doc.**
> `C:\CONFIG.SYS` says `menudefault=PGSB,5` — **the default is `PGSB`, not
> `VIBRAUSB`**, and the block is named `VIBRA`. `OPEN-FAULTS` sec. 7 was right
> and this was wrong; two documents disagreed and the card settled it. The full
> menu, in order:
>
>     1 PGSB     PicoGUS Sound Blaster        <- menudefault, 5 s timeout
>     2 PGADLIB  PicoGUS AdLib
>     3 PGGUS    PicoGUS Ultrasound
>     4 VIBRA    Vibra16 + PicoGUS USB (CD on D:)
>     5 NET      Networking (ODI + mTCP) -- transfers only, NOT measured runs
>     6 CLEAN    Recovery boot -- no sound, no video init, no TSRs
>
> `NET` is item **5**, matching `NET_MENU_ITEM` in `harness/vcctrl-collect`.
> **`CONFIG.SYS` states the never-measure-in-NET rule in its own menu text**,
> which is the best possible place for it.

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

## 18. The caret does not escape anything in MS-DOS 6.22  [measured 2026-08-19]

`^` is a **cmd.exe** escape character. COMMAND.COM has no escape mechanism at
all, and the eight sweep BATs writing `-^>` to mean a literal arrow were never
correct -- everyone reading them, this session included, assumed the idiom
worked because it looked like the idiom that works on Windows.

Settled on the machine rather than argued:

    C:\>DEL C:\arrow
    File not found                       <- clean slate

    C:\>ECHO caret test -^> arrow

    C:\>DIR C:\arrow
    ARROW              15  08-19-26  11:32a

**The file was created.** 15 bytes is exactly `caret test -^` plus CRLF: the
caret went in as literal text and the `>` redirected regardless.

Consequence: any DOS batch reaching for `^` to escape `<`, `>` or `|` is
silently broken, and the failure is invisible because the redirect target is
usually a plausible-looking word from the middle of the message. The only
reliable fix is to **reword so no bare `<` or `>` exists outside a real
redirect**, which is what the analysis session did across 21 BATs.

### The redirect audit that found it needs to be by parse, not by pattern

Grepping for arrow shapes found four bad lines in `RB.BAT`. Parsing every line
for a redirect target found many more, including the one that mattered:

    CLRENV.BAT:4   REM (env > CFG by design). CALLed by every cell BAT ...

**COMMAND.COM parses redirection inside REM comments.** A REM produces no
output, so this creates an empty file and nothing else -- which is why the
stray `CFG` was 0 bytes while `ADLIB` was 64. The size was the clue that it
came from a REM rather than an ECHO. And because `CLRENV` is CALLed by every
cell of every sweep, a file named `CFG` has been created before every
measurement ever taken on this rig.

No arrow-shaped search would ever have found it. The general rule: **audit for
the effect, not for the syntax you expect to cause it.**

### A related DOS limit worth carrying

`DIR` shows one `ADLIB`, not the two the arrow audit predicted: DOS filenames
are case-insensitive 8.3, so `adlib` and `ADLIB` are the same file and the
later ECHO simply overwrote the earlier one. And COMMAND.COM truncates a
command line past **127 characters**, which nearly shipped a truncation bug
inside the fix for a truncation bug.

## 19. The harness DoSed its own control host  [diagnosed 2026-08-19]

The Pi became unreachable for ~30 minutes mid-campaign. The symptom set was
unusual and worth recognising again:

    ping                 fine, 0% loss, 7 ms
    TCP port 22          accepts the connection
    SSH banner           never sent
    the Pi               never rebooted; it recovered on its own

Kernel networking alive, userspace stalled. The cause, from `dmesg`:

    [12:48:03] systemd[1]: systemd-journald.service: Watchdog timeout (limit 3min)!
    [12:51:03] ... [12:54:04] ... [12:57:04] ... [13:00:04] ... [13:06:05]

**journald hung**, and systemd's watchdog fired every three minutes through
the entire window. Isolated earlier hits at 10:33, 10:36, 10:39, 11:22 and
12:27 show it had been degrading for hours first.

### Why a hung logger takes down remote access

sshd, PAM and systemd-logind all write to journald. When journald stops
draining its socket, **writers block**. So sshd completes the TCP handshake in
the kernel and then blocks trying to log the connection, before it can send a
banner. That is why the port answered and the session never started.

It is also why nothing could be diagnosed live: **the component that failed
was the component that records failures.** The first search for logs of the
event returned "no entries", which read as "nothing happened" and actually
meant "nothing could be written". Another instance of absence of signal not
being absence of output.

### The cause was this repo's own design

`bin/vcctrl` made a **fresh ssh connection per call**, and every ssh spawns a
full systemd user session -- dbus, pulseaudio, roughly twenty journal lines per
call. The harness makes a call per keystroke. Measured 676 journal entries in
one ten-minute window, written to an SD card, on a 920 MB Pi.

So the rig's control plane generated enough logging to wedge the logger, which
then blocked the control plane. Nothing was leaking and nothing was broken --
the design simply did not scale to the rate the automation drove it at.

### The fix, and it pays twice

ssh `ControlMaster` with `ControlPersist`: one authenticated connection reused
by every later call. Session setup happens once instead of thousands of times.

    before   1.52 - 2.55 s per call
    after    0.19 - 0.24 s per call, mean 0.21

**That 1.5 s figure is the same number behind four separate timing bugs in
sec. 17.** The instrumentation cost that produced them was mostly ssh session
setup, and it was avoidable the whole time. Timeouts tuned against the old
figure are now generous rather than wrong, which is the safe direction.

Also added `ConnectTimeout` and `ServerAlive*`, because the outage presented as
calls that never returned -- indistinguishable from a long-running cell, which
is why a wedged host went unnoticed for thirteen minutes.

### The generalisable part

**A monitoring channel that costs the monitored system real work is part of the
load.** The harness watched the Pi by connecting to it, and connecting was
expensive enough to be the fault. Worth checking wherever an observer shares
resources with the observed -- and it is the strongest argument yet for the
persistent-daemon architecture the web-KVM work is building, which removes the
per-call connection entirely rather than making it cheaper.


---

## 20. A correct signal, overruled by a belief  [diagnosed 2026-08-19]

The other failures in this document are proxies going wrong: a stale LED level
read as current state, a returned prompt read as a successful command, file
arrival read as readiness. Each is a reading that could not distinguish success
from failure.

This one is not that. **The reading was right, available for hours, and
discarded.**

### What was heard

The operator, from the next room, after every reboot:

> btw on reboots, sometimes I think there are too many keyboard buffers, there
> are 4 rapid beeps after the PNP boot beep

That is the BIOS type-ahead buffer overflowing. It holds 15 keystrokes and
beeps once per key rejected past that. The beep is not a symptom that needs
interpreting -- it is the buffer reporting its own overflow, in hardware,
correctly, every time.

A fix was made: blind menu selection dropped from 209 attempts to 12. The beeps
got quieter. That was reported as fixed.

Hours later, same operator:

> I still keep on hearing like 8 or so beeps whenever you reboot, I thought that
> was fixed?

### What was actually wrong

The budget was expressed in **attempts** while the thing that overflows counts
**keystrokes**. Every attempt is digit-then-Enter:

    vcctrl-collect     12 attempts  =  24 keys   into a 15-key buffer
    vcctrl_common      20 attempts  =  40 keys   into a 15-key buffer

A unit error, hiding inside a cap that looked conservative. The menu consumes
two. Of the remaining twenty-two, some overflow -- one beep each, which is what
was audible -- and the rest **sit in the buffer until COMMAND.COM next reads
input**, which may be minutes later:

    C:\>5C:\MTCP\PUT.BAT M64A
    Bad command or file name

That is a stray keystroke from the boot menu arriving inside a log transfer and
prefixing it. Keystrokes from one operation landing inside another, minutes
later. The transfer silently did nothing.

### Why the bad fix survived

The attempt count went down and the noise went down with it, so the metric that
was being watched improved. Nobody looked at the screen afterwards -- and the
screen showed eight `Bad command or file name` lines the entire time, one call
away, for hours.

**The beep had already answered the question and was overruled by a belief
about a fix.** That is worse than a proxy failing, because a proxy that cannot
distinguish success from failure at least never claimed to. Here the
distinguishing evidence existed, was correct, was reported by a human, and lost
an argument to a number that had improved.

### The rule

**A partial improvement in a metric is not evidence that the fault is gone.**
When someone reports that a symptom persists, the symptom outranks the fix.

And the narrower one, worth stating because it generalises past this rig:
**bound a resource in the units the resource is measured in.** The buffer holds
keys. Anything counted in attempts, rounds, or iterations is a proxy for keys
and will drift from it the moment the number of keys per attempt changes.

### What replaced it

- The bound is keystrokes (6, against a 15-key buffer), in
  `vcctrl_common.spam_menu` -- **one** implementation, because the loop existed
  twice with different constants, which is why the fix had to be found twice.
- `flush_input_line()` sends Esc before the first command after a reboot, so a
  survivor cannot prefix it.
- The reboot path **verifies the profile** by reading NET's `[NET] ready`
  banner off the screen. Pressing 5 and booting NET are different events, and a
  missed menu boots something else that also reaches a prompt.

That last check was unaffordable at 40 s per capture and costs 0.2 s through
the KVM daemon's frame ring. **Making a check cheap is what turns an assumption
into a verification** -- the second time in one day that the same trade paid
off, and the strongest practical argument for the daemon architecture in
sec. 19.


---

## 21. An offered framebuffer the engine declines  [RETRACTED -- see sec. 23]

**The central claim of this section is wrong.** There was no engine defect. A
half-written UniVBE configuration advertised a mode the card cannot produce, and
everything below is the engine faithfully using it. Kept unedited because the
reasoning is a worked example of how far a wrong root cause can be carried on
correct-looking evidence; the correction is sec. 23.

## 21 (as originally written). An offered framebuffer the engine declines

The Mach64 at 640x480 rendered **nothing** -- the DOS console stayed on screen,
untouched, for a whole 158 s cell, with three captures 45 s apart byte-identical.
Black on the physical monitor too, so not a capture fault.

### The controlled swap

Same card, same pin, same boot profile, same seed. The only variable is whether
`C:\UNIVBE\UNIVBE.EXE` exists on disk.

    cell   UniVBE   has_lfb  use_lfb  pitches_match  src_pitch  draws  fps
    M64A   absent      0        0          1            640      YES   26.9
    M64B   present     1        0          0            320      no    19.9
    M64D   present     1        0          0            320      no     --
    NUVB   absent      0        0          1            640      YES   26.8

### Defect 1: banked writes fail only when an LFB was available

With `has_lfb=0` the engine takes `banked_multibank` and it works. With
`has_lfb=1` it takes the *same* banked path and the writes never reach visible
VRAM. So the fault is not "banked is broken" -- it is **banked is broken when
an LFB was offered and declined**, which is a much narrower and stranger claim,
and it is why this survived every previous test: nothing had ever presented the
engine with an LFB it then refused.

It also costs 30%: 19.9 against a 26.8/26.9 pair.

### Defect 2: src_pitch is not re-derived after a mode change

UniVBE supplies a 320x240 mode (`0x01F8`), so mode-set #1 lands there and #2
moves to 640x480. The engine keeps `src_pitch=320` against `vram_pitch=640`.
Without UniVBE that mode does not exist, #1 goes straight to 640x480, and the
pitch is correct by construction.

**The pin is not the variable.** It was stood down in all four cells above. The
MODE LIST is the variable, and UniVBE determines the mode list. Every earlier
conclusion that framed this as a pin question -- including two of mine -- was
looking at the wrong lever.

### What this cost, and why

The rig spent an evening on this, and the delay was not the defect. It was that
**every check available said the machine was fine**:

  - the cell reported success and exited 0
  - the environment verified, all five variables read back
  - the log recorded both mode-sets, ending at 640x480
  - capture was locked, frames arriving, non-repeating
  - `grab()` reported PICTURE with a plausible brightness

Each of those answers a real question. None of them answers *"is there a game
on the screen"*, and the one that came closest -- duplicate-hash rejection --
called a uniform constant a picture, because it tests for repetition and a flat
frame need not repeat. See sec. 22.

The operator settled it in one sentence: **"I remember seeing the mach64 at
640x480 in the KVM playing, so it was working."** That is a memory of the
system in a working state, and it dated the regression to within a few hours
when no instrument on the rig could. Twice tonight the decisive evidence came
from the person in the room rather than from the harness -- the other being
eight beeps across a room (sec. 20).

### Operating state

`UNIVBE.EXE` is renamed to `UNIVBE.SAV`. AUTOEXEC line 36 is not `IF EXIST`
guarded, so it fails harmlessly and every other TSR still loads. Reverse with:

    REN C:\UNIVBE\UNIVBE.SAV UNIVBE.EXE

**26.8/26.9 is a valid 640x480 pair** -- both `pitches_match=1`, both drawing,
both provider-attested, spread 0.1. It is NOT comparable to the existing corpus,
which was measured on UniVBE with a linear framebuffer. New baseline or nothing.

### Unrelated, and now isolated

The **missing background tiles** are present in the NUVB frames -- no UniVBE,
no LFB, `pitches_match=1`. So that artifact is independent of both defects here
and needs its own investigation. It had been convenient to assume it was a
pitch symptom. It is not.


---

## 22. Zero variance is not a picture  [measured 2026-08-19]

`grab()` reported `PICTURE mean 7.0` for two frames taken twenty seconds apart
during a cell that was displaying nothing:

    md5 163305a5...   15011 bytes   extrema (7, 7)

Byte-identical, every pixel exactly 7. It passed because duplicate-hash
rejection asks **"is this frame a repeat?"** and a uniform frame can be a
singleton in the sampled window.

But a flat field is not a picture whether or not it repeats. The check answers
a narrower question than the one being asked -- the same fault as every other
entry in this document, appearing inside the detector all the others are judged
with.

**The fix is an independent test, not a better threshold.** A real capture of
any scene, however dark, has a spread of values: analog sampling noise alone
guarantees it. Equal extrema means the hardware is emitting a constant.

Measured by the web-KVM session at their 1/8 decode scale: real captures span
77-226 even when almost entirely black (one at mean 0.10); a constant is exactly
0. A floor of 4 sits far below any real frame and far above a blank.

### The constant is one constant

Four independent confirmations, hours apart, for unrelated causes -- two
in-game frames, a `MODE03` test, a `lastgood` off the NET profile -- all
byte-identical at 15011 bytes.

So the stick emits **one** constant whenever it cannot lock, and that constant
does not encode the reason. `frozen` is honestly one state, not two: no video
diagnostic can ever distinguish "the cable is out" from "the mode is
unlockable", and the LED channel does not separate them either, being up in
both. The distinguishing evidence has to come from the boot profile or a
`MODESET` line.

**This retracted a result already passed to another session**, who were about
to special-case a "brief gap at cell launch" in the KVM overlay -- writing a
real fault out of the interface on the strength of a false frame.


---

## 23. The configurator that was never finished  [measured 2026-08-19]

Six hours of investigation, three retracted theories, two falsely reported
engine defects, and a rig left unusable. One cause, and it printed itself on
the screen the moment the procedure was run properly:

> **Note that the ATI Mach64-CT and Mach64-ET based boards do not support
> double scanning, so all 320x200 and 320x240 resolution modes are not
> available.**  -- SciTech UniVBE 6.70, on this card

### What happened

`vcctrl-uvconfig` ran `UVCONFIG.EXE` at 14:47, then declined to press keys into
a screen it could not read. Refusing to drive blind was correct. **Writing
first and refusing second was not.** `UVCONFIG` writes `UNIVBE.DRV` and
`UVCONFIG.DAT` as it runs, so the files had already changed by the time
anything could decide to stop.

The result advertised `0x01F8` 320x240 on a board that physically cannot
double-scan. The game asks for 320x240, closest-match returned the mode that
did not exist, and from there:

    mode-set #1 -> 0x01F8 320x240 (impossible)   src_pitch latched at 320
    mode-set #3 -> 0x0101 640x480                vram_pitch 640
    pitches_match=0, LFB declined, nothing drawn

Every one of those is correct behaviour given a false mode table.

### Three checks that agreed, and were all wrong

- `at_prompt()` said the configurator had finished. It is a BIOS Caps Lock
  probe and returns True while a program sits waiting (sec. 20). The check that
  cannot see DOS was used to certify a DOS program had completed.
- File size and timestamp on `UNIVBE.DRV` looked correct throughout, before and
  after a rollback. **A config file is not the configuration.** The mode list
  the running system offers is, and it is only visible in a cell log.
- The cell reported success, the environment verified, capture locked, the log
  recorded both mode-sets. None of them asks whether anything was drawn.

### The unfixable part

`UVCONFIG` writes to the text buffer at `0xB800`. In mode 12h -- the mode the
capture stick requires -- those writes land in memory that is not displayed, so
the menus are invisible to the harness **and** to anyone at the monitor. In text
mode 03h they are visible on the monitor but the stick cannot lock 70Hz.

**There is no video mode in which the harness and the configurator can both see
the screen.** So the answer was never a braver tool. Interactive configurators
are operator work on this rig, permanently.

### The rule

**Refuse to write anything unless the whole procedure can complete.** Not "be
more cautious" -- the tool was already cautious, in the wrong half. A machine
left neither configured nor untouched is worse than one left alone, and the
guard belongs before the first write rather than before the first keystroke.

### What was measured once it was configured properly

    per_loop_fps  28.3
    oem_string    'Universal VESA VBE 6.70'
    0x01F8        no matches            (the card cannot double-scan)
    LFB-decision  use_lfb=1             taken unaided, no FORCE_LFB needed
    FB-INIT       pitches_match=0  src_pitch=512  vram_pitch=640

So **defect 1 is retracted and defect 2 is real.** `src_pitch` is genuinely not
re-derived when a later mode-set changes VRAM dimensions -- mode-set #1 lands on
512x384 and #3 moves to 640x480, and the engine keeps composing 512 wide. That
is why 28.3 matches the 512x384 control pair exactly: it is a 512x384 workload
with a 640x480 presentation, and it is **not** a 640x480 measurement.

### What actually resolved it

Not the harness. The operator: *"I don't get why you couldn't just do the 'we
know there was a video card swap, so we'll run uvconfig, reboot, and things are
all good' like we've done dozens of times before."*

That is the third time in one session the decisive input came from the person in
the room -- after eight beeps heard across a room (sec. 20) and a memory of the
card working earlier (sec. 21). Each time the harness had evidence that looked
sufficient and was not, and each time the human had context the instruments
could not hold: what the procedure normally is, what the machine normally sounds
like, what it looked like an hour ago.


---

## 24. The harness cannot see its own cable  [measured 2026-08-19]

With the USB4VC PS/2 lead **physically unplugged from the target**, the harness
reported everything healthy:

    usb4vc:  {'vcctrl virtual keyboard': True, 'vcctrl virtual mouse': True}
    caps:    input ok, leds ok, power ok, video ok
    leds:    {'capslock': 1, 'numlock': 0, 'scrolllock': 1}

Every check green. None of them can detect that the cable is out.

The reason is structural rather than a missing test. Those devices are `uinput`
nodes **on the Pi**; they exist whether or not the STM32 is connected to
anything, and the LED values are the last ones published, so they read plausible
rather than absent. **The harness reports on its own side of a cable whose far
end it cannot see.**

Same family as the physical-layer limit: every instrument on this rig reports
software state, so anything upstream of that -- a half-seated card, an unplugged
lead, a marginal edge connector -- is invisible while all the greens stay green.

**The only honest test of the input path is a round trip**: type something and
confirm it appeared, which is what `type_command()` does (sec. 20). Anything
that asks the Pi about the Pi will answer yes.


---

## 25. The POST chirp  [RESOLVED by the operator -- see the end]

A PC-speaker chirp during POST appeared "new" tonight and looked like evidence
of damage, arriving alongside a NIC that had dropped off the PCI bus. Chased
properly, by the operator, with three controlled boots:

    cold boot,   USB4VC connected, harness talking     no chirp
    warm reboot, USB4VC connected, harness talking     CHIRP
    warm reboot, USB4VC connected, harness SILENT      CHIRP
    warm reboot, USB4VC UNPLUGGED ENTIRELY             CHIRP

So: **warm reboot only, nothing to do with the harness or its PS/2 emulation.**
The operator's own hypothesis -- that it was the harness pulsing Caps/Num/Scroll
Lock -- was ruled out by a silence test rather than argued about, which is what
made the result trustworthy.

Conclusion: the machine has almost certainly always chirped on a warm reboot.
It became audible because **the rig changed the workload**. Before the harness,
warm reboots were occasional; a sweep does dozens in an evening. A rare event at
a new rate reads as a new event.

### Worth generalising

**Automation changes what is normal, and the baseline nobody wrote down is the
operator's ear.** Two symptoms tonight were reported by hearing before any
instrument had them -- this one, and the keyboard-buffer overflow of sec. 20.
The difference is that the buffer overflow was real and this was not, and
neither could be told apart from the other without a controlled test.

So the rule is not "trust the operator's report" or "distrust it" -- it is that
a report from the room is a genuine independent channel, and the way to use it
is to design the experiment that separates the cases. Both times, the
experiment was cheap and the speculation was expensive.


### CORRECTION, same evening: the conclusion above is not established

The operator then pressed the **hardware reset button** -- bypassing
Ctrl-Alt-Del entirely -- and heard the chirp again. And he is confident it is
new as of today.

    cold boot                                        no chirp
    warm reboot, Ctrl-Alt-Del, harness talking       CHIRP
    warm reboot, Ctrl-Alt-Del, harness silent        CHIRP
    warm reboot, Ctrl-Alt-Del, USB4VC unplugged      CHIRP
    warm start,  HARDWARE RESET BUTTON               CHIRP

So it is **any warm start**, not the key combination -- which is a better
characterisation than before. But "the machine always did this and automation
made it audible" was **my inference from the change in rate**, not a measured
fact, and it is contradicted by the person who has listened to this machine for
far longer than the rig has existed.

Retracting it, and noting why it was attractive: it explained the observation,
required nothing to be wrong, and arrived the moment I had a story that fit.
That is the same failure as the three NIC mechanisms (sec. 23) -- **a plausible
cause proposed before anything constrained the search.** I wrote the rule about
the operator's report being an independent channel in this very section, and
then used my own reasoning to overrule it two paragraphs later.

**What actually distinguishes cold from warm here:** a cold boot resets PnP ISA
cards and re-runs full initialisation; a warm start retains configured state and
skips it. The Vibra16S is a PnP ISA card, it was fitted today, and the operator
notes the only previous beep was "the PnP initialisation before the boot menu" --
so the new tone is adjacent to a subsystem that changed today.

**The test is one variable and the card is coming out anyway** (see
SOUND-PROFILES): remove the Vibra16S, warm-start, listen. Not yet run.


### RESOLVED: the chirp is the PCI NIC, on any warm start

The operator isolated it in one move — pull the Intel PRO/100 and warm-start:

    NIC INSTALLED    Ctrl-Alt-Del  ->  CHIRP     reset button  ->  CHIRP
    NIC REMOVED      Ctrl-Alt-Del  ->  none      reset button  ->  none

One variable, both warm-start paths, both directions. **The chirp is the NIC.**

And it explains the novelty completely, which my "you only just started noticing
it" story never did: **the NIC is not normally fitted.** It goes in for transfers
and comes out again, so there had never been a reason to hear it. The tone is
benign — the adapter's boot-agent option ROM re-entered on a warm start, where a
cold boot initialises it as part of the full POST sequence.

### The scoreboard for this one symptom

Three explanations were offered before the right one, all mine, all plausible,
all wrong:

1. the harness pulsing Caps/Num/Scroll Lock  (killed by a silence test)
2. USB4VC's PS/2 emulation                   (killed by unplugging it)
3. "it always did this, automation made it audible"  (killed by the reset button)

Every one was proposed before anything constrained the search, and each looked
sufficient at the time. The operator's method beat all three and it was not
cleverer — it was **remove one thing and listen**. He also supplied the
discriminating observation for free: *"the NIC isn't usually in the system"*,
which is baseline knowledge no instrument on this rig holds and none of my
reasoning could have reconstructed.

### What to do with it

Nothing. It is expected behaviour whenever the NIC is fitted, which under the
new standing configuration is during transfers. **Do not investigate it again**,
and do not read it as a symptom during a collect — that is exactly the window
where it will be heard and exactly the window where it means nothing.

---

## 26. The first Mach64 sweep, and what it is not a measurement of

Collected 2026-08-19 22:07 as files (`vcctrl-collect RB 1`), not read off the
screen. Ten artifacts, every `STOR` `completed=1` in the FTP server log.

### What card produced them

`GRB.NFO` says `video_declared=S3 ViRGE`. That is wrong, and it is wrong the
way `vcctrl-cardid`'s docstring predicted: RB.BAT hardcodes the string, so it
keeps asserting ViRGE after a Mach64 goes in. `vcctrl-cardid` scores all four
SDL logs **ATI Mach64, 4/4 signature points**, on `total_vram=2048 KB` -- the
field that reads hardware through UniVBE's shim.

Note for the next person, because it nearly caught me again: these logs carry
`lfb=0x78000000`, which appears in cardid's *ViRGE* signature table. It is
worth one point and it is shared. The VRAM size is the discriminator. Reading
one field by eye is what produced the ViRGE card-swap scare earlier the same
day; running the tool is what avoided repeating it.

### The numbers are not 640x480 numbers

All four cells:

    src_pitch=512  vram_pitch=640  pitches_match=0

Defect 2, confirmed on real hardware at the mode the operator requires. The
mode-set to 640x480 lands, `vram_w=640 vram_h=480`, and the source pitch is
never re-derived -- so what is measured is a 512-wide workload presented into
a 640-wide framebuffer. The *comparisons below survive this*, because all four
cells carry the identical defect. The *absolute figures do not*. Nothing here
may be banked as "doskutsu at 640x480".

### `per_loop_fps` is not the frame rate -- but it is not corrupt either

`GR3` reports `per_loop_fps=114.9`, with `overhead_s=484` of `dur=590s`, while
its own per-stage table reads 21.6-32.3.

I first wrote this up as an artifact that "means nothing". **The benchmarking
session corrected that and the correction is the useful part.** `per_loop_fps`
is approximately `flips / (dur - overhead_s)` -- the rate *as if overhead were
free*. It answers a hypothetical, and it answers it correctly:

    cell  flips   dur  ovh  loop  fps_mean  per_loop  factor
    GR4    2853   131   24   107      22.6      27.7    1.23
    GR4B   2857   130   24   106      22.6      27.7    1.23
    GR5    3134   122   16   106      26.5      30.4    1.15
    GR3   11816   590  484   106      20.1     114.9    5.72

`per_loop / fps_mean` tracks `dur / (dur - ovh)` to within 3% in every cell,
and GR3 hit the same `auto-exit at tick 5140` as the others. The reel
completed. Flips accrue *during* overhead while overhead is subtracted from
the denominator, so a cell that spends 82% of its wall-clock loading reports a
spectacular number by construction.

And the table carries a signal I had missed entirely: **the loop denominator
is ~106 s in all four cells.** GR3 is not a broken cell, it is the same 106 s
of work with 484 s of loading in front of it.

Two different errors, worth keeping apart. Mine was calling arithmetic corrupt
because its output was implausible -- discarding a cell rather than
understanding a field. The one that was actually available to be made is
quoting 114.9 as a frame rate. Rules recorded downstream: cross-check the
ratio against `dur/(dur-ovh)`; treat per-loop as inadmissible above ~25%
overhead; never quote a per-loop figure without its overhead beside it.

Two stages report exactly `50.0` in every cell -- the TAS 50 Hz ceiling, not a
result. Both are dropped from the comparisons below.

### The repeat pair is the useful part

`R4` and `R4B` are configurationally IDENTICAL -- a diff over every hint and
every ENGAGED/DISABLED line is empty. So `R4B` is not a lever, it is a repeat,
and it gives the noise floor that makes every other comparison readable:

    comparison              mean d      range        stages faster
    opl3 vs opl3 (REPEAT)    +0.13   -0.20..+0.70        5/10
    opl3 vs organya          -0.03   -2.30..+1.50        6/10
    opl3 vs adlib            +3.19   +2.00..+7.00       10/10

Organya sits inside the repeat pair's own scatter with mixed signs: no
difference, and any single-stage comparison that claimed one would have been
reading noise.

AdLib is faster on **every stage**, and its smallest win (+2.00) is about
three times the repeat pair's worst-case scatter. That is a real effect. It is
also cheap to test now that the Vibra16S is out of the standing configuration.

The general point: a paired stage-by-stage comparison against a measured
repeat pair answers a question that comparing two aggregate numbers cannot.
Without `R4B` in this sweep, `organya -0.03` and `adlib +3.19` would both have
been single numbers with no scale to judge them on.

### A transient reported as a result

The collector printed `GR4SDL.LOG  0 bytes` and I flagged the cell as dead.
The file is 42223 bytes and the server logged `completed=1 bytes=42223
seconds=0.095`. The arrival poll runs every 2 s against transfers lasting
0.1-0.5 s, so it caught the file between creation and fill. The size was real;
it was a sample of a file mid-write.

`arrived()` now rejects zero-length as "still arriving" and confirms the size
has stopped changing before reporting it. The failure mode is worth naming
separately from the fix: a measurement taken at the wrong instant is not a
wrong measurement, which is what makes it so easy to publish.

The zero-byte guard added to `inspect_logs()` at the same time is still
correct and stays -- `GC0B` and `GP1` really are 0 bytes, and an empty file
passes a `[critical]`-line scan precisely because it has no lines.

### The last hardcoded lie in the chain

`RB.BAT` printed `video_declared=S3 ViRGE` unconditionally, and kept printing
it after a Mach64 went in. Both peer sessions flagged it independently as the
thing to fix before anyone reads these numbers cold.

The irony is in the file. Eight lines above the offending `ECHO`, a comment
explains that `config=%config%` proves provenance *precisely because it is
chosen at the boot menu rather than asserted* -- "a log PROVES its own
provenance rather than attesting to the absence of something". Then the next
line but one asserts the video card.

It now reads `%QAVID%`, defaulting to `UNDECLARED`. An honest gap sends the
reader to `vcctrl-cardid`; a confident wrong string sends them nowhere,
because it does not look like a question.

Patched in `~/doskutsu-netiter/stage/RB.BAT`. **Not yet deployed to the CF
card** -- that needs the target, which is powered down. The four logs already
collected still carry the false declaration, and §26 above is the record that
they do.

### Two copies of the same fix, and neither of us was holding the other's

The benchmarking session reported that `RB.BAT` emitted `video_declared=`
twice -- an empty parameterised line and the hardcoded ViRGE one -- and that
the four Mach64 logs therefore carry the field twice with contradictory
values. Generously, that "you quoted the second" was a reasonable read.

Checked against the artifact rather than accepted:

    incoming/GRB.NFO          video_declared lines: 1
    stage/RB.BAT (pre-patch)  video_declared lines: 1

`GRB.NFO` is what the g2k itself wrote and FTP'd back. **One line.** The
collected logs are not ambiguous; they are simply wrong, and the value I
quoted was the only one present. Worth declining the absolution, because
"the artifact was ambiguous" and "the artifact was wrong" are different facts
about the same four logs.

What it actually shows is that the commit adding the parameterised line never
reached this staging tree or the CF card -- and, in the other direction, that
their resync never reached `~/doskutsu-netiter/stage/RB.BAT`, which still
holds the patch written here at 22:15. **Two divergent copies of one file,
each of us reasoning confidently about the one we held.**

Which is [[a-config-file-is-not-the-configuration]] arrived at from the
opposite side. That note was written after verifying a configuration from a
file's size instead of from the running system's mode list. Here the file was
read correctly and the error was assuming it was the only file. Same root:
the artifact on disk in front of you is evidence about that artifact, and
about nothing else, until something ties it to what actually ran.

Nothing deploys to the CF card until the trees are reconciled. Deployment is
the step where one copy silently wins.

---

## 27. Two rules that came out of one bad line

`MTCP/CHK.BAT` line 16 read `ECHO CHK done: %1 -^> incoming\%2`. Fixed in
`g2k@9d001c5`. Both rules below are general and neither is about DOS.

### Documenting a bug can reproduce it

There is no escape character in MS-DOS 6.22 -- the caret is a caret and the
`>` redirects. That was established by hardware test in the morning and
reasserted by me the same evening as settled background, in a sentence that
was not the topic of the message.

The consequence: the line printed truncated and tried to CREATE
`incoming\<name>` under the cwd. From `C:\DOSKUTSU` there is no `incoming\`,
which is a file creation error -- the one the operator had reported and I had
written off as unanswerable while the capture path was dark. It was answerable
from a file already on this disk.

Then the fix reproduced the defect. `REM` is an internal command and
COMMAND.COM parses redirection **before** dispatch, so a `>` inside a comment
is live. The first patch quoted the broken line in a `REM` to explain it, which
would have created the files the patch existed to stop creating.

**The safest-feeling action carried the defect.** Writing a comment to warn the
next person is what a careful author does. Nothing in "redirection is parsed
inside REM" makes that consequence visible until you have walked into it, and
it was caught on re-read rather than by reasoning -- which means the defence is
the re-read, not the understanding.

### A check that can fabricate an answer is worse than no check

`CHK done`, `PUT done` and `GET done` were all echoed after `FTP.EXE` returned
and conditioned on nothing, so they announced success on transfers that moved
nothing. The obvious repair is `IF ERRORLEVEL 1`.

It was declined. It is unverified whether mTCP's `FTP.EXE` sets an errorlevel,
and **if it does not, `IF ERRORLEVEL 1` reads the previous command's value**.
That is not a check that fails; it is a check that reports confidently on a
different operation while wearing the right label. Strictly worse than the
unconditional message it replaces, because it looks like rigour.

All three now state the attempt and name the witness -- the file arriving on
the server. A sentence that cannot be false beats a status whose provenance is
unverified.

### The scoreboard on this one

The four files carrying the caret were reported *only* because their divergence
had the wrong shape. The content assessment attached to that report -- "cosmetic,
nothing to fix" -- was wrong. Had reporting been conditional on my judgement of
importance, four sweeps would have deployed broken, because the judgement was
the part that failed.

And the divergence itself recurred within twenty minutes of my diagnosing it: I
patched `~/doskutsu-netiter/stage` without checking that `ecliptik/g2k` tracks
those files. Naming a failure shape does not confer immunity to it. What caught
it was checking rather than assuming, which is a habit -- and habits work when
understanding does not.

---

## 28. The control path vanished and every instrument said healthy  [measured 2026-08-20; MECHANISM CORRECTED, see the end]

Mid-session, `ssh <rig>` stopped answering for about three minutes. Not slow
-- a bare TCP connect to port 22 completed and then sat there with no banner.
Ping was fine. `https://vcctrl-pi.example.ts.net/state.json` was fine, and
reported video capturing at 1.08M frames with a 26 ms frame age, audio
capturing, no errors, lock free. Every reading available said the rig was
healthy, and the rig *was* healthy. What had gone was the path used to drive
it.

### The mechanism

`systemd-journald` had been SIGABRTed on `Watchdog timeout (limit 3min)!`
**122 times in 34 hours** -- roughly one every three to six minutes. While it
is wedged, anything that logs blocks, and sshd logs every connection before it
gets as far as a banner. The kernel completes the handshake into the accept
queue on sshd's behalf, so the client sees a connection that opens and then
nothing. `vcctrld` was untouched because it was not logging: a quiet process
is immune, a chatty one is not.

### It feeds itself

A journald killed uncleanly leaves its open file behind renamed `*.journal~`.
At 122 kills the directory held **101 files, 824 MB**, most of them those
corpses. More files to scan is more startup work is more chance of missing the
next three-minute deadline. The kills manufacture the condition that causes the
kills.

It was not disk space -- `/` had 21 GB free. It was not memory either: 371 MB
used of 920 with 549 MB available, and the first hypothesis in the room
(memory pressure, from misreading `free` as `available`) was wrong and was
contradicted by the very next measurement. The cost is CPU and deadline, on a
1.2 GHz A53 already at load 3.5 from continuous USB video and audio capture on
a Pi 3's single shared USB 2.0 bus.

Capped at `SystemMaxUse=100M`, `SystemMaxFileSize=16M`, `SystemMaxFiles=12`
and vacuumed: 824M -> 96M, 101 files -> 12.

### Why this one is worth a section

Every existing check in this harness answers a question about the *target*.
None of them answers "can I still drive it?", and the failure was invisible to
all of them precisely because the daemon and the web path were unaffected. The
KVM in a browser looked perfect throughout.

**The dangerous version of this is not the three minutes of no ssh.** It is a
150 s measurement cell, which is not resumable, driven entirely over the CLI
path, silently losing that path partway through while the web view keeps
showing a live picture. `pi/deploy.sh` already refuses to deploy into a running
cell because a restart destroys it; this is the same destruction arriving with
no actor to refuse.

### The generalisable part

An instrument that reports on the target cannot report on itself. Three
observations agreed the system was fine -- ping, `/state.json`, the live MJPEG
stream -- and all three were true and all three were about something other
than the thing that had broken. The reading that mattered was the one nobody
was taking, and the only reason it got taken is that a command hung rather
than returning a wrong answer.

Related: sec. 24, the harness cannot see its own cable.

### CORRECTION, two hours later: the mechanism above is WRONG

Everything in this section about *what journald was doing* is retracted. The
symptom, the blast radius and the generalisable part all stand. The cause does
not.

The claim was that 824 MB across 101 files made journald too slow to meet its
deadline. **One number in the kill line refutes it:**

    systemd-journald.service: Consumed 2.704s CPU time.

2.7 seconds across a 45-minute lifetime. Something grinding through a file
pile burns CPU. This was **blocked, not busy**, and the file-pile story never
explained that -- it was assembled from a plausible-looking correlation (a big
journal was present, and a big journal is a known problem) and never tested
against the one figure that was sitting in the same log line.

Capping the journal to 100M/12 files was real disk hygiene and changed nothing:
the kills continued at the same ~3 minute cadence, which is the watchdog limit
itself, because a restarted instance immediately re-enters the stall and dies
at its first deadline.

**Second wrong mechanism.** The next candidate was ssh session churn -- which
is documented in the header of `bin/vcctrl` as the cause of the 2026-08-19
outage, and is correct *for that outage*: every plain ssh spawns a full systemd
user session, ~20 journal lines per call, 676 entries in ten minutes. It does
not describe this one. Measured with the churn eliminated: **zero new login
sessions in 15 minutes, 23 journal entries in 5 minutes, kills continuing.**

Worth recording that the harness was never the churner. `bin/vcctrl` has passed
ControlMaster since that fix. What churned was ad-hoc `ssh <rig>` from outside
the wrapper -- including a monitoring loop I had armed to watch for this exact
fault, polling with plain ssh, i.e. reproducing the fault it was watching for.
There was no `~/.ssh/config` on the VM at all, so nothing outside the wrapper
multiplexed. Fixed by configuring it at the host level, sharing the wrapper's
ControlPath: three consecutive plain `ssh <rig>` calls now leave the session
counter unchanged where they previously created three sessions.

### What it actually was, measured rather than reconstructed

A probe sampling `/proc` every 2 s from tmpfs -- so it neither wrote to SD nor
entered the journal, and could not perturb what it measured:

    wchan=file_tty_write  nr=146 (writev)  fd=41  target=/dev/console
    ttyfds: 41=>/dev/console          <- the only tty fd journald held

Blocked in a `writev()` to `/dev/console`, continuously, 31 of 31 samples
through a stall, while PID 1 stayed healthy.

**Why a console write never returns here, and this is the rig-specific part.**
The kernel console is `console=tty1`, a physical VT on a headless Pi, with
`ixon` enabled. The vcctrl virtual keyboard is a keyboard to the **Pi** as well
as to the DOS target -- which is already why `pi/install.sh` masks
`ctrl-alt-del.target`. A `Ctrl-S` aimed at the g2k is XOFF on tty1: output
suspends, the next console write blocks forever, journald stops draining its
sockets, and sshd, PAM, logind and sudo all block behind it. **Same class of
bug as the ctrl-alt-del one, on a different chord**, and the existing mask is
the proof that this class was already known.

Corroboration arrived by accident: `stty -F /dev/tty1 -ixon` also hangs, because
`n_tty_write()` holds `termios_rwsem` while blocked and setting termios needs
the write lock. The command that would clear the condition queues behind it.

### The fix, and what it does not fix

    ForwardToConsole=no    MaxLevelConsole=emerg
    ForwardToWall=no       MaxLevelWall=emerg

`ForwardToConsole` was already off by default with no drop-ins, so the write was
the wall path. All four are set explicitly, because the point is that journald
must never touch a tty a stray keystroke can stop.

The evidence this worked is **structural, not statistical** -- which matters,
because the two wrong mechanisms above were each declared fixed on an absence
of events during what turned out to be a normal quiet gap:

    10:04:14  jd=9918   file_tty_write  fd=41 -> /dev/console  ttyfds: 41=>...
    10:04:16  jd=14545  do_epoll_wait   fd=32 -> eventpoll     ttyfds:

journald now holds **no tty fd at all**. Blocking on a console write is not
merely unobserved, it is unavailable.

**The root cause is untouched.** tty1 is still flow-stopped; a Ctrl-S can still
freeze it; anything else writing to `/dev/console` still hangs forever. This
removes journald from the blast radius, which is what was taking sshd down.
A real fix is boot-time: clear `ixon` on tty1 before anything writes, or take
the console off tty1 entirely.

### What this cost, and the part worth keeping

Three times in one investigation the instrument damaged the thing it measured:
a monitor polling with plain ssh reproduced the churn fault; a `pkill -f
journald-probe.sh` matched its own ssh command line and killed the shell
running it, silently, three times; and retried `stty -F /dev/tty1` calls each
spun a full core for their whole timeout window -- nine at once, on a machine
at 3.6% idle, while the operator was watching the video stream stutter. One of
those is still spinning: it survives SIGKILL because it is stuck in a kernel
path that does not process signals, and it will clear on reboot.

Two tooling failures were silent rather than loud. `strtonum()` is a gawk
extension and Debian ships mawk, so the field that resolved the fd came back
empty and read as "no fd" rather than "parser broken" -- the decisive fact was
lost for a round to a function that does not exist. And a monitor comparing
`NRestarts` with `!=` instead of `>` reported an explicit restart's counter
reset as a fresh kill.

**The rule that would have shortened all of this:** when a process is failing a
deadline, establish *blocked or busy* before proposing any mechanism. It is one
number, it is printed in the kill line itself, and it eliminates entire
families of explanation before they are written down. Both wrong mechanisms
here were stories about journald having too much work, and the CPU figure had
already ruled that out before either was proposed.

---

## 29. Every diagnostic healthy, nothing driven  [measured 2026-08-20]

After the Pi 5 migration the Gateway would not accept input. Not intermittently
— not at all, from the harness or from a USB keyboard plugged into the Pi.

What made it expensive is that **every instrument reported healthy**:

    SPI to the protocol board       answers, live, verified with a raw xfer
    board detection                 PBID 1, IBM PC Compatible, fw 0.5.7
    protocol selection              AT/PS2 set, set_protocol sent, OLED agrees
    daemon -> uinput                KEY_A down/up read off /dev/input/event5
    USB4VC has the device open      opened device: 0x1209 0xdea1
    input lock                      held, 0 input.refused events
    GPIO 20 (PCARD_BUSY)            low, so the SPI gate was not blocking
    capture, audio, board, power    all fine

And the OLED's debug view **showed the keypresses arriving**. So "the app
receives events" and "the app sends events" were simultaneously true and false.

### The cause

`usb4vc_usb_scan.py` reads input events with a hardcoded 32-bit layout:

    data = this_device['file'].read(16)
    data = list(data[8:])
    if data[0] == EV_KEY: ...

`struct input_event` is `timeval + u16 type + u16 code + s32 value`, and
`timeval` is two `long`s: 8+2+2+4 = 16 on armhf, **16+2+2+4 = 24 on arm64**. A
16-byte read returns only the timestamp, and `data[8:]` is the tail of
`tv_usec`. Measured on one real KEY_A:

    raw:      0365876a 00000000  1a630500 00000000  0100 1e00 01000000
              |-- tv_sec (8) --|  |-- tv_usec (8) -| type code  value
    usb4vc:   data[0] = 26        garbage
    correct:  data[0] = 1, code = 30      EV_KEY, KEY_A

`EV_KEY` is 1. The dispatch compared against 26 and never fired.

This was the operator's own caveat, raised before the migration began: "usb4vc
was 32-bit only, the Pi 5 is 64-bit." It was in the plan as an architecture
note and nobody turned it into a search for hardcoded struct sizes.

### Why every diagnostic passed

**Because they were all in different code paths.** SPI status, board detection,
protocol selection and the OLED are unrelated to the input read loop. The one
path that mattered was the only one with no instrument on it — and the OLED's
debug view actively misled, because `my_oled.kick()` runs BEFORE the parse, so
the UI reported activity the send path then discarded.

### What actually found it

Two operator tests, not software probing.

1. **A real USB keyboard plugged into the Pi.** Same failure, which eliminated
   vcctrl entirely — nothing of ours was in that path.
2. **The OLED in debug mode**, which proved the app was receiving what it would
   not forward.

Before those, the investigation was narrowing the *transport* — cables, chip
selects, the busy pin — and would have kept going. The operator was about to
power down and reseat, which would have proven nothing and cost a power cycle.

Three mechanisms were proposed and refuted before the right one, all by
checking rather than by reasoning: `PROTOCOL_OFF` from a config file that
turned out to be written lazily (the second time `config.json` looked
authoritative and was not — see BOARD-IDENTITY sec. 2), the SPI chip selects
reading as plain GPIO outputs, and the busy-pin gate.

### The rule

**When every diagnostic is green and the system does nothing, suspect the path
with no diagnostic on it.** Health checks cluster where instrumentation was easy
to add, which is not where faults cluster. A green board is not a driven target;
they are separated by exactly the code nobody was watching.

Corollary, for porting: an architecture change is not a note to carry in a plan.
It is a search. `grep` for hardcoded struct sizes, `read(N)` on binary
interfaces, and anything slicing a fixed offset out of a kernel structure.

### Recorded alongside: the mouse, first proven the same day

`vcctrl mouse move` had never moved a cursor on any target, on either machine.
It has now — under Windows 3.11 at VGA 640x480, cursor tracked across
(360,269) -> (461,298) -> (281,112) -> (102,1) -> (2,1), confirmed
independently by the webkvm session from a separate process reading the frame
ring. Full detail in docs/MOUSE.md, including why no DOS-based test could have
produced it: everything that renders a cursor switches to a video mode this
capture path cannot lock.

## 30. `--out` writes on the Pi, and the error blames the VM  [measured 2026-08-20]

Found while setting up the phase-6 sweep, in a path the sweep depends on.

    $ vcctrl shot --out $SCRATCH/postdel.jpg
    could not write .../postdel.jpg: [Errno 2] No such file or directory:
      '.../postdel.jpg.part'

The directory existed and was writable, and a `touch` in it succeeded a second
later. Two rounds went into looking for a local permissions or sandbox problem
that was not there.

**`bin/vcctrl` is an ssh wrapper.** It quotes its arguments and `exec ssh`s the
lot to the daemon host — deliberately, so the same command works unchanged if
Claude Code ever runs on the Pi. Which means `--out PATH` is a path **on the
Pi**, and the ENOENT was the Pi's filesystem answering truthfully about a
directory that only exists on the VM.

**The dangerous case is not the one that errored.** A path that exists on both
machines — `/tmp/shot.jpg` is the obvious one — writes on the Pi and returns 0.
A VM-side caller then analyses whatever `/tmp/shot.jpg` on the VM happens to
contain: nothing, or worse, a frame from an earlier run. That is precisely the
failure `--out`'s two-valued contract was written to prevent ("either F is this
run's frame, or F does not exist"), reappearing one host over, where the
contract cannot see it. The contract is sound; its scope is one machine and
nothing said so.

**From the VM, pull frames over HTTP instead.** `vcctrl-sweep`'s `grab()`
already does this and is the model to copy:

    curl -s -o out.jpg "$VCCTRL_WEB/shot.jpg?n=16"

`grab()` is not doing that for speed. It is doing it because it runs on the VM
and needs the file on the VM, which is the same reason any other VM-side caller
has.

The general shape, which is [[the-instrument-is-part-of-the-system]] rotated
slightly: a guarantee holds inside the boundary it was written for, and a
transport that crosses that boundary carries the words without the guarantee.
Nothing here is broken. `shot --out` does what it says on the machine it says
it on.

## 31. A key's absence read as a value  [audited 2026-08-20]

Two sessions hit this within an hour, from opposite directions, and neither
bug was in the function that failed. Both were in the **seam**: a caller met a
contract carrying a premise nobody had written down.

- The webkvm session's ring **pin** was correct code wired to nothing. The
  save path walked the ring frame by frame without ever taking it, so when the
  signal returned, eviction destroyed frames mid-copy and the download
  silently produced almost nothing.
- `write_frame` judged raw responses by `resp["picture"]`, which is **absent**
  on that path rather than false. Every frame fetched by sequence number would
  have reported "no picture" — and because the two-valued contract removes the
  destination on a no-picture, it would then have **deleted the caller's
  file**.

The second is the worse kind: it fails by destroying the thing it was asked to
produce rather than by doing nothing.

### What the audit found

Scanning every `.get()` in `bin/` and `daemon/` for absence becoming a claim.
Most hits are parameter defaults where absence honestly means "the caller did
not specify" — an env var, a request argument. **Those are fine.** The
dangerous class is a *reading* where absence becomes a statement about the
world, and there were four.

**1. A preflight that could not fail.** `vcctrl-sweep`:

    held = st.get("usb4vc", {})
    if not all(held.values()):     # all({}.values()) is TRUE

With the field missing, this printed `input devices held by USB4VC: ok` and
committed the machine to a 23-minute unattended run it had verified nothing
about. **An absent answer read as a clean bill of health**, and it produced
character-for-character the same output as a real pass.

**2. Power collapsed from three states to two.** `power_on()` was
`bool(...get("power", {}).get("on"))`. The daemon goes to some trouble to keep
`on` tri-state — null when the plug cannot be reached — because "the machine
is off" and "I cannot reach the plug" are opposite facts. `bool(None)` is
False, so the harness threw that away one line after the daemon preserved it.
With `--power-on`, `ensure_powered()` would then send `power on` to a machine
that might be running and wait 240 s for a boot edge that could not arrive.
Nothing was cut — the command is idempotent — but the result was a confident
false statement about the world, then four minutes, then a wrong refusal.

**3. A missing count read as zero.** `vcctrl-capcheck` defaulted `n`,
`distinct` and `repeated` to 0, so `live` came out 0 — which is the signature
of a frozen capture. A daemon that renamed a key would be reported as a
capture stick that had stopped locking.

**4. `relay_state` absent read as off**, in the daemon's own Kasa parse,
undoing the tri-state one layer below where it was carefully built.

### The rule

**Absence is a third state or it is a bug.** Before defaulting a `.get()`, ask
which of two questions the key answers:

- *"What did the caller ask for?"* — a default is correct. Absence means
  unspecified.
- *"What is true of the world?"* — a default is a **fabricated observation**.
  Absence means could-not-look, and it needs its own state.

And the direction matters more than the presence of a default. Absence
defaulting to the alarming value produces a false alarm, which gets
investigated. **Absence defaulting to the reassuring value produces silence,
which does not** — item 1 sat in the preflight of every sweep this project has
run.

Related: sec. 24 (the harness cannot see its own cable), sec. 29 (every
diagnostic healthy, nothing driven). Same family — an instrument reporting its
own state, or its own ignorance, as the target's.

### The same shape, six hours later, in a control run

Proving the docstring-drift test could fail, I deleted a row from the
documented table and expected a failure. **It passed.** The word survived in
the surrounding prose and a substring check accepted it.

The webkvm session made the connection, and it is sharper than either rule we
had: the control did not merely fail to fail — **it reported success.** That is
`all({}.values()) is True` in a different costume. The preflight printed `ok`
about nothing; the control printed `pass` about nothing. Six hours apart, in
unrelated code, and neither of us would have connected them from the
descriptions.

Most rules about weak checks assume a check that is SILENT when it should
speak. Both of these **spoke the reassuring word** when they should have
objected, which is strictly worse: silence invites a second look and a green
tick closes the question.

### And the test that matters is for the state you have not thought of

Three times in one day, a correct reading of correct code produced a wrong
prediction about a seam — in both directions, between two sessions who were
each reading carefully. The reason is not carelessness:

> Reading code tells you what it does against the inputs you have in mind. It
> cannot tell you what it does against an input that does not exist yet,
> because that input is not in the room.

The version that worked was the webkvm session's: assert with a deliberately
invented value — `why: "martian"` — so the property under test becomes
**"unknown states degrade correctly"** rather than "these five states work".
That required imagining no specific future value, only that there would be
one.

**One such test per seam is worth more than five more specific assertions**,
because the specific ones are all drawn from the set you already know.


## 32. Two combination rules, and picking the wrong one  [2026-08-20]

Two composite verdicts were specified the same day and they needed **opposite**
combination rules. Getting this backwards is a real error in either direction,
so the discriminator is worth writing down.

**Dependent checks take a CONDITION.** The PUMP scorer reports a pair spread
and an arm delta. The delta's acceptance window is derived from sigma, and the
pair spread is precisely what estimates sigma — so a run whose spreads have
widened has refuted the premise its own delta threshold rests on. Scoring the
delta anyway counts one fault twice and dresses it as two independent
findings. So: spread FAIL means the delta is **UNSCORED**, not failed. "This
run cannot tell you" is a different claim from "the value is bad".

**Independent checks take a PRECEDENCE.** `vcctrl preflight` runs seven checks
that do not condition one another — a dead capture stick says nothing about
whether the plug answers. So:

    any FAULT        -> FAULT     the definite fault is the actionable one
    else any UNKNOWN -> UNKNOWN   an unknown MUST NOT read as a pass
    else             -> PASS

with the unknown still reported separately, so an earlier could-not-look is
not masked by a later fault or the reverse.

**Deciding which rule applies is part of specifying the verdict**, not an
implementation detail to be settled while writing the aggregator.

### And a composite verdict must name its subject

`vcctrl preflight` answers **"is the apparatus fit to drive the target"**. It
does not answer **"is the target fit to be measured"** — graphics provider,
sound mode, boot profile. Those are two preflights with different subjects and
passing one says nothing about the other.

The receipt, from the benchmarking session: a lost round had a completely
healthy harness — keystrokes landing, capture locked, logs collected — driving
a target whose graphics provider had silently failed to load. **Seven green
checks and fourteen wasted minutes.**

So the tool states its own scope in its output (`scope`, `does_not_cover`)
rather than returning a bare `ok`. A verdict with an unstated subject gets
read as covering whatever the reader was worried about, which is the same
failure as sec. 30: a guarantee is sound inside the boundary it was written
for, and nothing about the words says where that boundary is.

**Why a bare `ok` is not neutral**, in the benchmarking session's words, which
are sharper than mine: *the reader supplies the boundary their current
question needs.* It is not that the reader is careless — it is that nothing in
the artifact contradicts the extension, so the most useful reading is also the
unopposed one.

**And the remedy belongs in the OUTPUT, not the docs.** Documentation is read
before a tool is trusted; output is read at the moment of trusting it.

Both instances are worth keeping together, because the pair is what shows this
is a shape rather than two bugs: a two-valued file contract, sound on the
machine it ran on, producing the forbidden third state one host over; and a
readiness verdict, sound about the harness, readable as "fit to run". Both
correct. Both read wider than written, by the people who wrote them.

## 33. The right measurement of the wrong moment  [demonstrated 2026-08-20]

Distinct from sec. 31 and sec. 32, and it took the benchmarking session to
name why: those were a proxy standing in for a thing, and a guarantee read
past its boundary. **This one is a correct reading of a moment that has
passed.** Same consequence, different cause.

A status surface retains the last value published to it. A target that is off
publishes nothing. So the surface keeps reporting what was true before — and
**it does not read as stale, because a stale value and a current one are the
same value.**

### Demonstrated, minutes after powering the Gateway off

    $ vcctrl power state
      on: False                                    <- correct

    $ vcctrl leds
      {"available": true, "capslock": 1, "numlock": 1, "scrolllock": 1}

    $ curl /state.json
      input_verified: {"available": true, "ok": true, "age_s": 104.8}
      video: "frozen"

The daemon says **"the target is powered off"** and **"the target
acknowledged a keystroke"** in the same breath. Both fields are working
exactly as written. Only one of them is about now.

**`available: true` is a field added earlier the same day**, in the change
that gave `leds` three honest states. It distinguishes "this board has no LED
channel" from "the channel is unreadable" and it does **not** distinguish
either from "the machine is not powered to publish on it". A schema built to
stop a Macintosh looking like a broken Gateway does not stop a powered-off
Gateway looking like a live one.

Note `input_verified` carries `age_s`, so a careful reader *could* catch it at
104.8 s. `leds` carries nothing at all. The difference is not principle, it is
that one of them happened to be built with a clock.

### The rule

**A reading must be shown to belong to the current epoch.** Not "recently
read" — reading it again returns the same retained value — but demonstrably
produced by the system as it is now.

The worked example is `wait_cold_boot()`, and the general statement is what
makes it more than a quirk: **readiness is the last thing a healthy boot
sets**, so a level check for "ready" returns TRUE immediately after power-on
on a machine that has not begun to POST. Observed 2026-08-19, 2.5 s after
power-on. The edge pair — wait for the LEDs to CLEAR, proving this boot, then
wait for them to set — is the proof of epoch, not a workaround for a flaky
check.

The benchmarking session's line is the one to keep: **the most misleading
moment for a retained reading is exactly the moment it is most likely to be
consulted** — because that is when something has just changed and the reader
wants to know whether it has finished changing.

### Fixed  [2026-08-20, same evening]

`TargetEpoch` — a module-level fact, not a call across capabilities.
`PowerCapability` reports every observed power transition to it;
`LedsCapability` reads it. Neither holds a reference to the other, the same
shape as `installed_board_id()`.

Two guards, and the second is the one a power check alone would miss:

**Powered off → `why: "unpowered"`, values OMITTED.** A positive
determination, not a guess: a machine with no power publishes nothing, so
whatever the nodes hold predates the cut.

**Powered on, but nothing published since the transition → `why:
"unproven"`.** This is the sharp case. Just after power returns, the nodes
still hold the *previous* boot's values and the machine is on, so a power
check alone reports them as live. It is the moment `wait_cold_boot()` exists
for — readiness is the last thing a healthy boot sets, so a level check for
"ready" reads TRUE 2.5 s after power-on on a machine that has not begun to
POST. Evidence of currency is that the value has CHANGED since the
transition; the capability records which epoch each change was seen in.

Deliberately **not** "refuse whenever power is unknown". An unreachable plug
does not mean an unpowered target, and refusing there would make `at_prompt()`
return could-not-look on a perfectly healthy machine every time the Kasa was
unreachable — a cure that stalls sweeps. Only the positive determinations act.

Verified live against the powered-off Gateway, the exact reading that was
wrong an hour earlier:

    leds            available false, why "unpowered", no value keys
    leds_available  (False, 'unpowered', ...)
    stable_led      None          -- could not look
    at_prompt       None          -- NOT False, which reads as "still running"
    wait_led        None in 0.05s -- rather than burning a 30 s timeout
    preflight       fault, decided_by power

`input_verified` is voided the same way: a proof of the input path is a
statement about a moment, and a power transition since means it describes a
machine that no longer exists in that state.

**Still owed to the webkvm session:** a full change record with a monotonic
clock, which answers this *and* the intermittent LED divergence that the
boot-path edges pass straight through. What is here proves currency only
across power transitions. That is the case that was demonstrated, and it is
not the whole of the problem.

## 34. The pin that hides its own release  [measured 2026-08-20]

**A capture went black and stayed black through three correct fixes, because
each fix worked and the instrument could not report it.**

The KVM auto-pins the frame ring whenever the picture is lost — sensible, so
the last good frames can be examined. And `_push` drops the **NEW** frame when
the ring is pinned and full, rather than freeing one somebody may be looking
at — also sensible, and the whole point of a pin.

Together they are a trap:

> **The condition that engages the pin is the same condition that prevents you
> from observing it end.** The picture comes back, the ring refuses the frames
> that would show it, and the daemon goes on serving the black frames that
> triggered the pin in the first place.

Measured twice in one evening. `video state` read `frozen` for two solid
minutes after the VGA lead was already reconnected and the stick was already
locked — and flipped to `locked` within seconds of `vcctrl pin off`, with no
other change.

**This is not a bug in the pin.** The pin does exactly what it says. What is
missing is that nothing in its design knows "picture lost" is a state you want
to *leave*. An auto-engaging pin needs a release condition, or the pin should
drop oldest rather than newest when full — and the choice between those is a
real decision, not a detail.

**It also stalls the frame counter.** `frames_total` was incremented after the
pinned early return, so the counter froze while pinned — and a consumer
computing a rate from the difference saw zero and kept displaying the last good
figure as current. Retained value rendered as live, which is sec. 33 in a third
place. Fixed by counting arrivals before the return: a frame that arrived and
was dropped **was** seen, and the drop is separately counted in
`dropped_while_pinned`.

### Three causes stacked on one symptom

Worth recording as a shape, because it is why the fault survived each correct
fix and why every explanation looked wrong:

1. **The stick had latched.** A physical reseat cleared it. Every software
   reset tried first — ffmpeg respawn, `video release`/`acquire`, USB
   unbind/rebind — addresses a different layer. **`unbind`/`bind` re-binds the
   driver and never drops power to the device**, so it cannot clear a latched
   analog front end. Only unplugging it does.
2. **The cabling changed under the diagnosis.** With no splitter, the single
   VGA lead feeds either the stick or the external monitor. Some "still black"
   readings were taken with the lead on the monitor. The configuration was
   confirmed once and then treated as durable across twenty minutes of tests.
3. **The pin masked both recoveries.**

**The diagnostic rule:** when a state persists across a fix that should have
worked, ask whether something is holding the *observation* rather than the
subject. And re-establish the physical configuration at each step — a
confirmation is a reading, and readings expire (sec. 33).

**The measurement that stayed honest** was a raw `ffmpeg -input_format yuyv422`
grab straight from the device: it bypassed the daemon, the ring, the pin and
the MJPEG path, and reported black when things were genuinely black. When every
layered instrument agrees, the one that shares no layers with them is worth
more than another opinion from inside the stack.

## 35. A check may not pass on an empty population  [2026-08-20]

Five instances in one day, in five unrelated checks, written by three
sessions. The unifying form, which the benchmarking session stated and which
is worth having as a rule with teeth:

> **A check MUST assert its input set is non-empty before it may return a
> pass.**

**Because an empty population and a passing population produce the same
output** unless something explicitly says otherwise. The check does not fail
to speak — it speaks the reassuring word about nothing, which is strictly
worse than silence: silence invites a second look, a green tick closes the
question.

The five:

| check | how it passed on nothing |
|---|---|
| sweep preflight | `all({}.values())` is `True`, so a missing field printed `input devices held by USB4VC: ok` |
| docstring-drift control | deleted row survived as a substring in nearby prose |
| KVM frame rate | stalled counter, difference of zero, last good figure shown as live |
| an ASCII check | fired regardless of what `grep` returned |
| **clip check** | filtered to lit frames, matched **zero**, printed `CLIP HOLDS: margins are black in every lit frame` |

The last is mine, from the MQ5 analysis, and it is the cleanest specimen:
the sentence names the population — *"in every lit frame"* — and there were
none. It read exactly like a result.

**Four of the five were caught by whoever wrote the check**, which is the
only reason none of them cost anything. That is not a system; it is luck
repeated. The one-line assertion is the system.

    lit = [f for f in frames if mean(f) > 25]
    assert lit, "no lit frames -- this check examined nothing"

Related: sec. 31 (absence read as a value) is the same failure in data;
this is it in control flow. And sec. 32's rule — a verdict must name its
subject — has a sibling here: **a verdict must also name its sample size.**

## 36. Two recordings, one ring: 94.5% of a cell's "glass" was the previous cell's  [measured 2026-08-20]

`GMQ3-glass.avi` is 815 MB and looks like a recording of cell MQ3. It is not.
**18,871 of its 19,976 frame packets are byte-identical to frames in
`GMQ2-glass.avi`** — 94.5%, checked by hashing the MJPEG packets on both
sides without re-encoding. Its own unique content is the last ~78 s.

The metadata says so plainly and nobody read it:

    cell   first_seq   last_seq   window          real_span   avi_dur   thinned
    GMQ2     128800     146848    21:29:27..21:39:29   602.5s    602.5s   no
    GMQ3     129163     148485    21:29:39..21:48:08  1108.6s    665.9s   YES

`first_seq` is the daemon's **global** frame counter. GMQ3's window opens
twelve seconds after GMQ2's, so the two files necessarily contain the same
frames. A dump names the cell that requested it; it does not bound itself to
that cell.

### How it produced a confident, wrong refutation

A peer session sampled the AVIs to check my claim that no Mach64 glass had
locked since MQ2, found clean 640x480 game frames in `GMQ3-glass.avi` at
t=533 s, and reported the claim refuted. **The frames are real, correctly
identified, and MQ2's** — t=533 s falls in the shared region, which runs
0..~590 s of that file. Sampling MQ3's unique tail at 2 Hz gives 156 samples,
max colour saturation **0.0%**, zero game frames.

So the refutation was the same shape as the error it corrected: a sample
generalised past what it covered. Mine trusted one frame to speak for a cell;
theirs trusted one file to belong to a cell.

### The cause is the ring bug that was already fixed

The ring was holding far more than `target_span_s` asked for — 480 s asked,
1193 s held — because eviction was running on bytes and not on age. A ring
that overruns its window by 2x guarantees that consecutive dumps overlap.
**Only the three thinned files can overlap** (`avi_dur` well under `span_s`
means the ring was over-full and thinning); of those, only GMQ2/GMQ3 actually
do, because the other gaps were long enough. The age-eviction fix closes this
as a side effect nobody was aiming at.

### What the corrected record says

    MQ0  game frames present     capture worked
    MQ1  game frames present     capture worked
    MQ2  game frames present     capture worked
    MQ3  NO game frames          (own content only, 156 samples, 0.0% colour)
    MQ4  NO RECORDING AT ALL     unknown -- not a finding
    MQ5  NO game frames          (104 samples, 0.0% colour, real console text)

MQ5 is console text and not a black recording: 102 of 104 samples carry lit
pixels, max lit fraction 10.0%. "Console-only" is a positive observation, not
an absence.

**MQ4 is the part my own table got wrong**, and in the way I have written
three memories about: it had no recording, and I reported `NO LOCK` — the
instrument's state published as the target's. Two cells failed to show game
glass and one was never looked at.

### Rules

- **A dump is bounded by the ring, not by the cell that asked for it.** Read
  `first_seq`/`first_t` before treating a recording as evidence about a run,
  and check it against the neighbouring cells' ranges.
- **Overlap is detectable for free** — compare `first_seq`/`last_seq` across
  the archive. It costs one pass over the `.meta` files and would have caught
  this before the analysis, not after.
- `written`/`fps` well under `span_s` means the ring **thinned**, so AVI time
  is not proportional to wall time in that file and any t-to-wall arithmetic
  on it is wrong.
- A recording should carry the cell's own start time and be clipped to it.
  Until it is, the filename is a label and not a provenance claim.

## 37. The prompt detector inverts the text the harness types  [measured 2026-08-20]

`vcctrl type` produces an uppercase letter by holding SHIFT. **Caps Lock
inverts SHIFT.** `at_prompt()` toggles Caps Lock as its probe, several times a
second, for the whole life of a cell. So **the case of everything this harness
types is decided by a bit its own prompt detector is flipping.**

Measured on the target, same command, same second, one LED apart:

    capslock=1  ->  set | find "blaster" | find /c "="   ->  count: 0
    capslock=0  ->  SET | FIND "BLASTER" | FIND /C "="   ->  count: 1

DOS does not care about the case of `SET` or `FIND`, which is why this hid for
so long. **`FIND`'s search string is case-sensitive by default**, so the
pattern matched nothing and the pipeline answered 0.

### Why that was worse than a wrong answer

**`0` is also what "absent" looks like.** The forbidden-variable gate — added
hours earlier specifically because a counting check cannot prove absence —
would print

    DOSKUTSU_SHOT_TICKS   absent (count 0, read back)

for a variable that was **set**. Absence read as a value, inside the gate
written to prevent absence being read as a value. R1A passed it because Caps
Lock happened to be off, which is luck, not design.

It surfaced only because the same pipeline was also asked about BLASTER, a
variable that is *known to be set*. A false 0 there refuses a cell loudly; a
false 0 on the forbidden check passes one silently. **The check that could
only fail safely is the one that revealed the bug in the check that could
fail dangerously.**

### The fix that is not just a patch

`FIND /I` removes the case dependency, and that alone would have closed this
instance. The structural fix is the ordering:

**A known-present probe now runs FIRST, through the identical pipeline, and
an absence is only believed after it returns 1.** BLASTER proves `SET`, the
pipe, `FIND`, the typing and the OCR all work end to end. Only then does a 0
mean the variable is absent rather than the instrument being broken. The log
carries the warrant with the claim:

    -- profile witness --
      BLASTER              set (profile is PGSB)
    -- and 1 that must be ABSENT (pipeline proven by BLASTER above) --
      DOSKUTSU_SHOT_TICKS  absent (count 0, read back)

One probe, two jobs: boot-profile guard, and the control that makes every
absence under it mean something.

### Rules

- **Never prove an absence on a pipeline that has not just proved a presence.**
  A negative result from an untested instrument is not evidence.
- Use `FIND /I` for every DOS-side match. Any case-sensitive comparison driven
  by this harness has the same exposure — including
  `MEM /C | FIND "UNIVBE"`, which appears in a proposed provider check.
- The deeper defect is still open: **`type_text` cannot produce a requested
  string independent of Caps Lock.** `/I` fixes matching, not typing. The
  daemon should read the LED and invert the shift for letters.
- **An instrument that perturbs its subject can perturb its own other
  instruments.** `at_prompt` was known to move Caps Lock; nobody asked what
  else read that bit. Fourth instance this week of the diagnostic changing
  what it measures (sec. 28, 33, 34).

## 38. The evidence capture answered "nothing happened" by asking the wrong question  [measured 2026-08-20]

After `vcctrld` aborted mid-cell, the round runner gained a `grab_journal`
step whose entire purpose was to preserve the daemon's dying words. It ran on
the very next abort and produced:

    -- No entries --

**Which reads as "the daemon was fine."** The runner timestamps with
`date -u`; `journalctl` on the Pi reads **local** time. PDT is UTC-7, so
`--since '2026-08-21 06:26:29'` asked for entries starting nine hours in the
future. There were none, and there could never have been any.

Only the separate MainPID check — which compares two integers and has no
timezone — proved a restart had happened at all. Querying by hand with a
relative window returned the whole thing: `double free or corruption (!prev)`,
`Fatal Python error: Aborted`, and eighteen threads of faulthandler output.

### Why this one is worse than the others

**The tool was written one hour earlier, specifically to prevent this class of
failure.** It was built after the first abort, by someone who had spent the
evening cataloguing checks that pass on nothing, and it shipped with the same
defect: an empty result and a healthy result are the same output.

That is not carelessness about the rule. It is the rule being known,
articulated, freshly applied — and the implementation still not obeying it.
Recent diagnosis is exactly when the guard slips, because naming a hazard
produces a feeling of coverage that substitutes for the check.

### Rules

- **A capture that finds nothing must say so loudly.** `grab_journal` now
  refuses to be quiet: under three non-empty lines prints
  `JOURNAL CAPTURE RETURNED ALMOST NOTHING -- suspect the query, not the
  daemon. Do NOT read this as 'no fault'.`
- **Use relative windows across a timezone boundary.** `--since '-45 min'`
  has nothing to get wrong. Absolute stamps must be generated in the
  *reader's* timezone, not the writer's.
- Two hosts, two clocks: the VM logs UTC, the Pi journals PDT. Any query
  crossing that boundary is suspect by default.
- **Prefer a check with no units.** The MainPID comparison worked precisely
  because integers carry no timezone, no locale, and no format. When a guard
  can be built out of an identity comparison rather than a parsed quantity,
  build it that way.

Same family as sec. 31 (absence read as a value), sec. 35 (a check passing on
an empty population) and sec. 37 (a pipeline whose silent failure looks like a
negative answer) — but this is the first where the guard containing the defect
existed *because of* the others.

## 39. Eight cells compared a condition against itself  [measured 2026-08-21]

Round R ran eight cells, `A B B A A B B A`, to measure the cost of patch 0320.
Every guard passed. The result looked textbook:

    A cells 26.9 26.9 26.8 26.9    A-to-A spread 0.10
    block 1 delta +0.05            block 2 delta +0.05
    >>> PATCH 0320 IS FREE <<<

**All eight cells had 0320 stood down, including the four that were meant to
be stock.** Every cell's own log says so:

    backdrop thrash centring DISABLED (killswitch)

The round compared arm B against arm B. Two blocks agreeing to two decimal
places, a spread landing exactly on the pre-registered band — **every summary
statistic was healthy precisely BECAUSE the comparison was empty.**

### The mechanism

    FIND /I /C "THRASH_CENTRE" CLRENV.BAT  ->  count: 0
    FIND /I /C "SET "          CLRENV.BAT  ->  count: 205   <- control

`CLRENV.BAT` clears 205 names. The engine has ~164 levers and **97 are not
among them** — including `SHOT_TICKS`, `BACKDROP_CACHE`, `PIN_NATIVE_MODE` and
every `TAS_*`. One B cell set `SDL_HINT_DOSKUTSU_THRASH_CENTRE` and it
persisted for the rest of the sitting.

The contamination even predates the round that was kept: an earlier B cell
**set its variables and then refused at the profile gate.** Variables are set
before that gate by design, so a prompt cannot eat a `SET` — a refusal is not
a rollback.

Round Q escaped only because collection reboots into NET between cells.

### The gate that existed and was pointed the wrong way

A gate had been built that same evening to prove a named variable ABSENT,
tested in both directions, made non-vacuous with a positive control, and
written up twice. **It was pointed at `DOSKUTSU_SHOT_TICKS` and never at the
other arm's lever.**

**A control arm needs the same rigour as the treatment arm.** Arm A is
*defined* by that variable being absent. What was verified was the variable
that would spoil the *class* of measurement; what was left unchecked was the
variable that defined the *comparison*.

### The fix is not an environment check

Enumerating levers is whack-a-mole and `CLRENV` will always lag the source.
**The engine prints its own effective configuration every run**, and that
reading is downstream of the environment, the config file, the defaults and
every killswitch — so a lever nobody enumerated cannot fool it.

Each cell now reads its own log off the card and refuses unless the engine
reports the arm it was supposed to be (`--expect-log`). Pointed at the invalid
R1A it returns `count: 1` for DISABLED: it would have refused that cell.

`--forbid` also had to CLEAR as well as verify — verifying alone is a stronger
statement about history but makes a round un-runnable, since a B cell
legitimately sets the lever and the next A cell would refuse rather than
proceed.

### The re-run, and the sign

    A cells 27.6 27.6 27.7 27.6    A-to-A spread 0.10
    block 1 delta -0.60            block 2 delta -0.70

**Standing 0320 down costs 0.65 fps.** The pre-registered label said "0320 has
a cost" and assumed the opposite sign; the criteria fixed a threshold and
sign-agreement, never which side of zero. The label was corrected to match the
measurement and the change was declared. *Changing a description to match
reality is not moving a goalpost; changing a threshold would have been.*

The mechanism, from the source: the tile loop bound is in **source space**, so
both arms blit the same tiles. B (`_cox=0`, no clip) writes every byte into
the 640x480 surface with nothing to discard the overhang; A's clip throws it
away. **A writes strictly fewer bytes** — 2.4% for trimming a tile-width off
two edges.

### The witness nobody designed

Mid-cell brightness, which no scoring depends on:

    A cells  24.7 24.6 24.1 24.6
    B cells  24.2 23.4 23.4 23.6
    invalid round, ALL eight cells: 23.4 - 23.7

**The invalid round's uniform readings are the B signature** — independent
confirmation, from a measurement collected before anyone suspected a problem,
that those eight really were one arm.

### Rules

- **Verify the arm from the thing that ran it, not from what it was asked to
  run.** An engine that states its own configuration is a witness; an
  environment is a request.
- **A refusal is not a rollback.** Anything set before a gate survives the gate
  refusing.
- Agreement between blocks is not evidence of a valid comparison. **A null
  result is exactly what a comparison of a thing with itself produces**, and
  it arrives wearing the clothes of a clean measurement.

## 40. The repeatability band was the metric's own truncation  [measured 2026-08-24]

**`per_loop_fps` is quantised in the COMPUTATION, not the format.**

    main.cpp:1530   _perloop_x10 = _flips * 500UL / _reel_ticks     integer division
    main.cpp:1617   fprintf("per_loop_fps=%lu.%lu", x/10, x%10)      just formatting

It truncates — it does not round — to 0.1 fps before anything prints it, so
**every cell reads low**, here by 0.017 to 0.081. Mostly common-mode in a delta;
not exactly so, and it passes through twice in a delta of deltas (measured at
0.0125 of the 0.100 increment).

**Four stock cells then read `27.6, 27.6, 27.6, 27.6` — spread 0.00 — and that
became a "±0.1 within-session repeatability band".** It was the binning. The
exact values were `27.6459, 27.6459, 27.6265, 27.6167`: a true spread of
**0.029**, three flips out of 2840.

### The exact numbers were already on disk

`fps_true_flips` and `reel_ticks` are emitted in every manifest, and `reel_ticks`
was **identical (5140) in all twelve cells** of the confirming round. The reel is
a fixed TAS replay, so **flips over a fixed reel is an exact integer measure with
no quantisation at all**, recoverable from cells already collected — no rebuild,
nothing near a binary mid-round.

**Look for the raw counts beside the ratio before proposing to emit them.** A
patch to emit `per_loop_fps_x100` was designed and dropped for this reason.

### Precision was the wrong limit anyway

The question it was meant to settle — does `ASM_BLIT` add 0.10 on top of
`TILE_DWORD_COPY` — is **variance-limited, not resolution-limited**:

    the increment                      +9 flips
    same-config pair spread, archive    0 to 16 flips

More decimal places would have reported those nine flips beautifully and
resolved nothing. **Resolution and repeatability are different limits; check
which one binds before spending a binary change on it.**

### What it cost

**The band became the confirming round's acceptance criterion** — 0.10 fps is
10.3 flips, against a rig whose identically-configured pairs differ by up to 16.
The test could not have reliably confirmed anything, and `ASM_BLIT`'s
NOT-CONFIRMED is a property of the test rather than the lever.

### And it condemned a healthy result the same day, live

The harness move out of `bin/` (`CONFIG-PLAN` phase 6) was accepted against the
**restated** criterion. The first cell from the new paths:

    banked six, pre-move   2842 2842 2840 2839 2844 2849   mean 2842.67
    ACC1, post-move        2827                            -0.152 fps

**Under the retracted 0.10 floor: `|−0.152| > 0.10` → "IS evidence" → phase 6
FAILS**, and somebody spends the evening hunting a fault in a file move that
changed nothing. `ACC2` then read **2840 — inside the banked band** — so `ACC1`
was ordinary scatter and the level had not moved at all.

**Worse, the old floor's verdict depended on how many cells you happened to
run.** `ACC1` alone is `−0.152` and fails it; the two-cell mean is `−0.089` and
passes it. A margin that is narrower than the measurement noise does not test
the subject, it tests the sample size.

**The restated criterion got it right from the first cell**, and it did so
because the margin came from the archive's own pair spread rather than from the
metric's resolution.
`T1-CONFIRM-RESULTS.md` has the round; `HARNESS-STANDARD.md` 10.0e is the
design-time gate that prevents the next one.

### And the band that replaced it is a PAIR spread, quoted as a population

`FRS1` — a cell run from a **freshly cloned, post-rewrite** repo, exercising the
full chain from the clone's `harness/`: profile read, env clear, config copy,
launch, engine attestation, prompt return — read **2858 flips, 27.8 fps**. The
rewrite is therefore transparent to the measurement, which is what it was run to
show. It also completes a stock population:

    banked six, pre-move        2842 2842 2840 2839 2844 2849
    ACC1 / ACC2, post-move      2827 / 2840
    FRS1, post-rewrite          2858

    nine cells, three sittings  2827 .. 2858   range 31 flips = 0.30 fps

**The archive's `0 1 2 5 12 15 16` are same-config PAIRS. Two stock cells in the
accumulated population differ by 31** — roughly double the figure now being
quoted as "the rig's repeatability", including in this file above.

This is not a contradiction and the 16 is not retracted: range grows with `n`,
and these two numbers are the two bands `HARNESS-STANDARD.md` 10.0 already
requires to be measured and published separately. **The defect is that only one
of them is in circulation**, so a comparison across sittings gets read against
the within-pair figure. `PI5-MIGRATION.md` sec. 6 was exactly that case — its
restated criterion, `~16 flips is not evidence`, is applied to a test that is
across machines and across sittings, where `FRS1` alone would clear it by 9.

**A margin narrower than the noise tests the sample size rather than the
subject** — §40's own conclusion, arriving a second time because the replacement
margin was taken from a narrower statistic than the comparison it now gates.
Re-derive the band from the accumulated population whenever the population
grows, and name which band a criterion is quoting.

`FRS1` and the pre-rewrite cells were run by the peer session that owns the
benchmarking chain; the flip counts above are as reported by it. The fps
conversions are checked here against `_flips * 500 / _reel_ticks` at
`reel_ticks = 5140`.

## 41. `verify_input` proved a link that was not there  [measured 2026-08-25]

The g2k was powered off. `power state` said so (`on: false`), and `leds`
correctly refused with `why: unpowered` — the exact guard sec. 33 put in
place. `verify_input` did not agree with either of them:

    $ vcctrl power state
      on: false
    $ vcctrl leds
      {"available": false, "why": "unpowered", "reason": "the target has
       no power, so these nodes hold what it published before the cut"}
    $ vcctrl verify-input
      {"verified": true, "note": "the target acknowledged a keystroke"}

Three readings of the same instant, and the one built specifically to be
trustworthy — `verify_input`'s whole reason to exist is that every other
input status "describes the Pi's own end of the wire" — was the one that
was wrong.

### It was not a stale value. Something moved, live.

Sec. 33's fault was a RETAINED reading: a value true before power-off,
still sitting in the nodes, read again and mistaken for current. This is a
different shape. `led-changes` right after the probe:

    seq 1  epoch 0  capslock 0 -> 1   t 1787686040.490
    seq 2  epoch 0  capslock 1 -> 0   t 1787686040.516

A genuine transition, toggle then restore, 26 ms apart — exactly the pair
`_verify_input()` produces — recorded at **epoch 0**, the epoch that means
"before the first observed power-on" (`TargetEpoch.__init__`: `self.powered
= None`; sec. 33's own vocabulary treats epoch 0 as belonging to no proven
power-on). Nothing was retained here. The sysfs node moved WHILE the target
had no mains.

### The mechanism

`_verify_input()` sends `KEY_CAPSLOCK` to our own uinput device and polls
the LED sysfs node USB4VC's `change_kb_led()` writes on an inbound `0xED`
Set-LEDs message from the real target. But that same sysfs node belongs to
a kernel-visible input device, and the Pi's own input stack tracks lock-key
state for any keyboard device locally, independent of whatever the external
protocol board is doing — pressing Caps Lock on a uinput device is enough to
move its own LED node without any reply ever crossing PS/2. `_sample()`
cannot tell that apart from a genuine Set-LEDs echo: both are "the value
moved and moved back" on the same node, and `_verify_input()` reads only
that.

**This means the round trip's central claim was never quite what it said.**
"The value returns only if the target's keyboard controller received the
key" (this method's own docstring, until now) is true for a *board with the
lead attached and no local echo* — which is what the finding this docstring
already cites (the vcctrl session unplugging the PS/2 lead, FINDINGS sec.
29's family) happened to test. It is not true unconditionally, and
"unpowered" is the one state where the gap is provable rather than merely
suspected: an unpowered target cannot answer, so any observed transition
during that state is, by construction, not from the target.

### The fix

`daemon/vcctrld.py` `LedsCapability._verify_input()` now reads
`TARGET.state()` — the same fact `snapshot()` already gates `why: unpowered`
on (sec. 33) — and refuses **before sending anything**, rather than sending
the probe and letting a local artifact be misread as the target's answer.

Deliberately NOT extended to `snapshot()`'s other gate, `_proven_epoch`
("unproven"): that one exists precisely so `verify_input` has a way to prove
a channel snapshot() itself has shut, and gating the prover on the thing it
proves would make the channel unprovable. "Unpowered" has no such
bootstrapping problem — an unpowered target can never legitimately produce
this value regardless of how many times it is asked, so refusing there
closes a real hole without reopening a needed one.

`tests/test_core.py::test_verify_input_refuses_when_unpowered` reproduces
the mechanism with an instrumented fake (`Devs.key()` flips its own LED
value on every call, exactly like the measured local echo) and asserts the
property that matters: the key is never sent while unpowered, not merely
that the reply says so. A powered control case in the same test confirms
the fake — and therefore the refusal above it — is doing real work rather
than a probe that could never succeed anyway.

Found by the vcctrl session's MCP tool layer (`agent/vcctrl_mcp.py`,
worktree `mcp-server`) on its first supervised run against real,
deliberately-idle hardware — the first thing to call `verify_input` against
a target confirmed off since sec. 33 landed.

## 42. A frequency probe with sub-Hz resolution reads real music as silence  [measured 2026-08-27]

`AudioCapability._spectrum` (`daemon/vcctrld.py`) exists to answer a gap
`vcctrl-audio`'s existing amplitude checks (sec. 11) cannot: whether a signal
is *shaped* like music/SFX — energy spread across several frequency bands —
or like a steady tone or mains hum, which concentrates it in one, even when
both read at a similar level. The first version measured one exact
frequency per band (a Goertzel filter, one point-probe per band) over the
same multi-second window `_levels` already reads.

Against synthetic sine tones, generated at the exact frequency each probe
was tuned to, it worked and its own unit tests passed. Against real Passage
music playing on the rig, at the same moment `_level` read `mean_db: -31.79,
peak_db: -19.67` (unambiguous, loud, real music by every existing check),
every one of the 8 bands read below -100 dB and `active_bands` was 0 —
indistinguishable from the true silence reading at Passage's own (silent)
title screen taken minutes earlier.

### Why: the window length that helps `_levels` hurts a point-probe

A Goertzel evaluation over N samples has frequency resolution `rate/N`. For
a 3000 ms window at 48 kHz, N = 144000, so the resolution is 0.33 Hz — the
probe was effectively asking "is there energy at EXACTLY 800.000 Hz", not
"is there energy anywhere in this octave band." Real music is essentially
never sitting on that exact point; the broadband amplitude figures stayed
correct because RMS/peak don't care what frequency the energy is at, only
how much there is. The synthetic tests never caught this because they
generated tones at the bin-exact frequency the code itself computed — the
test and the code agreed with each other rather than with a reference
signal that doesn't know what bin the code is asking about. (Compare sec.
41's own "unconditionally true" gap, and the general rule in
`vcctrl-repo-conventions`/this session's own memory: a port that
re-implements its subject agrees with itself.)

### The fix

Replaced the per-band Goertzel point-probe with a real FFT (`_fft`, radix-2,
pure Python — no numpy dependency; `requirements.txt` stays PyYAML only) over
short ~85 ms windows (`SPECTRUM_FFT_N = 4096`, 11.7 Hz/bin), Hann-windowed,
averaged in the linear power domain across however many such windows fit the
requested `ms`. Each of the 8 bands sums power over its own **range** of
bins (`center/sqrt(2)` to `center*sqrt(2)`, contiguous since centers double
each step) rather than reading one bin — "how much energy is in 566-1131 Hz"
instead of "is there energy at exactly 800.000 Hz." A signal no longer needs
to land on an exact frequency to be attributed to the right band, which is
exactly the property real music needs and a synthetic bin-aligned tone
never tested for.

### Measured, after the fix, real Passage music vs. real silence

Same rig, same moment discipline as sec. 11 (`_level` and `_spectrum` read
back to back, both against the live ring):

    real Passage gameplay:  mean -35.2 dB, peak -20.8 dB (from `_levels`)
                             active_bands 7/8, band_db -39.5 .. -79.2 dB
                             (only the 12.8 kHz band excluded -- this game's
                             audio genuinely has little content there)
    Passage's own title screen (silent by design until a keypress):
                             active_bands 0/8, every band below -100 dB

`BAND_ACTIVE_MARGIN_DB = 30.0` and `BAND_FLOOR_DB = -65.0`
(`daemon/vcctrld.py`) are calibrated against these two real readings, not
assumed. `tests/test_core.py::test_audio_spectrum` covers the FFT's own
math (Parseval, on a bin-exact tone) and the behavioral shape (single tone,
three summed tones, broadband noise, digital silence) against the corrected
implementation.

## 43. A CPU-tier-independent typing pace corrupts long typed lines on a slow CPU  [measured 2026-08-27]

`_prove_net()` (`daemon/vcctrld.py`) proves a `send_file`/`get_file` job's
NET boot by typing one long command line — `type_line("C:\\MTCP\\VCCHK.BAT
%s %s" % (...))`, ~40 characters — and waiting for the resulting file to
arrive. On a Pentium OverDrive 83 this rig ran all day without a single
corrupted command. After a CPU swap to a 486DX2-50, the identical code path
failed twice in a row: the target's screen showed `C:\MTCPp.` and then
`C:\MTCP\VCCHKCE` where the real command should have been — characters
dropped mid-string, no error anywhere in the chain, `_prove_net()` timing
out at 180s with `why: "no-net"` both times because the mistyped line never
ran `VCCHK.BAT` at all.

### Why: `pace_s` is tuned for the Pi's own event loop, not the target's

`RegistryDriver.__init__` (`daemon/vcctrld.py`) threads a `pace` parameter
through every input call, defaulting to `DEFAULT_PACE_S` —
`capabilities.input.settings.pace_s`, 0.012s (12ms) — whose own comment
explains what it's actually calibrated against: *"USB4VC drains one event
per device per loop pass and sleeps 5 ms when idle; PS/2 wire time adds
~1 ms per byte."* That's a Pi-side constraint: how fast the USB4VC bridge
can drain its own event queue. It says nothing about how fast the DOS
*target's* BIOS keyboard ISR can service its own hardware buffer between
characters — and that half of the round trip is CPU-speed-dependent in a
way the Pi-side pacing has no visibility into. A 486DX2-50 servicing
IRQ1 has less headroom between keystrokes than a Pentium OverDrive at the
same nominal pace, so the same 12ms gap that was safe on the faster board
wasn't safe on the slower one — not deterministically (the very first
`send_file` job on this same 486 completed cleanly with the same pace), but
often enough to fail two file-transfer jobs in a row.

### The fix

Bumped `capabilities.input.settings.pace_s` from `0.012` to `0.04` in the
**deployed** daemon config (`/opt/vcctrl/vcctrl.yaml` on the daemon host,
not the git-tracked copy in this repo — see `vcctrl-repo-conventions` on
why these stay separate, real, host-local files) and restarted `vcctrld`
to pick it up. Every `send_file`/`get_file` job run against the same
486DX2-50 afterward — several, including one moving an 11MB `SONG.WAV` —
completed cleanly with no repeat of the corruption.

### Not yet fixed properly

The fix is a global, rig-wide pace bump, not a per-board one — it makes
every keystroke on this rig slower, including on faster boards where the
old pace was never a problem, and there is no code path that varies
`pace_s` by which CPU is currently installed. `RegistryDriver`'s `pace`
parameter already threads all the way from the daemon protocol layer
(`req.get("pace")`) down to the input backend, so a per-call or per-job
override is architecturally cheap — it just isn't wired to anything that
knows the installed CPU's tier today. Whoever next swaps this rig's CPU
should expect to re-tune `pace_s` by hand rather than have it happen
automatically, and should consider lowering it back down if the rig
returns to faster hardware, since a slower pace than necessary just makes
every typed command take longer for no benefit.

## 44. Pi 5's USB-C port drives a real USB HID keyboard+mouse gadget  [measured 2026-09-01]

This rig's own Pi 5 (`usb4vc`, kernel `6.18.39+rpt-rpi-2712`, plain Model B,
not a CM5) was tested as a second, independent keyboard/mouse source for a
Linux server target (`modernpc`), using the SoC's own `dwc2` USB-C
controller in peripheral/device mode -- a path that public reports as of
late 2024 described as unreliable on Pi 5 (`dwc2` probe failing with
`error -16`). On this kernel it did not reproduce.

`dtoverlay=dwc2,dr_mode=peripheral` was added under the existing `[pi5]`
section of `/boot/firmware/config.txt` (not `[all]`, and not the file's
existing `[cm5]` section, which is inert for a Model B). After a reboot,
`dwc2` probed cleanly every time (`EPs: 8, dedicated fifos, 4080 entries in
SPRAM`, no `-16`) and `/sys/class/udc/1000480000.usb` appeared. A composite
gadget was built by hand via configfs (`libcomposite`, one boot-protocol
keyboard function and one 4-byte relative-report mouse function), bound to
that UDC, and driven by writing raw HID reports directly to `/dev/hidg0`
and `/dev/hidg1`. A typed sentence and a 10-second mouse square both landed
correctly on a real host across the wire.

This was tested with a laptop as the USB host, not `modernpc` itself, over
the wiring the operator built for it: an externally-powered USB hub with
its upstream USB-A port plugged into the host machine, and a downstream
USB-A-to-USB-C cable back to the Pi -- meaning the Pi's own power now comes
from the hub backfeeding VBUS on that same cable, not from a direct PD
supply into the Pi's USB-C port. The two roles (power sink, gadget device)
can't coexist on one physical connector, so this wiring choice is load-
bearing for the whole rig, not just the new feature: if the hub can't
sustain the Pi 5's current draw under full load, the harness Pi loses
power, not just the gadget. `vcgencmd get_throttled` read `0x0` (no
undervoltage) through this testing, but that was idle/light-load testing,
not the rig's full working load (SPI to the STM32 board, VGA capture, the
room camera, and the HID gadget all running at once).

Reboot persistence was also checked, not assumed: after a clean reboot,
`dwc2` re-probed cleanly and the UDC reappeared, but the gadget itself did
not -- `libcomposite` and the configfs tree are not currently persisted by
anything, so `/dev/hidg0`/`/dev/hidg1` only exist after the setup script is
re-run by hand. That is expected at this stage (no boot-time service exists
yet) and is not itself a finding about `dwc2`'s reliability, but it means
the gadget cannot be relied on to survive a reboot unattended until that
service is built.

### Not yet fixed properly

No `vcctrld` integration exists yet -- this was a standalone configfs
spike, independent of the daemon, to de-risk the hardware question before
writing any capability code. No boot-time systemd unit exists either, so
the gadget does not currently survive a reboot without manual
intervention. And the power-path risk above has not been load-tested: it
has only been observed idle, not under the rig's full concurrent load.

## 45. A capture chip's first frame after opening the device can be a stale "no signal" placeholder, even with a real signal present  [measured 2026-09-01]

While validating a new HDMI capture dongle (MacroSilicon-chipset UVC
device, same family as this rig's existing VGA capture stick) for the
`modernpc` profile, a single `ffmpeg -frames:v 1` grab produced a solid
black frame. A second grab taken exactly one minute later, from a fresh
process, produced a byte-identical file (matching MD5). Two genuinely
different digitizations of even a static black desktop would not hash
identically -- a deterministic match across independent opens is the
signature of the chip's own synthetic "locked, no source" frame, not
absence of a picture.

The actual cause here was mundane (the laptop being used as the test
source had not been set to mirror/extend its display to the HDMI output,
confirmed and fixed by the operator) -- but the same signature reappeared
in a different shape after a reboot with the display genuinely mirrored:
the *first* frame grabbed immediately after opening the v4l2 device was
still the stale black placeholder, and a second grab taken 2 seconds later
from the same device returned a correct, real capture. The chip needs a
brief resync window after a fresh open before it reflects the actual
input, and a single-frame probe taken immediately on open cannot tell
"no signal" apart from "hasn't resynced yet."

This is the same hazard already named for this rig's VGA capture path (see
`black-frames-are-not-black-screens` in the operator's own notes) --
recorded here specifically because it reproduced on a *different* capture
chip being brought up for a *different* target, which means any future
video capability (this one included) needs to discard or retry the first
read after opening the device rather than trust it, not just the existing
one.

### Not yet fixed properly

No code exists yet for the `modernpc` profile's video capability, so
there is nothing to fix -- this is a design constraint to carry into that
capability when it is written (matching how the existing `VideoCapability`
already has to handle this for the VGA path).

## 46. Sharing vcctrld's process/GIL with modernpc-shaped I/O costs the primary target under a millisecond  [measured 2026-09-01]

Before designing a single-process, multi-profile `vcctrld` (which would put
`modernpc`'s video-capture and HID-gadget I/O in the same process/GIL as the
primary target's timing-sensitive PS/2 keystroke emission), measured
whether that sharing actually degrades timing, rather than assuming Python's
GIL-releasing I/O makes it a non-issue. Conditions: DOS target CPU is an
Intel 486DX2 ~66MHz (`vcctrl_sysinfo`), `capabilities.input.settings.pace_s`
is `0.04` (the value finding #43 tuned it to, not the original `0.012`).

Method: imported the real `vcctrld.py`/`Devices` (unmodified) into a
throwaway harness standing in for `vcctrld.service` for the duration of the
test only (both `vcctrld.service` and `vcctrld-modernpc.service` stopped
first, confirmed idle via `vcctrl_activity`/`vcctrl_lock_status` on both
profiles, restarted and verified active afterward). Instrumented
`Devices._tap` to time each keystroke's real wall-clock duration. Measured
880 keystrokes (10 trials x 88 chars) with nothing else running, then 880
more with two threads running IN THE SAME PROCESS: one continuously reading
real MJPEG frames from modernpc's actual HDMI capture dongle via a real
ffmpeg subprocess (the same pattern `VideoCapability` uses), one writing
HID-gadget-shaped reports to `/dev/hidg0`/`/dev/hidg1` at a light idle rate
plus periodic 20-report typing-shaped bursts -- deliberately including
concurrent "someone is using modernpc too" load, not just idle background
noise, since that is the actually risky moment for shared timing.

Baseline: mean 80.153ms, p95 80.190ms, p99 80.206ms, max 80.228ms per
keystroke (expected value is exactly `2 x pace_s` = 80ms; the near-zero
spread shows this Pi 5 paces almost perfectly with nothing competing for
it). Loaded: mean 80.205ms, p95 80.246ms, p99 80.308ms, max 80.669ms. The
worst measured degradation is +0.441ms (+0.55%) on the single slowest
keystroke, and the mean shift is +0.052ms -- two orders of magnitude
smaller than the ~28ms margin finding #43 needed to add to fix a real
corruption. A separate, real, visible check (a genuinely typed sentence,
recovered from the DOS screen after restarting the real service) showed no
dropped or garbled characters in either condition.

### Not yet fixed properly, and a real methodological gap to flag

N=10 trials/880 keystrokes per condition is enough to characterize the
*typical* jitter shape but not enough to rule out a rare, `#43`-style
probabilistic tail event -- that finding's own first repro attempt
succeeded before the fault started reproducing twice in a row. This result
supports proceeding to the single-process design; it does not prove the
tail risk is zero at any N. Separately: the intended behavioral check
(typing one long, distinctive marker string and reading it back
character-for-character) did not produce usable evidence -- the harness's
own jitter-measurement trials never sent Enter between them, so ~1800
characters accumulated on one COMMAND.COM input line before the marker was
even sent, past DOS's own input-buffer limit, which silently discards
excess keystrokes regardless of any daemon timing question. That is a
DOS-side buffer-length artifact in the test's own design, not a timing
finding, and a repeat of this measurement should clear the line (Enter or
Escape) between trials.

## 47. A Wemo answers ICMP and SSDP for minutes after its HTTP server has stopped answering at all  [measured 2026-09-01]

Adding a `wemo` power backend alongside `kasa-legacy`. Conditions, because
every number below is worthless without them: one Belkin Wemo **Insight**,
firmware `WeMo_WW_2.00.11532.PVT-OWRT-Insight`, on the lab LAN, probed from the
**control host** (not the daemon host) over wired Ethernet on the same /24 as
the device — no router hop, ~0.9 ms RTT. The plug's relay was **open** and its
Insight meter read **0 mW** throughout, so nothing measured here involved a
load, and nothing was switched.

### The protocol works and needs no library

`GET /setup.xml` and SOAP `POST /upnp/control/basicevent1` both answered
first time, no cloud account and no pairing:

| call | reply |
|---|---|
| `setup.xml` | `modelName Insight`, `deviceType urn:Belkin:device:insight:1`, firmware as above |
| `basicevent:1#GetBinaryState` | `<BinaryState>0</BinaryState>` |
| `basicevent:1#GetFriendlyName` | the plug's configured name |
| `insight:1#GetInsightParams` | `0`, then the counters: last-change epoch, on-for, on-today, on-total, timespan `1209600`, average power `9`, current power `0` mW, threshold `8000` |

That is the same shape as the Kasa path (finding #12): LAN only, stdlib only.

### Three reply quirks, each of which reads as a different bug

1. **`BinaryState` 8 means ON.** On an Insight it is "relay closed, load under
   the standby threshold" — a powered machine, idling. Anything that maps it
   to false reports a running target as powered off, which is the exact
   collapse `power_state`'s tri-state `on` exists to prevent.

2. **The state can arrive pipe-joined to the Insight counters**
   (`1|1788297732|0|...`). Compared whole against `"1"` it matches nothing and
   a healthy plug reads as unreachable.

3. **The reply's XML namespace can name the wrong service.** The
   `insight:1#GetInsightParams` reply above came back inside
   `<u:GetInsightParamsResponse xmlns:u="urn:Belkin:service:metainfo:1">` —
   *metainfo*, not *insight*. A namespace-aware parse of that document finds
   nothing. The backend therefore matches the **local tag name** and ignores
   the prefix entirely.

### The finding that changes how it must be polled

After a 200-thread TCP port scan of the device, **every** connection to 49153
was refused — while SSDP `M-SEARCH` kept answering from the same address, with
`LOCATION: http://<device>:49153/setup.xml` naming the exact port that was
refusing.

Characterised over a 180 s window, sampling every 4 s: ICMP echo succeeded on
**45/45** samples, TCP connect to 49152/49153/49154/49155 succeeded on
**0/45**. Every one of those failures was `ECONNREFUSED` — an active RST, not a
timeout, so the device's TCP stack was alive and answering and **nothing was
bound to the port**. A full 1–65535 sweep during the outage found no other
listening port on the device, so the server had not moved: it was gone.
It had recovered ~1 minute after the first such outage earlier the same hour,
then wedged again after a handful of ordinary requests. The outage
characterised above ran for **17.5 minutes** and then cleared with nothing
touching the device — no unplug, no reboot, no request that succeeded in
between. So the 3-minute window is a lower bound on the outage and not the
shape of it, and "it needs a physical power cycle" — which is what this looked
like at minute three, and what a person would reasonably have done next — was
about to be a wrong conclusion drawn from a true observation.

**What is established and what is not.** Established: the device can be fully
alive at the network layer, and advertising a port over SSDP, while that port
accepts nothing — for at least three minutes. Not established: the *cause*.
The first outage followed a heavy parallel scan, which makes the scan the
obvious suspect and is why the backend walks candidate ports strictly one at a
time; but the second outage followed only a handful of sequential requests, so
"the scan did it" does not survive its own evidence, and neither does "it is
just flaky under load". Do not carry either sentence forward as fact.

### What it costs the backend

- **The port is walked, not assumed** — `49153, 49152, 49154, 49155`, one at a
  time, with the winner cached per host. A pinned `port:` in config is obeyed
  and never second-guessed, because "the port you named is not answering" is a
  more useful sentence than "no Wemo anywhere".
- **An HTTP error ends the walk.** Something spoke SOAP back, so that *is* the
  device and its complaint is the finding. Only a connection that never landed
  moves on. Without the split, a plug returning 500 would be reported as
  absent from a port it is plainly sitting on.
- **`on_time_s` and `rssi` are null.** Kasa carries both inside the one
  `get_sysinfo` it already makes; Wemo needs a separate round trip for each,
  and nothing in this repo reads either field. Two extra requests per poll to
  a device with this availability record, to fill a field no consumer reads,
  is the wrong trade. Null means "this backend does not report it"; a zero
  on-time would have read as "just switched on".
- **`SetBinaryState` answering `Error` is not necessarily a failure.** This
  device is documented to answer that word when asked for the state it is
  already in — a no-op that `power cycle` walks into every time it starts
  from off. The backend does not trust the word in either direction: it reads
  the relay back and lets the hardware settle it. *That specific
  already-in-state case has not been reproduced on this rig* — nothing was
  switched — so it is handled defensively rather than confirmed, and the
  read-back is what makes the handling safe either way.

### The read path, proven through the backend and not just by hand

Everything above was gathered with `curl`, which proves the protocol and not
the code. `WemoPower.state()` was then run against the device the moment it
came back, and answered:

```
{'on': False, 'alias': 'RaspberryPi', 'model': 'Insight',
 'on_time_s': None, 'rssi': None, 'reason': None}     port cache: {<device>: 49153}
```

— the relay read live, the alias and model from `setup.xml`, the two
unavailable fields null rather than zero, and the port walk settling on 49153
and remembering it. (`alias` is the plug's own friendly name, which on this
device is a leftover label and not a description of what it feeds.)

### Not measured, and worth saying so

**No switching was performed.** `SetBinaryState` has never been sent to this
device from this repo, so the write path — `on`, `off`, `cycle`, and the
`Error`-means-already-in-that-state handling — is exercised only by
`tests/test_core.py::test_wemo_power_backend_reads_the_states_the_device_actually_sends`
over a fake transport. Every read above is real; nothing about real mains is.

## 48. The hid-gadget mouse is proven against `modernpc`, and software H.264 fits on the Pi 5  [measured 2026-09-02]

Two measurements taken while reviewing the multi-profile web KVM, filed
here because both settle a question the next piece of work depends on.

### The relative HID mouse reaches `modernpc` and is acted on

Nothing had recorded a cursor moving on `modernpc` from the daemon --
FINDINGS #44's "10-second mouse square" was raw `/dev/hidg1` writes with a
laptop as the host, not `vcctrld` against the real target. Conditions: one
`vcctrld` process serving both profiles (build `7dffed77`, the tree at
commit `6895cdc`), gadget `vcctrl-hid-km` bound to `1000480000.usb` with
`/sys/class/udc/1000480000.usb/state` reading `configured` and
`current_speed` `high-speed` (a host had enumerated it); `modernpc`'s HDMI
capture at 1920x1080, `analog: false`; modernpc's display blanked -- every
frame in the ring a flat 7/7/7 grey, 41,109 bytes per JPEG.

One `vcctrl_mouse_move dx=60 dy=0 profile=modernpc` over MCP (event seq 169,
`ok: true`, 40.1 ms). Within about seven seconds the ring went from
`video.locked` to a `video.frozen` event and the next raw frame was a lock
screen (photo wallpaper, a clock reading 9:41 AM), 166,035 bytes -- extrema
0..255 against the previous frame's 7..7, difference bounding box the full
1920x1080. A display that wakes on a HID mouse report is a host that
received and acted on that report. **The daemon-to-target mouse path works.**

What this does NOT show: clicks, drags, the wheel, or anything about
pointer precision -- one report, one wake-up. And the web page was not
involved at all: `daemon/kvm.html` has never forwarded pointer events
(`git log -S requestPointerLock -- daemon/kvm.html` and `-S "post('mouse_move'"`
both come back empty across the whole history), so "the mouse does not
work from the browser" is the absence of a feature, not a regression.

Two side findings from the same session, both real:

- `vcctrl_shot profile=modernpc` refused with "no picture: every frame in
  the window was a duplicate" on that flat-black screen. Duplicate frames
  are only evidence of no signal on an ANALOG capture (commit `0b8a051`
  fixed exactly this for the status line); the shot judgement still
  applies the analog rule to a digital source. `/frame.jpg?seq=` returned
  the frame fine.
- `/p/modernpc/state.json` reports `targets` as gateway2000's two boards.
  `Handler._route_profile()` binds `self.registry` per request but not the
  `CFG` proxy, so `configured_targets()` (and any other `CFG.optional`
  read on a web-request thread) resolves against the primary's config
  whatever the `/p/<name>/` prefix said.

### Software H.264 on this Pi 5, synthetic source

The Pi 5 has no hardware H.264 encoder (`v4l2-ctl --list-devices` shows
`rpi-hevc-dec` and `pispbe` only; the `h264_v4l2m2m` entry ffmpeg lists is
a wrapper with no device behind it on this board). So H.264 for the live
view means `libx264`, and the question was whether that fits beside the
daemon. Conditions: Raspberry Pi 5 Model B Rev 1.0, 4 cores, governor
`performance`, `vcctrld` running and idle-ish (load 0.26), everything
under `nice -n 19`; the source was `testsrc2` at 1920x1080/30fps encoded
once to an MJPEG file at `-q:v 4` (180 frames, 16.2 MB, ~90 KB a frame --
a LOW-ENTROPY synthetic picture, so every figure below is a best case, not
a desktop). `speed=` as ffmpeg reports it, one run each:

    MJPEG decode only, 1080p                                   6.46x realtime
    decode + libx264 ultrafast/zerolatency, 1080p, 2 threads   2.97x
    decode + libx264 ultrafast/zerolatency, 1080p, 4 threads   3.37x
    decode + scale to 1280x720 + libx264, 2 threads            4.77x

Read as cores: about 0.15 of a core to decode 1080p MJPEG, and roughly two
thirds of a core to decode-and-encode it at 30 fps on two threads. Real
desktop content (text, wallpaper, video) will cost more -- budget one to
one-and-a-half cores at 1080p30 and measure again on the real dongle
before trusting it, because FINDINGS #46's "under a millisecond" figure
for the primary's PS/2 timing was measured against modernpc-shaped I/O, not
against an encoder. Bandwidth today for comparison: that lock screen at
30 fps is ~40 Mbit/s of MJPEG; a 1080p desktop at x264 ultrafast sits
at 2-4 Mbit/s.
