---
name: vcctrl-classic-mac
description: Driving a classic Macintosh (System 6/7 on a 512Ke, Plus or similar) through vcctrl's USB4VC Mac board (board 3) -- which USB key is Command and which is Option, why typing and clicks need slowing down, how to work menus, the pointer and the Finder, how to free a key stuck down on the Mac, and how to type a password without logging it. Use before sending any keyboard or mouse input to a board-3 target, or when its input seems dropped, stuck or wrong. Not for the DOS/PS/2 target (see vcctrl-rig-hazards).
---

# Driving a classic Mac with vcctrl

Everything here was measured on a Macintosh 512Ke running System 6.0.8,
2026-10-15 (Mouse Tracking: Tablet), through USB4VC's Lisa/Mac/ADB board (board 3, fw 0.1.0) and an
HDMI capture. A Plus or a System 7 Mac on the same board should behave the
same way. Where that is a guess, it says so.

**The Macintosh Plus (on the same cables since 2026-10-16).** It uses the same board, keymap,
converter and plug; `internal/navi/switch-mac.sh plus` relabels the profile.
Measured: the capture is still 1:1 at (65,70), 512x342, and `pointer-check`
moved 23 px for 21 expected, so its tracking is linear too. About the
Finder: System 6.0.8, Finder 6.1.8, MultiFinder on, 4,096K total, System
845K, largest unused block 3,091K. It boots from its own disks ("System 6",
"Data"), not the Floppy Emu's lean608. After Dark's Flying Toasters is its
screensaver. **Its AppleTalk 58.0 (Phase 2) drops most incoming frames to
user sockets** (Chooser lookup replies and talkip's MacIP; AEP echoes get
through), even with every INIT set aside. So it stays out of the System
Folder, in "AppleTalk 58 (off)" at the disk root, with AppleShare Prep, and
the Plus runs the ROM's AppleTalk v19 as the 512Ke does. AppleShare then
needs the `.XPP` driver (DRVR 40) in the System file, added with AppleShare's
XPP Install (2026-10-17). With that, the Chooser lists tashtalk, TashTalk
Share mounts, and talkip works (Fetch 5,639 bytes in 2.45 s, as on the
512Ke), so `share put` reaches the Plus. MacTCP is on LocalTalk, not the
Ethernet Built-In it had. Don't move AppleTalk 58 back (retromac).

## Modifier keys: Ctrl is Option, Alt is Command

USB4VC's Mac board does not map keys the way the keycaps suggest:

| send | the Mac sees |
|---|---|
| `leftmeta`, `rightmeta`, `lalt`, `ralt` | **Command** (all four) |
| `lctrl` | **Option** (`rctrl` is assumed the same, not measured) |

So Command-O is `combo leftmeta o` and Option-E is `combo lctrl e`. The
M0110 keyboard has no Control key, no function keys and no arrow keys, so
`f1`..`f12` and the arrows do nothing useful on a 512Ke. A Plus's M0110A
has arrows; untested.

## Typing: slow, or keys stick down

The Mac's keyboard link carries roughly 15-20 key transitions a second,
and the board **drops** what it can't pass on. A dropped key-up is a key
held down on the Mac, and vcctrl has no way to know:
`release-all` reports `released: 0` because the daemon did send the up.

- The profile sets `capabilities.input.settings.key_gap_s: 0.05`, a 50 ms
  floor between any two key events, including the browser's. `type` then
  runs at about 6 characters a second and arrives complete, with the last
  characters landing a second or more after the command returns. **Wait
  before you judge the result.**
- If a profile lacks it, pass `--pace 0.05` on the CLI. The default 12 ms
  lost most characters and left Shift plus four keys held.
- **Recovering a stuck key:** `hold <key> 150` for each stuck key, about
  300 ms apart. Auto-repeat of one character (`??????`) is a stuck key.
  Shift stuck shows as every later letter in capitals.

## Clicks and menus

- The Mac reads the button once per vertical blank (16.7 ms). The profile's
  `click_hold_s: 0.1` makes `mouse click` hold long enough. Without it, a
  12 ms click is silently ignored. Browser clicks were always fine.
- There's no double-click helper. Select with one click, then `combo leftmeta o`.
- **Menus are press-drag-release:** `mouse down left` on the title (it
  opens and stays open while held), `mouse move` down with the button
  still held, `shot` to see which item is highlighted, then
  `mouse up left`. Release over nothing to cancel.
- **Never `mouse goto` while a menu is held open.** goto parks the
  pointer in a corner and nudges it to find the cursor, which drags off
  the menu, so the release lands on nothing. `goto` to the menu TITLE
  first, press, then use a relative `mouse move` to the item.
- **Drag to copy:** `mouse down` on the icon, moves, `shot`, then
  `mouse up`. The drop target highlights when you're over it. Dropping on
  another disk or volume copies, and the source stays.

## The pointer: linear once nothing clips it

Two facts, both measured on the 512Ke with Control Panel → Mouse set to
**Very Slow (Tablet)**, which turns acceleration off:

- **The Mac board clips one large event.** A single relative event of
  110-400 counts moved the pointer the same ~17 px as 100 counts, while the
  same distance in smaller events moved its full length. The profile's
  `mouse_max_step: 100` splits a move into events of at most 100 counts,
  and `mouse_step_gap_s: 0.03` spaces them, because steps 12 ms apart get
  merged again downstream and clipped as one. With both set, a move is
  linear.
- **Compute in MAC px, never capture px.** About 0.18 Mac px per count on
  both axes (the profile's `counts_per_px` 5.65 × 5.4). Where a capture
  stretches the screen, capture px differ per axis: with the 512Ke's old
  511x455 picture it was about 0.19 across but 0.24 down, and a peer
  computing drags from capture px overshot the bottom edge three times.
  The 512Ke's capture is 1:1 now (see Video), but Mac px stays the rule.
- So **compute the move, then correct once.** One move from a known spot
  lands within a few pixels. Earlier advice here to "look, don't count"
  was really about the clipping, now fixed. Still look before you click.
- **For a known position, park it:** a few `mouse move -2000 -2000` pins it
  in the top-left corner (edges clamp). The Apple menu is then about
  +125, +40 counts away.
- **Small events are NOT linear** (measured 2026-10-16, the same at any
  spacing): 1 count moves nothing, 2 counts 1 px, 3-6 counts ~1.5 px, 12
  counts 3 px, 20 counts 4 px; from ~30 counts on it's the linear 0.177
  px/count. So 40 `mouse move 3 0` go 62 px, not 21. For small steps use
  `mouse px DX DY` (native px): the daemon picks each event's counts from
  the profile's measured curve (`pointer.settings.event_px`). 40 × `px 1 0`
  went 41 px.
- If tracking is set faster than Tablet, the Mac accelerates and none of
  the arithmetic holds. Check Control Panel → Mouse.
- Find the cursor by eye in a `shot`, cropped and zoomed. Automatic scans
  for "a run of black pixels" latch onto window title bars and inverted
  labels. **The hotspot is the arrow's tip:** a click with the tip on a
  close box's border misses, so aim a few pixels inside.

## Pointer verbs: use these instead of steering by eye

With `capabilities.pointer` (backend `video-diff`) configured, as on the
512Ke's profile:

- `vcctrl cursor` (MCP `vcctrl_cursor`) nudges the pointer, diffs the
  picture, puts it back, and returns its `hotspot` in capture px and
  `native` in the Mac's own screen px (0..511 × 0..341 on a 512Ke), and
  `shape` (arrow: hotspot at the tip; crosshair / ibeam / watch: the middle;
  sizes are judged in Mac px because the capture is stretched).
- `vcctrl mouse goto X Y` (MCP `vcctrl_mouse_goto`): park, one computed
  move, one correction. It lands within about 1 px in ~4 s
  (`--no-refine`: open loop, within a few px). Follow it with
  `mouse click`. **This is the normal way to click on something.**
- `vcctrl pointer-check` (MCP `vcctrl_pointer_check`), and `verify-input`
  on this board, is the input round trip: `verified: true` means the
  pointer moved and came back on screen. A `warning` means the scale is
  off, i.e. tracking is no longer Tablet (a PRAM reset does that). It
  sends no keystroke.
- To find a target's native coordinates from a `shot`: native =
  (capture − picture origin) × native size / picture size. On the 512Ke
  the converter now outputs 640x480 at 1x, so that's just x = cx − 65,
  y = cy − 70.
- In the web KVM a click on the picture **grabs** it, like any KVM: the
  pointer is captured and the Mac's pointer moves with your hand, 1:1 with
  the picture as drawn (Grab sends `mouse_px` with `queue: true` every
  tick; the daemon merges whatever arrives while it's busy and sends it in
  diagonal steps; a button waits until those moves are sent). "Click to point" (Settings) is
  opt-in since 2026-10-16: it makes a click a goto plus a click, ~4 s each
  with a jump to the corner first, which the owner found unlike a KVM.
- Limits: the diff assumes the dark arrow on a lighter ground. A blinking
  insertion point or the Alarm Clock beside the pointer can widen it, and
  the I-beam and watch cursors have different hotspots.

## Dragging, selecting, deleting (lessons from the peer that drove it)

- **`vcctrl mouse drag X1 Y1 X2 Y2`** (Mac px): goto the source, press, one
  computed move with no park, release. It always releases the button.
  Never `goto` with the button held: it parks in the corner first and drags
  the icon across the screen. `cursor` is unreliable *mid*-drag, because the
  icon outline confuses the diff (once ~50 px out), so check with a `shot`.
- **Select first, then drag.** A press in a gap of an icon's artwork
  (between Disk Copy's two floppies, or just above an icon) starts a
  selection rectangle, and its release can select two icons. Goto a solid
  part of the artwork, click, and zoom-check that only that icon is
  inverted. Then drag from the same spot.
- **A click on "empty" space can hit a window behind** and raise it, so the
  next press grabs that window's title bar. Look before you click.
- **The Trash's drop target is the can's body**; the lid and label edges
  don't highlight it. Dropping an application asks "Are you sure you want
  to remove the application …?" right away, at the drop (Return = OK). Then
  Special → Empty Trash.
- The stop alert "The Trash couldn't be emptied…" did NOT take Return:
  click its OK button.
- **A file on the share is "busy" while any SMB client holds it open**
  (fruit:locking = netatalk), so the Mac can't delete it, and the Finder
  puts it back. Close it on the other machine first.
- **Get Info** (Command-I on a selected icon) shows a name the window cuts
  off, plus kind and size. Command-W closes it. Useful when two icons
  overlap exactly (a copy keeps the original's position).
- **Timing:** the Finder's redraw after quitting an app took ~30 s, and a
  24 KB copy from the share ~20 s. Wait before judging.

## Uploads (shared folder)

With `capabilities.share` (backend `smb`): `vcctrl share put <file> [name]`
(or `-` for stdin: `cat f.bin | ssh <host> vcctrl share put - f.bin`), MCP
`vcctrl_share_put` / `vcctrl_share_list`, or the web KVM's "Upload file".
Files land in the share's `Uploads` folder. A MacBinary `.bin` becomes a
real Mac file (resource fork, type/creator), and `.txt` gets TEXT. On the
Mac, close and reopen the Uploads window to see it (the System 6 Finder
doesn't refresh an open server folder). Replacing a file works the same
way: quit the old copy, `share put` the new one under the same name, close
and reopen Uploads, and the Mac runs the new version (no remount
needed). Apps run straight from the share, even on a 512K machine (the
real 512Ke's About the Finder: System 224K, 288K left for the app). After
an app quits, the Finder takes **~45 s** to come back while it rereads
open AppleShare windows. That's normal: wait for it before the next
step, or close server windows first to make it quicker. A name
with no `.txt` and no
MacBinary wrapper arrives with no type, as a blank generic document.
Requires an SMB account, `smbclient` and a credentials file on the daemon
host. Configured on the 512Ke's profile and on the synthetic twin (both
upload to the same AppleShare server's share).

## Shutting down (the measured sequence)

Done on the 512Ke on 2026-10-15, at the owner's request. Use exactly this:

1. Check nobody is driving (lock, recent `browser` input), take the lock,
   and narrate with `note-set`.
2. `mouse goto 184 8` (Special, in Mac px), then `mouse down left`.
3. With the button held: `mouse move 0 540` (Shut Down is the LAST item,
   ~116 Mac px down; 626 counts overshot into the window below). Take a
   `shot` and check the Shut Down row is inverted and Restart is not. If
   nothing is highlighted you're off the menu, so move back by ±90.
   If Restart is highlighted (2026-10-16: 540 stopped there), `mouse move 0 60`
   and shot again. The shot lags ~0.4 s, so wait a second before judging.
4. `mouse up left`. The Finder closes windows and unmounts AppleShare
   volumes over LocalTalk, with the watch cursor up for **30-60 s**.
5. Wait for **"You may now switch off your Macintosh safely."** (a dialog
   with a Restart button; the 512Ke has no soft power-off). Only then:
6. `vcctrl power off` (the plug; owner's go-ahead). Proof it's off:
   `power state` shows `on: false` and **0 mW**. NOT the video: with the Mac
   off, the RGB2HDMI emits a flat WHITE frame, which `shot` still calls a
   picture.
7. Closing `note-set` ("… shut down and powered off at the plug"), release
   the lock, and tell any peer.

To start it again: `vcctrl power on` (owner's OK). The 512Ke boots from a
Floppy Emu in HD20 mode (it has no SCSI port). AppleShare volumes are not
remounted at boot: no volume is set to open at startup, and the owner wants
it kept that way (2026-10-16). Mount by hand when needed (Chooser login, see
AppleShare below); don't tick "open at system startup" or save the password.

**If it stops at a plain disk icon:** a grey pattern with a static floppy
icon in the middle, no question mark, and `framestats` showing one
distinct frame. The boot device isn't answering. On 2026-10-16 the Floppy
Emu showed a checksum error. Before that, the Finder sat on the watch
cursor after an app quit and the Mac bombed ("address error") ~6 min
later. The owner tapped the Floppy Emu's button, it restarted, and the Mac
booted. No vcctrl verb reaches that button, so ask the owner. Don't blame
the app that was running until the boot device is ruled out.

**If the Finder loops while opening its windows at startup:** the desktop
comes up, the Finder opens the first saved window, then the screen goes
white with an empty window outline that keeps flashing under the watch
cursor. Left alone it ends in a frozen screen of repeating garbage that
looks like a hardware fault but isn't (2026-10-16, three boots in a row). The
Finder is out of memory: this volume has ~2,800 files in 512K. Fix: power
on, `keydown leftmeta` and `keydown lctrl` (Command+Option) once "Welcome to
Macintosh" shows, hold until "Are you sure you want the desktop rebuilt…",
`keyup` both, Return. On this disk the rebuild ran ~4 min and ended with
"could not be completely rebuilt (out of Finder memory)" (click OK, it
didn't take Return); the Finder then came up with no windows open. Shut
down with as few windows open as you can, because the Finder reopens them
all at the next boot.

**Two boot disks (2026-10-16):** the Floppy Emu card holds the big original
`macos608` (about 2,800 files, so the Finder's memory is tight) and a lean
`lean608` built for real-hardware app tests: the System Folder without
the 512K extras, which sit in `Optional (512K)` with MacTCP and the TalkIP
Driver, and only the apps under test. About the Finder: System 186K and
326K free on lean608, against 224K/288K on macos608. Which one booted is part
of any memory measurement, so record it. The owner picks the disk in the
Floppy Emu's menu; no vcctrl verb can.

**The bench camera:** the 512Ke's rig has a room camera on its logic board
(`vcctrl camera`, and the web KVM's camera panel), 1080p MJPEG. Filming the
CRT in auto exposure, it showed a dark bar rolling down the screen. That's
the camera's exposure beating against the 60.15 Hz refresh, not a sync
fault in the Mac or the capture. The profile's
`capabilities.camera.settings.controls` now fixes the exposure at one CRT
frame (`exposure_time_absolute: 166`) with 60 Hz anti-flicker. The daemon
sets them before each spawn, and `camera state` shows `controls_error` if
one is refused.

**Disk First Aid can't check the HD20 volume.** DFA v7.2.2 (macos608 ›
Setup and Repair) lists it as "Floppy Emu (Startup Disk)" and warns that a
startup disk can only be verified. Verify then reports "This is not an HFS
disk" and stops, although the Mac boots and reads that volume fine
(2026-10-16). That's could-not-look, not damage. Check a copy of the image
offline instead:

1. Shut the Mac down and power it off (above). The owner pulls the Floppy
   Emu's SD card. The image is `macos608-512ke.dsk` (1,971,322,880 bytes,
   a raw HFS volume). Mount the card read-only (`mount -o ro`), stream the
   file to a private path, and compare sha256 on both ends. The image is
   private, so keep it out of git and notes.
2. `hfsck -n -v <copy>` (hfsutils): the volume header and both B-trees.
   Debian ships no hfsck. Build it from the hfsutils 3.2.6 source, and patch
   main.c, because upstream `-n` never opens the volume and fails with
   "Input/output error". Then walk the catalog (`hmount`, `hls -R -a -l :`)
   and check the counts against the header's drFilCnt/drDirCnt.
3. `tools/hfs-alloc-check.py <copy>`: the bitmap against every fork's
   extents (hfsck doesn't check that). Exit 1 means cross-linked blocks, a
   used block marked free, or a short fork.

Result on 2026-10-16: clean. 2776 files and 457 folders, matching the header;
the bitmap's free count matched; nothing cross-linked. 53 blocks (~1.6 MB) are
leaked: marked used but owned by no file, which is harmless and also on the
older delivered image. So the bomb that day was the Floppy Emu, not the
disk.

## Restart, screensaver

- Restart: Special → Restart (press-drag the menu). There is no Power key
  to send. Hard restart: the plug (`vcctrl power cycle`), but only with the
  owner's go-ahead, because it cuts power with no shutdown.
- After Dark is on the disk. If a screensaver is running, the picture
  changes on its own and `pointer_check` may fail or find nothing. Wake it
  with `mouse move 40 0` then `mouse move -40 0`: **a 1-count move does
  nothing** on this board (see `event_px`), so it doesn't wake anything. Wake
  it right before the step. Once on the Plus it blanked again between a wake
  and a `pointer-check`, which then reported a whole-screen diff and a scale
  warning; a rerun passed.

## Caps Lock: tracked, never read

- The M0110's Caps Lock is a latch inside USB4VC's Mac board. It survives
  USB4VC restarts and Pi reboots, and the board reports no LEDs, so
  nothing can read it from the target.
- With `capabilities.capstrack` (backend `evdev-latch`), vcctrld counts
  every Caps Lock press on every keyboard (the operator's and its own) and
  flips a saved latch: `vcctrl capslock` (MCP `vcctrl_capslock`). It is
  `unknown` until set from what Key Caps shows: `vcctrl capslock set
  on|off`. Re-set it whenever you've checked in Key Caps.
- The KVM's C lamp shows it dashed (not a live reading). The key panel
  says "probably on" when it's on.
- Trust it in proportion to `presses_since_set`. A missed press (while the
  daemon was down, or dropped by the board) desyncs it until the next set.

## The link lamp and verify on the page

The web KVM's Verify button and the lamps' tooltip use the pointer round
trip on this board ("Link proven Ns ago by moving the pointer"), with the
same rules as the PS/2 check: a power change voids an old proof.

## No sound

The 512Ke's video goes through an RGB2HDMI, which carries no audio, so
there is nothing to capture. `audio: none` is the hardware, not a gap.

## Easy Access, keypad and Clear

- The keypad reaches the Mac (`kp0`..`kp9` and friends light in Key Caps),
  and `numlock` is the Mac's **Clear** key.
- Mouse Keys (⌘⇧Clear: `combo leftmeta lshift numlock`) did **not** turn
  on, on this 512Ke's System 6.0.8, probably because the Easy Access INIT
  isn't installed. With it installed, the keypad would move the pointer
  pixel by pixel. Untested.
- CloseView is installed (in Control Panel). It magnifies the screen,
  which doesn't help a fixed-resolution capture.

## AppleShare (a worked menu-and-dialog flow)

Chooser (Apple menu, press-drag) → click AppleShare → click the server
name → Return (OK) → the login dialog. **Click into the Password field
first**, then type it with `type --secret` (see Passwords) → Return. The
login can take about 10 s over LocalTalk, so wait before judging. If
nothing has happened after ~15 s, click OK instead (once on the
synthetic twin Return did nothing there, and clicking OK went straight
through). Pick the
volume → Return. The volume appears on the desktop, and the Chooser closes
from its close box. If the server's file server restarted, an open
Chooser fails: close and reopen it, then dismiss the "file server has
closed down" alert with Return.

## Finder and desk accessories

- System 6's Finder does **not** select an icon when you type its name.
  Typed letters with nothing editable do nothing.
- Desk accessories (Key Caps, Calculator...) take Command-W as an ordinary
  keystroke. Close them with the close box, at the left of the title bar.
- **Key Caps (Apple menu) is the keyboard test bench.** It saves nothing,
  lights every key the Mac thinks is held, and redraws the keyboard for
  the modifier layer in force: uppercase means Shift or Caps Lock, and
  ™ £ ¢ ∞ means Option. Use it to check typing, to find stuck keys, and to
  see whether Caps Lock is on. Caps Lock has no LED channel on this board,
  so the daemon cannot report it.

## Video

- The capture is a digital HDMI path (`analog: false`). `state: frozen` on
  a still desktop is normal, not a fault.
- **To record a clip** (a window opening, a glitch): `T=$(date +%s.%N)` just
  before the action, then afterwards `vcctrl record --out f.avi --since $T
  --clip`. Without `--clip`, a `--since` later than the ring's oldest frame
  (it holds ~30 s) is refused with exit 4 rather than mixing in older frames.
- An HDMI converter may stretch the 512x342 screen, for example to 511x455
  of a 640x480 frame. The profile's `machine.picture` lets the web KVM
  restore 3:2. `shot` coordinates are in capture pixels, stretch included.
- A non-integer stretch bands 1-px lines: title-bar stripes look uneven or
  "shifted" (342 lines on ~455 rows alternate solid black and grey pairs).
  It isn't the Mac. Set the RGB2HDMI's Resolution to 640x480 with Integer
  scaling (Main Menu, Save Configuration, exit: it restarts), then
  re-measure `machine.picture`. On the 512Ke that gave 512x342 1:1 at
  (65,70) (2026-10-15).
- **A scaled view of the grey desktop shows a coarse checkerboard.** The
  System 6 desktop is a 1-px 50% dither, and any downscale (an image viewer,
  the web KVM at a non-integer size) aliases it into big diagonal checks.
  That isn't the converter mis-sampling: check the raw `shot` at pixel level
  (rows should alternate `#.#.` / `.#.#`). On the Plus (2026-10-16) it was
  exact.

## Passwords

Anything sent with `type` is kept word for word in the activity bus,
`/events` and the input log on disk. For a password:

- shell: `<secret source> | vcctrl type --secret`. It reads stdin only and
  refuses text on the command line. The logs show `(secret, N chars)`.
- web KVM: the lock button beside the command field, for one line.
- MCP: there's no secret mode, deliberately. A secret handed to a tool is
  already in the agent's context, so use the shell pipe instead.

## The synthetic twin (profile snow512)

A true Macintosh 512Ke in the Snow emulator (an HD20 build) on the control
host, for trying things without the real machine: its own vcctrld profile,
driven with the usual verbs and MCP tools. The same recipes hold.
`key_remap` makes Ctrl Option and Alt/Meta Command, exactly as on the
512Ke. Since 2026-10-16 it boots a scratch copy of the real 512Ke's own
disk, copied off the Floppy Emu's SD card, so the files and System Folder
are the same as of that day. Changes on either side don't carry over to the
other. It's on the same LocalTalk network (LocalTalk over UDP to the same
router).
Differences:
- `power on`/`off` start and stop the emulator, not a plug. **It's off when
  unused:** check `power state` and `power on` first, wait ~15 s for the
  Finder, and `power off` when you're done. A timer switches it off after 30
  min with no input and no lock (2026-10-16: it cost ~1.8 of the host's
  cores and kept it hot). Its display and capture start and stop with it.
- The pointer is absolute and exact: `goto` lands within 1 px. Within
  ~12 px of the screen's right-hand rounded corners the cursor's shape
  can misread.
- A 1.4 MB AppleShare copy runs at about 17 KB/s, like real LocalTalk.
- A `shot` taken right after an input usually still shows the screen from
  before it. On 2026-10-16 the Apple menu first showed in a `shot` a median
  374 ms (336-433) after `mouse down` was sent: the Mac draws the menu, then the
  capture chain adds ~100-150 ms. Wait about 0.5 s, or shoot again until two
  shots agree, before you judge. Not measured on the real 512Ke.
- It does **not** model the serial chip's (SCC) receive timing. A LocalTalk
  listener that passed every emulator run lost 43 of 57 frames to SCC
  overruns on the real 512Ke, until it read the rest of each frame first.
  A pass on the twin isn't a pass on the hardware for timing-sensitive
  serial or network code: rerun it on the real Mac.

## Not supported here

- No power control on a rig without a plug. Power and reset are done by hand.
- No LEDs (Caps Lock is tracked, see above), no file transfer channel
  (`files: none`; AppleShare is the way), and no audio through the RGB2HDMI.
- Ctrl+Alt+Shift may not release a web-KVM grab where the desktop takes
  Alt (seen on Cinnamon/X11). Ctrl+Shift+F12 does.
