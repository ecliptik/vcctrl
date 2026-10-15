---
name: vcctrl-classic-mac
description: Driving a classic Macintosh (System 6/7 on a 512Ke, Plus or similar) through vcctrl's USB4VC Mac board (board 3) -- which USB key is Command and which is Option, why typing and clicks need slowing down, how to work menus, the pointer and the Finder, how to free a key stuck down on the Mac, and how to type a password without logging it. Use before sending any keyboard or mouse input to a board-3 target, or when its input seems dropped, stuck or wrong. Not for the DOS/PS/2 target (see vcctrl-rig-hazards).
---

# Driving a classic Mac with vcctrl

Everything here was measured on a Macintosh 512Ke running System 6.0.8,
2026-10-15, through USB4VC's Lisa/Mac/ADB board (board 3, fw 0.1.0) and an
HDMI capture. A Plus or a System 7 Mac on the same board should behave the
same way. Where that is a guess, it says so.

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
- **Drag to copy:** `mouse down` on the icon, moves, `shot`, then
  `mouse up`. The drop target highlights when you're over it. Dropping on
  another disk or volume copies, and the source stays.

## The pointer: look, don't count

- It's relative and the Mac accelerates: the same counts move it
  different distances depending on packet size and speed. As a rough
  guide, 100 counts per call moved about 18 px of the 640x480 capture.
- **For a known position, park it:** about 20× `mouse move -100 -100` pins
  it in the top-left corner. The Apple menu is then about +125, +40 counts away.
- Find the cursor by eye in a `shot`. Automatic scans for "a run of black
  pixels" latch onto window title bars and inverted labels. When in doubt,
  crop and zoom the region.
- Screen edges clamp, so overshooting into an edge is a safe way to align.

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
- An HDMI converter may stretch the 512x342 screen, for example to 511x455
  of a 640x480 frame. The profile's `machine.picture` lets the web KVM
  restore 3:2. `shot` coordinates are in capture pixels, stretch included.

## Passwords

Anything sent with `type` is kept word for word in the activity bus,
`/events` and the input log on disk. For a password:

- shell: `<secret source> | vcctrl type --secret`. It reads stdin only and
  refuses text on the command line. The logs show `(secret, N chars)`.
- web KVM: the lock button beside the command field, for one line.
- MCP: there's no secret mode, deliberately. A secret handed to a tool is
  already in the agent's context, so use the shell pipe instead.

## Not supported here

- No power control on a rig without a plug. Power and reset are done by hand.
- No LEDs, no file transfer channel (`files: none`), no audio.
- Ctrl+Alt+Shift may not release a web-KVM grab where the desktop takes
  Alt (seen on Cinnamon/X11). Ctrl+Shift+F12 does.
