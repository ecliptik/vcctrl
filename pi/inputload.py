#!/usr/bin/env python3
"""inputload.py -- hammer vcctrld's INPUT path, the way a running cell does.

WHY THIS EXISTS. vcctrld aborted twice with a glibc double-free, 32 and 28
minutes in. Both aborts happened INSIDE a measurement cell. Neither of the
stress arms run so far had a cell running:

    aborts            cells YES   streamers YES
    wsload 12.2 min   cells NO    streamers YES
    clean 73.6 min    cells YES   streamers NO

Neither arm alone reproduces it. **The combination has never been tested.**
This is the cell-shaped half, and it needs no Gateway: uinput writes and LED
reads happen on the Pi, and the target being dark only means nothing echoes
back.

WHAT A CELL ACTUALLY DOES TO THE DAEMON. `at_prompt()` toggles Caps Lock and
then polls `leds` until it flips -- several times a second, for the entire
cell -- plus a `type` per command and a `key` per keystroke. That is sustained
traffic through `evdev._uinput` (writes) and the LED sysfs nodes (reads).
`evdev._input` and `evdev._uinput` are two of the four third-party C
extensions loaded at both aborts; the other two are Pillow's.

Rates here are deliberately far above a real cell, the same way the Pillow
stress runs ~100x the daemon's decode rate.

NOT A PROOF EITHER WAY. A null here does not exonerate the input path; it
bounds it. And with the target off the ring holds uniform 14.7 KB frames
rather than 35-54 KB of game screen, so any allocation churn downstream of a
frame is about a third of what was present at the aborts. Say so in the
writeup rather than letting a null look cleaner than it is.

    python3 inputload.py [--seconds N] [--threads N] [--quiet]
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

SOCKET_PATH = "/run/vcctrl.sock"


def call(cmd, **kw):
    """One request on its own connection, as the real client does."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    try:
        s.connect(SOCKET_PATH)
        req = {"cmd": cmd}
        req.update(kw)
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8").strip() or "{}")
    finally:
        s.close()


class Stats(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.ops = 0
        self.errs = 0
        self.last_err = None

    def bump(self, n=1, err=None):
        with self.lock:
            self.ops += n
            if err:
                self.errs += 1
                self.last_err = err


STOP = threading.Event()


def worker(stats, idx):
    """One cell-shaped loop: toggle Caps, poll LEDs hard, type, repeat."""
    n = 0
    while not STOP.is_set():
        n += 1
        try:
            # The Caps Lock toggle is the prompt probe. It is a uinput write
            # of a press and a release.
            call("key", keys=["capslock"])
            stats.bump()
            # at_prompt then polls for the echo. With the target dark nothing
            # ever changes, which is fine -- the READ traffic is the point.
            for _ in range(8):
                if STOP.is_set():
                    break
                call("leds")
                stats.bump()
            # A cell types a command every few seconds. Keep the string short:
            # type_text sleeps between characters, so a long one throttles the
            # loop rather than loading anything.
            call("type", text="DIR")
            stats.bump()
            call("key", keys=["enter"])
            stats.bump()
            if n % 5 == 0:
                call("status")
                stats.bump()
        except Exception as e:                       # noqa: BLE001
            stats.bump(0, err="%s: %s" % (type(e).__name__, e))
            time.sleep(0.5)


def daemon_pid():
    try:
        with os.popen("systemctl show vcctrld -p MainPID --value") as f:
            return f.read().strip()
    except Exception:                                # noqa: BLE001
        return "?"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=2700)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    pid0 = daemon_pid()
    print("inputload: %d threads, %d s, daemon MainPID=%s"
          % (a.threads, a.seconds, pid0), flush=True)
    print("  ops = uinput writes + LED reads; watching for the PID to change",
          flush=True)

    stats = Stats()
    ts = [threading.Thread(target=worker, args=(stats, i), daemon=True)
          for i in range(a.threads)]
    for t in ts:
        t.start()

    t0 = time.time()
    last_ops, last_t = 0, t0
    try:
        while time.time() - t0 < a.seconds:
            time.sleep(15)
            now = time.time()
            with stats.lock:
                ops, errs, last_err = stats.ops, stats.errs, stats.last_err
            rate = (ops - last_ops) / (now - last_t)
            last_ops, last_t = ops, now
            pid = daemon_pid()
            # THE ONLY THING THAT MATTERS. A restart is 3 s and everything
            # downstream keeps working, so the op counter would not blink.
            # Compare identities, not rates -- an integer has no units to get
            # wrong.
            if pid != pid0:
                print("\n*** DAEMON PID CHANGED: %s -> %s AFTER %.1f MIN ***"
                      % (pid0, pid, (now - t0) / 60.0), flush=True)
                print("*** STOP. Run pi/core.sh before anything else touches "
                      "the box. ***", flush=True)
                STOP.set()
                return 3
            if not a.quiet:
                print("  t+%5.1f min  ops %8d  %6.1f ops/s  errs %d  %s"
                      % ((now - t0) / 60.0, ops, rate, errs,
                         last_err or ""), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()

    with stats.lock:
        ops, errs = stats.ops, stats.errs
    mins = (time.time() - t0) / 60.0
    print("\ninputload: %.1f min, %d ops, %d errors, daemon PID unchanged (%s)"
          % (mins, ops, errs, pid0), flush=True)
    print("NULL RESULT -- this bounds the input path, it does not clear it.",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
