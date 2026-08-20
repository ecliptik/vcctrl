# The mouse: what is proven, and what building on it needs

Written 2026-08-20, the day `vcctrl mouse move` was first exercised against a
target. Until then FINDINGS sec. 9 recorded that the command existed, the
uinput device existed, and **no finding anywhere said a cursor had ever moved**.
This is that finding.

## 1. Status: proven, end to end, on the Gateway

    daemon -> uinput -> USB4VC -> IBM PC protocol board -> Gateway -> cursor

Proven under Windows 3.11 (VGA 640x480). Controlled moves, measured off the
captured frames:

    slam to top-left           cursor at (2,1)-(11,19)
    +240,+180 of deltas        cursor at (360,269)-(372,289)
    +240,+180 again            vacates (360,269); arrival not detected (sec. 4)
    slam back to top-left      cursor at (2,1)-(11,19)

Every changed region is 12x20 px or smaller, which is the size of a Windows
3.11 arrow. The cursor goes where it is told and returns.

This also confirms the 64-bit `input_event` patch (FINDINGS sec. 29) fixed
**both** input paths rather than only the keyboard. Same shared event-read
loop, so it should have — but "should have" and "measured" are different
claims and this is the measurement.

**Independently confirmed** by the webkvm session, from a different process,
over HTTP, reading the scrub ring rather than driving anything. Their tracker
tails `/events` for `mouse*` commands and diffs the frames spanning that
moment. Cursor positions across two bursts:

    (360,269) -> (461,298) -> (281,112) -> (102,1) -> (2,1)

with box sizes 12x20, 10x26, 9x18, 13x18 — cursor-sized throughout, and the
last two at the top-left corner where a homing move ends. Threshold 45.

**Still unproven:** clicks, drag, the ADB/Macintosh side, and anything at all
about DOS-native mouse programs.

## 2. Numbers worth keeping

**Acceleration is real and was ~1.49x under Windows.** From (2,1) to (360,269)
is 358 px horizontal and 268 px vertical, for 240 and 180 delta units sent:

    358 / 240 = 1.49        268 / 180 = 1.49

The ratio is consistent on both axes, which means it is a scale factor rather
than noise. **It belongs to the guest, not to us** — Windows applies its own
curve, DOS programs apply theirs, and the Macintosh will apply a different one
again. Do not build anything that assumes deltas map to pixels.

**The cursor clamps at screen edges, and clamping is silent.** Two identical
`+240,+180` moves produced very different results: the first crossed most of
the screen, the second went nowhere detectable. A move into an edge is
absorbed with no feedback. This is why homing works and why open-loop
positioning does not.

**Cursor size in VGA 640x480 is 12x20 px.** Useful as a filter: a changed
region much larger than that is not the cursor.

## 3. Homing is the only way to establish position

PS/2 and ADB mice are both **relative-only**. There is no "put the cursor at
320,240" — only deltas. Combined with silent clamping, that gives exactly one
reliable primitive:

**Slam into a corner, then move by deltas from there.** `mouse move -120 -120`
repeated a dozen times pins the cursor at (0,0) regardless of where it started,
because the excess is absorbed by the edge. Measured landing: (2,1)-(11,19),
i.e. the arrow's hotspot at the corner.

Anything that needs the cursor at a known place must home first. Anything that
needs it at a *specific* place must home, move, and then **verify from the
picture** — because of the acceleration in sec. 2, the delta that gets there is
not calculable in advance.

## 4. Detecting the cursor from capture: what works and what lies

This is the part that cost the most time, and both failures were measurement
rather than mechanism.

**`getbbox()` on a raw difference is useless.** It includes every sub-threshold
noise pixel and returns the full frame every time. It reported
`bbox=(0,0,640,480)` for a cursor that had moved 12 px. **Threshold first, then
locate** — `ImageChops.difference(a,b).point(lambda p: 255 if p > 60 else 0)`
then `getbbox()`. Threshold 60 on 0-255 sits well above this stick's analog
noise floor, which is ~1.0 mean absolute difference with peaks near 40.

**Comparing consecutive frames can report zero on a move that worked.** A large
move pins the cursor at an edge, so by the time the pair is captured both
frames match and the diff is empty. This produced `changed=0` across four moves
that had all succeeded, and very nearly got reported as "the mouse does not
work". **Compare against a frame whose cursor position you have actually looked
at**, not against the previous frame.

**Diff against the FIRST frame of a window, not against the previous frame.**
This is the webkvm session's technique and it is strictly better than mine.
Holding one reference frame and diffing every subsequent frame against it makes
a moving object show as a box that GROWS to span origin and destination, then
COLLAPSES to cursor-size once the origin stops differing:

    t+0.06  bbox=(360,269,462,356)  102x87
    t+0.09  bbox=(360,269,552,424)  192x155
    t+0.12  bbox=(360,269,638,479)  278x210
    t+0.16  bbox=(360,269,372,289)   12x20
    t+0.19  bbox=(360,269,372,289)   12x20

**That expansion-then-collapse is the signature of one object travelling**, and
a repaint or a blink cannot produce it — a redraw gives a box in the same place
every frame. It also distinguishes movement from a cursor that merely appeared
or vanished, which pairwise diffing cannot.

**Departure is a more reliable signal than arrival.** A single cursor-sized
region means "it left here", which proves movement but does not say where it
went. A white arrow on a light-grey dialog need not differ by 60 greyscale
levels from what it covers. If the destination matters, move onto a **dark
background** or compare in colour rather than luminance.

**Threshold has a floor and a ceiling, and 60 is near the ceiling.** Measured
on this stick by the webkvm session: a still frame has ~1.0 mean absolute
difference with peaks near 40. So

    below ~45   you are reading the analog noise floor
    around 45   the lowest honest threshold on this capture path
    above ~60   you are requiring contrast the DESTINATION may not have

The 60 used above was chosen to make departure unambiguous and it did that.
For arrival, prefer 45 — the extra sensitivity costs little once the object is
also required to be cursor-sized, since noise does not arrive in 12x20 blocks.

## 5. Why no DOS test can prove this, which is why Windows was used

Both obvious approaches are structurally incapable, not merely awkward:

- **A bare `C:\>` prompt never shows a cursor.** CuteMouse keeps it hidden
  until a program calls INT 33h function 1 (Show Cursor), and COMMAND.COM
  never does. Moving the mouse changes nothing on screen whether or not the
  hardware works, so a negative result is meaningless.
- **EDIT shows a cursor but switches to text mode 03h**, which this capture
  path cannot lock. We lost the screen to exactly that and recovered by
  watching `video state` go `frozen` -> `locked` after sending the exit
  sequence blind.

Generalised: **everything that renders a mouse cursor changes to a video mode
the capture stick cannot watch.** Windows 3.11 breaks the deadlock only because
`display.drv=vga.drv` at 640x480 is the resolution already known to lock — read
off `C:\WINDOWS\SYSTEM.INI` rather than assumed.

`CTMOUSE` reporting "Installed at PS/2 port" during boot is **not** evidence a
mouse answered. It appears to reflect the BIOS advertising an auxiliary port.
It was cited early in the investigation as evidence the link worked, and it was
not.

## 6. For whoever builds mouse support

1. **Home before every positioning operation.** Do not track position across
   operations; clamping and acceleration both destroy dead reckoning.
2. **Close the loop from capture.** Move, look, correct. The same rule as
   driving the Cave Story menu by cursor position rather than counting
   keypresses.
3. **Do not compute deltas from pixel distances.** Measure the guest's scale
   factor once per environment if you need one, and re-measure per target.
4. **Expect the Macintosh to differ in every respect** — ADB, a different
   acceleration curve, a different cursor bitmap, and video through RGB2HDMI
   rather than the VGA stick.
5. **Pointer Lock is the right browser primitive**, per the webkvm session:
   `movementX/movementY` are relative natively, so they match PS/2 and ADB with
   no cursor-position fiction in between. The hard part is not the transport.

## 7. Open questions

- Clicks. `vcctrl mouse click` has never been exercised at all.
- Whether the acceleration factor is stable within one environment or varies
  with speed, which is what a real acceleration *curve* would imply. 1.49x was
  measured at one speed only.
- Whether any DOS program on the Gateway renders a cursor in mode 12h. If one
  does, it removes the need to start Windows for every mouse test.
- The Macintosh side, entirely.
