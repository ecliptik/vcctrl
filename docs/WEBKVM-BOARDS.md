# Two targets, one KVM

Written 2026-08-20, after the Pi 5 cutover and the `board` contract landing.

`docs/BOARD-IDENTITY.md` owns the identity itself: where the board ID comes
from, what `/state.json` publishes, and how the daemon's capabilities are
scoped. **This document owns the other half — what the capture path and the
page do about it**, and it exists because "dynamic switching" turns out to be
mostly about the things that are ABSENT on one target and present on the
other, not about the switching.

## 0. Decisions already taken

Asked and answered by the operator on 2026-08-20:

- **The capture sticks are swapped with the boards**, one at a time. There is
  no permanent two-device setup to arbitrate between.
- **The mouse gets proved before it gets a UI.** A closed-loop test of the
  primitive first; no browser work on top of a path nothing has ever
  exercised.
- **Gateway first.** Testing on the g2k resumes now and is the priority. The
  Mac is interactive-first while it is being characterised; automation later
  if it earns it.

The operator swaps the board, the capture stick, and the mains lead if it
moves. Everything after that should follow from `board` without anyone typing
a command — that is what makes it dynamic. The line is drawn where the
harness physically cannot cross it, which is the right place.

## 1. The capture device now vanishes, and that is NORMAL

Swapping sticks means `/dev/video0` disappears and reappears while the daemon
is running. This is new: on the old rig the stick was permanent.

**Verified in the code, not assumed:** `_watchdog` respawns on an exponential
backoff capped at 30 s and **never gives up** — `if self.running: self._acquire()`
runs unconditionally after the sleep. So a stick that comes back is picked up
within 30 s with no restart and no command. The swap works today.

What does not work today is the REPORTING. A missing device and a broken
device are different facts and the page says the same thing about both:

| situation | truth | what the page says now |
|---|---|---|
| stick unplugged, mid-swap | operator is doing this deliberately | "No signal" |
| stick plugged, will not open | fault — wrong node, busy, dead | "No signal" |
| stick open, source dark | target off or asleep | "No signal" |

The first is not a fault at all, and telling the operator their rig is broken
while they are holding the cable is the kind of small lie that trains people
to ignore the status line.

**Proposal:** the daemon reports whether the device NODE exists, separately
from whether it opens — `os.path.exists(DEVICE)` for video, the card list for
ALSA. One boolean, checked at spawn time, published in `/state.json`:

    "video": {"state": "unavailable", "device": "/dev/video0",
              "device_present": false, "last_error": null, ...}

Then the page can say **"no capture device attached"** for the swap and keep
"no signal" for a device that is present and silent. `last_error` already
carries ffmpeg's own words for the third case, as of `b54a0eb`.

This is small and it is worth doing before the first swap, because the swap is
the moment the distinction first matters.

## 2. `frozen` must be re-measured before it is trusted on the Mac

The frozen detector calls a stream dead after `FROZEN_RUN = 8` bit-identical
frames. The reasoning behind it is measured and correct **for the VGA stick**,
and the exact measurements are worth restating because they are subtler than
"analog is noisy":

- Mode 12h, a static console: **90 frames, 90 distinct hashes.** The stick
  samples continuously and sampling noise makes every frame differ.
- Mode 03h, DOS text: **30 frames, ONE distinct hash.** The stick stops
  sampling and repeats a held buffer.

So identical frames are a positive identification of *this stick repeating
itself*. That is a property of the device, not a law about analog video.

**RGB2HDMI into an HDMI capture device has no analog sampling anywhere.** The
Mac Plus emits TTL video, RGB2HDMI digitises it deterministically, the capture
device encodes an already-digital signal. If the encoder is deterministic —
and MJPEG encoders generally are — then a static Finder screen produces
byte-identical frames, and the daemon would call a perfectly healthy Mac
`frozen` within 0.27 s. The page renders that as **"No signal"**, so the first
Mac screen anyone looks at would claim the machine is not producing video.

**Do not tune FROZEN_RUN.** On a digital source, content-identity genuinely
cannot separate "held buffer" from "static screen" — they are the same bytes.
A check that cannot distinguish its two answers should say so rather than
pick one.

**The gate, before anything is designed around this:** with the Mac capturing
a static screen, pull ten consecutive frames from `/frame.jpg?seq=` and
compare their md5s.

- Ten distinct → the path has noise after all, the detector is valid, nothing
  changes.
- Ten identical → the detector is invalid here and needs a per-source flag:
  `analog: true` for the VGA stick, false for the HDMI path, and when false
  liveness is judged on frame ARRIVAL only, `state` never takes the value
  `frozen`, and `/state.json` says the content check is not applicable rather
  than silently reporting a healthier or sicker machine than exists.

## 3. Geometry: already handled, with two open edges

**Measured, not assumed.** The page was run against a synthetic 512x342 frame
in headless Chromium on 2026-08-20:

    natural 512x342, viewport 1240x652
    Fit       976.1 x 652.0   fills the height, aspect 1.497 preserved
    Original  512.0 x 342.0   exact
    200%     1024.0 x 684.0
    400%     2048.0 x 1368.0

The zoom maths reads `naturalWidth`/`naturalHeight` off the frame and the
canvas resizes to whatever the decoder hands it; 640x480 survives only as a
fallback for before the first frame. **No page change is needed for a
different geometry.**

Two edges that are not settled:

**The Mac image will probably arrive letterboxed.** HDMI capture devices tend
to emit a standard mode, so a 512x342 picture may sit inside 720x480 or
1280x720. That is what the letterbox detector is for, and it requires the
surround to be black and SYMMETRIC — a rule chosen deliberately, because it is
what stops a short line of DOS text being mistaken for a letterbox. A centred
Mac image satisfies it. An off-centre one does not, and Fit would then include
the black. **Look at one real frame's bounding box before deciding anything.**

**The adaptive fps ceiling is 20**, chosen because the VGA stick produces
about 24. That is an assumption written as a constant. If the HDMI device runs
at 30 or 60 the client will under-ask forever. Read the rate from the stream
rather than hardcoding it, once there is a stream to read.

## 4. Audio is ABSENT on the Mac, which is not the same as broken

`BOARD-IDENTITY.md` records audio on the Mac as "not planned" — RGB2HDMI
carries video only and the Mac Plus's sound is not routed anywhere.

Today the page would show a sound button, a volume slider and a level meter
that never moves, and the daemon would report `unavailable` with
`fast_failures` climbing forever against an ALSA device that does not exist.
Both are describing a fault that is not one.

The page should say **"no audio on this board"** and stop offering the
controls, which means the daemon needs to distinguish *not fitted* from
*failed*. The board table already knows; this is the same shape as the LED
problem below and should get the same treatment rather than a second
mechanism.

## 5. LEDs: unavailable is a FOURTH state

The lamps have three: lit, dark, and stale-dashed. On ADB there is no LED
return channel at all, so the honest reading is a fourth state — unavailable —
and it must come from the daemon rather than from the page mapping board 3 to
"no LEDs". Every consumer that re-implements that mapping is a consumer that
can drift from it.

The same applies to `input_verified`: the Caps-Lock-and-watch-the-LED proof is
the one check that works when the picture is lying, and it simply does not
exist on the Mac. "Verify input link" should be absent there, not failing.

## 6. Power: done, and the rest is not doable

Settled 2026-08-20. One socket serves whichever machine is connected; the plug
cannot report what is plugged into it, and the board tells you which protocol
board is fitted, not which computer is on the mains. So the confirmation names
**the plug** — `Cut mains at "retro-rig-plug" (EP10(US))?` — and states the
board fitted beside it as a fact rather than an inference from it.

There is no enforcement to add. A refusal keyed to a board-to-plug table would
be a check that fabricates its answer, which is the failure mode this project
has spent a day unpicking. The guard is the operator's eyes, and the page's
job is to put both facts where those eyes already are.

## 7. What the page does when the board changes

Already live as of `063c579`: the target chip in the status rail names the
machine (`Macintosh Plus`), not the board (`Apple Lisa/Mac/ADB`), because the
question anyone actually has is what they are typing into. Hidden entirely
when unknown; yellow when the identity came from the journal fallback rather
than the running instance.

Still to do:

- **`board.changed` in the activity log.** The daemon publishes it on the bus;
  the page's event poll should render it. "Why is this suddenly a Mac" is a
  question with an answer, and the log is where answers live.
- **Re-fit on a board change.** A new board means a new geometry, so
  `applyZoom(true)` and a crop re-measure, and drop the stale-frame overlay —
  it belongs to the previous machine and showing it under a new target's
  header is precisely the kind of lie the veil exists to prevent.
- **Per-board zoom memory** is probably not worth it. Fit is right for both.

## 8. The mouse: prove the primitive, then decide

Decided: **no browser work yet.** `vcctrl mouse move|click` exists, the uinput
device exists, and no finding anywhere records a cursor ever moving on any
target. Building Pointer Lock on top of that is two unknowns stacked.

The test, which is the same discipline as driving a game menu by reading the
screen rather than counting keypresses:

1. Board reports 3, RGB2HDMI feeding the capture stick, picture locks
2. `vcctrl mouse move 200 0`
3. Capture, diff against the previous frame
4. The cursor moved, or it did not — read it off the picture

There is a Pi-side question inside this one: whether USB4VC's 0.75 s discovery
scan even picks up the daemon's uinput MOUSE device. The keyboard is known to
be discovered; the mouse has never been checked. If the cursor does not move,
that is the first thing to look at, not the ADB translation.

**What the browser would need afterwards**, recorded so it is not re-derived:
Pointer Lock delivers `movementX`/`movementY`, which are relative deltas
natively — matching ADB and PS/2 exactly, with no cursor-position fiction in
between. The transport is the easy half. The hard half is that the guest
applies its own acceleration curve, so deltas do not map linearly to cursor
travel and "move to the menu bar" is not solvable open-loop. Homing by slamming
into a corner first is the only reliable origin.

## 9. Sequencing

**Gateway first**, per the operator.

1. PS/2 board, CF reader and VGA stick attached. `board` reports 1 / IBM PC.
2. Video and audio prove out on the Pi 5's ffmpeg 7.1.5 — the option surface
   is already verified, only the device half is unknown.
   ALSA card index is the one to watch: `hw:1,0` was correct on the Pi 3, and
   the Pi 5 has no analog jack, so the stick may be card 0. `VCCTRL_ALSA`
   exists for exactly this; `VCCTRL_VIDEO` was added on 2026-08-20 for the
   same reason on the other path.
3. Resume g2k testing. Nothing in this document blocks that.

**Then the Mac**, in this order, because each step's failure is only
diagnosable if the one before it worked:

4. `device_present` (sec. 1) — small, and the swap is when it first matters.
5. The ten-frame md5 gate (sec. 2). Everything about how the Mac's video is
   reported hangs on the answer.
6. One real frame's bounding box (sec. 3), which settles the letterbox
   question and the fps ceiling in the same look.
7. Absent-not-broken for audio and LEDs (secs. 4, 5).
8. The mouse primitive (sec. 8). Only then, a decision about the browser.

## 10. What would change this plan

- **Ten distinct hashes** on the Mac's static screen: section 2 disappears
  entirely and the detector stands as written.
- **An off-centre letterbox**: the symmetry rule needs revisiting, and that
  rule is load-bearing for the g2k, so it would be a change with two targets
  to satisfy rather than one.
- **The mouse not moving**: the question becomes USB4VC device discovery on
  the Pi side, not the KVM, and it moves out of this document.
- **The Mac earning automation**: interactive-first is the current call. If it
  becomes a second doskutsu, the input primitives and the frozen detector stop
  being conveniences and become the product, and section 8 gets much larger.
