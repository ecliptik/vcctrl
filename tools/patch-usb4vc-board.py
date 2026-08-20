#!/usr/bin/env python3
"""Publish USB4VC's protocol-board identity to /run/usb4vc/board.json.

A LOCAL patch to the USB4VC application. Deliberately not offered upstream --
the operator's call -- so it is carried here and applied on the rig.

WHY THIS EXISTS. vcctrl drives two machines through one USB4VC: a Gateway 2000
over the IBM PC board and a Macintosh Plus over the Lisa/Mac/ADB board, swapped
by hand. Several vcctrl capabilities are wrong in useful-to-dangerous ways when
it does not know which is installed, so it has to be able to ask.

WHY NOT config.json. That file is keyed by board id and looks authoritative.
On the Pi 3 it read {"3": {...}} -- the Mac board -- for the entire time that
machine was driving the Gateway over PS/2, because rpi_app only writes a
section when settings are changed through the OLED menu. It records boards that
were once configured, not the board that is present.

WHY /run. It is tmpfs. The file cannot outlive the boot that wrote it, so the
config.json failure -- a stale value that reads as current -- is structurally
impossible here rather than merely unlikely.

Idempotent: running twice inserts once. Run with --check to verify the patch is
still applied without modifying anything, which is what a deploy should do so
an upstream update that drops it fails loudly instead of silently reverting
vcctrl to guessing.

    sudo python3 patch-usb4vc-board.py [--check] [path/to/usb4vc_ui.py]
"""

import sys

DEFAULT = "/home/pi/usb4vc/rpi_app/usb4vc_ui.py"

MARKER = "vcctrl board publish"

# Anchored on the line that finishes populating the board record. Anchoring on
# fw_ver rather than on `this_pboard_id = ...` is deliberate: at the assignment
# the name is known but hw_rev and fw_ver are not, and publishing a half-filled
# record would be its own small version of the problem this file exists to fix.
ANCHOR = ("        pboard_database[this_pboard_id]['fw_ver'] = "
          "(pboard_info_spi_msg[5], pboard_info_spi_msg[6], pboard_info_spi_msg[7])\n")

BLOCK = '''
    # --- vcctrl local patch: publish board identity ------------------------
    # Written where rpi_app has just finished learning the board over SPI.
    # Raw facts only -- which COMPUTER a board implies is vcctrl's mapping to
    # own, not this file's, so there is one table rather than two that drift.
    # Wrapped so that a failure here can never take down input injection: an
    # unwritable /run is a vcctrl inconvenience, not a reason to stop driving
    # the target.
    try:
        import json as _vc_json, os as _vc_os, time as _vc_time
        _vc_rec = pboard_database.get(this_pboard_id, {})
        _vc_os.makedirs('/run/usb4vc', exist_ok=True)
        _vc_tmp = '/run/usb4vc/.board.json.tmp'
        with open(_vc_tmp, 'w') as _vc_f:
            _vc_json.dump({
                'id': this_pboard_id,
                'name': _vc_rec.get('full_name'),
                'fw_ver': list(_vc_rec.get('fw_ver') or ()),
                'hw_rev': _vc_rec.get('hw_rev'),
                't': _vc_time.time(),
            }, _vc_f)
        # Rename rather than write in place: a reader must never catch a
        # partially written file and parse it as truth.
        _vc_os.replace(_vc_tmp, '/run/usb4vc/board.json')
    except Exception as _vc_e:
        print('vcctrl board publish failed:', _vc_e)
'''


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
            "  vcctrl will report board identity as unknown, which is honest\n"
            "  but means board-scoped behaviour is unavailable. Re-apply with:\n"
            "    sudo python3 %s\n" % (path, sys.argv[0]))
        return 1
    if ANCHOR not in src:
        sys.stderr.write(
            "ANCHOR NOT FOUND in %s -- upstream has changed this function.\n"
            "  Refusing to guess at a new insertion point. Re-read the file\n"
            "  and update ANCHOR in this script.\n" % path)
        return 3

    open(path, "w").write(src.replace(ANCHOR, ANCHOR + BLOCK, 1))
    print("patched %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
