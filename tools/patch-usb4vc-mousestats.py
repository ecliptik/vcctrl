#!/usr/bin/env python3
"""Poll the protocol board's mouse delivery counters; publish them.

A LOCAL patch to USB4VC's rpi_app (https://github.com/dekuNukem/USB4VC, MIT,
dekuNukem), deliberately not offered upstream (the operator's call).
Contains only a few one-line match anchors copied from USB4VC to locate the
patch sites, not a redistribution of USB4VC itself.

WHY. Stock IBM PC firmware abandons a PS/2 mouse packet when the host
inhibits the clock mid-byte and counts nothing, so a lost click has no
witness (docs/MOUSE.md sec. 10). vcctrl's patched firmware
(firmware/usb4vc-ibmpc/patches/0001) counts every way a mouse event can fail
to reach the host, and answers SPI request 0x40 with the counts. This polls
that about once a second, only while no input is flowing, and:

  - writes /run/usb4vc/mouse_stats.json, atomically (tmpfs, like board.json:
    it cannot outlive the boot that wrote it);
  - prints one timestamped line to the debug log (and so the journal) ONLY
    when a loss counter moved, so the file stays small and a loss has a time.

WITH STOCK FIRMWARE it is inert and safe. Stock firmware ignores an unknown
request type, no reply ever matches, and after 5 misses the file says
supported=false and polling backs off to every 30 s.

THE READ PROTOCOL. Request, then two NOPs; take whichever NOP carries a reply
with our sequence number. That is rpi_app's own pattern (get_pboard_info
reads the second NOP), and the firmware arms its reply for both, because the
SPI TX FIFO can already hold stale bytes when the first one starts. A NOP,
never REQ_ACK, so a keyboard-LED request the board is waiting to hand over is
not acknowledged by accident.

Idempotent; --check verifies without modifying, which is what a deploy runs.

    sudo python3 patch-usb4vc-mousestats.py [--check] [path/to/usb4vc_usb_scan.py]
"""

import sys

DEFAULT = "/home/pi/usb4vc/rpi_app/usb4vc_usb_scan.py"

MARKER = "vcctrl mouse stats"

# Module level: define the poller just before get_pboard_info, which it
# mirrors. Loop: call it just before the PBOARD INTERRUPT block, where `now`
# and `last_usb_event` are both in scope.
ANCHOR_DEF = "def get_pboard_info():\n"
ANCHOR_CALL = "        # ----------------- PBOARD INTERRUPT -----------------\n"

BLOCK_DEF = '''# --- vcctrl mouse stats: protocol-board mouse delivery counters ------------
# Local patch, see vcctrl tools/patch-usb4vc-mousestats.py. Wrapped so a
# failure here can never take down input injection.
_VC_MS_NAMES = ('ev_in', 'ev_buf_full', 'pkt_built', 'pkt_ok', 'pkt_inhibit',
                'pkt_timeout', 'pkt_partial', 'ev_discarded', 'edge_merged',
                'host_cmd', 'host_fe', 'host_ff', 'host_f5', 'kb_inhibit_retry')
_VC_MS_LOSS = ('ev_buf_full', 'pkt_inhibit', 'pkt_timeout', 'pkt_partial',
               'ev_discarded', 'edge_merged', 'host_fe')
_VC_MS = {'next': 0.0, 'seq': 0, 'prev': None, 'misses': 0}
_VC_MS_DIR = '/run/usb4vc'

def _vc_mouse_stats_write(rec):
    import json as _vc_json
    os.makedirs(_VC_MS_DIR, exist_ok=True)
    _vc_tmp = os.path.join(_VC_MS_DIR, '.mouse_stats.json.tmp')
    with open(_vc_tmp, 'w') as _vc_f:
        _vc_json.dump(rec, _vc_f)
    # Rename, so a reader never parses a half-written file as truth.
    os.replace(_vc_tmp, os.path.join(_VC_MS_DIR, 'mouse_stats.json'))

def _vc_mouse_stats_poll(now):
    st = _VC_MS
    try:
        st['seq'] = (st['seq'] % 255) + 1          # never 0: stock traffic is 0
        xfer_when_not_busy([SPI_MOSI_MAGIC, st['seq'], 0x40] + [0] * 29)
        time.sleep(0.001)
        r1 = xfer_when_not_busy(list(nop_spi_msg_template))
        time.sleep(0.001)
        r2 = xfer_when_not_busy(list(nop_spi_msg_template))
        resp = None
        for r in (r2, r1):
            if (r and len(r) >= 32 and r[0] == 0xcd and r[1] == st['seq']
                    and r[2] == 0xc0 and r[3] == 1):
                resp = r
                break
        if resp is None:
            st['misses'] += 1
            if st['misses'] == 5:
                _vc_mouse_stats_write({
                    'supported': False, 't': time.time(),
                    'reason': 'no reply to request 0x40 in 5 tries -- stock '
                              'firmware, or a board without the counters'})
            st['next'] = now + (30.0 if st['misses'] >= 5 else 1.0)
            return
        st['misses'] = 0
        rec = {}
        for i, name in enumerate(_VC_MS_NAMES):
            rec[name] = resp[4 + 2 * i] | (resp[5 + 2 * i] << 8)
        _vc_mouse_stats_write({'supported': True, 'layout': 1,
                               't': time.time(), 'counters': rec})
        prev = st['prev']
        if prev is not None:
            moved = {}
            for k in _VC_MS_LOSS:
                d = (rec[k] - prev[k]) & 0xffff
                if d:
                    moved[k] = d
            if moved:
                print(int(time.time()), 'VC MOUSE LOSS', moved)
        st['prev'] = rec
        st['next'] = now + 1.0
    except Exception as _vc_e:
        print('vcctrl mouse stats poll failed:', _vc_e)
        st['next'] = now + 30.0

'''

BLOCK_CALL = '''        # vcctrl mouse stats: only while no input is flowing, so a poll never
        # delays an event by more than the few ms it takes.
        if now >= _VC_MS['next'] and now - last_usb_event > 0.05:
            _vc_mouse_stats_poll(now)

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
            "  vcctrl cannot see the protocol board's mouse delivery counters.\n"
            "  Harmless with stock firmware. Re-apply with:\n"
            "    sudo python3 %s\n" % (path, sys.argv[0]))
        return 1
    for a in (ANCHOR_DEF, ANCHOR_CALL):
        if src.count(a) != 1:
            sys.stderr.write(
                "ANCHOR %r found %d times in %s -- upstream has changed.\n"
                "  Refusing to guess at a new insertion point.\n"
                % (a.strip(), src.count(a), path))
            return 3

    src = src.replace(ANCHOR_DEF, BLOCK_DEF + ANCHOR_DEF, 1)
    src = src.replace(ANCHOR_CALL, BLOCK_CALL + ANCHOR_CALL, 1)
    open(path, "w").write(src)
    print("patched %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
