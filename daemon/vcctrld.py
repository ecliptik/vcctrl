#!/usr/bin/env python3
"""
vcctrld -- virtual PS/2 input server for the g2k DOS box.

Runs on the Pi that carries the USB4VC HAT. Creates two uinput devices and
holds them open for the process lifetime; USB4VC discovers them on its 0.75 s
scan and relays their events over SPI to the PS/2 protocol board.

Why the devices are persistent: USB4VC only opens input devices it finds on a
poll (usb4vc_usb_scan.py:946). A device created per-keystroke is invisible for
up to 0.75 s, so the first keystrokes are silently lost.

Constraints read out of the USB4VC source, all load-bearing:
  - name must not contain "motion"          (usb4vc_usb_scan.py:896)
  - keyboard needs KEY_ENTER and KEY_Y      (:913)
  - mouse needs BTN_LEFT and EV_REL         (:911)
  - must not declare gamepad buttons        (:877 check_is_gamepad)
  - one event per device per loop pass      (:772), 5 ms idle sleep (:766)
"""

import errno
import glob
import json
import os
import socket
import struct
import sys
import threading
import time

from evdev import UInput, ecodes as e

SOCKET_PATH = "/run/vcctrl.sock"
USB4VC_LOG = "/home/pi/usb4vc/usb4vc_debug_log.txt"

VENDOR = 0x1209
KBD_PRODUCT = 0xDEA1
MOUSE_PRODUCT = 0xDEA2

# Minimum gap between input events. USB4VC drains one event per device per
# loop pass and sleeps 5 ms when idle; PS/2 wire time adds ~1 ms per byte.
DEFAULT_PACE_S = 0.012

# ---------------------------------------------------------------- key tables

NAMED_KEYS = {
    "enter": e.KEY_ENTER, "return": e.KEY_ENTER, "esc": e.KEY_ESC,
    "escape": e.KEY_ESC, "space": e.KEY_SPACE, "tab": e.KEY_TAB,
    "backspace": e.KEY_BACKSPACE, "bs": e.KEY_BACKSPACE,
    "delete": e.KEY_DELETE, "del": e.KEY_DELETE, "insert": e.KEY_INSERT,
    "home": e.KEY_HOME, "end": e.KEY_END,
    "pgup": e.KEY_PAGEUP, "pgdn": e.KEY_PAGEDOWN,
    "up": e.KEY_UP, "down": e.KEY_DOWN, "left": e.KEY_LEFT,
    "right": e.KEY_RIGHT,
    "ctrl": e.KEY_LEFTCTRL, "lctrl": e.KEY_LEFTCTRL, "rctrl": e.KEY_RIGHTCTRL,
    "alt": e.KEY_LEFTALT, "lalt": e.KEY_LEFTALT, "ralt": e.KEY_RIGHTALT,
    "shift": e.KEY_LEFTSHIFT, "lshift": e.KEY_LEFTSHIFT,
    "rshift": e.KEY_RIGHTSHIFT,
    "capslock": e.KEY_CAPSLOCK, "caps": e.KEY_CAPSLOCK,
    "numlock": e.KEY_NUMLOCK, "scrolllock": e.KEY_SCROLLLOCK,
}
for _i in range(1, 13):
    NAMED_KEYS["f%d" % _i] = getattr(e, "KEY_F%d" % _i)
for _c in "abcdefghijklmnopqrstuvwxyz":
    NAMED_KEYS[_c] = getattr(e, "KEY_%s" % _c.upper())
for _d in "0123456789":
    NAMED_KEYS[_d] = getattr(e, "KEY_%s" % _d)

# US layout: char -> (keycode, needs_shift)
_UNSHIFTED = {
    " ": e.KEY_SPACE, "-": e.KEY_MINUS, "=": e.KEY_EQUAL,
    "[": e.KEY_LEFTBRACE, "]": e.KEY_RIGHTBRACE, "\\": e.KEY_BACKSLASH,
    ";": e.KEY_SEMICOLON, "'": e.KEY_APOSTROPHE, "`": e.KEY_GRAVE,
    ",": e.KEY_COMMA, ".": e.KEY_DOT, "/": e.KEY_SLASH,
    "\t": e.KEY_TAB, "\n": e.KEY_ENTER,
}
_SHIFTED = {
    "!": e.KEY_1, "@": e.KEY_2, "#": e.KEY_3, "$": e.KEY_4, "%": e.KEY_5,
    "^": e.KEY_6, "&": e.KEY_7, "*": e.KEY_8, "(": e.KEY_9, ")": e.KEY_0,
    "_": e.KEY_MINUS, "+": e.KEY_EQUAL, "{": e.KEY_LEFTBRACE,
    "}": e.KEY_RIGHTBRACE, "|": e.KEY_BACKSLASH, ":": e.KEY_SEMICOLON,
    '"': e.KEY_APOSTROPHE, "~": e.KEY_GRAVE, "<": e.KEY_COMMA,
    ">": e.KEY_DOT, "?": e.KEY_SLASH,
}

CHARMAP = {}
for _c, _k in _UNSHIFTED.items():
    CHARMAP[_c] = (_k, False)
for _c, _k in _SHIFTED.items():
    CHARMAP[_c] = (_k, True)
for _c in "abcdefghijklmnopqrstuvwxyz":
    CHARMAP[_c] = (getattr(e, "KEY_%s" % _c.upper()), False)
    CHARMAP[_c.upper()] = (getattr(e, "KEY_%s" % _c.upper()), True)
for _d in "0123456789":
    CHARMAP[_d] = (getattr(e, "KEY_%s" % _d), False)

MOUSE_BUTTONS = {
    "left": e.BTN_LEFT, "right": e.BTN_RIGHT, "middle": e.BTN_MIDDLE,
}


class Devices(object):
    """Owns the two uinput devices for the life of the process."""

    def __init__(self):
        # KEY_ENTER and KEY_Y are required for USB4VC to classify this as a
        # keyboard. EV_LED gives us the PS/2 return channel (sec 2.1).
        kbd_keys = sorted(set(NAMED_KEYS.values()) |
                          set(k for k, _ in CHARMAP.values()))
        self.kbd = UInput(
            {e.EV_KEY: kbd_keys,
             e.EV_LED: [e.LED_CAPSL, e.LED_NUML, e.LED_SCROLLL]},
            name="vcctrl virtual keyboard",
            vendor=VENDOR, product=KBD_PRODUCT, version=1)
        # BTN_LEFT + EV_REL is the mouse test. None of BTN_LEFT/RIGHT/MIDDLE
        # appear in USB4VC's gamepad_event_code_name_list, so this cannot be
        # misclassified as a gamepad.
        self.mouse = UInput(
            {e.EV_KEY: sorted(MOUSE_BUTTONS.values()),
             e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL]},
            name="vcctrl virtual mouse",
            vendor=VENDOR, product=MOUSE_PRODUCT, version=1)
        self.lock = threading.Lock()
        self.led_paths = self._find_led_paths()

    def _find_led_paths(self):
        """Map our keyboard's event node to its /sys/class/leds entries.

        USB4VC's change_kb_led() writes the state the DOS host sent back over
        PS/2 into every *capslock*/*numlock*/*scrolllock* node it finds, ours
        included. Reading them is the return channel.
        """
        out = {}
        try:
            ev = os.path.basename(self.kbd.device.path)          # eventN
            real = os.path.realpath("/sys/class/input/%s" % ev)  # .../inputM/eventN
            inp = os.path.basename(os.path.dirname(real))        # inputM
            for name in ("capslock", "numlock", "scrolllock"):
                p = "/sys/class/leds/%s::%s/brightness" % (inp, name)
                if os.path.exists(p):
                    out[name] = p
        except Exception:
            pass
        return out

    def read_leds(self):
        out = {}
        for name, path in self.led_paths.items():
            try:
                with open(path) as f:
                    out[name] = int(f.read().strip())
            except Exception:
                out[name] = None
        return out

    # -- emission -----------------------------------------------------------

    def _tap(self, dev, code, pace):
        dev.write(e.EV_KEY, code, 1)
        dev.syn()
        time.sleep(pace)
        dev.write(e.EV_KEY, code, 0)
        dev.syn()
        time.sleep(pace)

    def key(self, names, pace=DEFAULT_PACE_S):
        with self.lock:
            for n in names:
                code = NAMED_KEYS.get(n.lower())
                if code is None:
                    raise ValueError("unknown key: %s" % n)
                self._tap(self.kbd, code, pace)

    def type_text(self, text, pace=DEFAULT_PACE_S):
        with self.lock:
            for ch in text:
                ent = CHARMAP.get(ch)
                if ent is None:
                    raise ValueError("untypable character: %r" % ch)
                code, shift = ent
                if shift:
                    self.kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 1)
                    self.kbd.syn()
                    time.sleep(pace)
                self._tap(self.kbd, code, pace)
                if shift:
                    self.kbd.write(e.EV_KEY, e.KEY_LEFTSHIFT, 0)
                    self.kbd.syn()
                    time.sleep(pace)

    def hold(self, name, ms, pace=DEFAULT_PACE_S):
        code = NAMED_KEYS.get(name.lower())
        if code is None:
            raise ValueError("unknown key: %s" % name)
        with self.lock:
            self.kbd.write(e.EV_KEY, code, 1)
            self.kbd.syn()
            time.sleep(ms / 1000.0)
            self.kbd.write(e.EV_KEY, code, 0)
            self.kbd.syn()
            time.sleep(pace)

    def combo(self, names, pace=DEFAULT_PACE_S):
        codes = []
        for n in names:
            c = NAMED_KEYS.get(n.lower())
            if c is None:
                raise ValueError("unknown key: %s" % n)
            codes.append(c)
        with self.lock:
            for c in codes:
                self.kbd.write(e.EV_KEY, c, 1)
                self.kbd.syn()
                time.sleep(pace)
            for c in reversed(codes):
                self.kbd.write(e.EV_KEY, c, 0)
                self.kbd.syn()
                time.sleep(pace)

    def mouse_move(self, dx, dy, pace=DEFAULT_PACE_S):
        with self.lock:
            if dx:
                self.mouse.write(e.EV_REL, e.REL_X, int(dx))
            if dy:
                self.mouse.write(e.EV_REL, e.REL_Y, int(dy))
            self.mouse.syn()
            time.sleep(pace)

    def mouse_click(self, button, pace=DEFAULT_PACE_S):
        code = MOUSE_BUTTONS.get(button.lower())
        if code is None:
            raise ValueError("unknown button: %s" % button)
        with self.lock:
            self._tap(self.mouse, code, pace)


def usb4vc_holds_us():
    """Did USB4VC open our devices, and has it not dropped them since?

    Reads its debug log rather than guessing. Returns dict name -> bool.
    """
    want = {"vcctrl virtual keyboard": False, "vcctrl virtual mouse": False}
    try:
        with open(USB4VC_LOG, "rb") as f:
            # Only the tail matters; the log grows without bound.
            try:
                f.seek(-262144, os.SEEK_END)
            except OSError:
                f.seek(0)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return want
    for line in lines:
        for name in want:
            if name in line:
                if line.startswith("opened device:"):
                    want[name] = True
                elif line.startswith("Device disappeared:"):
                    want[name] = False
    return want


def handle(devs, req):
    cmd = req.get("cmd")
    pace = float(req.get("pace", DEFAULT_PACE_S))
    if cmd == "status":
        return {"ok": True,
                "keyboard": devs.kbd.device.path,
                "mouse": devs.mouse.device.path,
                "usb4vc": usb4vc_holds_us(),
                "led_paths": devs.led_paths,
                "leds": devs.read_leds()}
    if cmd == "key":
        devs.key(req["keys"], pace)
    elif cmd == "type":
        devs.type_text(req["text"], pace)
    elif cmd == "hold":
        devs.hold(req["key"], float(req["ms"]), pace)
    elif cmd == "combo":
        devs.combo(req["keys"], pace)
    elif cmd == "mouse_move":
        devs.mouse_move(req.get("dx", 0), req.get("dy", 0), pace)
    elif cmd == "mouse_click":
        devs.mouse_click(req.get("button", "left"), pace)
    elif cmd == "leds":
        return {"ok": True, "leds": devs.read_leds()}
    elif cmd == "ledwait":
        before = devs.read_leds()
        deadline = time.time() + float(req.get("timeout", 5.0))
        while time.time() < deadline:
            now = devs.read_leds()
            if now != before:
                return {"ok": True, "changed": True,
                        "before": before, "after": now}
            time.sleep(0.02)
        return {"ok": True, "changed": False, "before": before,
                "after": devs.read_leds()}
    else:
        return {"ok": False, "error": "unknown command: %r" % cmd}
    return {"ok": True}


def serve(devs):
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)
    srv.listen(8)
    sys.stderr.write("vcctrld ready: kbd=%s mouse=%s leds=%s\n" % (
        devs.kbd.device.path, devs.mouse.device.path,
        sorted(devs.led_paths)))
    sys.stderr.flush()
    while True:
        conn, _ = srv.accept()
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            if not buf.strip():
                continue
            try:
                resp = handle(devs, json.loads(buf.decode("utf-8")))
            except Exception as exc:
                resp = {"ok": False, "error": "%s: %s" % (
                    type(exc).__name__, exc)}
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except Exception:
            pass
        finally:
            conn.close()


def main():
    if os.geteuid() != 0:
        sys.stderr.write("vcctrld must run as root (needs /dev/uinput)\n")
        return 1
    devs = Devices()
    # Give USB4VC's 0.75 s scan time to find us before accepting work, so the
    # first command a client sends is not silently dropped.
    time.sleep(1.5)
    held = usb4vc_holds_us()
    if not all(held.values()):
        sys.stderr.write("warning: USB4VC has not opened %s\n" % (
            [k for k, v in held.items() if not v],))
    serve(devs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
