#!/usr/bin/env python3
"""Fix USB4VC's 32-bit `struct input_event` assumption for 64-bit userland.

A LOCAL patch, not offered upstream, carried here and applied on the rig.
Patches USB4VC (https://github.com/dekuNukem/USB4VC, MIT, dekuNukem) --
this file contains only a handful of one-line match anchors copied from it
to locate the patch site, not a redistribution of USB4VC itself.

THE BUG. usb4vc_usb_scan.py reads input events with a hardcoded layout:

    data = this_device['file'].read(16)     # struct input_event
    ...
    data = list(data[8:])                   # skip struct timeval
    if data[0] == EV_KEY: ...               # dispatch on type

`struct input_event` is `timeval + __u16 type + __u16 code + __s32 value`, and
`struct timeval` is two `long`s. On 32-bit armhf that is 8 + 2 + 2 + 4 = 16, so
the constants are right. On 64-bit arm64 a `long` is 8 bytes, timeval is 16 and
the struct is 24 -- so a 16-byte read returns ONLY the timestamp and `data[8:]`
is the tail of tv_usec.

MEASURED on the rig, one KEY_A press through the daemon's virtual keyboard:

    raw 24 bytes:  0365876a 00000000  1a630500 00000000  0100 1e00 01000000
                   |-- tv_sec (8) --| |-- tv_usec (8) -| type code  value

    usb4vc  (read 16, skip 8)  -> data[0] = 26      garbage from tv_usec
    correct (read 24, skip 16) -> data[0] = 1, code = 30   EV_KEY, KEY_A

EV_KEY is 1, so `if data[0] == EV_KEY` was comparing against 26 and never
firing. Every keystroke was discarded inside the app, well before SPI.

WHY IT LOOKED LIKE A CABLE. Nothing downstream ever saw a keystroke, so there
were no activity LEDs and nothing reached DOS -- while SPI status exchanges,
the OLED, board detection and protocol selection all worked perfectly, because
they are different code paths. It presented identically for a real USB keyboard
and for the daemon's virtual one, which is what ruled out vcctrl. The OLED's
debug view still showed keypresses because `my_oled.kick()` is called BEFORE
the parse: the UI noticed activity the send path then threw away.

THE FIX. Compute both offsets with struct.calcsize instead of hardcoding them,
so the same source is correct on 32-bit and 64-bit alike.

Idempotent. `--check` verifies without modifying, which is what a deploy should
run so an upstream update that reverts it fails loudly.

    sudo python3 patch-usb4vc-64bit.py [--check] [path/to/usb4vc_usb_scan.py]
"""

import sys

DEFAULT = "/home/pi/usb4vc/rpi_app/usb4vc_usb_scan.py"
MARKER = "vcctrl 64-bit input_event patch"

ANCHOR_CONST = "SPI_XFER_TIMEOUT = 0.025\n"
CONST_BLOCK = '''
# --- vcctrl 64-bit input_event patch --------------------------------------
# struct input_event = timeval + u16 type + u16 code + s32 value.
# 16 bytes on 32-bit, 24 on 64-bit, because timeval is two longs. Hardcoding
# 16/8 silently discards every event on a 64-bit kernel: the read returns only
# the timestamp and the type byte lands in the middle of tv_usec.
import struct as _vc_struct
VC_INPUT_EVENT_SIZE = _vc_struct.calcsize('llHHi')   # 16 on armhf, 24 on arm64
VC_INPUT_EVENT_HDR = _vc_struct.calcsize('ll')       # 8 on armhf, 16 on arm64
'''

OLD_READ = "                data = this_device['file'].read(16)\n"
NEW_READ = "                data = this_device['file'].read(VC_INPUT_EVENT_SIZE)\n"

OLD_SKIP = "            data = list(data[8:])\n"
NEW_SKIP = "            data = list(data[VC_INPUT_EVENT_HDR:])\n"


def main(argv):
    check = "--check" in argv
    args = [a for a in argv if not a.startswith("--")]
    path = args[0] if args else DEFAULT

    try:
        src = open(path).read()
    except OSError as exc:
        sys.stderr.write("cannot read %s: %s\n" % (path, exc))
        return 2

    if MARKER in src:
        print("already applied: %s" % path)
        return 0
    if check:
        sys.stderr.write(
            "PATCH MISSING from %s\n"
            "  On a 64-bit kernel this means NO INPUT EVENT EVER REACHES THE\n"
            "  PROTOCOL BOARD -- no keystrokes, no mouse, no activity LEDs,\n"
            "  while SPI status and the OLED keep working normally. Re-apply:\n"
            "    sudo python3 %s\n" % (path, sys.argv[0]))
        return 1

    missing = [n for n, s in (("const anchor", ANCHOR_CONST),
                              ("read(16)", OLD_READ),
                              ("data[8:]", OLD_SKIP)) if s not in src]
    if missing:
        sys.stderr.write(
            "ANCHOR(S) NOT FOUND in %s: %s\n"
            "  Upstream has changed this file. Refusing to guess at new\n"
            "  insertion points -- re-read it and update this script.\n"
            % (path, ", ".join(missing)))
        return 3

    src = src.replace(ANCHOR_CONST, ANCHOR_CONST + CONST_BLOCK, 1)
    src = src.replace(OLD_READ, NEW_READ, 1)
    src = src.replace(OLD_SKIP, NEW_SKIP, 1)
    open(path, "w").write(src)
    print("patched %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
