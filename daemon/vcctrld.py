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

import collections
import errno
import glob
import hashlib
import io
import json
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time

from evdev import UInput, ecodes as e

SOCKET_PATH = "/run/vcctrl.sock"
USB4VC_LOG = "/home/pi/usb4vc/usb4vc_debug_log.txt"
CONFIG_PATH = "/opt/vcctrl/config.json"

# Seconds the rails stay down during a power cycle. The g2k is an AT-style
# PicoRC setup with no soft-off, so it boots as soon as power returns; the delay
# is only to let the supply drain rather than to satisfy any handshake.
#
# Raised from 6 s after measuring POST at 26 s from a cold start but 44 s after
# a 6 s cycle -- most likely the 12 V brick and picoPSU had not fully
# discharged. This is the recovery path of last resort, so it should be the
# most reliable thing in the system rather than the fastest.
POWER_CYCLE_OFF_S = 15.0

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

# The table above is a *typing* table: it covers what `type` needs, and
# punctuation was reachable only through CHARMAP. A KVM has to address every
# physical key by name for keydown/keyup, so the rest of the US PS/2 layout
# goes in here.
#
# Safe against USB4VC's classification checks (usb4vc_usb_scan.py): these are
# all KEY_*, none appear in gamepad_event_code_name_list (:877), and
# KEY_ENTER/KEY_Y (:913) are already present above.
#
# NOTE these names are what the daemon accepts. Whether the STM32 protocol
# board emits a PS/2 scancode for each of them is a separate question and is
# NOT answerable from this side -- the Pi forwards raw evdev (type, code,
# value) and the mapping lives in the board's firmware. See docs/WEBKVM.md
# sec. 5.2; it needs measuring at the g2k, not asserting here.
NAMED_KEYS.update({
    "minus": e.KEY_MINUS, "equal": e.KEY_EQUAL,
    "leftbrace": e.KEY_LEFTBRACE, "rightbrace": e.KEY_RIGHTBRACE,
    "backslash": e.KEY_BACKSLASH, "semicolon": e.KEY_SEMICOLON,
    "apostrophe": e.KEY_APOSTROPHE, "grave": e.KEY_GRAVE,
    "comma": e.KEY_COMMA, "dot": e.KEY_DOT, "period": e.KEY_DOT,
    "slash": e.KEY_SLASH,
    # aliases matching the printed keycap, so a UI can send what it shows
    "-": e.KEY_MINUS, "=": e.KEY_EQUAL, "[": e.KEY_LEFTBRACE,
    "]": e.KEY_RIGHTBRACE, "\\": e.KEY_BACKSLASH, ";": e.KEY_SEMICOLON,
    "'": e.KEY_APOSTROPHE, "`": e.KEY_GRAVE, ",": e.KEY_COMMA,
    ".": e.KEY_DOT, "/": e.KEY_SLASH,
    # keypad -- a real concern on this box: DOS software reads the numeric
    # keypad distinctly from the number row.
    "kpasterisk": e.KEY_KPASTERISK, "kpminus": e.KEY_KPMINUS,
    "kpplus": e.KEY_KPPLUS, "kpdot": e.KEY_KPDOT, "kpenter": e.KEY_KPENTER,
    "kpslash": e.KEY_KPSLASH,
    # the rest of a 101-key board
    "sysrq": e.KEY_SYSRQ, "printscreen": e.KEY_SYSRQ, "prtsc": e.KEY_SYSRQ,
    "pause": e.KEY_PAUSE, "break": e.KEY_PAUSE,
    "menu": e.KEY_COMPOSE, "compose": e.KEY_COMPOSE,
    "leftmeta": e.KEY_LEFTMETA, "rightmeta": e.KEY_RIGHTMETA,
    "102nd": e.KEY_102ND,
})
for _i in range(0, 10):
    NAMED_KEYS["kp%d" % _i] = getattr(e, "KEY_KP%d" % _i)

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


# ---------------------------------------------------------------- power

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _kasa_encrypt(text):
    key = 171
    out = bytearray()
    for b in text.encode():
        key ^= b
        out.append(key)
    return struct.pack(">I", len(out)) + bytes(out)


def _kasa_decrypt(data):
    key = 171
    out = bytearray()
    for c in data:
        out.append(key ^ c)
        key = c
    return out.decode(errors="replace")


def kasa_send(host, payload, timeout=5.0):
    """Legacy TP-Link smart-home protocol on port 9999.

    4-byte big-endian length prefix plus an XOR-autokey cipher seeded at 171.
    No dependency and no cloud account -- the EP10 speaks this directly on the
    LAN. Newer Kasa firmware may move to KLAP on port 80, in which case this
    stops working and needs the python-kasa library instead.
    """
    sock = socket.create_connection((host, 9999), timeout)
    try:
        sock.sendall(_kasa_encrypt(json.dumps(payload)))
        hdr = b""
        while len(hdr) < 4:
            chunk = sock.recv(4 - len(hdr))
            if not chunk:
                raise IOError("short header from %s" % host)
            hdr += chunk
        want = struct.unpack(">I", hdr)[0]
        buf = b""
        while len(buf) < want:
            chunk = sock.recv(want - len(buf))
            if not chunk:
                break
            buf += chunk
        return json.loads(_kasa_decrypt(buf))
    finally:
        sock.close()


def power_state(host):
    info = kasa_send(host, {"system": {"get_sysinfo": {}}})
    info = info["system"]["get_sysinfo"]
    return {"on": bool(info.get("relay_state")),
            "alias": info.get("alias"),
            "model": info.get("model"),
            "on_time_s": info.get("on_time"),
            "rssi": info.get("rssi")}


def power_set(host, on):
    resp = kasa_send(host, {"system": {"set_relay_state": {"state": 1 if on else 0}}})
    err = resp.get("system", {}).get("set_relay_state", {}).get("err_code")
    if err not in (0, None):
        raise IOError("kasa set_relay_state err_code=%s" % err)
    return err


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
        # Keys currently held by keydown with no matching keyup. Tracked so a
        # disconnecting client cannot strand one down (see release_all).
        self.held = set()
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

    def keydown(self, name):
        """Press and hold. The matching keyup may never come -- see release_all.

        Unlike key/type/hold/combo this does NOT bracket a complete operation,
        so it takes the lock only for the single event. That is safe because a
        lone press is atomic; it is the multi-event operations that must not
        interleave.
        """
        code = NAMED_KEYS.get(name.lower())
        if code is None:
            raise ValueError("unknown key: %s" % name)
        with self.lock:
            self.kbd.write(e.EV_KEY, code, 1)
            self.kbd.syn()
            self.held.add(code)

    def keyup(self, name):
        code = NAMED_KEYS.get(name.lower())
        if code is None:
            raise ValueError("unknown key: %s" % name)
        with self.lock:
            self.kbd.write(e.EV_KEY, code, 0)
            self.kbd.syn()
            self.held.discard(code)

    def release_all(self, pace=DEFAULT_PACE_S):
        """Release every key held via keydown. Returns how many.

        The web KVM must call this when a viewer's socket closes. A dropped
        wifi connection mid-keypress would otherwise leave a key down at the
        g2k forever, which at a DOS prompt types until the buffer fills.
        """
        with self.lock:
            codes = sorted(self.held)
            for code in codes:
                self.kbd.write(e.EV_KEY, code, 0)
                self.kbd.syn()
                time.sleep(pace)
            self.held.clear()
        return len(codes)

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


# ---------------------------------------------------------------- capabilities
#
# One daemon owns every device on this rig, and capabilities are how that stays
# manageable. See docs/WEBKVM.md sec. 2 for why a single owner rather than a
# daemon per device: arbitration. The moment a browser exists there are two
# independent things that can type at the g2k, and only a single owner can hold
# the rule that stops them colliding.
#
# This is a registry, deliberately NOT a plugin loader: capabilities are listed
# in a table below, in this file. No discovery, no dynamic import. The value is
# that each one owns a device and can fail alone; a loader is a feature to add
# when something outside this repo needs to plug in, and nothing does.
#
# Three rules make the merge safe, and all three are load-bearing:
#
#   1. The input path takes no LOGICAL dependency on any other capability, and
#      is scheduled ahead of all of them. It must stay possible to type at the
#      g2k with everything else broken. Note this is a claim about scheduling
#      as well as dependency: one Python process means a keystroke can queue
#      behind another capability's work even with no dependency between them.
#      What protects it is keeping pure-Python per-request work small, and it
#      is settled by measurement (p99 of `key` under load), not by this comment.
#   2. A capability that raises is unloaded, not fatal. The core catches at the
#      module boundary. A dead video pipeline must never cost the keyboard.
#   3. Anything that can take the process down stays out of process. ffmpeg
#      will be a subprocess. Rule 3 does not cover memory -- an OOM kills the
#      process as dead as a segfault -- so anything buffering frames caps
#      itself in BYTES, not in items.


# The commands the input lock gates. Everything else is ungated on purpose:
# observation is never gated (docs/WEBKVM.md sec. 2), and `power` is
# deliberately in the ungated set because cutting mains is how you rescue a
# sweep that has wedged past the point where input would help.
GATED_COMMANDS = frozenset([
    "key", "type", "hold", "combo", "keydown", "keyup", "release_all",
    "mouse_move", "mouse_click",
])


class Bus(object):
    """Ring of recent events, published by every command the daemon runs.

    This is what makes the browser show *the harness typed RB at 19:22:04 and
    took the input lock* rather than only *the screen changed* -- the
    difference between the automation working and the automation thinking it
    is working. Three stuck-failures on 2026-08-19 would all have been visible
    here with no video at all.

    Bounded by count rather than bytes, unlike the frame ring: events are
    small fixed-shape dicts, so count IS a bound on memory here. The frame
    ring's byte cap exists because frame size varies by two orders of
    magnitude and an OOM takes the uinput devices down with it.
    """

    def __init__(self, cap=2000):
        self.lock = threading.Lock()
        self.seq = 0
        self.events = collections.deque(maxlen=cap)

    def publish(self, kind, **fields):
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, "t": time.time(), "kind": kind}
            ev.update(fields)
            self.events.append(ev)
            return ev

    def since(self, seq, limit=200):
        with self.lock:
            out = [ev for ev in self.events if ev["seq"] > seq]
            newest = self.seq
            oldest = self.events[0]["seq"] if self.events else 0
        # `missed` tells a reconnecting client it fell off the back of the ring
        # rather than letting it believe it has a complete history.
        return {"events": out[:limit], "seq": newest,
                "missed": seq != 0 and seq + 1 < oldest}


class Activity(object):
    """In-flight commands and their age.

    "harness has been in ledwait for 14 minutes" is the operator's question
    rendered in one line, and it needs nothing but a start time.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.inflight = {}
        self.next_id = 0

    def begin(self, cmd, who=None):
        with self.lock:
            self.next_id += 1
            self.inflight[self.next_id] = (cmd, time.time(), who)
            return self.next_id

    def end(self, ident):
        with self.lock:
            self.inflight.pop(ident, None)

    def report(self):
        now = time.time()
        with self.lock:
            # `by` is here so a caller can tell a viewer's poll from a
            # harness command. Without it the deploy guard refused on a
            # browser's own 1.5s status poll, which is not a run and not
            # something worth protecting.
            return sorted(
                ({"cmd": c, "age_s": round(now - t0, 3), "by": who}
                 for c, t0, who in self.inflight.values()),
                key=lambda d: -d["age_s"])


class Arbiter(object):
    """The input lock. Gates input only -- never observation.

    Unheld by default, and while unheld every input command behaves exactly as
    it did before this existed. Nothing in the existing tooling acquires it, so
    adopting it is opt-in and this can land without touching a sweep.
    """

    def __init__(self, bus):
        self.lock = threading.Lock()
        self.bus = bus
        self.owner = None
        self.since = None

    def acquire(self, owner, force=False):
        with self.lock:
            if self.owner is not None and self.owner != owner and not force:
                return {"ok": False, "error": "input locked by %r since %.0f"
                        % (self.owner, self.since), "locked_by": self.owner}
            broke = self.owner if (self.owner and self.owner != owner) else None
            self.owner, self.since = owner, time.time()
        if broke:
            # A break is the tainting event. Published so it lands in the run's
            # log rather than living only in whoever clicked the button's head.
            self.bus.publish("lock.broken", broke=broke, by=owner, taint=True)
        self.bus.publish("lock.acquired", owner=owner)
        return {"ok": True, "owner": owner}

    def release(self, owner=None, force=False):
        with self.lock:
            if self.owner is None:
                return {"ok": True, "owner": None}
            if owner != self.owner and not force:
                return {"ok": False, "error": "input locked by %r" % self.owner,
                        "locked_by": self.owner}
            was, self.owner, self.since = self.owner, None, None
        self.bus.publish("lock.released", owner=was, forced=bool(force))
        return {"ok": True, "owner": None, "was": was}

    def check(self, who):
        """None if `who` may send input, else the refusal to return."""
        with self.lock:
            if self.owner is None or self.owner == who:
                return None
            return {"ok": False,
                    "error": "input locked by %r since %.0f"
                             % (self.owner, self.since),
                    "locked_by": self.owner, "held_s": time.time() - self.since}

    def status(self):
        with self.lock:
            return {"owner": self.owner,
                    "held_s": (time.time() - self.since) if self.since else None}


class Capability(object):
    """One device or concern. Subclasses declare a name and a command map."""

    name = None

    def __init__(self, devs):
        self.devs = devs

    def commands(self):
        """Return {command_name: handler(req) -> dict}."""
        return {}

    def start(self):
        pass

    def stop(self):
        pass


class InputCapability(Capability):
    """Keyboard and mouse over the USB4VC PS/2 bridge.

    Rule 1 applies to this class specifically: nothing here may import,
    call into, or wait on any other capability.
    """

    name = "input"

    def commands(self):
        return {
            "key": self._key, "type": self._type, "hold": self._hold,
            "combo": self._combo, "keydown": self._keydown,
            "keyup": self._keyup, "release_all": self._release_all,
            "mouse_move": self._mouse_move, "mouse_click": self._mouse_click,
        }

    def _key(self, req):
        self.devs.key(req["keys"], _pace(req))
        return {"ok": True}

    def _type(self, req):
        self.devs.type_text(req["text"], _pace(req))
        return {"ok": True}

    def _hold(self, req):
        self.devs.hold(req["key"], float(req["ms"]), _pace(req))
        return {"ok": True}

    def _combo(self, req):
        self.devs.combo(req["keys"], _pace(req))
        return {"ok": True}

    def _keydown(self, req):
        self.devs.keydown(req["key"])
        return {"ok": True}

    def _keyup(self, req):
        self.devs.keyup(req["key"])
        return {"ok": True}

    def _release_all(self, req):
        return {"ok": True, "released": self.devs.release_all(_pace(req))}

    def _mouse_move(self, req):
        self.devs.mouse_move(req.get("dx", 0), req.get("dy", 0), _pace(req))
        return {"ok": True}

    def _mouse_click(self, req):
        self.devs.mouse_click(req.get("button", "left"), _pace(req))
        return {"ok": True}


class LedsCapability(Capability):
    """The PS/2 LED return channel -- non-video proof a keystroke landed.

    Reads only; the LEDs are written by USB4VC from what the DOS host sends
    back over PS/2. Touches no lock and blocks nothing.
    """

    name = "leds"

    def commands(self):
        return {"leds": self._leds, "ledwait": self._ledwait}

    def _leds(self, req):
        return {"ok": True, "leds": self.devs.read_leds()}

    def _ledwait(self, req):
        # Blocks for up to `timeout`. Safe to block only because serve() is
        # threaded -- before that, this stalled every other client.
        before = self.devs.read_leds()
        deadline = time.time() + float(req.get("timeout", 5.0))
        while time.time() < deadline:
            now = self.devs.read_leds()
            if now != before:
                return {"ok": True, "changed": True,
                        "before": before, "after": now}
            time.sleep(0.02)
        return {"ok": True, "changed": False, "before": before,
                "after": self.devs.read_leds()}


class PowerCapability(Capability):
    """Mains control for the target, via the Kasa EP10.

    Touches no device and holds no lock, so it runs fully concurrently with
    input -- which matters, because `cycle` blocks for 15 s.
    """

    name = "power"

    # Mains control is the most consequential thing this rig can do, and the
    # event bus is in MEMORY. A daemon restart erases it -- and a restart is
    # exactly the event most likely to be happening around an unexplained power
    # change, so the record disappears precisely when it is needed.
    #
    # Found the hard way: the g2k was discovered powered off, and answering
    # "did anything here turn it off" required reasoning from absence rather
    # than reading a line. An append-only file survives restarts, reboots and
    # the ring wrapping.
    AUDIT = "/var/lib/vcctrl/power.log"

    def commands(self):
        return {"power": self._power, "powerlog": self._powerlog}

    def _audit(self, action, who, outcome):
        line = "%s\t%s\taction=%s\tby=%s\t%s\n" % (
            time.strftime("%Y-%m-%dT%H:%M:%S%z"), int(time.time()),
            action, who if who else "(unidentified)", outcome)
        try:
            os.makedirs(os.path.dirname(self.AUDIT), exist_ok=True)
            with open(self.AUDIT, "a") as f:
                f.write(line)
        except Exception as exc:
            sys.stderr.write("power audit write failed: %s\n" % exc)
        # journald too, so it is in the same place as everything else and
        # survives the file being lost.
        sys.stderr.write("POWER %s" % line)
        sys.stderr.flush()

    def _powerlog(self, req):
        n = int(req.get("n", 50))
        try:
            with open(self.AUDIT) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return {"ok": True, "entries": [], "note": "no power action has "
                    "been recorded since the audit log was added"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "entries": lines[-n:], "total": len(lines)}

    def _power(self, req):
        cfg = load_config()
        host = req.get("host") or cfg.get("kasa_host")
        if not host:
            return {"ok": False, "error":
                    "no kasa_host configured (set it in %s)" % CONFIG_PATH}
        action = req.get("action", "state")
        if action == "state":
            return {"ok": True, "power": power_state(host)}
        # Reads are not audited -- they happen on a timer from every open
        # browser tab and would bury the two lines that matter.
        self._audit(action, req.get("as"), "requested")
        if action == "on":
            power_set(host, True)
        elif action == "off":
            power_set(host, False)
        elif action == "cycle":
            # Deliberately unconditional: a wedged machine may report on while
            # being useless, so cycle means cycle rather than "on if off".
            power_set(host, False)
            time.sleep(float(req.get("off_seconds", POWER_CYCLE_OFF_S)))
            power_set(host, True)
        else:
            return {"ok": False, "error": "unknown power action: %r" % action}
        time.sleep(0.5)
        st = power_state(host)
        self._audit(action, req.get("as"), "done on=%s" % st.get("on"))
        return {"ok": True, "power": st}


class VideoCapability(Capability):
    """Owns /dev/video0 for the life of the daemon and fans frames out.

    Why a persistent owner rather than a capture per request: the device is
    single-open, and every capture on this rig used to be a fresh ffmpeg spawn
    costing ~40 s end to end. That made frames stale enough to mis-diagnose
    machine state twice in one day, and it made "watch the sweep live"
    impossible rather than merely slow.

    Rule 3 (sec. 2): ffmpeg stays a SUBPROCESS. A native decoder segfault must
    not be a keyboard outage. Nothing in the per-frame path decodes, encodes or
    base64s -- frames are passed through as the bytes the stick produced, and
    the only per-frame work is a memcpy and a bounds check.

    Rule 3 does not cover memory, so the ring is capped in BYTES. Frames is the
    wrong unit: frame size varies by an order of magnitude between a text
    console and a game screen, and an OOM takes the uinput devices with it.
    """

    name = "video"

    DEVICE = "/dev/video0"
    # 48 MB. 30 s at 30 fps is 900 frames: 13.5 MB of text console but 63 MB of
    # a dense screen, a 4.7x spread. A buffer sized in seconds has no fixed
    # cost and one sized in bytes has no fixed duration, so this is capped in
    # BYTES -- the units the resource is actually measured in -- and the span
    # it currently buys is reported rather than promised.
    RING_BYTES = 48 * 1024 * 1024
    # Wall-clock span the scrub buffer tries to preserve. When bytes run out,
    # the old end is thinned rather than dropped, so 30 s stays 30 s and only
    # its granularity degrades.
    TARGET_SPAN_S = 30.0
    # Never let the ring push the Pi towards OOM. There is NO SWAP on this box:
    # an overshoot does not slow the daemon down, it kills it, and that drops
    # the uinput devices.
    MEM_FLOOR_MB = 200
    # A pin stops eviction so a frame cannot be freed while it is being looked
    # at. It expires, because a pinned buffer stops accepting new frames and a
    # silently stale KVM is the failure this tool exists to prevent.
    PIN_TIMEOUT_S = 300.0
    NOSIGNAL_AFTER_S = 2.0
    # How many recent frames must be bit-identical before the stream is called
    # frozen. Eight is ~0.27 s at 30 fps -- long enough that a genuinely static
    # screen still fails it (analog noise makes real frames differ every time,
    # measured: 90 frames, 90 distinct hashes) and short enough to notice a
    # mode change within a third of a second.
    FROZEN_RUN = 8
    # A frame whose pixels are all the same value is not a picture, however
    # unrepeated it is. Duplicate-hash rejection asks "is this frame a repeat?"
    # and a uniform frame can pass that -- the check answers a different
    # question from the one being asked.
    #
    # Measured at the 1/8 scale the selector decodes at: real captures range
    # 77-226 even when almost entirely black (mean 0.10), while the stick's
    # no-lock constant is exactly 0. Four is far below any real frame and far
    # above a constant.
    #
    # The vcctrl session found this the expensive way: two in-game frames,
    # byte-identical twenty seconds apart, every pixel exactly 7, reported as
    # PICTURE mean 7.0 -- which turned "capture relocks during gameplay" into a
    # result that was false. My selector is the algorithm theirs was ported
    # from and had the same gap by construction.
    MIN_RANGE = 4

    def __init__(self, devs, bus=None):
        Capability.__init__(self, devs)
        self.bus = bus
        self.lock = threading.Lock()
        self.ring = collections.deque()
        self.ring_bytes = 0
        self.proc = None
        self.reader = None
        self.running = False
        self.owned = False
        self.last_frame_t = 0.0
        self.frames_total = 0
        self.state = "starting"
        self.spawns = 0
        self.last_error = None
        self.spawn_t = 0.0
        self.fast_failures = 0
        self.last_good = None
        self.seq = 0
        self.thin_passes = 0
        self.dropped_pinned = 0
        self.pinned_at = None
        self.mem_limited = False

    # -- device lifecycle ---------------------------------------------------

    def start(self):
        self.running = True
        self._acquire()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def stop(self):
        self.running = False
        self._release()

    def _acquire(self):
        with self.lock:
            if self.owned:
                return True
            try:
                self.proc = subprocess.Popen(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error",
                     "-f", "v4l2", "-input_format", "mjpeg",
                     "-video_size", "640x480", "-framerate", "30",
                     "-i", self.DEVICE,
                     "-c:v", "copy", "-f", "mjpeg", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    bufsize=0)
            except Exception as exc:
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                return False
            self.owned = True
            self.spawns += 1
            self.spawn_t = time.time()
            self.state = "starting"
            self.reader = threading.Thread(target=self._read_frames,
                                           args=(self.proc,), daemon=True)
            self.reader.start()
        self._publish("video.acquired", spawns=self.spawns)
        return True

    def _release(self):
        with self.lock:
            proc, self.proc, self.owned = self.proc, None, False
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        self._publish("video.released")

    def _publish(self, kind, **kw):
        if self.bus is not None:
            self.bus.publish(kind, **kw)

    # -- the per-frame path -------------------------------------------------

    def _read_frames(self, proc):
        """Split JPEGs out of the mjpeg stream.

        Frames are delimited by SOI (FFD8) and EOI (FFD9). This is safe rather
        than merely usual: inside entropy-coded data every 0xFF is byte-stuffed
        as FF 00, and restart markers are FFD0-FFD7, so FFD9 appears only as a
        genuine EOI.

        Length is still validated, because partial writes happen on this path
        -- one of the existing shot files on the Pi is zero bytes.
        """
        buf = b""
        fd = proc.stdout.fileno()
        while self.running and proc.poll() is None:
            try:
                # os.read, not stdout.read1: with bufsize=0 Popen hands back a
                # raw FileIO, which has no read1 -- and BufferedReader.read(n)
                # would block for the FULL n bytes, holding frames hostage
                # until the buffer filled. A single read syscall returning what
                # is available is what a stream wants.
                chunk = os.read(fd, 65536)
            except Exception as exc:
                # Recorded, not swallowed. The first version of this caught and
                # broke silently, and the reader thread died on an
                # AttributeError while `video state` cheerfully reported
                # owned=true, frames=0 -- a capability reporting healthy while
                # doing nothing is worse than one reporting failure.
                self.last_error = "reader: %s: %s" % (type(exc).__name__, exc)
                self._publish("video.reader_error", error=self.last_error)
                break
            if not chunk:
                break
            buf += chunk
            while True:
                i = buf.find(b"\xff\xd8")
                if i < 0:
                    # No SOI in hand: nothing here is the start of a frame.
                    # Keep one trailing byte in case FF and D8 straddle reads.
                    buf = buf[-1:]
                    break
                j = buf.find(b"\xff\xd9", i + 2)
                if j < 0:
                    buf = buf[i:]
                    break
                frame, buf = buf[i:j + 2], buf[j + 2:]
                if len(frame) >= 128:
                    self._push(frame)

    def _cap(self):
        """Effective byte cap, lowered if the Pi is short of memory."""
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        avail_mb = int(line.split()[1]) / 1024.0
                        break
                else:
                    return self.RING_BYTES
        except Exception:
            return self.RING_BYTES
        headroom = (avail_mb - self.MEM_FLOOR_MB) * 1024 * 1024
        if headroom < self.RING_BYTES:
            self.mem_limited = True
            return max(4 * 1024 * 1024, int(headroom))
        self.mem_limited = False
        return self.RING_BYTES

    def _thin(self):
        """Drop every other frame from the oldest third. Caller holds the lock.

        Trades temporal resolution for wall-clock span, in the region where it
        is needed least: recent frames stay at the full rate for frame-by-frame
        work, and the old end degrades to 15 fps, then 7.5. Thirty seconds of
        history survives in every case.

        O(n) but infrequent -- a pass frees roughly a sixth of the ring, which
        at 30 fps is several seconds of headroom before the next one.
        """
        items = list(self.ring)
        third = max(1, len(items) // 3)
        kept, freed = [], 0
        for i, entry in enumerate(items):
            if i < third and i % 2 == 1:
                freed += len(entry[2])
                continue
            kept.append(entry)
        if freed:
            self.ring.clear()
            self.ring.extend(kept)
            self.ring_bytes -= freed
            self.thin_passes += 1

    def _push(self, frame):
        now = time.time()
        with self.lock:
            pinned = self.pinned_at is not None
            cap = self._cap()
            if pinned and self.ring_bytes + len(frame) > cap:
                # Pinned: the buffer is being examined, so drop the NEW frame
                # rather than free one somebody may be looking at.
                self.dropped_pinned += 1
                self.last_frame_t = now
                return
            self.seq += 1
            self.ring.append((now, self.seq, frame))
            self.ring_bytes += len(frame)
            while self.ring_bytes > cap and len(self.ring) > 1:
                span = now - self.ring[0][0]
                if span > self.TARGET_SPAN_S * 1.05 or len(self.ring) < 8:
                    _t, _s, old = self.ring.popleft()
                    self.ring_bytes -= old and len(old)
                else:
                    before = self.ring_bytes
                    self._thin()
                    if self.ring_bytes >= before:
                        _t, _s, old = self.ring.popleft()
                        self.ring_bytes -= len(old)
            self.last_frame_t = now
            self.frames_total += 1
        # NOTE: this does NOT set state. A frame arriving attests that the USB
        # device produced bytes -- nothing more. Whether those bytes are a live
        # picture is a question about content across several frames, and the
        # watchdog is the single owner of that judgement.
        #
        # The first version set state="locked" here on every frame. At 30 fps
        # that overwrote the watchdog's twice-a-second classification 30 times
        # a second, so the daemon flapped between locked and frozen ~140 times
        # while every `video state` sample returned "locked" -- the race was
        # invisible to polling and only showed up in the event log. Two writers
        # for one piece of state, which is the bug, not the frequency.

    # -- the watchdog -------------------------------------------------------

    def _watchdog(self):
        """Respawn is decided by ffmpeg's liveness, NOT by a clock.

        The distinction that matters, and it is not cosmetic:

          frames stopped, process ALIVE  -> the stick is not locking. The input
            is absent. Respawning cannot manufacture a signal, so publish
            nosignal and do nothing, indefinitely.
          frames stopped, process EXITED -> a wedge. Respawn.

        A timer-driven respawn churns forever against a correct state. Text
        mode 03h never locks, and with the Mach64 fitted a whole game run at
        512x384 produces no frames and nothing is wrong. Both are correct
        steady states that a retry loop would grind against for their entire
        duration.

        The alive-but-wedged case is deliberately NOT handled by a timeout. It
        is indistinguishable from a correct no-lock without a positive signal,
        and inventing a threshold to separate them would reintroduce exactly
        the bug this design removes. If it is ever observed, it needs a real
        signal, not a number.
        """
        while self.running:
            time.sleep(0.5)
            with self.lock:
                owned, proc = self.owned, self.proc
                age = time.time() - self.last_frame_t if self.last_frame_t else None
                state = self.state
                recent = [f for _t, _s, f in list(self.ring)[-self.FROZEN_RUN:]]
            # Hashing 8 frames once per half-second is ~240 KB/s of md5 --
            # nothing next to a 4 Mbit/s stream, and it stays off the per-frame
            # path where rule 1 cares.
            frozen = None
            if len(recent) >= self.FROZEN_RUN:
                frozen = len(set(hashlib.md5(f).digest() for f in recent)) == 1

            # A pin that outlives its usefulness turns the KVM stale, which is
            # the failure this whole tool exists to prevent. Expire it.
            with self.lock:
                if self.pinned_at is not None and \
                        time.time() - self.pinned_at > self.PIN_TIMEOUT_S:
                    self.pinned_at = None
                    expired = True
                else:
                    expired = False
            if expired:
                self._publish("video.pin", state="expired")

            if not owned:
                continue
            if proc is not None and proc.poll() is not None:
                # The process died. Respawn -- but a process that dies within
                # seconds of every spawn is not a transient wedge, it is a
                # device that cannot be opened at all (unplugged, or held by
                # something else). Respawning that at full speed is the same
                # churn this design set out to avoid, arriving from the other
                # direction.
                #
                # The backoff is keyed on an OBSERVED fact -- this process
                # could not stay up -- rather than on an inferred one. That is
                # the distinction that makes it legitimate where a timeout
                # against a correct no-lock was not.
                lifetime = time.time() - self.spawn_t
                with self.lock:
                    if lifetime < 5.0:
                        self.fast_failures += 1
                    else:
                        self.fast_failures = 0
                    fails = self.fast_failures
                self._publish("video.wedged", rc=proc.returncode,
                              lifetime_s=round(lifetime, 2),
                              fast_failures=fails)
                self._release()
                if fails:
                    backoff = min(30.0, 2.0 ** min(fails, 5))
                    with self.lock:
                        self.state = "unavailable"
                    time.sleep(backoff)
                if self.running:
                    self._acquire()
                continue
            # Frames arriving is NOT the same as picture arriving, and this
            # rig proved it the hard way. Prediction said DOS text mode 03h
            # would stop the stream; measurement on 2026-08-19 says the stick
            # keeps delivering 30 fps of BIT-IDENTICAL frames instead -- 30
            # frames, one distinct hash. The daemon reported "locked"
            # throughout, which would have put a frozen text screen on the page
            # captioned as live. That is exactly the lie this tool exists to
            # not tell.
            #
            # So freshness is judged on content, not on arrival. This is the
            # positive signal the design kept asking for: identical hashes
            # attest "the source is repeating", where frame arrival attests
            # only "the USB device is producing bytes".
            # Before the first frame ever arrives there is no last_frame_t to
            # age, so fall back to the spawn time. Without this the daemon sits
            # in "starting" forever on a device that never delivers, which is
            # the state it is least useful to be silent about.
            if age is None:
                age = time.time() - self.spawn_t if self.spawn_t else None

            # One classifier, three outcomes, in order of what each is
            # evidence of:
            #   nosignal -- no bytes at all
            #   frozen   -- bytes, but the same bytes: the source is repeating
            #   locked   -- bytes that keep changing: a live picture
            if age is not None and age > self.NOSIGNAL_AFTER_S:
                new = "nosignal"
            elif frozen is None:
                new = state          # not enough frames yet to judge
            else:
                new = "frozen" if frozen else "locked"

            # While the picture IS live, keep the newest frame as the last
            # known-good one. Cheap -- a reference copy, no decode -- and it
            # has to happen here rather than in shot(), because the whole point
            # is to have a picture available when nobody is asking for one.
            #
            # Measured why this matters: during DOS text mode 03h the stick
            # emits a constant FLAT BLACK frame, not a stale copy of the last
            # real screen. So a viewer in that state has nothing to look at
            # unless something kept the last live frame, and "black rectangle"
            # is indistinguishable from a powered-off machine.
            if new == "locked":
                # Validate before storing. The watchdog used to keep the newest
                # frame unconditionally while locked, so a uniform frame became
                # "the last frame that was picture" -- which is exactly the
                # black lastgood reported earlier, and it was never only a
                # naming problem.
                with self.lock:
                    newest = self.ring[-1] if self.ring else None
                if newest is not None and self._is_picture(newest[2]):
                    with self.lock:
                        self.last_good = (newest[0], newest[2], None)

            if new != state:
                with self.lock:
                    self.state = new
                self._publish("video.%s" % new,
                              last_frame_age_s=round(age, 2) if age else None)

    # -- queries ------------------------------------------------------------

    def _recent(self, n):
        with self.lock:
            items = list(self.ring)[-n:] if n else list(self.ring)
        return items

    def commands(self):
        return {"video": self._video, "burst": self._burst,
                "framestats": self._framestats, "shot": self._shot,
                "lastgood": self._lastgood, "pin": self._pin,
                "timeline": self._timeline, "frame": self._frame}

    # -- scrub --------------------------------------------------------------

    def _pin(self, req):
        """Stop eviction so a frame cannot be freed while it is examined.

        Without this the scrub feature is subtly broken in exactly the case it
        exists for: the live stream keeps writing while you look at something
        interesting, and the frame under the cursor gets evicted from under it.
        """
        action = req.get("action", "status")
        with self.lock:
            if action == "on":
                self.pinned_at = time.time()
            elif action == "off":
                self.pinned_at = None
                self.dropped_pinned = 0
            held = (time.time() - self.pinned_at) if self.pinned_at else None
            out = {"ok": True, "pinned": self.pinned_at is not None,
                   "held_s": round(held, 1) if held else None,
                   "dropped_while_pinned": self.dropped_pinned,
                   "expires_in_s": round(self.PIN_TIMEOUT_S - held, 1)
                   if held else None}
        if action in ("on", "off"):
            self._publish("video.pin", state=action)
        return out

    def _timeline(self, req):
        """Index of what is in the buffer: one entry per frame, no pixels.

        Carries the gap to the previous frame so the UI can draw where the
        buffer has been thinned. A scrub bar that looks uniform while stepping
        1/30 s in one place and 1/7.5 s in another is a lying interface.
        """
        with self.lock:
            items = list(self.ring)
            pinned = self.pinned_at is not None
            span = (items[-1][0] - items[0][0]) if len(items) > 1 else 0.0
            cap = self._cap()
            used = self.ring_bytes
            thins = self.thin_passes
            memlim = self.mem_limited
        out, prev = [], None
        for t, sq, f in items:
            out.append({"seq": sq, "t": round(t, 3), "bytes": len(f),
                        "gap_ms": None if prev is None
                        else round((t - prev) * 1000.0, 1)})
            prev = t
        return {"ok": True, "frames": out, "count": len(out),
                "span_s": round(span, 2), "pinned": pinned,
                "ring_bytes": used, "cap_bytes": cap,
                "thin_passes": thins, "mem_limited": memlim,
                "target_span_s": self.TARGET_SPAN_S}

    def _frame(self, req):
        """One frame by sequence number. Raw, and labelled raw.

        Scrubbing wants the exact frame at a position, not a judgement about
        it -- so unlike `shot` this does no duplicate rejection and no
        brightness selection, and says so.
        """
        import base64
        want = int(req.get("seq", 0))
        with self.lock:
            for t, sq, f in reversed(self.ring):
                if sq == want:
                    return {"ok": True, "raw": True, "seq": sq, "t": t,
                            "age_s": round(time.time() - t, 3),
                            "bytes": len(f),
                            "jpeg": base64.b64encode(f).decode()}
            oldest = self.ring[0][1] if self.ring else None
            newest = self.ring[-1][1] if self.ring else None
        return {"ok": False, "error": "seq %d is not in the buffer" % want,
                "oldest": oldest, "newest": newest}

    # -- selection ----------------------------------------------------------

    def _select(self, items):
        """Duplicate-hash rejection, then brightest survivor.

        Ported from grab() in bin/vcctrl-sweep so the two cannot drift, and the
        reasoning is worth repeating rather than referencing:

        Settle frames are BIT-IDENTICAL to each other; real picture never
        repeats, because analog sampling noise differs every frame. A repeated
        hash is therefore a positive identification of a settle frame, where
        brightness is only a heuristic.

        Step 4 -- returning nothing when every frame repeated -- is the point.
        Brightness alone cannot distinguish a flat-black NO-LOCK from a
        genuinely dark screen: it picks the least-black frame either way and
        reports a number as though it meant something.

        Measured on the live 30 fps stream 2026-08-19, because the original
        claim was measured on 8 fps bursts and needed rechecking at the rate it
        actually runs: 90 frames of a static mode-12h console gave 90 distinct
        hashes, 0 repeated. The assumption holds at full rate.
        """
        digests = {}
        for t, _sq, f in items:
            digests.setdefault(hashlib.md5(f).hexdigest(), []).append((t, f))
        live = [tf for group in digests.values() if len(group) == 1
                for tf in group]
        if not live:
            return None, None, "every frame in the window was a duplicate"
        try:
            from PIL import Image, ImageStat
        except ImportError:
            return None, None, "PIL is not available on this host"
        best, best_mean, errs, flat = None, -1.0, [], 0
        for t, f in live:
            try:
                im = Image.open(io.BytesIO(f))
                # DCT-domain downscale: decoding 1/8 scale is several times
                # cheaper than a full decode and preserves the mean, which is
                # all the selection needs. Measured against full decode below.
                im.draft("L", (im.size[0] // 8, im.size[1] // 8))
                g = im.convert("L")
                lo, hi = g.getextrema()
                if hi - lo < self.MIN_RANGE:
                    flat += 1          # a constant, not a dark picture
                    continue
                m = ImageStat.Stat(g).mean[0]
            except Exception as exc:
                # Recorded rather than swallowed. The first version returned a
                # bare None for three different causes -- no live frames, PIL
                # missing, every decode failing -- and reported all of them as
                # "every frame was a duplicate". A NameError on io.BytesIO
                # (this module did not import io) therefore presented as a
                # confident and wrong statement about the picture.
                #
                # Twice in one file now: an except that discards the reason
                # turns a bug into a lie. A capability may report failure; it
                # may not report a different failure than the one it had.
                errs.append("%s: %s" % (type(exc).__name__, exc))
                continue
            if m > best_mean:
                best, best_mean = (t, f), m
        if best is None:
            if flat and not errs:
                # Say which of the two it is. "No picture" and "a constant" are
                # different facts, and the second one names a specific hardware
                # state worth recognising.
                return None, None, ("all %d non-repeated frames were a uniform "
                                    "constant -- the stick is emitting a blank, "
                                    "not capturing a dark screen" % flat)
            return None, None, "all %d candidate decodes failed: %s" % (
                len(live), errs[0] if errs else "unknown")
        return best, best_mean, len(live)

    def _is_picture(self, frame):
        """True if the frame has any spread at all. See MIN_RANGE."""
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(frame))
            im.draft("L", (im.size[0] // 8, im.size[1] // 8))
            lo, hi = im.convert("L").getextrema()
            return (hi - lo) >= self.MIN_RANGE
        except Exception:
            return False

    def _shot(self, req):
        """One frame, selected -- or an explicit "no picture". The default API.

        Never returns a frame when it cannot tell picture from no-lock. A shot
        that hands back a plausible dark frame during a no-lock reintroduces
        precisely the ambiguity _select exists to remove, and vcctrl-uvconfig
        refuses to start on a null for that reason.
        """
        import base64
        items = self._recent(int(req.get("n", 16)))
        with self.lock:
            state = self.state
        if not items:
            return {"ok": True, "picture": False, "state": state,
                    "reason": "no frames in the ring"}
        best, mean, live = self._select(items)
        if best is None:
            # `live` carries the actual reason in this branch.
            return {"ok": True, "picture": False, "state": state,
                    "reason": live, "considered": len(items)}
        t, frame = best
        with self.lock:
            self.last_good = (t, frame, mean)
        return {"ok": True, "picture": True, "state": state,
                "mean": mean, "t": t, "age_s": round(time.time() - t, 3),
                "considered": len(items), "live": live,
                "bytes": len(frame),
                "jpeg": base64.b64encode(frame).decode()}

    def _lastgood(self, req):
        """The last frame that was positively picture, with its age.

        This is NOT the frozen-last-frame failure. The difference is entirely
        the age: a silently frozen frame makes a KVM lie, while a frame
        captioned "last locked picture, 4m12s ago" is the most useful thing on
        the page during a no-lock -- and on the Mach64, where a whole game run
        at 512x384 never locks, it may be the only picture available.
        """
        import base64
        with self.lock:
            lg = self.last_good
            state = self.state
        if lg is None:
            return {"ok": True, "picture": False, "state": state,
                    "reason": "nothing has been positively picture yet"}
        t, frame, mean = lg
        if mean is None:
            try:
                from PIL import Image, ImageStat
                im = Image.open(io.BytesIO(frame))
                im.draft("L", (im.size[0] // 8, im.size[1] // 8))
                mean = ImageStat.Stat(im.convert("L")).mean[0]
            except Exception:
                mean = None
        return {"ok": True, "picture": True, "state": state, "stale": True,
                "mean": mean, "t": t, "age_s": round(time.time() - t, 3),
                "jpeg": base64.b64encode(frame).decode()}

    def _video(self, req):
        action = req.get("action", "state")
        if action == "state":
            return {"ok": True, **self._state()}
        if action == "release":
            # Escape hatch for tools that still open /dev/video0 directly.
            self._release()
            return {"ok": True, **self._state()}
        if action == "acquire":
            ok = self._acquire()
            return {"ok": ok, "error": self.last_error if not ok else None,
                    **self._state()}
        return {"ok": False, "error": "unknown video action: %r" % action}

    def _state(self):
        with self.lock:
            age = (time.time() - self.last_frame_t) if self.last_frame_t else None
            return {"state": self.state, "owned": self.owned,
                    "frames": self.frames_total, "spawns": self.spawns,
                    "ring_frames": len(self.ring),
                    "ring_bytes": self.ring_bytes,
                    "fast_failures": self.fast_failures,
                    "last_error": self.last_error,
                    "pinned": self.pinned_at is not None,
                    "span_s": round(self.ring[-1][0] - self.ring[0][0], 2)
                    if len(self.ring) > 1 else 0.0,
                    "last_frame_age_s": round(age, 3) if age else None}

    def _burst(self, req):
        """RAW frames, labelled raw. Callers that want a judgement want shot.

        The label is not decoration: settle frames after a device open or a
        mode change are flat black, and a caller comparing the first and last
        raw frames of a burst once reported 11% pixel difference on a provably
        static screen.
        """
        n = int(req.get("n", 16))
        items = self._recent(n)
        import base64
        return {"ok": True, "raw": True, "n": len(items),
                "frames": [{"t": t, "seq": sq,
                            "jpeg": base64.b64encode(f).decode()}
                           for t, sq, f in items]}

    def _framestats(self, req):
        """Duplicate-hash statistics over a window. Diagnostic, not a judgement.

        Exists because the selection algorithm rests on a measured claim --
        settle frames are bit-identical, real picture never repeats -- and that
        was measured on 8 fps bursts, not on a 30 fps persistent stream. This
        is how the claim gets rechecked at the rate it will actually run.
        """
        items = self._recent(int(req.get("n", 90)))
        digests = {}
        for _t, _sq, f in items:
            digests.setdefault(hashlib.md5(f).hexdigest(), 0)
            digests[hashlib.md5(f).hexdigest()] += 1
        counts = sorted(digests.values(), reverse=True)
        return {"ok": True, "n": len(items), "distinct": len(digests),
                "repeated": sum(1 for c in counts if c > 1),
                "largest_group": counts[0] if counts else 0,
                "sizes": [len(f) for _t, _s, f in items[-8:]]}


class AudioCapability(Capability):
    """Owns the capture stick's ALSA device and keeps a rolling PCM ring.

    Same shape as video and for the same reason: the device is exclusive, and a
    persistent owner turns every level query from a 3-second capture into a
    read. But the stakes are different here, and worth stating.

    `bin/vcctrl-audio` is, by its own docstring, the harness's ONLY observation
    channel that is safe to run during a measured cell -- it touches neither
    the keyboard nor /dev/video0. Taking this device without shimming that tool
    would remove the one check that can watch a sweep without perturbing it. So
    the shim lands with this class, not after it.

    The format is fixed by the hardware and happens to be exactly what a
    browser wants: S16_LE, 2ch, 48000 Hz and nothing else. No resampling
    anywhere, on either side. Do not "improve" the capture settings.
    """

    name = "audio"

    DEVICE = os.environ.get("VCCTRL_ALSA", "hw:1,0")
    RATE = 48000
    CHANNELS = 2
    SAMPLE_BYTES = 2
    # 4 MB is ~21 s of 48k stereo S16. Capped in BYTES, like the frame ring and
    # for the same reason: rule 3 does not cover memory (sec. 2).
    RING_BYTES = 4 * 1024 * 1024
    # ~20 ms per chunk. Small enough for a responsive jitter buffer, large
    # enough that the per-chunk overhead is irrelevant.
    CHUNK = 3840
    STALLED_AFTER_S = 2.0

    def __init__(self, devs, bus=None):
        Capability.__init__(self, devs)
        self.bus = bus
        self.lock = threading.Lock()
        self.ring = collections.deque()
        self.ring_bytes = 0
        self.proc = None
        self.owned = False
        self.running = False
        self.last_chunk_t = 0.0
        self.bytes_total = 0
        self.state = "starting"
        self.spawns = 0
        self.spawn_t = 0.0
        self.fast_failures = 0
        self.last_error = None
        self.seq = 0

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        self.running = True
        self._acquire()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def stop(self):
        self.running = False
        self._release()

    def _acquire(self):
        with self.lock:
            if self.owned:
                return True
            try:
                self.proc = subprocess.Popen(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error",
                     "-f", "alsa", "-ar", str(self.RATE),
                     "-ac", str(self.CHANNELS), "-i", self.DEVICE,
                     "-acodec", "pcm_s16le", "-f", "s16le", "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    bufsize=0)
            except Exception as exc:
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                return False
            self.owned = True
            self.spawns += 1
            self.spawn_t = time.time()
            self.state = "starting"
            threading.Thread(target=self._read_pcm, args=(self.proc,),
                             daemon=True).start()
        self._publish("audio.acquired", spawns=self.spawns)
        return True

    def _release(self):
        with self.lock:
            proc, self.proc, self.owned = self.proc, None, False
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
        self._publish("audio.released")

    def _publish(self, kind, **kw):
        if self.bus is not None:
            self.bus.publish(kind, **kw)

    # -- the per-chunk path -------------------------------------------------

    def _read_pcm(self, proc):
        """Raw interleaved S16LE off ffmpeg's stdout. No decode, no analysis.

        Deliberately does no level computation: that would put a per-chunk cost
        on the streaming path for a number almost nobody is asking for. Levels
        are computed on demand from the ring, which is the same discipline that
        keeps the video path from decoding frames it is only passing through.
        """
        fd = proc.stdout.fileno()
        buf = b""
        while self.running and proc.poll() is None:
            try:
                data = os.read(fd, 16384)
            except Exception as exc:
                self.last_error = "reader: %s: %s" % (type(exc).__name__, exc)
                self._publish("audio.reader_error", error=self.last_error)
                break
            if not data:
                break
            buf += data
            while len(buf) >= self.CHUNK:
                self._push(buf[:self.CHUNK])
                buf = buf[self.CHUNK:]

    def _push(self, chunk):
        now = time.time()
        with self.lock:
            self.seq += 1
            self.ring.append((now, self.seq, chunk))
            self.ring_bytes += len(chunk)
            while self.ring_bytes > self.RING_BYTES and len(self.ring) > 1:
                _t, _s, old = self.ring.popleft()
                self.ring_bytes -= len(old)
            self.last_chunk_t = now
            self.bytes_total += len(chunk)

    def _watchdog(self):
        """Liveness, not a clock -- same rule as video (sec. 4.5).

        One difference worth noting: for audio, "no data" really is a fault.
        A capture device that is open delivers samples whether or not anything
        is playing, because silence is still samples. So unlike video, where a
        stopped stream is often correct, a stalled audio stream means the path
        broke.
        """
        while self.running:
            time.sleep(0.5)
            with self.lock:
                owned, proc = self.owned, self.proc
                age = (time.time() - self.last_chunk_t) if self.last_chunk_t \
                    else (time.time() - self.spawn_t if self.spawn_t else None)
                state = self.state
            if not owned:
                continue
            if proc is not None and proc.poll() is not None:
                lifetime = time.time() - self.spawn_t
                with self.lock:
                    self.fast_failures = (self.fast_failures + 1
                                          if lifetime < 5.0 else 0)
                    fails = self.fast_failures
                self._publish("audio.wedged", rc=proc.returncode,
                              fast_failures=fails)
                self._release()
                if fails:
                    with self.lock:
                        self.state = "unavailable"
                    time.sleep(min(30.0, 2.0 ** min(fails, 5)))
                if self.running:
                    self._acquire()
                continue
            new = "stalled" if (age is not None
                                and age > self.STALLED_AFTER_S) else "capturing"
            if new != state:
                with self.lock:
                    self.state = new
                self._publish("audio.%s" % new)

    # -- levels, computed on demand -----------------------------------------

    def _levels(self, ms=3000):
        """RMS and peak in dBFS over the last `ms`, plus a bucket histogram.

        Matches what `ffmpeg -af volumedetect` reports, because
        `bin/vcctrl-audio` reads mean_volume, max_volume and the histogram
        bucket count, and its verdicts are tuned to those numbers. Changing the
        scale would silently invalidate every reference level in FINDINGS.
        """
        want = int(self.RATE * self.CHANNELS * self.SAMPLE_BYTES * ms / 1000.0)
        with self.lock:
            chunks, got = [], 0
            for _t, _s, c in reversed(self.ring):
                chunks.append(c)
                got += len(c)
                if got >= want:
                    break
        if not chunks:
            return None
        frag = b"".join(reversed(chunks))

        full = 32768.0
        import array as _array
        a = _array.array("h")
        a.frombytes(frag[:len(frag) // 2 * 2])
        if sys.byteorder == "big":
            a.byteswap()
        if not len(a):
            return None

        peak = max(max(a), -min(a))

        # RMS in FLOATING POINT, from a strided subsample.
        #
        # Not audioop.rms, which returns an INTEGER: at the noise floor an RMS
        # of 1.41 truncates to 1, which is -90.3 dB instead of -87.3 -- a 3 dB
        # error precisely where "connected but silent" is distinguished from
        # "nothing on the wire". Measured against ffmpeg on a dither-level
        # signal, which is what an idle but connected path actually looks like.
        #
        # Striding to ~20k samples keeps this a few milliseconds and is well
        # inside 0.1 dB for any signal; a level meter does not need every
        # sample, but it does need the arithmetic done in floats. Dropping
        # audioop also outlives its removal in Python 3.13.
        step = max(1, len(a) // 20000)
        sub = a[::step]
        rms = (sum(float(v) * v for v in sub) / len(sub)) ** 0.5

        def db(v):
            return 20.0 * math.log10(v / full) if v > 0 else -91.0

        # Histogram of per-sample levels, bucketed to the dB like volumedetect,
        # over the same subsample as the RMS.
        hist = {}
        for v in sub:
            av = abs(v)
            # Digital zero is not "no reading" -- it is full attenuation, and
            # ffmpeg counts it in the bottom bucket. Skipping it made an
            # all-silent fragment report zero buckets where ffmpeg reports one.
            b = 91 if av == 0 else int(round(-20.0 * math.log10(av / full)))
            hist[b] = hist.get(b, 0) + 1
        # ffmpeg does NOT print every non-empty bucket, and `vcctrl-audio`
        # displays the count it prints. Measured against ffmpeg: a pure sine
        # gives 1 bucket where a naive distinct-count gives 36. volumedetect
        # walks down from the loudest bucket and stops once the buckets it has
        # printed cover 0.1% of samples, so the number is really "how many dB
        # of headroom hold the loudest 0.1%" -- a crest-factor measure, not a
        # count of distinct levels. Replicated here so the figure keeps meaning
        # what every reading in FINDINGS meant.
        total = sum(hist.values())
        shown, acc = {}, 0
        for k in sorted(hist):
            if acc >= total / 1000.0:
                break
            acc += hist[k]
            shown[k] = hist[k]
        # Return the TRUNCATED histogram, not the full one. `vcctrl-audio`
        # prints len(hist) as the bucket count, so the dict it receives has to
        # be the set ffmpeg would have printed -- returning all 36 non-empty
        # buckets would keep the number honest-looking and wrong.
        return {"mean_db": round(db(rms), 2), "peak_db": round(db(peak), 2),
                "hist": shown, "buckets": len(shown),
                "window_ms": round(len(frag) * 1000.0
                                   / (self.RATE * self.CHANNELS
                                      * self.SAMPLE_BYTES), 1)}

    # -- commands -----------------------------------------------------------

    def commands(self):
        return {"audio": self._audio, "level": self._level}

    def _state(self):
        with self.lock:
            age = (time.time() - self.last_chunk_t) if self.last_chunk_t else None
            return {"state": self.state, "owned": self.owned,
                    "rate": self.RATE, "channels": self.CHANNELS,
                    "bytes": self.bytes_total, "spawns": self.spawns,
                    "ring_chunks": len(self.ring), "ring_bytes": self.ring_bytes,
                    "last_chunk_age_s": round(age, 3) if age else None,
                    "fast_failures": self.fast_failures,
                    "last_error": self.last_error}

    def _audio(self, req):
        action = req.get("action", "state")
        if action == "state":
            return dict({"ok": True}, **self._state())
        if action == "release":
            self._release()
            return dict({"ok": True}, **self._state())
        if action == "acquire":
            ok = self._acquire()
            return dict({"ok": ok, "error": None if ok else self.last_error},
                        **self._state())
        return {"ok": False, "error": "unknown audio action: %r" % action}

    def _level(self, req):
        lv = self._levels(int(req.get("ms", 3000)))
        if lv is None:
            return {"ok": True, "level": None, "state": self.state,
                    "reason": "no audio in the ring"}
        return dict({"ok": True, "state": self.state}, **lv)


# The registry. A table in the source, in load order. Video, web, audio, reset
# and files join this list; each is one entry and touches nothing above it.
CAPABILITIES = [InputCapability, LedsCapability, PowerCapability,
                VideoCapability, AudioCapability]

# Bind address for the web UI: LOOPBACK ONLY.
#
# Nothing listens on the tailnet directly. `tailscale serve` terminates TLS for
# vcctrl-pi.example.ts.net and proxies here, so the only way in is over HTTPS,
# and that is the operator's decision -- plain http is not merely discouraged,
# it is unreachable.
#
# This is a stronger guarantee than binding to the tailscale address was. That
# still answered unencrypted requests from anything on the tailnet; this
# answers nothing that has not come through the proxy.
#
# Consequence, and it is the whole reason this is one line with a long comment:
# every tool that talks to the daemon over HTTP must use the HTTPS name.
# bin/vcctrl-sweep and bin/vcctrl-audio were updated with it. If the KVM
# becomes unreachable, `vcctrl` over the unix socket still works -- the
# recovery path does not route through the web server.
WEB_BIND = os.environ.get("VCCTRL_WEB_BIND", "127.0.0.1")
WEB_PORT = int(os.environ.get("VCCTRL_WEB_PORT", "8080"))
# HTTPS served by the daemon itself, so browsers get HTTP/1.1 and WebSocket
# works. `tailscale serve --tcp` forwards this port as raw TCP. See TLSServer.
WEB_TLS_PORT = int(os.environ.get("VCCTRL_WEB_TLS_PORT", "8443"))


def _pace(req):
    return float(req.get("pace", DEFAULT_PACE_S))


class Registry(object):
    """Instantiates capabilities and dispatches commands to them.

    Rule 2 lives here: a capability that raises during start is recorded as
    failed and the daemon carries on without it.
    """

    def __init__(self, devs):
        self.devs = devs
        self.bus = Bus()
        self.activity = Activity()
        self.arbiter = Arbiter(self.bus)
        self.caps = {}
        self.failed = {}
        self.routes = {}
        for cls in CAPABILITIES:
            try:
                # Capabilities that publish take the bus; the original three
                # do not, so the signature stays optional rather than forcing
                # a churn through classes that have no use for it.
                try:
                    cap = cls(devs, self.bus)
                except TypeError:
                    cap = cls(devs)
                cap.start()
            except Exception as exc:
                self.failed[cls.name] = "%s: %s" % (type(exc).__name__, exc)
                sys.stderr.write("capability %s failed to start: %s\n" % (
                    cls.name, self.failed[cls.name]))
                continue
            self.caps[cls.name] = cap
            for cmd, fn in cap.commands().items():
                self.routes[cmd] = (cls.name, fn)

    def dispatch(self, cmd, req):
        route = self.routes.get(cmd)
        if route is None:
            return None
        _name, fn = route

        # Gating and event publishing are central rather than per-capability,
        # so a new capability cannot forget either. A capability that wants to
        # be gated only has to name its command in GATED_COMMANDS.
        if cmd in GATED_COMMANDS:
            refusal = self.arbiter.check(req.get("as"))
            if refusal is not None:
                self.bus.publish("input.refused", cmd=cmd,
                                 locked_by=refusal["locked_by"],
                                 by=req.get("as"))
                return refusal

        ident = self.activity.begin(cmd, req.get("as"))
        t0 = time.time()
        try:
            resp = fn(req)
        except Exception as exc:
            self.bus.publish("cmd.error", cmd=cmd, by=req.get("as"),
                             error="%s: %s" % (type(exc).__name__, exc))
            raise
        finally:
            self.activity.end(ident)
        self.bus.publish("cmd", cmd=cmd, by=req.get("as"),
                         ok=bool(resp.get("ok")),
                         ms=round((time.time() - t0) * 1000.0, 1),
                         detail=_summarise(cmd, req))
        return resp

    def execute(self, req):
        """Full command path, including the ones handled outside the routes.

        `status`, `caps`, `events`, `activity` and `lock` live in handle()
        rather than in a capability, so a caller that only used dispatch() saw
        them as unknown commands. The web UI hit exactly that: state.json
        returned nulls for lock and activity while cheerfully reporting ok.
        """
        return handle(self.devs, self, req)

    def start_web(self):
        """Started after the registry, because it is a view onto the others.

        Failure here is reported and survivable: no browser, everything else
        untouched. That is rule 2 with the one capability most likely to break.
        """
        try:
            import vcweb
            web = vcweb.WebCapability(self, WEB_BIND, WEB_PORT,
                                      tls_port=WEB_TLS_PORT)
            web.start()
        except Exception as exc:
            self.failed["web"] = "%s: %s" % (type(exc).__name__, exc)
            sys.stderr.write("capability web failed to start: %s\n"
                             % self.failed["web"])
            return None
        self.caps["web"] = web
        sys.stderr.write("web ui on http://%s:%d/  tls=%s\n"
                         % (WEB_BIND, WEB_PORT,
                            WEB_TLS_PORT if web.tls_up else "unavailable"))
        return web

    def report(self):
        out = {name: {"ok": True} for name in self.caps}
        for name, err in self.failed.items():
            out[name] = {"ok": False, "error": err}
        return out


def _summarise(cmd, req):
    """One short human-readable field for the activity log.

    Deliberately the literal text for `type`: the operator watching a sweep
    needs to see WHAT was typed, since "the harness typed something" does not
    distinguish a correct launch from a wrong one. Truncated, because a log
    line is not a transcript.
    """
    if cmd == "type":
        t = req.get("text", "")
        return t if len(t) <= 60 else t[:57] + "..."
    if cmd in ("key", "combo"):
        return " ".join(req.get("keys", []))
    if cmd in ("keydown", "keyup", "hold"):
        return req.get("key", "")
    if cmd == "power":
        return req.get("action", "state")
    if cmd == "mouse_move":
        return "%s,%s" % (req.get("dx", 0), req.get("dy", 0))
    return ""


def handle(devs, registry, req):
    cmd = req.get("cmd")

    # `status` predates the registry and its exact shape is a compatibility
    # contract -- the peer session's tooling parses it. Do NOT add keys here;
    # capability health is reported by `caps` precisely so this stays
    # byte-identical to what it returned before the refactor.
    #
    # The sharper reason, from the vcctrl session, is not "it would break a
    # caller today" -- their preflight reads status["usb4vc"] and would not
    # notice. It is that adding capability health here makes `status` a dict
    # whose truthiness varies with unrelated subsystem health, and then someone
    # writing the obvious `all(status.values())` gets a preflight that refuses
    # to run a sweep because the web UI is down. Keeping them separate means
    # that mistake is not available to make.
    if cmd == "status":
        return {"ok": True,
                "keyboard": devs.kbd.device.path,
                "mouse": devs.mouse.device.path,
                "usb4vc": usb4vc_holds_us(),
                "led_paths": devs.led_paths,
                "leds": devs.read_leds()}

    if cmd == "caps":
        return {"ok": True, "capabilities": registry.report()}

    if cmd == "events":
        return dict({"ok": True},
                    **registry.bus.since(int(req.get("since", 0)),
                                         int(req.get("limit", 200))))

    if cmd == "activity":
        # Everything the browser needs to answer "is it stuck": what is running
        # and for how long, what holds the input lock, and how long since
        # anything at all happened. Silence and wedged are indistinguishable
        # without that last one.
        latest = registry.bus.since(max(0, registry.bus.seq - 1), 1)["events"]
        return {"ok": True,
                "inflight": registry.activity.report(),
                "lock": registry.arbiter.status(),
                "last_event_age_s": (round(time.time() - latest[0]["t"], 3)
                                     if latest else None),
                "seq": registry.bus.seq}

    if cmd == "lock":
        action = req.get("action", "status")
        who = req.get("as")
        if action == "status":
            return dict({"ok": True}, **registry.arbiter.status())
        if action == "acquire":
            if not who:
                return {"ok": False, "error": "lock acquire needs 'as'"}
            return registry.arbiter.acquire(who)
        if action == "release":
            return registry.arbiter.release(who)
        if action == "break":
            # Break-glass. Taints the run by design -- see docs/WEBKVM.md
            # sec. 2. The taint is published to the bus, not merely returned,
            # so it lands in the run's log rather than only in the reply to
            # whoever clicked the button.
            if not who:
                return {"ok": False, "error": "lock break needs 'as'"}
            return registry.arbiter.acquire(who, force=True)
        return {"ok": False, "error": "unknown lock action: %r" % action}

    resp = registry.dispatch(cmd, req)
    if resp is None:
        # Error text preserved verbatim from before the refactor: callers
        # branch on `ok` and some match on the message.
        return {"ok": False, "error": "unknown command: %r" % cmd}
    return resp


def _serve_one(conn, devs, registry):
    try:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        if not buf.strip():
            return
        try:
            resp = handle(devs, registry, json.loads(buf.decode("utf-8")))
        except Exception as exc:
            resp = {"ok": False, "error": "%s: %s" % (
                type(exc).__name__, exc)}
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
    except Exception:
        pass
    finally:
        conn.close()


def serve(devs, registry):
    """One thread per connection.

    Why: the daemon used to handle one request at a time, to completion. A
    `power cycle` blocked it for 15 s and `ledwait 5` for 5 s, so the keyboard
    froze whenever anything slow ran -- including the operator's own click on
    the power button. Unusable behind a browser.

    What threading does NOT break, and the reason it is safe: every multi-event
    operation in Devices already takes `self.lock` for the WHOLE operation, not
    per event. `type_text` holds it across the entire string. So two concurrent
    `type` calls serialise into two intact strings rather than interleaving
    into one corrupt one. That property is now load-bearing and is an
    acceptance test (docs/WEBKVM.md sec. 13.2) -- if anyone ever narrows one of
    those locks to per-event, `CD \\DOSKUTSU` and `QA 1` start arriving as
    `CQDA  \\1DOSKUTSU`, which corrupts a sweep launch silently and looks like
    a DOS quirk rather than a bug here.
    """
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o666)
    srv.listen(8)
    sys.stderr.write("vcctrld ready: kbd=%s mouse=%s leds=%s caps=%s\n" % (
        devs.kbd.device.path, devs.mouse.device.path,
        sorted(devs.led_paths), sorted(registry.caps)))
    if registry.failed:
        sys.stderr.write("vcctrld degraded: %s\n" % (sorted(registry.failed),))
    sys.stderr.flush()
    while True:
        conn, _ = srv.accept()
        t = threading.Thread(target=_serve_one, args=(conn, devs, registry),
                             daemon=True)
        t.start()


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
    registry = Registry(devs)
    registry.start_web()
    serve(devs, registry)
    return 0


if __name__ == "__main__":
    sys.exit(main())
