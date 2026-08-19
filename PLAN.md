# vcctrl -- plan for Claude-driven control of the g2k DOS machine

Status 2026-08-18. Planning document. Everything marked **[measured]** was
verified on the live hardware while writing this; everything marked
**[predicted]** has not been.

The goal: Claude, running on the VM, drives the real g2k DOS box end to end --
types at it, sees its screen, delivers builds to it, and power-cycles it when it
wedges -- so real-hardware testing stops being gated on a human.

---

## 1. Architecture

The VM stays the brain. The Pi becomes a device server. This is the central
decision and it is worth stating plainly: the VM has the repo, the DJGPP
toolchain, the build and the analysis. Moving Claude to the Pi would mean moving
all of that too, or shuttling artifacts anyway. The Pi's job is to be the thing
with the cables in it.

```
  VM (claude)                        Pi 3 "usb4vc"                    g2k (DOS 6.22)
  +-----------------+  ssh over      +------------------+
  | repo, DJGPP,    |  tailscale/lan | vcctrld          |  USB4VC HAT
  | build, analysis |<-------------->| uinput kbd+mouse |------ SPI ---> PS/2 kbd + mouse
  | vcctrl CLI      |                | frame grabber    |<--- USB ------ VGA capture stick
  +-----------------+                | CF reader mount  |<--- USB ------ CF card (when swapped)
         |                           +------------------+
         |  smart plug (HTTP/API)                                  AC power
         +--------------------------------------------------------------->
         |  TCP (after Phase 4)                            PCI NE2000 + mTCP
         +--------------------------------------------------------------->
```

Four independent channels to the g2k, deliberately kept independent:

| channel | carries | needs DOS-side software? | always available? |
|---|---|---|---|
| PS/2 keystrokes | commands, recovery | no | **yes** |
| VGA capture | screen | no | yes (if it locks -- sec. 4) |
| TCP / mTCP | files both ways | yes | only when the harness console is up |
| smart plug | hard reset | no | yes |

**The robustness property to preserve:** the keystroke channel is blind and
low-bandwidth but needs nothing running on the DOS side, so it is the bootstrap
and recovery channel. The network channel is the payload channel but depends on
DOS-side software that a bad build can take down. Every design decision below
keeps keystrokes able to recover the network channel. If that invariant ever
breaks, the loop needs a human and a CF card again.

---

## 2. Input -- the PS/2 path  **[measured, works]**

### The finding this rests on

USB4VC needs no fork. Its scanner (`usb4vc_usb_scan.py:894`) enumerates with
`evdev.list_devices()`, which returns every `/dev/input/event*` node -- not just
USB ones. The `input_device_path = '/dev/input/by-path/'` constant at line 41 is
vestigial and unused by the scan. A kernel `uinput` virtual device is therefore
indistinguishable to USB4VC from a real USB keyboard.

Verified live on the Pi: created a uinput device, and USB4VC logged

    opened device: 0x1209 0xdead vcctrl virtual keyboard

within its 0.75 s rescan interval. **[measured]**

### Constraints read out of the USB4VC source

These are not style preferences -- violating them silently breaks the path:

- **Device name must not contain "motion"** (any case). Line 896 skips those.
- **Keyboard classification requires `KEY_ENTER` and `KEY_Y`** in the
  capability set (line 913). Declare the full key range you need plus those two.
- **Mouse classification requires `BTN_LEFT` and `EV_REL`** (line 911).
- **Do not declare gamepad buttons.** `check_is_gamepad()` (line 877) matches
  against `gamepad_event_code_name_list`; a hit reclassifies the device and
  routes its events down the gamepad path. Verified `BTN_LEFT`/`BTN_RIGHT`/
  `BTN_MIDDLE` are **not** in that list, so a normal mouse device is safe.
- **Devices must be held open for the daemon's lifetime.** USB4VC only opens
  devices it finds on a 0.75 s poll (line 946). A device created per-keystroke
  is invisible for up to 0.75 s, so the first keystrokes are silently lost.
  `vcctrld` opens both devices once at startup, waits for USB4VC to pick them
  up, and verifies before reporting ready.
- **Pace events at >= 10 ms.** USB4VC reads exactly one 16-byte event per device
  per loop pass (line 772) and sleeps 5 ms when idle (line 766). PS/2 wire time
  is roughly 1 ms per byte on top. 10 ms per event is the safe default; make it
  tunable, because game input timing may want tighter and the PS/2 board may
  not tolerate it.

### CLI surface

```
vcctrl type "CD \DOSKUTSU"      # literal text, shifted chars handled
vcctrl key enter esc f1          # named keys, in sequence
vcctrl hold left 800ms           # press, dwell, release -- what games need
vcctrl combo ctrl-alt-del        # soft reboot, free of charge
vcctrl mouse move +40 -12
vcctrl mouse click left
vcctrl input status              # is USB4VC still holding our devices?
```

`hold` with an explicit dwell is the primitive that makes gameplay testing
possible at all; a keystroke with no duration cannot walk Quote across a room.

`vcctrl input status` greps `usb4vc_debug_log.txt` for our vendor id and warns
on a matching `Device disappeared` line. Cheap, and it catches the failure mode
where the daemon is alive but USB4VC has dropped us.

---

## 2.1 The LED return channel -- input verification without video

**[fully measured 2026-08-19 -- both halves]** The DOS half settled itself
before a single keystroke was sent: the first `vcctrl status` after the g2k
booted returned `numlock: 1`, where it had been `0` with the box powered off.
That is the BIOS setting NumLock at POST, `0xED` travelling back up the PS/2
wire, the protocol board relaying it as SPI message 129, and `change_kb_led()`
writing it into our virtual device's sysfs node. Forward path confirmed twice
after that: Caps Lock 0 -> 1, then 1 -> 0, with the host answering faster than a
20 ms poll could take its first sample.

Sec. 5.4 identifies a circularity: verifying that typing works means reading the
console back, and the console is only readable if capture is working. So a
silent failure is ambiguous between "keystrokes are not arriving" and "capture
cannot lock" -- two unrelated faults with one symptom.

There is a way out, and it needs neither video nor the network.

PS/2 is bidirectional. When Caps/Num/Scroll Lock state changes, the host's BIOS
keyboard handler issues the PS/2 `0xED` set-LEDs command back down the wire.
USB4VC already handles this: `SPI_MISO_MSG_TYPE_KB_LED_REQUEST = 129` is an
inbound message from the protocol board, and `change_kb_led()`
(`usb4vc_usb_scan.py:694`) writes the received state into `/sys/class/leds`.

The full loop is therefore:

```
uinput KEY_CAPSLOCK -> USB4VC -> SPI -> protocol board -> PS/2 -> DOS BIOS
   |                                                                  |
   |                                            BIOS sends 0xED set-LEDs
   |                                                                  v
sysfs brightness <- change_kb_led() <- SPI msg 129 <- protocol board <-+
```

**Verified on the Pi side:** a uinput device that declares `EV_LED` with
`LED_CAPSL`/`LED_NUML`/`LED_SCROLLL` gets kernel-created sysfs nodes --
`input11::capslock`, `input11::numlock`, `input11::scrolllock` appeared for the
test device. `change_kb_led()` writes to every `*capslock*` node it finds, so it
writes to ours. The state is then readable either from sysfs or as an `EV_LED`
event on our own uinput fd.

**What this buys:**

- **A non-video, non-network proof that the DOS machine received and processed
  a keystroke.** This is the only such witness available before the NIC lands,
  and it breaks the phase 1 circularity outright.
- **A liveness detector.** After a warm boot, lock-key response tells the
  harness that the BIOS keyboard handler is alive again.

**But liveness is not readiness, and on this box the gap is large.** The BIOS
keyboard handler is alive during POST -- long before the machine can accept a
command. `AUTOEXEC.BAT` still has real work to do after that point:

```
LH C:\UNIVBE\UNIVBE.EXE
LH C:\WINDOWS\SMARTDRV.EXE 4096 1024 C A-
LH C:\DRIVERS\CTMOUSE.EXE
LH DOSKEY
```

UniVBE plus a 4 MB SMARTDRV cache on a DX2-50 is a meaningful stretch, and
CONFIG.SYS runs before any of it. A harness treating first-LED-response as
readiness would type into a machine still loading drivers; those keystrokes sit
in the BIOS buffer and get consumed unpredictably later -- landing mangled at
the prompt, or eaten by a driver. The same objection kills NumLock-at-POST as a
free boot signal: it fires at the wrong end of the boot.

So the LED channel is a good **liveness** signal and a poor **readiness**
signal. Necessary, not sufficient.

**Constraints:**

- **Never mid-sweep.** Caps Lock is a keystroke; sending one during a cell
  injects it into the running game (sec. 5.1). This is a prompt-time and
  boot-time instrument only, never a liveness probe during a run.
- **Restore the state.** Toggling Caps Lock twice returns it, but the harness
  should read the state rather than assume it, so a probe does not leave the
  machine in shift-inverted mode for whatever types next.
- **The DOS half is unverified.** Issuing `0xED` on lock-key changes is
  standard AT/PS-2 BIOS behaviour, but standard is not measured. Confirm it
  with the first powered-on test, and if this box does not do it, the channel
  simply is not available and phase 1 falls back to the ambiguous case.
- **Never mid-sweep**, for two reasons. Caps Lock is a keystroke and would
  inject into a running cell (sec. 5.1). Beyond that, once the game is running
  SDL3's DOS backend owns the keyboard, and what it does with lock keys and LED
  state is simply unknown -- nobody has looked, and nothing in the port depends
  on it. Mid-sweep the channel is not just dangerous, its semantics are
  undefined.

### Turning it into a real readiness signal: send `0xED` deliberately

Both weaknesses above -- liveness-not-readiness, and the unmeasured dependence
on BIOS lock-key handling -- have the same fix: **do not rely on the BIOS
issuing `0xED`. Issue it directly.**

A small hand-assembled COM that talks to the keyboard controller -- poll status
at port `0x64`, write `0xED` to `0x60`, write the LED bitmask -- puts exactly
the byte the protocol board already relays as SPI message 129 onto the wire,
deterministically. The Pi-side half is unchanged. This is the same class of
artifact as the existing `VGACAP` COM files.

Two things it buys:

- **A true readiness edge.** As the last line of `AUTOEXEC.BAT`, an LED change
  means CONFIG.SYS is done, UniVBE is resident, SMARTDRV is up and the prompt
  is live. No video, no network, no BIOS quirk dependence -- and a genuine
  DOS-ready edge rather than a POST edge.
- **A discriminator for the caveat above.** If Caps Lock produces no LED change
  but the explicit-`0xED` COM does, the relay path is healthy and only BIOS
  lock-key behaviour is absent. If neither works, the relay is broken. Without
  the COM a null result cannot distinguish those -- the same class of ambiguity
  phase 1 exists to remove.

**This needs an operator decision.** `AUTOEXEC.BAT` lives on the CF outside the
QA payload, so the edit is the operator's, and it affects every boot including
manual ones. The COM itself is useful without the AUTOEXEC change -- run
manually at the prompt it still serves as the discriminator -- so the two can be
decided separately.

---

## 3. Power

Escalation ladder, tried in order, each step only after the previous fails to
produce a screen change:

0. **Classify first.** A timeout is not necessarily a wedge -- see sec. 5.1.
   A stale Organya cache legitimately runs 3-4x long and is *not* fixed by any
   step below. Never enter this ladder on a timeout alone.
1. `esc` / `ctrl-c` -- application-level
2. `ctrl-alt-del` -- **only works at the DOS prompt, NOT while the game runs**
   (see below). Useless for the case that matters most.
3. **GPIO pulse on the motherboard reset header** -- the real recovery step
4. smart plug off / 5 s / on -- for hangs a warm reset cannot clear
5. page the human

### Ctrl-Alt-Del does not work while the game is running  **[measured]**

Sent `ctrl-alt-del` with DOSKUTSU on its title screen. The machine did not
reboot: the title screen was still up 60 s later, the cursor still responded to
arrow keys, and the operator confirmed independently that **the PC speaker POST
beep never sounded**. Input was alive; the chord was swallowed.

Cause: SDL3's DOS backend hooks INT 09h and owns the keyboard, so the chord
never reaches the BIOS reset handler. This is the doskutsu session's "SDL owns
the keyboard, lock-key and chord semantics are undefined" caveat turning out to
be worse than undefined -- it is reliably *absent*.

**Consequence: the escalation ladder had no working recovery step for a hung
cell.** A wedged game is exactly when a reboot is needed, and exactly when
Ctrl-Alt-Del cannot deliver one. This elevates hardware reset from a
nice-to-have to the load-bearing recovery mechanism.

Note also that the LED channel is a poor *reboot* detector when NumLock is
already 1, because POST sets it to 1 again and a 2 s poll misses the
transient. To detect a reboot, set **Caps Lock on first** -- POST clears it, so
the edge is unambiguous.

### GPIO to the motherboard reset header -- the recommended fix

The front-panel RESET header is a momentary short to ground. Driving it from
the Pi gives a true hardware reset, independent of anything software is doing,
which is precisely what the Ctrl-Alt-Del failure leaves missing.

**Do not wire a GPIO directly to the header.** That line is usually pulled up
to +5 V and Pi GPIOs are 3.3 V and not 5 V tolerant. Use an isolator:

- **Opto-isolator (preferred, ~$0.50):** PC817 or similar. Pi GPIO -> ~330 ohm
  -> LED anode, LED cathode -> Pi ground; transistor side across the two reset
  pins. Full galvanic isolation, no shared ground with the g2k, and it cannot
  damage either machine if something is miswired.
- **N-channel MOSFET / NPN:** cheaper in parts count but shares ground with the
  g2k, which is worse in a rig where the two boxes are separately powered.

Pulse for ~200 ms, then release -- that is a button press.

**Free BCM pins** (USB4VC claims 2, 3, 8, 9, 10, 11, 16, 19, 20, 21, 22, 25,
26, 27 for SPI, I2C, buttons and board control): **5, 6, 12, 13, 17, 23** are
clear and away from the HAT's cluster.

**Keep the smart plug as well.** Reset and power are not redundant: a warm reset
cannot clear a hang where a device is holding the bus, and cannot recover from a
PSU or drive fault. Reset is the fast primary; power is the nuclear fallback.

Retries of the same sweep are capped regardless of how the fault classified.

Steps 1-3 are all Claude's, which is what makes unattended overnight runs
possible. **Open item: make/model and control API of the smart plug.**

---

## 4. Video -- RESOLVED for ViRGE/Cirrus, still open for Mach64  **[measured 2026-08-19]**

### The result

    baseline, DOS text mode 03h (720x400 @70)  YMIN=7   YMAX=7        flat, no lock
    after C:\VGACAP\MODE12 (640x480 @60)       YMIN=0   YMAX=207-210  LOCKED
    DOSKUTSU.EXE running, its own 320x240      YMIN=0   YMAX=255      LOCKED, full range

The double-scan argument held: 320x240 emits 480 physical lines at ~31.5 kHz /
60 Hz, the same signal class as mode 12h. **The engine's pinned mode is
capturable as-is on the ViRGE, so the conditional mode-pin patch is not needed
for the ViRGE or the Cirrus** and the render path stays unchanged.

**Correction, and it matters:** that conclusion does NOT extend to the Mach64,
and the Mach64 is the card carrying the one open visual defect (backdrop missing
its right column and bottom row). Under UniVBE that card runs **512x384** --
proven by centring offsets `@96,72` = (512-320)/2, (384-240)/2 -- and by the
same mechanism that predicts 320x240 locks, 512x384 cannot be double-scanned and
must run near 77 Hz, back in the failing band. The operator runs UniVBE on all
three cards, so the M64VBE 320x240 escape route is not in use.

So: **capture works on the two cards with no visual defect, and is still
predicted to fail on the one card that has one.** Test the Mach64 before writing
the pin patch off -- it needs a physical card swap. If the Mach64 does fail, the
conditional pin forcing 640x480 is the only known route to seeing that defect,
and 640x480 still centres 320x240 (at offset 160,120 instead of 96,72) so it
still exercises the centred path the defect lives in.

### Original analysis, retained for context  **[was: predicted]**

Per the doskutsu session's `docs/internal/VGA-CAPTURE-FINDINGS.md`: the
MACROSILICON stick's firmware mode table is 60 Hz-only. DOS text mode
(720x400 @70 Hz) never locks -- exhaustively verified. BIOS mode 12h
(640x480 @60) is the only confirmed-capturable mode.

**Nobody has confirmed the stick locks on the mode the game actually runs.** On
the current ViRGE/DX target that is UniVBE OEM mode `0x01F8`, 320x240 8bpp.
The prediction is that it locks, because VGA low-res modes are double-scanned
and emit 480 physical lines at ~31.5 kHz / 60 Hz -- the same signal class as
mode 12h. Predicted, not measured.

**This gates the entire video half of the harness and is the first thing to
run** once the stick is on the Pi and the game is up:

    ffmpeg -f v4l2 -input_format mjpeg -video_size 640x480 -i /dev/videoN \
      -frames:v 30 -vf signalstats,metadata=mode=print -f null - 2>&1 \
      | grep -oE 'YM(IN|AX)=[0-9]*' | sort -u

YMIN and YMAX diverging = real picture. Flat `YMIN=YMAX=7` = no lock.

### The Mach64 case is two questions, not one

Found via the read-only `AUTOEXEC.BAT` diff (sec. 5.5). The card carries a
`MACH64` boot profile that loads **M64VBE instead of UniVBE**, and M64VBE
exposes 320x240 8bpp as mode `0x0212`, where UniVBE's mode list for that chip
starts at 512x384.

Every logged Mach64 round -- G, H, I, K -- reports
`oem_string='Universal VESA VBE 6.70'`. **No logged Mach64 run has ever used the
M64VBE profile that exists specifically to give that card a native 320x240.**

So:

| boot profile | mode | lock prediction |
|---|---|---|
| UniVBE (all banked rounds) | 512x384, cannot double-scan, ~77 Hz | **fails** |
| `MACH64` (M64VBE, never logged) | 320x240, double-scanned ~60 Hz | **locks** |

**Resolved 2026-08-19: the operator uses UniVBE with all three cards** (Cirrus,
ViRGE, Mach64) and does not need the M64VBE profile. So the split collapses back
to one question -- the Mach64 case is the UniVBE 512x384 case, the one predicted
**not** to lock. The `MACH64` profile remains on the card but is out of use and
out of the repo snapshot.

This also confirms that "every logged Mach64 round used UniVBE" (rounds G, H, I,
K) was intentional rather than accidental.

If it does not lock, the fallback is a code change, not configuration: the
engine pins its mode via `SDL_HINT_DOS_PIN_WINDOW_TO_NATIVE_MODE` at
`SDL_HINT_OVERRIDE` priority, so no environment variable can defeat it. The
doskutsu session has offered to make that pin conditional, letting the engine's
own 640x480 request through. Two caveats they flagged: 4x the pixels, so it must
never be enabled for a performance-measurement cell, and it changes the render
path, so it is a visual-QA tool only. **Do not queue that patch until the test
above has actually failed.**

### Design, assuming it locks

- `vcctrl shot [--out f.jpg]` -- one-shot ffmpeg grab on the Pi, JPEG pulled to
  the VM, where Claude reads it directly.
- **Discard the first N frames.** The stick needs time to re-lock after any mode
  change; the first frames after a mode set are garbage or black.
- **The device is single-open.** VLC must be closed or ffmpeg gets EBUSY. Take a
  lock file on the Pi so two shots cannot collide.
- **Capture blacks out between cells -- SOLVED in payload r14.** On exit the
  game restores text mode 03h (720x400 @70), which does not lock. Keystrokes
  cannot fix this (sec. 5.1: the BAT is still executing, no shell is reading a
  command line), so it had to be baked into the sweep BATs. It now is:
  `VGACAP\MODE12.COM` is invoked on the line immediately after each
  `DOSKUTSU.EXE` and never before it, gated on `DKTCAP=1`, and `IF EXIST`-
  guarded so a machine without a `VGACAP` directory does not throw an error
  between every cell. See sec. 5.3.
- **Mode 12h console text is slow** -- planar 4-bit-plane, so BIOS TTY does
  read-modify-write per glyph and `CLS`/`ECHO` visibly crawl. Do not put a timed
  bracket around console I/O while in mode 12h.

### Deliberately not doing

- **Audio DOES capture. An earlier claim here that it could not was wrong.**
  See sec. 4.2. The `0x0602` descriptor was treated as proof of "no analog
  input"; it is a vendor firmware label, not a description of the physical
  jack. Re-cabling the sound card's line-out into the stick produced signal
  immediately. VGA carries no
  audio. Audio QA needs separate hardware (e.g. a Behringer UCA202) and is out
  of scope here.
- **No OCR initially.** Once the NIC lands, logs come back over the network as
  text and OCR is mostly pointless. If it is ever needed, mode 12h console text
  is a fixed 8x16 VGA font on a fixed grid, so template matching would be
  near-exact and cheap -- much better than a general OCR engine.

### VGA split

Decision: swap the cable by hand. Monitor when you are working, capture stick
when Claude is driving. Costs nothing and the operator requirement that the
monitor keep working is satisfied.

Worth noting: after Phase 4, logs and results come back over TCP, so capture is
mainly for visual QA and for seeing a wedged box -- which lowers how much the
hand-swap actually costs. If it becomes annoying, a **powered** (not passive)
VGA splitter is about $15. Passive Y-splitters halve drive strength and smear
sync, which on a stick this fussy about lock is a bad trade.

---

## 4.1 Capture is not trustworthy frame-by-frame

**[measured]** The capture path emits **intermittent flat-black frames while
correctly locked**. A 90-frame burst showed frames 1-2 at mean brightness 7.0
and the remaining 88 at ~59. Taking "the last frame" is therefore unreliable --
it produced a black grab twice, and briefly led to the wrong conclusion that the
game had exited.

`vcctrl shot` takes a burst and selects the brightest non-flat frame.

**A black frame is not evidence of a black screen.** This would otherwise have
caused false wedge detection in the autonomous runner (sec. 5.1), where "the
screen went black" is exactly the signal being watched for.

### Regional artifacts need a stronger protocol than whole-frame selection

Brightest-non-flat handles a whole black frame. It does **not** protect a
judgement about part of a frame -- and the one open visual defect is precisely
that shape: a missing right column and bottom row, i.e. a localised absence of
picture, on a path that intermittently emits absence of picture.

Any regional judgement therefore needs all three of:

1. **Multiple bursts separated in time**, not one burst.
2. **The same region compared across frames within a burst.** A real defect is
   stable frame to frame; a capture artifact is not.
3. **A positive control inside the same frame.** The centred image has known
   image content and known-black margins around it. A frame in which the margin
   and the suspect region are indistinguishable is a frame that says nothing,
   and must be discarded rather than scored.

Point 3 is the same shape as the control probes in the doskutsu readback
instrumentation, for the same reason: **a null result has to be distinguishable
from a broken measurement.**

---

## 4.2 Audio capture works -- and the reasoning that said otherwise

**[measured 2026-08-19]**

| | mean | max |
|---|---|---|
| silence baseline | -65.6 dB | -52.7 dB |
| game, wrong boot profile, wrong cabling | -63.9 dB | -52.2 dB |
| **game, PGSB profile, line-out cabled in** | **-30.8 dB** | **-18.4 dB** |

35 dB above noise floor, and tonal rather than hum: dominant frequencies 879,
434, 141, 152 Hz (879/434 is close to a 2:1 octave), peak-to-mean spectral ratio
**32.8** where broadband noise sits at 1-3. Operator confirmed by ear that it is
the Cave Story title music.

### The error, kept because the shape of it matters

This plan previously asserted audio capture was **structurally impossible**,
reasoning from the USB descriptor: the only audio input terminal is `0x0602`
Digital Audio Interface, therefore no analog input exists, therefore no cable
change could ever help.

Every step was wrong in the same way -- a *label* was promoted to a *proof*.
`0x0602` is what the vendor's firmware reports, inherited from HDMI variants of
the same chip; it does not describe the physical jack. A VGA-only device has no
HDMI to embed audio in, which should have been the tell that the terminal had to
be fed by something else.

The failure mode worth remembering: **"structurally impossible" is a claim that
stops investigation.** It was also stated as *stronger* than the measurement
that supported it, on the grounds that a measurement "invites trying another
cable forever". Trying another cable was exactly the right move, and the
argument was constructed to rule it out.

Two things had to be fixed together, which is why single-variable testing missed
it: the boot profile was `VIBRAUSB` (no Vibra16 in the box, PicoGUS in USB mode,
so no DAC at all) **and** the line-out was not cabled to the stick's input.
Fixing either alone still yields silence.

### The `wiimidi` question -- my concern was real but narrower than I feared

An A/B on the live machine showed `SDL_HINT_DOSKUTSU_AUDIO_MIDI_SOURCE=wiimidi`,
set globally by the card's `AUTOEXEC.BAT`, materially changes the output:
`mean -31.3 dB` with it set, `mean -25.4 dB / max -13.6 dB` cleared. So the
variable does reach the engine.

I worried this meant banked results had been hearing MIDI where Organya was
expected. **It does not.** `CLRENV` clears both spellings (lines 21-22), and
every measured cell calls `CALL CLRENV` before running -- verified across the
sweeps and the individual audio cells (`G41`, `G51`, `G17`, `G22`, `C4` once
each; `VB.BAT` six times, once per cell), none of which set `MIDI_SOURCE`
themselves.

So the true statement is narrower: **manual play on this card runs with
`wiimidi` pinned; every measured cell does not.** That is a config-vs-expectation
mismatch for the operator, not a data-integrity problem for the matrix -- and it
cuts the way that is easy to get backwards. If something is heard in manual play
and cannot be reproduced in a cell, this is the first thing to suspect.
Removing the line from `AUTOEXEC` would close the gap; that is an operator
decision.

Note this is the second time `CLRENV` has been the thing protecting the matrix
-- it also clears `AUDIO_SB_FORCE_8BIT`, which is why the T3/T4 drift (sec.
5.5a) could not reach a cell either. That file does more load-bearing work than
its 204 dull lines suggest, and should not be "simplified".

### Consequences

- **Sec. 5.2's "the ear cells stay human, permanently" is withdrawn.** It was
  built on the false premise. Whether the audio sweeps can be automated is now
  an open and promising question rather than a closed one.
- The doskutsu session excluded `VB.BAT` and ~20 audio cells from `DKTCAP` and
  was advised to deny-list the listening sweeps, partly on this reasoning. That
  is theirs to re-decide with the corrected facts.
- Still unproven: that captured audio can be *judged* automatically. Capture
  proves sound exists. "Is this cell silent when it should not be" is now
  answerable; "does this cell sound correct" is not yet.
- Headroom not yet characterised on loud SFX; -13.6 dB peak observed on one
  configuration, so clipping is plausible and unmeasured.

---

## 5. Files -- retiring the CF swap

### Today

Claude builds a tarball on the VM, an operator runs a script on the laptop that
scp's it over, extracts to the mounted CF, verifies shas and BAT CRLF/ASCII,
unmounts; the operator carries the card to the g2k, boots, runs BATs; logs land
on the CF; the card comes back and a logback script scp's them to the VM. Two
human touchpoints and a physical carry per iteration.

### Phase 3 -- CF reader on the Pi

Moving the reader to the Pi lets Claude own mount, copy, sha-verify, CRLF/ASCII
gate and unmount directly, and retires the laptop from the loop entirely. The
physical carry remains. This is a strict improvement and it is worth doing even
though Phase 4 supersedes it -- because Phase 3 stays the recovery path forever.

The existing gates from `scripts/wave-iter-install-template.sh` port across
unchanged and must be kept: sha per binary, CRLF on every `.BAT`, ASCII-only,
sync before unmount.

### Phase 4 -- the NIC is ALREADY INSTALLED AND PROVISIONED

**Superseded 2026-08-19.** No card needs buying. The g2k already has an **Intel
PRO/100 PCI** (`8086:1229`, 8255x class), slot 7, **IRQ 10** (no collision with
the SB16 at IRQ 5), port FCC0, MAC `00:00:5E:00:53:02`, linking at 100 Mbps
full duplex. Brought up and verified on hardware in June 2026 by the g2k config
session.

And `C:\NET\` **is on the card** -- confirmed by reading the live machine:
`ODIPKT.COM`, `LSL.COM`, `E100BODI.COM`, `NET.CFG` all present, dated 06-23-26.
That resolves the g2k session's own open question; they could not confirm it
because the card was not mounted on their side.

Working stack -- Novell ODI plus a packet-driver shim, all plain `.COM` TSRs
from `AUTOEXEC.BAT`, needing nothing in `CONFIG.SYS` and profile-agnostic across
the boot menu:

```
CD \NET
C:\NET\LSL.COM
C:\NET\E100BODI.COM
C:\NET\ODIPKT.COM 0 0x7E
```

Three gotchas that cost that session real time, recorded so nobody re-walks
them:

- **`ODIPKT` takes logical board 0, not 1**, even though `E100BODI` prints
  "Board 1". Board 1 fails with "Cannot get MLID control entry".
- **Packet vector is `0x7E`, not the usual `0x60`** -- `0x60` was contended by
  sound TSRs on this machine. `TCP.CFG` must carry `packetint 0x7E`, pointed at
  by `SET MTCPCFG=C:\MTCP\TCP.CFG`. If `PING` says "Could not setup packet
  driver", run `PKTTOOL.EXE scan` -- it reports where ODIPKT actually installed.
- **`NET.CFG` must specify `Frame ETHERNET_II`** under `Link Driver E100BODI`.

Dead ends, do not retry on this card: the native `E100PKT` packet driver (v0.2
and v0.3) hangs ~15 minutes bit-banging the EEPROM then freezes; Crynwr
`e100bpkt` is 82557-only; and the whole NDIS2 path loads clean but fails
`NETBIND` with "Error 45 Unable to bind" under every `PROTOCOL.INI` variant.

### The direction constraint that changes the design

**mTCP FTP is a client only.** The DOS side initiates; nothing can be pushed to
the g2k unsolicited. So the flow is not "Claude uploads to a server on DOS" as
sec. 5 originally assumed -- it is:

1. Linux side serves the payload (`scripts/serve.sh`, vendored `pyftpdlib`, on
   192.0.2.10 control port 2121, login `USER/PASSWORD_FROM_ENV`).
2. **The harness types `GET.BAT` at the DOS prompt** to make the machine pull.
3. `PUT.BAT` pushes logs back the same way.

This suits the harness well, because the prompt is one of the two reliable
keystroke injection points (sec. 5.1). It does mean the network channel can
only be driven from the prompt, never mid-sweep -- which was already true of
everything else.

`LOADPKT.BAT` / `UNLOADPKT.BAT` exist so a measured run can be network-TSR-free,
preserving the conventional-memory picture the whole fps matrix was taken under.

Static IP in the g2k docs is 192.0.2.117, gateway/nameserver 192.0.2.1.

Reference bundle: `/tmp/g2k-net-2026-08-18.tar.gz` on this host, sha256
`d321c02e5b83a164f8f0a89d83d0d4d63894904eac10ab4860e8f3bb904e55cb`, containing
`GATEWAY2000-DOS-NETWORKING.md` plus the drivers, mTCP suite and both sides'
scripts.

**Still unverified:** whether `C:\MTCP\` exists on the card alongside
`C:\NET\`, and whether the AUTOEXEC actually loads the stack at boot -- the
`SET` dump showed no `MTCPCFG`, so it currently does not.

### Original plan, superseded

Card: an RTL8029AS PCI NE2000 clone (~$10), which has a solid DOS packet driver.
The g2k is a Socket 3 Anigma LP4IP1 with PCI, so it fits.

Flow, with the game **not** running:

1. AUTOEXEC drops the box into a harness console with the packet driver and
   mTCP `FTPSRV` up.
2. Claude uploads the build over FTP.
3. Claude sends keystrokes to quit FTPSRV, unload the packet driver, and run
   the cell.
4. Cell finishes, control returns to the console, packet driver and FTPSRV come
   back up.
5. Claude pulls the logs.

**Never run the packet driver and the game concurrently.** Conventional memory
on this box is the scarcest resource in the project and the `[VIBRA]` profile's
layout is not to be improvised against. Loading and unloading around the cell
costs a few seconds and keeps the game's memory picture byte-identical to what
every existing measurement was taken under -- which also means fps numbers stay
comparable to the whole historical matrix.

Risks to check before buying:

- **IRQ contention.** Vibra16S is on IRQ 5, PicoGUS is present, and this is a
  486-era board with limited free IRQs. Confirm a free IRQ (10/11 typically) and
  that the NIC's PCI IRQ routing does not collide.
- **A free PCI slot** alongside the ViRGE/DX.
- **Recovery.** A bad build that hangs before the console comes back leaves no
  network channel. Ctrl-Alt-Del plus keystrokes must always be able to reach a
  known-good state, so the CF needs a boot path -- a `CONFIG.SYS` menu entry
  with no autorun -- that comes up bare. That entry is the thing standing
  between "Claude recovers it" and "someone drives over and swaps a card".

---

## 5.1 The DOS-side harness contract

Supplied by the doskutsu session. These are the facts the harness has to be
built against; guessing at them was explicitly warned off, and one of them
already caused a silent failure this week.

### Keystroke injection is only reliable at two points

**This sets the granularity of autonomy: the harness drives sweeps, not cells.**

Inside a sweep there is no console prompt. When a cell's `DOSKUTSU.EXE` exits,
control returns to the still-executing BAT, which proceeds straight to the next
cell. Keystrokes injected there land in the BIOS keyboard buffer and are either
consumed by the next `PAUSE` -- skipping a wait that was wanted -- or leak into
the next cell's game process.

Reliable injection points, and there are exactly two:

1. The single `PAUSE` at the top of a sweep. One keypress starts it.
2. The real command prompt between sweeps, after a BAT returns.

Verified against every BAT on the payload, not from memory: `QA.BAT` has **zero**
`PAUSE` (it sets machine context and returns straight to the prompt, so it needs
no keypress), and every benchmark sweep -- `RB`, `WC`, `MINE`, `LEV`, `ADIAG`,
`DEEP`, `VB`, `VIDKS`, `VIDM` -- has **exactly one**, at the top, before any cell
runs.

**`RECORD.BAT` is the exception and must be on a hard deny-list.** It has two
`PAUSE` calls because it is the TAS reel-recording tool, inherently interactive
-- a human is playing the route being recorded. An autonomous runner must never
launch it. Encoding this as a deny-list rather than a comment means the failure
mode is a refusal instead of an inexplicable hang halfway through a night.

So a Claude-driven run looks like: boot -> prompt -> `QA <n>` -> prompt ->
`SET DKTCAP=1` (once per boot, sec. 5.3) -> `<SWEEP> <n>` -> one keypress at
the `PAUSE` -> wait out the whole sweep -> back at the prompt. Everything finer-grained than a sweep has to be expressed
in the BAT, not injected.

**Consequence for liveness detection, and it is not obvious:** because any
keystroke sent mid-sweep corrupts the run, the harness must never poke the
keyboard to ask "are you still alive?". Liveness during a sweep can only be
judged from channels that do not write to the machine. This is a second,
independent reason the eyes matter (sec. 4).

Of the two available channels, **wall-clock timeout is the primary and
frame-delta is only a secondary**, which is the opposite of what it looks like
at first glance:

- **Timeout has a usable prior**, and it is stronger than expected. Cells are
  tick-driven rather than wall-clock-driven, so cross-CPU scaling is only about
  **1.25x** from the POD-83 to the DX2-50 -- far less than the ~1.8x fps
  spread. Inter-cell overhead is nearly constant at ~10 s per cell (dominated
  by a deliberate banner delay the BATs set, not by anything CPU-bound), so it
  scales with cell *count*, not machine speed. **A single per-sweep timeout set
  against the slowest machine is therefore safe -- no per-CPU table needed.**

Measured spans, first cell start to last cell end, derived from rounds M/N/O
logs. Excludes the `PAUSE` and pre-sweep sound setup:

| sweep | cells | POD-83 | Am5x86-133 | DX2-66 | DX2-50 | harness timeout |
|---|---|---|---|---|---|---|
| `RB` | 4 | 9m11s | 9m28s | 10m27s | 11m31s | **23 min** |
| `WC` | 3 | 7m11s | - | - | - | **18 min** |
| `LEV` | 3 | 6m54s | - | - | - | **17 min** |
| `DEEP` | 2 | 6m40s | - | - | - | **17 min** |
| `MINE` | 2 | 4m49s | - | - | - | **12 min** |

Only `RB` has been run on all four CPUs. To derive a missing cell, multiply the
POD figure by ~1.14 (DX2-66) or ~1.25 (DX2-50).

`ADIAG`, `TAB` and `FINE` were measured separately, on the DX2-66, in rounds
I/J/K:

| sweep | cells | DX2-66 measured | POD-83 ~ | DX2-50 ~ | harness timeout |
|---|---|---|---|---|---|
| `ADIAG` | 4 | 10m16s | 9m00s | 11m15s | **22 min** |
| `TAB` | 3 | 7m59s | 7m00s | 8m45s | **18 min** |
| `FINE` | 2 | 5m25s | 4m45s | 5m56s | **12 min** |

All eight `DKTCAP`-enabled sweeps therefore have a measured basis for a timeout.

**The cross-check matters more than the numbers.** These came from different
rounds, different binaries and a different CPU than the table above, so they
independently test the duration model rather than restating it -- and it holds:

- Overhead was 42s / 29s / 15s for 4 / 3 / 2 cells: the predicted ~10 s per
  cell, and it did not move with the machine.
- `ADIAG` (4 cells, DX2-66) 10m16s against `RB` (4 cells, DX2-66) 10m27s --
  two unrelated 4-cell sweeps within 11 seconds.
- `FINE` (2 cells, DX2-66) 5m25s against `MINE` (2 cells, POD) scaled to
  ~5m30s -- within 5 seconds.

So sweep duration is essentially a function of cell count and CPU, near-
independent of what the cells actually do. Measurement on older binaries does
not weaken this: cells are tick-driven against the same `QA.TAS` reel with the
same tick budget, so wall-clock is set by tick count and CPU rather than by the
code under test. `RB` on the POD held at 9m11s across a binary change between
rounds M and O, which is that claim measured directly. Add the harness's own pre-
keypress wait on top, and note `RB` runs `pgusinit` mode switches before cell 1
and between some cells -- a few seconds each.
- **Frame-delta alone would produce false wedges.** A cell can legitimately
  hold a near-static frame for a long stretch -- the TAS reel pauses, and an
  Organya cold-render can sit on a loading screen for minutes when a cache is
  stale. Use frame-delta only with a generous window, and to *corroborate* a
  timeout rather than to replace it.

Because timeout carries the load, a capture failure degrades wedge detection
but does not eliminate it.

### A timeout is not necessarily a wedge -- classify before escalating

**This defeats the naive escalation ladder and has to be handled explicitly.**

A **stale Organya cache** makes a sweep legitimately run 3-4x expected. The PCM
cache is keyed to a build-sha stamped into every cached file; when the binary
changes and the cache is not re-rendered, cells fall back to cold-rendering the
music and the Organya cell (`R3`) sits on a loading screen for minutes. This is
not hypothetical -- the doskutsu session hit it earlier today after landing four
patches and had to re-render both cache tiers before the round could run.

The danger is specific: **rebooting does not fix a stale cache.** An escalation
ladder that responds to every timeout with warm boot, then power cycle, then
retry would loop forever on this condition, burn a night, and report "wedged"
about a machine that was working correctly the whole time.

So the harness classifies before it escalates:

1. The game logs a **cache-key line at startup**; a mismatch there is the
   signature. Read it before touching the power.
2. On mismatch, the remedy is **re-populate the cache, not reboot** -- and the
   harness must say so in the timeout message rather than reporting a wedge.
3. Cap retries of the same sweep regardless of classification, so that any
   unmodelled condition fails loudly instead of cycling the box all night.

### Sweep BAT skeleton

Invocation is always two steps after a boot: `QA <n>` to set machine context,
then `<SWEEP> <n>` (e.g. `RB`, `WC 1`, `MINE 1`).

```
@ECHO OFF
IF "%1"=="GO" GOTO GO
COMMAND /E:2048 /C %0 GO %1 %2      <-- re-entry with a larger environment
GOTO END
:GO
IF "%2"=="1" SET QAM=G              <-- machine tag
:START
ECHO ... > LOGS\%QAM%<SWEEP>.NFO     <-- per-sweep manifest
PAUSE                               <-- THE ONE KEYPRESS THAT STARTS THE SWEEP
CALL CLRENV                         <-- clears 204 env vars
SET DOSKUTSU_LOG_TAG=%QAM%R4
DOSKUTSU.EXE                        <-- CELL RUNS. RETURN POINT IS HERE.
CALL CLRENV                         <-- next cell begins
:END
```

### The argument-shift trap

`COMMAND /E:2048 /C %0 GO %1 %2` re-invokes the BAT with `GO` prepended, so
inside the child shell every argument is shifted by one: `%1` = `GO`, `%2` =
the CPU digit, `%3` = optional flag. This bit the project this week -- several
sweeps gated a PicoGUS mode switch on `%3`, it never fired, and cells silently
inherited whatever sound mode the previous sweep left behind. One sweep died at
`sdl_init` because the prior sweep handed back an AdLib-mode card. **If the
harness ever generates or edits a BAT, mind the shift.**

### Tagging and logs

| `<n>` | QAM | machine |
|---|---|---|
| 1 | `G` | Pentium OverDrive 83 |
| 2 | `A` | Am5x86-133 |
| 3 | `6` | 486DX2-66 |
| 4 | `5` | 486DX2-50 |

`DOSKUTSU_LOG_TAG=%QAM%<cell>` produces `LOGS\<TAG>.LOG` (engine) and
`LOGS\<TAG>SDL.LOG` (SDL), 8.3 names, under `LOGS\`. Per-sweep manifest is
`LOGS\%QAM%<SWEEP>.NFO`.

Two rules the harness must enforce:

- **Never reuse a log tag within a campaign.** Stale logs from a previous round
  get collected and silently analysed as current. The project has already lost
  time to exactly this. The harness should refuse to launch a sweep whose tags
  already exist on the target, rather than trusting the operator.
- **`CLRENV` wipes 204 variables at the start of every cell.** Anything the
  harness sets from outside is gone unless it happens not to be on that list.
  Do not build any mechanism that depends on externally-set environment
  surviving into a cell.

---

## 5.2 What this harness will never automate

Worth stating as a boundary rather than leaving it to be discovered as a gap.

**WITHDRAWN 2026-08-19 -- this section's premise was false.** Audio capture
works (sec. 4.2); the descriptor-based argument below was wrong. The text is
kept because the doskutsu session acted on it.

**Re-decided by that session with the corrected facts, and the exclusion
stands -- but narrowed, and it now splits in two:**

- **A silence-detection pass over the ear cells IS automatable and worth
  building.** It catches a dead DAC, a wrong boot profile, or a cell that
  produced nothing at all -- exactly the faults that waste a human's evening
  before they have judged anything. Cells in scope: `VB` plus the `G`-prefixed
  individuals. These come off the autonomous runner's deny-list **for the
  silence pass only**.
- **Judgement stays human.** The open audio questions are all of the second
  kind -- whether patch 0298/0307 drum envelopes are "too short", whether the
  Shack arrangement is right. No dB measurement settles either, and capture
  being proven does not change that.

The surviving argument for the exclusion never depended on capture at all: the
banner delay is the mechanism by which a human knows which cell is playing, so
for scoring runs it is load-bearing regardless.

The operative distinction, worth keeping verbatim: *"is this cell silent when it
should not be" is now answerable; "does this cell sound correct" is not.*

~~**The audio-QA "ear cells" stay human, permanently.** The capture stick's
audio input terminal is HDMI-embedded digital (`wTerminalType 0x0602`), measured
at the -67 dB noise floor, and VGA carries no audio -- so there is no path by
which this hardware can ever judge a sound cell.~~ Those sweeps print
`[n/10] CELL <name> -- listen for X` and depend on a human being in front of the
machine to know which cell is currently playing.

This is not a limitation to engineer around; it is a property of the signal.
Automating it would need separate hardware (e.g. a Behringer UCA202 on the
Sound Blaster line-out), which is out of scope here.

The practical split: the harness owns the **perf and visual** sweeps -- `RB`,
`WC`, `LEV`, `DEEP`, `MINE` -- and the ear cells (`VB.BAT` and ~20 individual
cell BATs) remain an operator activity. Phase 5's deny-list should therefore
carry the listening sweeps alongside `RECORD.BAT`, for the same reason: an
autonomous runner that launches one is not producing a result, it is producing
silence nobody is judging.

### Consequence for the banner delay

The 10 s banner delay (sec. 5.1) is **not** uniform overhead, and must not be
collapsed globally. For the listening cells it *is* the mechanism -- the window
in which a human reads which cell is about to play. DOS offers no
CPU-independent timed wait (`CHOICE.COM` is absent on the reference machine,
`PAUSE` needs a keypress, and a BAT busy-loop varies wildly between a Pentium
OverDrive and a 486), so doing it inside the engine was the only portable
option.

Collapse it for the unattended perf sweeps only. It is confirmed safe to do so:
the delay is a plain `SDL_Delay` sited before `SDL_Init` and before any tick, so
no logic tick has run and the TAS clock has not started -- a collapsed run stays
comparable to every banked historical cell.

**Mechanism, because the obvious approach fails:** the BATs hard-set
`SET SDL_HINT_DOSKUTSU_BANNER_DELAY_MS=10000` near the top of each sweep, so a
value the harness sets externally is overwritten. (It is not among CLRENV's 204
cleared variables -- the BAT is what stomps it.) Enabling harness control means
changing the BATs to respect a pre-existing value rather than setting
unconditionally. That change is the doskutsu session's to make, is being folded
into the same edit as the MODE12 flag-gating, and **is being held pending the
operator's direct go-ahead.** Payback is ~10 s per cell: ~40 s on `RB`, ~30 s on
a 3-cell sweep.

---

## 5.3 `DKTCAP` -- the unattended-session flag

Shipped in payload **r15** (superseding r14, which never reached a card) after operator approval. One flag covers both
behaviours the harness needs, so the harness sets one thing rather than two.

    SET DKTCAP=1

**Set it once per boot, at the prompt, before invoking a sweep.** It is *not*
among CLRENV's 204 cleared variables, so unlike everything else set externally
it survives into cells and persists for the whole session -- until `SET DKTCAP=`
or a reboot. The sweep prints a reminder when it is on, precisely because it
persists.

| | `DKTCAP` unset (default) | `DKTCAP=1` |
|---|---|---|
| banner delay | 10 s per cell, as every banked round | 0 |
| console between cells | text mode 03h (720x400 @70 Hz) | mode 12h via `VGACAP\MODE12.COM` |

Applied to eight sweeps, all verified free of `listen` text before the edit:
`RB` `WC` `MINE` `LEV` `DEEP` `ADIAG` `TAB` `FINE`.

The ear cells are excluded deliberately -- this is sec. 5.2's boundary
implemented rather than merely documented.

### Safety properties, as asserted by the repack

- **Unset is byte-identical to every banked round.** The default path still
  executes `SET SDL_HINT_DOSKUTSU_BANNER_DELAY_MS=10000` verbatim and the
  MODE12 condition is false. Asserted against the finished archive, not the
  staging tree.
- **Neither change can touch a measurement.** MODE12 runs only after the game
  has exited; the banner delay is an `SDL_Delay` before `SDL_Init` and before
  any tick.
- **Safe to leave on with a human at the monitor.** Mode 12h is a standard VGA
  mode at 80x30 -- slow, never dark. `VGACAP\MODE03` restores text mode.

### Two things to carry forward

- **Neither r14 nor r15 is on the CF card until the operator runs the populate
  step.** The harness must not assume `DKTCAP` exists on the card. Phase 1 should probe for
  it rather than trust it -- an unrecognised `SET` is silently harmless on DOS,
  so a harness that assumes the flag works would collapse no delays, capture no
  console, and report success.

  **The positive witness:** with `DKTCAP=1` set, a sweep prints a
  `[DKTCAP=1] capture session:` line among its banners before the `PAUSE`.
  Absence of that line on a sweep believed to be flagged means r14 is not on
  the card. But see sec. 5.4 -- reading that line requires setting mode 12h at
  the prompt first, because the banner is printed in the one mode that provably
  does not lock.
- **`RB.BAT` ships from tracked source for the first time in r14/r15.** The copy on
  the card had been carried from an older payload and never replaced, so it was
  of unverified vintage. Cell tags and configs match the Round M and O logs, so
  no drift is expected -- but if `RB` behaves unexpectedly after this populate,
  that is the first thing to suspect.

r15 is a BAT-only repack: binary and Organya caches untouched, cache key still
`66ff01f7f997` and binary `e9e8ff80ae10`, so nothing cold-renders (sec. 5.1)
and Round O's numbers stay directly comparable.

**r15 also makes each sweep self-witnessing.** The mode switch now fires once at
the top of the sweep -- after `DKTCAP` detection, before the banner prints -- as
well as after each cell:

```
55: IF NOT "%DKTCAP%"=="1" SET SDL_HINT_DOSKUTSU_BANNER_DELAY_MS=10000
56: IF     "%DKTCAP%"=="1" SET SDL_HINT_DOSKUTSU_BANNER_DELAY_MS=0
64: IF     "%DKTCAP%"=="1" IF EXIST C:\VGACAP\MODE12.COM C:\VGACAP\MODE12.COM
65: IF     "%DKTCAP%"=="1" ECHO  [DKTCAP=1] capture session: ...
```

So the banner, the `PAUSE` and any error text the sweep emits are printed into
mode 12h and are capturable. This does **not** replace the harness's own
`VGACAP\MODE12` step (sec. 5.4) -- the prompt, the `QA <n>` result, and anything
that fails before a sweep launches are still in text mode 03h. What it changes
is failure posture: if the harness's blind mode-set does not land, the sweep
switches the mode itself and the banner still appears, giving a second
independent chance to observe a working capture.

---

## 5.4 Session start: mode 12h at the prompt, before anything else

A consequence of two facts that are individually known but bite when combined.

The `DKTCAP` positive witness -- the `[DKTCAP=1] capture session:` banner --
prints **before** the `PAUSE`. At that moment `MODE12.COM` has not run: under
`DKTCAP` it is invoked after each `DOSKUTSU.EXE`, and no cell has run yet. So
the banner is printed into text mode 03h, 720x400 @70 Hz -- the exact mode
proven not to lock (sec. 4). **The witness is, by default, invisible to the
thing that needs to read it.**

The same holds for everything else the harness needs to see before a sweep
starts: the DOS prompt itself, the result of `QA <n>`, and any error text.

**Fix: the harness sets mode 12h at the prompt as the first action after
boot**, before `QA` and before `SET DKTCAP=1`:

```
boot -> prompt -> VGACAP\MODE12 -> QA <n> -> SET DKTCAP=1 -> <SWEEP> <n>
                                                          -> keypress at PAUSE
```

This works because mode 12h keeps the console usable at 80x30 -- text renders
through the BIOS TTY into the graphics framebuffer -- so the prompt and every
banner after it stay capturable. `VGACAP\MODE03` restores normal text mode when
a human takes the machine back.

The prompt is a valid keystroke injection point (sec. 5.1), so this costs
nothing but one typed command per boot.

**This is a phase 1 prerequisite, not a phase 2 nicety.** Verifying that typing
works at all means reading the console back, and the console is not readable
until mode 12h is set. Input and video are therefore less separable than the
phase table suggests: phase 1's *verification* depends on phase 2's capture,
even though phase 1's *mechanism* does not.

The LED return channel (sec. 2.1) is the escape hatch from that circularity and
should be tried first, because it answers the input question without involving
video at all. Order for the first powered-on test:

1. Grab a frame before sending anything -- establishes the black baseline.
2. Toggle Caps Lock, watch `/sys/class/leds/input*::capslock/brightness`.
   A change proves keystrokes reach DOS and are processed, with no video
   involved.
3. Only then type `VGACAP\MODE12` and grab again. Now a black frame means the
   capture cannot lock, rather than meaning something unattributable.

Cost: mode 12h console text is slow (planar 4-bit-plane, BIOS TTY does
read-modify-write per glyph), so pre-sweep banners visibly crawl. This is a
one-off per boot and sits outside every timed bracket, so it is paid in
patience, not in measurement error.

---

## 5.5 Boot profile selection -- blind, timed, and uncapturable

The card's `CONFIG.SYS` presents a six-way boot menu:

```
menuitem=VIBRAUSB, Vibra16 + PicoGUS USB (CD + MIDI)
menuitem=PGSB,     PicoGUS Sound Blaster
menuitem=PGADLIB,  PicoGUS AdLib
menuitem=PGGUS,    PicoGUS Ultrasound
menuitem=VIBRA,    Vibra16 only
menuitem=MACH64,   Mach64 video test (M64VBE, no UniVBE)
menudefault=VIBRAUSB,5
```

**This is a harness problem with three unpleasant properties at once:**

1. **It happens before anything is ready.** The menu is the DOS kernel, long
   before `AUTOEXEC.BAT`, so the readiness signal (sec. 2.1) cannot gate it.
   Profile selection has to be timed against POST, blind.
2. **It is uncapturable.** The menu renders in text mode 03h, 720x400 @70 Hz --
   the mode proven not to lock. The harness cannot see the menu it is
   answering, and cannot verify which entry is highlighted.
3. **It has a 5-second timeout**, after which it takes `VIBRAUSB` and proceeds.
   Miss the window and the machine boots the wrong profile silently, and the
   sweep runs against the wrong sound hardware.

Options, in increasing order of intervention:

- **Accept the default.** Send nothing, always boot `VIBRAUSB`. Safest, needs
  no change, and is correct for any sweep that sets its own `BLASTER` -- which
  per sec. 5.5a is all of them. **This is the recommended default.**
- **Change `menudefault` to whatever the campaign needs.** A `CONFIG.SYS` edit,
  so an operator decision, and it changes every manual boot too.
- **Send the digit blind at a calibrated delay.** Requires measuring
  POST-to-menu time on each machine and re-measuring whenever hardware changes.
  Fragile, and the failure is silent. Not recommended without a way to verify
  the outcome afterwards.

**Verification after the fact is the mitigation that makes any of these safe.**
Whatever route is taken, the harness should confirm which profile actually
booted before trusting a run -- the `[%config%] ready.` line that `AUTOEXEC`
prints at `:READY` carries it, and once the harness has set mode 12h that line
is capturable. Read it, do not assume it.

### 5.5a The card and the repo have drifted -- resolved, no measurement impact

A read-only diff of the card's `AUTOEXEC.BAT` against
`docs/internal/g2k-boot-profiles/AUTOEXEC.BAT` found three differences, in both
directions:

| | card | repo snapshot |
|---|---|---|
| boot profiles | 6, including `MACH64` | 5, no `MACH64` |
| UniVBE load | gated: `IF NOT "%config%"=="MACH64"` | unconditional |
| PGSB sound type | `T3` + `FORCE_8BIT=1`, dated 07-06 | `T4`, dated 07-07 |

The card is ahead on the video profile and behind on the sound line.

**Resolved: no banked measurement is affected**, for two structural reasons
rather than by luck. Every sweep executes its own `SET BLASTER=...T3` before its
first cell, overriding whatever `AUTOEXEC` left -- and it sets `T3`, matching
the card. And `CLRENV` clears both spellings of `AUDIO_SB_FORCE_8BIT` at the
start of every cell, so the card's `FORCE_8BIT=1` never reaches a measured run.
The sweeps are self-contained on both counts.

The drift therefore affects manual play sessions on the PGSB profile, not the
matrix. The repo snapshot is an undeployed proposal rather than a stale mirror
-- a documentation defect, not a data-integrity one.

Kept here because the diff is the thing that found the `MACH64` profile, which
does change the capture predictions (sec. 4). Zero-cost read-only checks against
the real hardware are worth running whenever the card is in the reader.

---

## 5.6 The Pi as CF host -- proven 2026-08-19

The r15 populate ran end to end from the Pi, retiring the laptop from the
delivery path. Recorded because it establishes the pattern for every future
payload.

What worked:

- Payload pushed VM -> Pi over wifi (188 MB, ~53 s), sha verified on arrival.
- The doskutsu installer's `CF_MOUNT` and `STAGING` are already environment-
  overridable, so **no change to their tooling was needed**:
  `CF_MOUNT=/mnt/cf STAGING=/home/claude/staging bash install-qa-v163.sh`
- Mount the vfat with `-o uid=<user>` so the installer runs unprivileged rather
  than under sudo. Mounting as root leaves the card root-owned and the extract
  fails partway through, after several steps have already reported PASS.
- Lowercase `CF_GAME_DIR` resolves fine -- Linux vfat lookups are case-
  insensitive, so the documented case gotcha did not bite.
- Result: 15 PASS, 0 FAIL, 0 WARN, card auto-unmounted.

Gotchas worth keeping:

- **Stage into a directory the ssh user owns.** `/home/pi/staging` created with
  `sudo mkdir` is root-owned and scp fails with a bare "Permission denied".
- **Read the cache-key line specifically.** `PASS: Organya 11025 cache keyed
  66ff01f7f997` is the line that distinguishes a healthy payload from one whose
  cells will cold-render for minutes and look like a wedge (sec. 5.1).
- **The AUTOEXEC audit is advisory and never edits.** It flagged five stale
  `DOSKUTSU_*` SETs. Those persist on the card by design; if a cell's audio
  reads wrong, they are the first suspect.

---

## 6. Phases

| phase | deliverable | gated on |
|---|---|---|
| 0 | Hardware moves: CF reader + capture stick onto the Pi; smart plug details | you |
| 1 | **Input end to end.** `vcctrl type "DIR" && vcctrl key enter` lists a directory on the g2k -- **built and verified to the Pi**; PS/2 delivery pending a powered g2k | nothing -- design is proven |
| 2 | **Video.** Lock test first; then `vcctrl shot` | the signalstats test passing |
| 3 | **CF on the Pi.** Retires the laptop from the loop -- **DONE 2026-08-19**, see sec. 5.6 | phase 0 |
| 4 | **NIC + mTCP.** Retires the CF swap | card purchase, IRQ check |
| 5 | **Autonomous sweep runner.** Escalation ladder, unattended QA sweep (sec. 5.1 sets the granularity) | 1-4 |
| 6 | **Pi 5 port.** Separate task, with the harness as its regression test | 1-5 working |

Phase 1 is worth starting immediately: it depends on nothing, the design is
already proven on the live hardware, and it delivers the single most useful
capability -- typing at the box -- on its own.

---

## 7. The Pi 5 port  **[measured blockers, unvalidated fix]**

Deferred to phase 6 by decision, but the analysis is done, and the commonly
repeated "USB4VC only works on 32-bit" turns out to be two lines of code rather
than anything architectural.

**Blocker 1 -- the input_event struct.** `usb4vc_usb_scan.py:772` reads exactly
16 bytes per event and line 787 slices `data[8:]` to skip the timestamp. That is
`struct input_event` on a 32-bit kernel: `struct timeval` is 2x32-bit, plus
2+2+4 = 16. On aarch64 `timeval` is 2x64-bit and the struct is **24** bytes.
Confirmed on the Pi 3: `struct.calcsize('llHHi')` = 16. On a Pi 5 it is 24, so
the app reads misaligned garbage forever. This is the real reason the official
image is 32-bit Raspbian.

Fix is mechanical:

```python
EVENT_SIZE = struct.calcsize('llHHi')   # 16 on armhf, 24 on aarch64
TS_SIZE    = struct.calcsize('ll')      #  8 on armhf, 16 on aarch64
...
data = this_device['file'].read(EVENT_SIZE)
...
data = list(data[TS_SIZE:])
```

**Blocker 2 -- RPi.GPIO.** Does not work on the Pi 5 at all; it pokes BCM
registers directly and the Pi 5 puts GPIO behind the RP1. The whole app uses
only 8 distinct API calls -- `setmode/setup/input/output/add_event_detect/`
`event_detected` plus the `BCM/IN/OUT/HIGH/LOW/PUD_*/RISING` constants -- all of
which `rpi-lgpio` provides as a drop-in. Install it, uninstall `RPi.GPIO`, no
source change.

Everything else ports clean: `spidev` works via the RP1, `evdev` is portable,
`luma.oled` over `/dev/i2c-1` is fine, and the SPI clock is only 2 MHz. The
STM32 protocol board does all the real-time PS/2 timing, so the Pi has no
latency requirement to meet.

Remaining unknowns, all physical: HAT fit and 5 V power on the Pi 5 header, and
the fact that Pi OS is 64-bit-only on the Pi 5 so there is no fallback to the
stock 32-bit image if the port misbehaves.

---

## 8. Repo layout

```
/home/claude/git/vcctrl/
  bin/vcctrl              CLI on the VM; thin ssh client, no logic
  daemon/vcctrld.py       on the Pi: owns uinput devices, unix socket
  daemon/capture.py       frame grab + lock detection
  pi/install.sh           systemd unit, deps, udev
  dos/                    mTCP config, harness BATs, VGACAP COMs
  docs/                   this plan, findings, runbook
```

The CLI holds no logic -- it shells to the daemon. That keeps the interesting
code in one place on the Pi, testable without the VM, and lets the doskutsu
harness call the same primitives.

---

## 9. Open risks, in order of how much they hurt

1. **Capture may not lock on gameplay** (sec. 4). Blocks all visual QA. Cheap to
   test, so test it first.
2. **Recovery path after a bad build** (sec. 5). If Ctrl-Alt-Del plus keystrokes
   cannot reach a bare boot, autonomy silently degrades to a human with a
   screwdriver.
3. **IRQ / conventional-memory contention from the NIC** (sec. 5).
4. **Pi 3 USB bus contention.** Capture stick and CF reader share one USB 2.0
   bus. Low risk in practice: the Pi 3's wifi is SDIO, not USB -- eth0 is down
   and the box is on wlan0 -- so those two devices are the only meaningful
   traffic, and they only overlap during a log pull with a screenshot.
5. **USB4VC event pacing under load** (sec. 2). One event per device per loop
   pass; a burst queues rather than drops, but timing-sensitive game input may
   need tuning.

---

## 10. Open questions for the operator

1. Smart plug make/model and how it is controlled (Tasmota / Home Assistant /
   Kasa / Zigbee)?
2. Does the g2k have a free PCI slot and a free IRQ, and is the chassis
   accessible enough to add the NIC?
3. **Not blocking.** Round O logs prove the installed card enumerates and sets
   `0x01F8 320x240 8bpp` with 4 MB VRAM, which rules out the Mach64 (no
   320x240 mode -- that absence is why the centring path exists). That is
   enough for the lock test, since 320x240 is exactly the double-scan case the
   prediction covers. The logs do not separate ViRGE/DX from the onboard Cirrus
   CL-GD5430; the manifest says ViRGE but that line is operator-typed text, not
   a hardware witness. An eyeball on the card would settle it whenever
   convenient.
4. Is the Pi 5 on the network anywhere? `raspberrypi5.local` does not resolve
   from the Pi 3.
5. **Add a `0xED` LED-pulse COM as the last line of `AUTOEXEC.BAT`?** (sec.
   2.1) It gives the harness a true DOS-ready signal with no video and no
   network, and it discriminates a broken relay from absent BIOS lock-key
   handling. `AUTOEXEC.BAT` is outside the QA payload, so this is an operator
   edit affecting every boot including manual ones. The COM is worth writing
   either way -- run by hand it is still the discriminator; only the AUTOEXEC
   line needs deciding.
