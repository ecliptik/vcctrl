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
