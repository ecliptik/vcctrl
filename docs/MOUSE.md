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

**Still unproven:** drag, the ADB/Macintosh side, and anything at all about
DOS-native mouse programs beyond a single AGS/Allegro game (see section 9 --
clicks are proven now, with a real timing caveat).

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

**CORRECTION 2026-08-20: those numbers are a property of THIS CONTENT, not of
the capture path, and carrying them elsewhere will break the measurement.**

They were measured against a white cursor on a light Windows desktop. The
benchmarking session swept the same way on Cave Story's backdrop, whose pixels
run 20–47, and found a stable plateau at **4–24** — where a threshold of 40
starts eating the backdrop itself and 60 clips everything but the HUD. I swept
a static DOS console, bright text on black, and found a plateau at **50–80**.

**The two plateaus do not overlap at all.** A single global constant would have
been wrong for one of us whichever number was picked, and I recommended 60 to
someone whose content it would have destroyed.

**What transfers is the method, not the value:**

1. Take ten *consecutive* frames of a static screen of the content class you
   actually mean to measure.
2. Sweep the threshold and record how many distinct results appear at each.
3. Take the **middle of the plateau** where the count is 1 — so a small drift
   in either direction changes nothing.

A threshold sitting on the edge of a plateau is the failure: 40 gave three
different bounding boxes across ten identical frames, because a faint row sat
exactly on the cut. Instability like that reads as a property of the subject
and is a property of the instrument.

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

## 7. The browser now forwards it — 2026-09-02, folded into Grab 2026-09-02

Section 6.5 above called Pointer Lock the right primitive before anything
used it. It is now what the web KVM's mouse forwarding (`kvm.html`) is
built on: `movementX/movementY` accumulated and flushed as `mouse_move`
on a fixed 40ms timer, so a fast trackpad costs one HTTP round trip per
tick rather than one per event.

**One button, not two.** The first version of this shipped as a second,
independent "Mouse" control beside Grab. The operator asked for one --
in practice keyboard and pointer are always wanted together -- so
`setArmed(true)` now both focuses the keyboard AND requests the lock,
and `setArmed(false)` releases both. The one thing that stays genuinely
independent underneath is WHETHER THE LOCK IS ACTUALLY HELD right now:
Escape always exits Pointer Lock and nothing can override that (it has
to stay typable at a DOS prompt while Grab is on), so losing the lock no
longer un-arms the keyboard -- the mouse half simply pauses and the
`pointerdown` handler re-requests the lock the next time the picture is
clicked, since that click is itself the user gesture a new request
needs.

**Two new daemon primitives, not just the existing move/click.**
`mouse_down`/`mouse_up` press-and-hold a button independently (for a
drag), the same shape as `keydown`/`keyup`. **Their release is a
SEPARATE verb from the keyboard's**, `mouse_release_all`, on purpose and
this did NOT change when the button merged: an MCP or CLI caller can
still hold one without the other even though the web page's own button
now starts both together, and the first draft of this fix made
`release_all` cover both, which meant releasing the KEYBOARD would
silently drop a mouse button still down mid-drag. Caught before it
shipped by asking what happens when both are held at once; the
regression test for it is `test_mouse_down_up_release_all` in
`tests/test_core.py`.

**A Park button is the browser's own copy of the section 3 technique**:
fifteen `mouse_move -300 -300` calls, sequential, the same "slam into a
corner" primitive proven there, not a new one.

**A wheel, added and confirmed 2026-09-02**: `mouse_wheel` writes the
third field of the same HID report `mouse_move`/`mouse_click` already
write (the descriptor always reserved a byte for it, per section 6.5's
own note -- nothing had ever generated the value until now) and
`EV_REL`/`REL_WHEEL` on the uinput path, chunked into signed-byte HID
reports the same way a large `mouse_move` already is. The browser
coalesces wheel events on the same 40ms timer as mouse_move.
**Confirmed live against modernpc**: six `mouse_wheel dy=50` calls
visibly scrolled a terminal window's content (FINDINGS #49). **Still
unmeasured on PS/2** -- whether this system's protocol carries a wheel
through to DOS at all, and what CTMOUSE does with one if it arrives --
this sends the EV_REL event on that path too, but nothing has looked at
a screen to confirm it does anything.

**Still not built:** the Macintosh/ADB side (untouched, as section 6.4
already expected); an absolute-positioning mode for `hid-gadget` targets
like `modernpc` (Pointer Lock's relative deltas are correct for PS/2 and
ADB, both genuinely relative-only, but a modern target expects an
absolute pointer — a planned second HID function, not this one); and
touch as a mouse on a phone (Pointer Lock has no meaningful behavior on
a touchscreen, so Grab's mouse half is simply inert there rather than
shown as a separate, non-functional control).

## 9. Clicks are proven now, and an instant one can be missed entirely

**2026-09-15**, the first real DOS-native mouse-click exercise on this rig:
`sdldos`'s dosags/AGS real-hardware campaign needed to quit a game running
at an extreme ~0.05 fps (see `docs/lab/OPEN-FAULTS.md` sec. 27) by clicking
its QUIT menu item.

**`vcctrl mouse click` (instant press+release) fell between the game's own
input polls three times in a row and registered nothing.** A game reading
its input state once per render frame samples it far more rarely than usual
at 0.05 fps -- roughly once every 20 seconds -- and a press-then-release
that both happen inside one gap between polls is a state transition the
poll never catches, at either edge. **`mouse down`, held across a full poll
interval, then `mouse up` (a real release edge instead of an instantaneous
pulse) is what actually registered** -- confirmed in the target's own
`STDOUT.TXT` ("Mouse click over GUI 2" x3, then a clean "Quitting the
game..." / "ENGINE HAS SHUTDOWN").

**The general lesson, not specific to this one game:** an instant
click/keypress assumes the target polls faster than the gap between press
and release. That is normally true and was never worth stating -- it stops
being true against anything polling unusually slowly (an extreme frame
rate, a busy-loop under load, deliberately coarse polling to save cycles),
and the failure is silent: the call succeeds, nothing lands, and there is
no error to notice. Prefer an explicit `mouse down` / wait / `mouse up`
pair over `mouse click` whenever the target's own polling rate is unknown
or suspected slow.

## 10. Open questions

- Whether the acceleration factor is stable within one environment or varies
  with speed, which is what a real acceleration *curve* would imply. 1.49x was
  measured at one speed only.
- Whether any DOS program on the Gateway renders a cursor in mode 12h. If one
  does, it removes the need to start Windows for every mouse test.
- The Macintosh side, entirely.
- Whether this system's PS/2 mouse protocol carries a scroll wheel through to
  DOS at all, and what CTMOUSE does with one if so — unmeasured, see
  section 7.
