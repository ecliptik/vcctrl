# Knowing which protocol board is installed

Written 2026-08-20. **Implemented the same day** — this now describes what the
daemon does, except sec. 6 (mouse) and sec. 5's `leds` item, which are still
open.

    $ vcctrl board
    {"id": 3, "name": "Apple Lisa/Mac/ADB", "target": "Macintosh Plus",
     "source": "status-file", "stale": false, "reason": null}

Shipped in `d251a44`: `BoardCapability` in the daemon, `board` in
`/state.json`, `vcctrl board` in the CLI, a `board.changed` event on the bus,
and `tools/patch-usb4vc-board.py` as the local rpi_app patch with a `--check`
mode that `pi/install.sh` runs so an upstream update cannot drop it silently.

**Sec. 5's `power` design was wrong and is corrected below.** The rig has ONE
Kasa plug with one machine connected at a time, so there is no board→plug table
to build and nothing to refuse against. What remains is unobservable rather
than enforceable: vcctrl can know which BOARD is installed and cannot know
which MACHINE is on the socket. The honest answer is to display the board
beside the power control so a mismatch is visible — a refusal keyed to a table
would have been a check that fabricates its answer, which is the failure this
whole document exists to avoid. The operator also asked for a confirmation on
Off and Cycle in the KVM, which asserts nothing about the machine and only
states what the action does.

Two contract details settled with the webkvm session and worth keeping:
`on` is **tri-state** — null when the plug stops answering, because "the
machine is off" and "I cannot reach the plug" are opposite facts that must not
share a JSON value — and the cache has a 60 s heartbeat, because without one
`stale` latches true after the first idle hour and a permanently-set flag
carries no information.

The rig now has two targets: the Gateway 2000 over a USB4VC **IBM PC** board,
and a Macintosh Plus over a USB4VC **Lisa/Mac/ADB** board. One USB4VC, boards
swapped by hand, so the installed board changes across a power cycle.

Everything below exists because **vcctrl currently has no idea which machine it
is driving**, and several of its capabilities are wrong in dangerous ways when
it guesses.

## 1. The IDs, read off the running system

`usb4vc_shared.py`:

    PBOARD_ID_UNKNOWN            = 0
    PBOARD_ID_IBMPC              = 1     # the g2k
    PBOARD_ID_ADB                = 2
    PBOARD_ID_APPLE_LISA_MAC_ADB = 3     # the Mac Plus, installed today

`rpi_app` learns this from the STM32 over SPI at startup and holds it in a
module global, `this_pboard_id`. It reaches the log as the status frame, where
byte 3 is the ID:

    PB INFO: [205, 0, 128, 3, 0, 0, 1, 0, 131, 134, 9, 10, 11, ...]
                            ^ PBID 3

## 2. `config.json` is NOT a source of truth, and this is measured

The obvious place to look is `/home/pi/usb4vc/config/config.json`, which is
keyed by board ID:

    {"rpi_app_ver": [0, 3, 1], "3": {"keyboard_protocol_index": 2, ...}}

**On the Pi 3 that file said `"3"` — the Mac board — for the entire time it was
driving the g2k over PS/2.** `rpi_app` only writes a section when settings are
changed through the OLED menu, so the file records boards that were once
configured, not the board that is present. Reading it would have returned the
wrong machine with total confidence.

This is the same shape as every other defect found this week: a value that is
real, and is about something other than the question being asked.

## 3. Where the answer comes from instead

Three sources, in order, each degrading honestly.

### 3a. A status file, written by our local patch

A small **local** modification to `/home/pi/usb4vc/rpi_app/usb4vc_ui.py` —
not submitted upstream, carried as a patch on this rig — writing the board it
just read:

    /run/usb4vc/board.json
    {"id": 3, "name": "Lisa/Mac/ADB", "fw_ver": [0,1,0], "hw_rev": 3, "t": 1787…}

**`/run` is tmpfs, and that is the point.** The file cannot outlive the boot
that wrote it, so the `config.json` failure — a stale value that reads as
current — is structurally impossible rather than merely unlikely.

Carry the patch in-repo (`dos/`-style, or a new `usb4vc-patches/`) with a test
that it still applies, so an upstream update that breaks it fails loudly at
deploy time rather than silently reverting the behaviour.

### 3b. The log, as fallback

`journalctl -u usb4vc` and parse the most recent `PB INFO` frame, byte 3. No
divergence at all, but it depends on an upstream print statement's format, so
it is the fallback rather than the primary.

### 3c. Unknown, said out loud

If neither is available — `usb4vc` not running, no frame yet — the answer is
`unknown` **with a reason**, never a guess and never a default to IBMPC:

    "board": {"id": null, "name": null, "source": null,
              "reason": "usb4vc service is not running"}

Three states, for the reason `verify_profile()` has three: "could not look" and
"looked, and it is a Mac" must not collapse into one value, because the caller
acts differently on each. See FINDINGS sec. 24.

## 4. What vcctrl exposes

`caps` and `/state.json` gain a `board` object as above. `vcctrl board` prints
it. The web KVM reads it from `/state.json`, which it already polls.

### 4.1 `keyboard` — which keyboard the KVM draws

Added 2026-08-25 with the KVM's full on-screen keyboard. One more
always-present key in the same object:

    {"id": 1, "name": "IBM PC", "target": "Gateway 2000",
     "keyboard": "pc-at-101", "source": "status-file", ...}

The value is an opaque **layout id**. The daemon never draws a key and
deliberately knows nothing about what is on one — it publishes only which
layout this board asks for, and the page resolves that against its own table
of keyboards it can draw. That split is what lets a page older than its config
say *"board 1 asks for a layout I do not have"* rather than quietly showing
the wrong keyboard.

Source, in the same order as `target`: a `keyboard:` word on the matching row
of `targets:` in `vcctrl.yaml`, else the built-in `BoardCapability.KEYBOARDS`.
A configured `targets:` list **replaces** the built-in table wholesale, so a
row with no `keyboard:` yields `null` — which means *this board is known and
no layout has been declared for it*, not *use the default*. That is deliberate
and it is the same rule `target` already follows: merging would let a rig that
configures only board 1 inherit this rig's board 3.

`null` is a first-class answer and the page draws no keyboard on it. It must
not fall back to `pc-at-101`, because a Macintosh drawn as a PC is a picture
of a keyboard that is not in the building — the same failure as reporting a
Macintosh Plus that is not in the building, one layer up.

**What this field does not say.** It names the keyboard the machine HAS. It
asserts nothing about which of those keys survive the trip: the Pi hands raw
evdev codes to the STM32 and the Linux→PS/2 mapping lives in that firmware, so
coverage is measurable only at the target and has not been measured. See
`docs/WEBKVM.md` sec. 5.2; the page carries that unknown itself rather than
implying this field settles it.

## 5. Per-board capability semantics — and one real hazard

This is the part that matters more than the identity itself.

| Capability | IBMPC (g2k) | Lisa/Mac/ADB (Mac Plus) |
|---|---|---|
| `input` keyboard | yes | yes, different protocol — USB4VC translates, so vcctrl still just sends uinput events |
| `input` mouse | present, **never exercised** | **primary interface** |
| `video` | VGA stick | RGB2HDMI → HDMI-USB capture |
| `audio` | VGA stick's line-in | not planned |
| `leds` | PS/2 LED return channel | **does not exist** — no ADB equivalent |
| `power` | Kasa plug 192.0.2.46 | **WRONG TARGET — see below** |

**`power` is the hazard.** `vcctrl power cycle` drives one specific smart plug,
which is the Gateway's mains. With the Mac board installed, that command would
still cut power to the *Gateway* — a machine the operator is not looking at and
may be mid-run. Nothing in the current design prevents this, because nothing in
the current design knows the board changed.

So `power` must become board-scoped: refuse, loudly, when the installed board
is not the one the configured plug belongs to. The config grows from a single
`kasa_host` to a mapping of board → plug, and an unmapped board means refusal
rather than a default. **A default here would be a power cycle of the wrong
computer.**

`leds` has a milder version of the same problem: `at_prompt()` and the whole
LED round-trip are IBM-PC-specific and simply have no meaning on ADB. They
should report unavailable for board 3, not fail obscurely — otherwise a Mac
session looks like a broken PS/2 session.

## 6. The mouse, which has never been tested

`vcctrl mouse move|click` exists, the uinput device exists, and there is no
finding anywhere recording that a cursor has ever moved on any target. On the
g2k that was acceptable because everything is keyboard-driven. **On the Mac it
is the whole interface.**

First real test, and it should be a closed loop rather than a keypress count:

1. Board reports 3, RGB2HDMI feeding the HDMI capture stick, picture locks
2. `vcctrl mouse move 200 0`, capture, compare frames
3. The cursor moved, or it did not — read it off the picture

Same discipline as driving the Cave Story menu by cursor position rather than
by counting keypresses: the harness has eyes, so close the loop.

Note PS/2 and ADB mice are both **relative**, so there is no "put the cursor at
320,240" — only deltas. Any Mac automation has to home the cursor by slamming
it into a corner first, then move by deltas from there.

## 7. Sequencing

1. Local patch + status file, with the log fallback and the honest unknown
2. `board` in `caps` / `/state.json` / `vcctrl board`
3. **Board-scope `power` before anything else uses this** — it is the one item
   that is a safety fix rather than a feature
4. Mark `leds` unavailable on non-IBMPC boards
5. Mouse round trip on the Mac, verified from capture
6. Only then consider what a Mac benchmark cell would even mean
