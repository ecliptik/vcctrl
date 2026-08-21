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
import faulthandler
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


# Every string below travels into /state.json, which every open tab polls every
# 1.5 s and which the page renders. An exception message is arbitrary text from
# somewhere else -- a device path, a library's complaint, whatever a subprocess
# wrote -- so it is bounded here rather than trusted to be short. The webkvm
# session already caps ffmpeg's stderr at the same 240; this brings the
# exception paths in line so there is one rule and not two.
#
# Bounding is a SIZE measure, not a safety one: escaping belongs where the text
# is rendered, and the page does that.
ERR_MAX = 240


def errstr(exc, prefix=""):
    s = "%s%s: %s" % (prefix, type(exc).__name__, exc)
    return s if len(s) <= ERR_MAX else s[:ERR_MAX - 1] + "\u2026"


def power_state(host):
    info = kasa_send(host, {"system": {"get_sysinfo": {}}})
    info = info["system"]["get_sysinfo"]
    # None, not False, when the plug answers WITHOUT saying. bool(None) is
    # False, which would report "the machine is off" on the word of a reply
    # that never mentioned it -- and PowerCapability goes to some trouble one
    # layer up to keep `on` tri-state for exactly this reason. Collapsing it
    # here would undo that before it ever reached the caller.
    relay = info.get("relay_state")
    return {"on": None if relay is None else bool(relay),
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
    """One device or concern. Subclasses declare a name and a command map.

    Every capability has a `bus`, set by the registry after construction. It
    used to be passed to whichever constructors happened to accept it, with a
    TypeError fallback for the rest -- so LedsCapability had no `bus` at all,
    and the first line of code that touched it raised. That is the same defect
    as the watchdog reaching for `pinned_at` on a class that never defined it:
    an attribute that exists on some siblings and not others, with nothing
    saying which.
    """

    name = None
    bus = None

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


class TargetEpoch(object):
    """When the TARGET last changed power state, so a reading can be shown to
    belong to the present rather than merely to have been read recently.

    THE PROBLEM (FINDINGS sec. 33). A status surface retains the last value
    published to it, and a machine that is off publishes nothing -- so it goes
    on reporting what was true before, and it does not read as stale, because
    a stale value and a current one ARE the same value. Minutes after the
    Gateway was powered off the daemon reported "the target is powered off"
    and "the target acknowledged a keystroke" in the same breath. Both fields
    were working exactly as written. Only one was about now.

    Reading it again does not help: the retained value is what you get. What
    is needed is evidence the reading was PRODUCED in the current epoch, and
    the cheapest such evidence is that the value has changed since the last
    power transition.

    A module-level fact rather than a call across capabilities: PowerCapability
    reports transitions here, LedsCapability reads them, and neither holds a
    reference to the other. Same shape as installed_board_id().
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.epoch = 0            # increments on every OBSERVED transition
        self.powered = None       # last observed: True / False / None
        self.changed_at = None    # wall clock of that transition

    def observe(self, on):
        """Record a power reading. Returns True if this was a transition."""
        with self.lock:
            if on == self.powered:
                return False
            first = self.powered is None
            self.powered = on
            if first:
                # The first reading is not a transition -- there is no prior
                # state for it to have moved from, and counting it would void
                # every verification made before the daemon's first heartbeat.
                return False
            self.epoch += 1
            self.changed_at = time.time()
            return True

    def state(self):
        with self.lock:
            return self.epoch, self.powered, self.changed_at


TARGET = TargetEpoch()

# When this daemon process began. The LED change record lives in memory, so a
# restart empties it -- and a restart is one of the events most likely to sit
# next to an intermittent worth investigating. The record therefore has to say
# how far back it goes, or a suddenly-empty list reads as "nothing has
# happened" rather than "I have not been watching long".
#
# Persisting it instead was considered and rejected, on the webkvm session's
# reasoning: a record that survives a restart also survives the epoch it
# belongs to, which is a different kind of lie. Saying the scope is the honest
# fix, and it belongs in the OUTPUT rather than in one consumer's UI -- the
# page already says it, and a CLI caller deserves the same sentence
# (FINDINGS sec. 32).
DAEMON_START_T = time.time()


# Boards that have a PS/2 LED return channel. PBID 1 is the IBM PC board; 2
# and 3 are ADB, which has no equivalent -- there is no protocol message in
# which a Macintosh reports its lock-key state back to a keyboard.
LED_BOARDS = (1,)


def installed_board_id():
    """The installed board's id from the primary source, or None.

    Deliberately reads ONLY /run/usb4vc/board.json and deliberately does NOT
    fall back to the journal the way BoardCapability does. This is used to
    decide whether a capability is MEANINGFUL, and for that question a
    confident wrong answer is far worse than an admitted unknown -- so the
    cheap, unambiguous source is the only one consulted and everything else is
    None.

    It duplicates a file read and NOT a policy. The thing that must not be
    duplicated is "which sources to trust in what order", which lives in
    BoardCapability and stays there; if this grew a second source the two would
    drift and the rig would have two answers to one question.

    Capabilities do not call into each other here (see InputCapability's rule
    1), which is why this is a module function rather than a reach across the
    registry.
    """
    try:
        with open(BoardCapability.FILE) as f:
            bid = json.load(f).get("id")
        return int(bid) if bid is not None else None
    except Exception:
        return None


class LedsCapability(Capability):
    """The PS/2 LED return channel -- non-video proof a keystroke landed.

    Reads only; the LEDs are written by USB4VC from what the DOS host sends
    back over PS/2. Touches no lock and blocks nothing.
    """

    name = "leds"

    # When was the input path last PROVEN, rather than assumed?
    verified_at = None
    verified_ok = None

    # Evidence that the target has published on this channel since the last
    # power transition. `_seen_values` is the last sample; when it changes we
    # know something on the far end produced it, and we record which epoch
    # that happened in. See TargetEpoch.
    _seen_values = None
    _proven_epoch = None

    # A BOUNDED history of transitions. The webkvm session asked for this to
    # catch an intermittent: the Pi held 1/0/0 while the target had 0/1/1, and
    # it cleared on its own with nobody watching. A level cannot show that
    # afterwards; a history can.
    #
    # WHAT IT PROVES, AND WHAT IT DOES NOT. There is no heartbeat on this
    # channel -- the LED byte arrives only when a lock key changes, so an idle
    # machine publishes nothing, indistinguishably from one that has fallen off
    # the wire. This therefore shows that an intermittent LEFT A TRACE. It does
    # NOT establish that a reading is current on a quiet machine, and the
    # naming should stop anyone reaching for that.
    #
    # Bounded at 200 deliberately: a daemon that runs for weeks with an
    # unbounded record has a slow leak nobody notices until it matters.
    CHANGES_MAX = 200
    POLL_S = 1.0
    _changes = None            # collections.deque, newest last
    _changes_seq = 0
    _poll_thread = None

    def start(self):
        """Poll at 1 Hz so a change is recorded whether or not anyone asks.

        Sampling only on demand would miss precisely the event this exists for
        -- a divergence that appears and clears with nobody watching. Three
        sysfs reads at 1 Hz is microseconds and bounds the timestamp error to
        one second, which is far inside anything we reason about here.
        """
        if LedsCapability._changes is None:
            LedsCapability._changes = collections.deque(maxlen=self.CHANGES_MAX)
        self._poll_thread = threading.Thread(
            target=self._poll, name="led-changes", daemon=True)
        self._poll_thread.start()

    def _poll(self):
        while True:
            try:
                self._sample()
            except Exception:
                pass
            time.sleep(self.POLL_S)

    def _sample(self):
        """Read the nodes and record a transition if the value moved.

        Returns the values read, or None if they could not be read. Shared by
        the poller and snapshot(), so both maintain the same record and there
        is one place that decides what counts as a change.
        """
        try:
            values = self.devs.read_leds()
        except Exception:
            return None
        if not values:
            return None
        prev = LedsCapability._seen_values
        if values != prev:
            epoch, _powered, _at = TARGET.state()
            LedsCapability._seen_values = dict(values)
            LedsCapability._proven_epoch = epoch
            if prev is not None:
                # The first sample of a daemon's life is not a transition --
                # there is no prior state for it to have moved from, and
                # recording one would put a fictitious change at every start.
                LedsCapability._changes_seq += 1
                LedsCapability._changes.append({
                    "seq": LedsCapability._changes_seq,
                    "t": time.time(),
                    # MONOTONIC alongside wall clock, because this Pi's clock
                    # can step and the entire value of this record is ordering.
                    "mono": round(time.monotonic(), 3),
                    "epoch": epoch,
                    "from": prev,
                    "to": dict(values),
                })
        return values

    def _led_changes(self, req):
        n = req.get("n", 50)
        try:
            n = max(1, min(self.CHANGES_MAX, int(n)))
        except (TypeError, ValueError):
            n = 50
        rec = list(LedsCapability._changes or ())
        rec.reverse()                      # newest first
        return {"ok": True, "changes": rec[:n], "count": len(rec),
                "bounded_at": self.CHANGES_MAX,
                # SCOPE, stated rather than left to be inferred: the record is
                # in memory and starts empty at every restart.
                "since_t": DAEMON_START_T,
                "since_s": round(time.time() - DAEMON_START_T, 1),
                "note": ("counted since this daemon started (see since_s) -- "
                         "the record is in memory and a restart empties it. "
                         "Shows that an intermittent left a trace. Does NOT "
                         "establish that a reading is current: the byte "
                         "arrives only on a lock-key change, so an idle "
                         "machine publishes nothing, indistinguishably from "
                         "one that has fallen off the wire.")}

    def commands(self):
        return {"leds": self._leds, "ledwait": self._ledwait,
                "verify_input": self._verify_input,
                "led_changes": self._led_changes}

    def support(self):
        """Is an LED return channel MEANINGFUL on the installed board?

        Three values, and the third is the point. `True` for the IBM PC board,
        `False` for ADB -- a Macintosh has no protocol message in which it
        reports lock-key state back to a keyboard, so there is nothing to read
        and never will be. `None` when the board is unknown, because
        "this board has none" and "I do not know which board" are different
        facts and a consumer acts differently on each.

        Without this, a Mac session was indistinguishable from a broken PS/2
        session: the same absent LEDs, the same failed round trip, the same
        red indicator. One is working hardware and the other is a fault.
        """
        bid = installed_board_id()
        if bid is None:
            return None, "board unknown, so it is not known whether LEDs apply"
        if bid in LED_BOARDS:
            return True, None
        return False, ("board %d has no PS/2 LED return channel (ADB does not "
                       "report lock-key state back to the keyboard)" % bid)

    def snapshot(self):
        """The LED object as it appears to consumers.

            {"available": true,  "why": null,          "capslock": 0, ...,
             "changed_at": 1787270112.481, "changes": 37}
            {"available": false, "why": "unsupported", "reason": "..."}
            {"available": false, "why": "error",       "reason": "..."}
            {"available": false, "why": "unknown",     "reason": "..."}
            {"available": false, "why": "unpowered",   "reason": "..."}
            {"available": false, "why": "unproven",    "reason": "..."}

        `why` names a distinct reason and is not several falsy values wearing
        one flag. `unsupported` is a Macintosh, which is working hardware.
        `error` is a Gateway whose PS/2 lead is dead, which is a fault.
        `unknown` is "not checked yet". `unpowered` and `unproven` are about
        CURRENCY rather than capability -- the channel exists and these values
        are real, they are simply not about now (sec. 33).

        **THE SET IS NOT CLOSED, AND THIS DOCSTRING IS WHERE THAT IS LEARNED.**
        It said CLOSED SET and listed three while emitting five, for two hours,
        and the cost was not hypothetical: the webkvm session read this code in
        good faith, wrote a consumer branch against the three, and a Gateway
        that was merely switched off would have been described to the operator
        as a board with no LED hardware. Then I read THEIR code, equally in
        good faith, and predicted it would render correctly. Same seam, three
        times in one day, in both directions.

        So: a consumer must map `why` to PRESENTATION and show `reason` as
        written, never paraphrase it, and must degrade sensibly on a value it
        has never heard of. And **adding a value here is a seam event** -- it
        is announced to consumers in the same breath as it is deployed, not
        left to be discovered by reading.

        `changed_at` and `changes` are present only when available, and are a
        SUMMARY of the `led_changes` record rather than a second source --
        both read the same bounded deque. The full history is its own command
        because it has a different lifetime and size from a snapshot.

        WHEN available IS FALSE THE VALUE KEYS ARE ABSENT, NEVER ZERO. A
        plausible set of zeroes is worse than no data: a consumer that forgets
        to check `available` reads it as "all three LEDs are off" and is
        confidently wrong, where a missing key gives it undefined and it shows
        a dash. Half a schema is worse than none, and this is the half that
        usually gets skipped.
        """
        supported, reason = self.support()
        if supported is False:
            return {"available": False, "why": "unsupported", "reason": reason}
        # One sampler, shared with the 1 Hz poller, so both maintain the same
        # record and there is a single place that decides what a change is.
        values = self._sample()
        if not values:
            return {"available": False, "why": "error",
                    "reason": "could not read the LED nodes"}

        epoch, powered, changed_at = TARGET.state()

        if powered is False:
            # POSITIVE determination, not a guess: a machine with no power
            # publishes nothing, so whatever these nodes hold was produced
            # before the plug was cut. Values OMITTED, per the same rule as
            # every other unavailable state -- a plausible set of retained
            # numbers is worse than none, because it is indistinguishable
            # from a live reading.
            return {"available": False, "why": "unpowered",
                    "reason": ("the target has no power, so these nodes hold "
                               "what it published before the cut -- a real "
                               "reading, and not about now")}

        if (LedsCapability._proven_epoch is not None
                and LedsCapability._proven_epoch != epoch):
            # The sharpest case, and the one a power-off check alone misses:
            # just after power returns, the nodes still hold the PREVIOUS
            # boot's values and the machine is on. This is the moment
            # wait_cold_boot() exists for -- readiness is the last thing a
            # healthy boot sets, so a level check for "ready" reads TRUE
            # 2.5 s after power-on on a machine that has not begun to POST.
            return {"available": False, "why": "unproven",
                    "reason": ("the target's power changed at %s and it has "
                               "not published on this channel since, so these "
                               "values belong to the previous epoch"
                               % (time.strftime("%H:%M:%S",
                                                time.localtime(changed_at))
                                  if changed_at else "an unknown time"))}

        if supported is None:
            # Readable, but we cannot say the reading MEANS anything, because
            # we do not know which board is in. Values are withheld rather
            # than published with a caveat -- a caveat next to a number gets
            # dropped and the number does not.
            return {"available": False, "why": "unknown", "reason": reason}
        out = {"available": True, "why": None, "reason": None}
        # SUMMARY of the change record, not a second source of truth -- both
        # are derived from the same deque. This is what a lamp tooltip needs
        # ("last moved 4m ago") without fetching a history it will not show.
        last = (LedsCapability._changes or ())
        out["changed_at"] = last[-1]["t"] if last else None
        out["changes"] = len(last)
        out.update(values)
        return out

    def _verify_input(self, req):
        """Prove the input path by round trip, because nothing else can.

        The vcctrl session unplugged the PS/2 lead and the harness reported
        everything healthy: usb4vc holding both devices, input ok, LEDs
        returning plausible values. All true, and all about the Pi -- the
        uinput nodes exist whether or not the STM32 is attached to anything.
        Every status this daemon publishes about input is a statement about
        its own end of the wire.

        A round trip is different in kind: toggle Caps Lock and watch for the
        LED to come back. The value returns only if the target's keyboard
        controller received the key and published its state, which cannot
        happen with the lead out. It proves the LINK, not that DOS read
        anything -- Caps Lock is BIOS-serviced, and conflating those is a
        separate mistake this rig has already paid for.
        """
        # REFUSE rather than run on a board with no return channel. On ADB
        # this would toggle Caps Lock, wait 1.5 s for an LED that cannot
        # arrive, and then report "the PS/2 link is not carrying keystrokes"
        # -- a false alarm about working hardware, latched into verified_ok
        # where the page renders it red. Not looking is not a finding, so the
        # verified_* fields are deliberately LEFT ALONE here: a stale honest
        # result from the Gateway is better than a fresh dishonest one from
        # a machine this test cannot address.
        supported, why = self.support()
        if supported is not True:
            return {"ok": True, "verified": None,
                    "available": False,
                    "why": "unsupported" if supported is False else "unknown",
                    "reason": why,
                    "note": ("not attempted -- this test can only prove a "
                             "PS/2 link, and the LED round trip it depends "
                             "on does not exist here")}

        before = self.devs.read_leds()
        try:
            self.devs.key(["capslock"])
        except Exception as exc:
            return {"ok": False, "error": "could not send: %s" % exc}
        changed, deadline = False, time.time() + 1.5
        while time.time() < deadline:
            if self.devs.read_leds() != before:
                changed = True
                break
            time.sleep(0.02)
        try:
            self.devs.key(["capslock"])          # put it back
        except Exception:
            pass
        LedsCapability.verified_at = time.time()
        LedsCapability.verified_ok = changed
        if self.bus:
            self.bus.publish("input.verify", ok=changed)
        return {"ok": True, "verified": changed, "before": before,
                "after": self.devs.read_leds(),
                "note": ("the target acknowledged a keystroke"
                         if changed else
                         "no LED change -- the PS/2 link is not carrying "
                         "keystrokes, whatever the device status says")}

    def _leds(self, req):
        """`leds` stays a FLAT dict of values, and that is load-bearing.

        bin/vcctrl_common.leds() returns this key and every caller does
        .get("capslock"). Nesting it would make stable_led(), wait_led(),
        arm_leds() and at_prompt() all read None -> False on a perfectly
        healthy machine, which is the worst failure direction this harness
        has: at_prompt() returning False reads as "something is still
        running", so an unattended sweep stalls looking exactly like a wedge.
        The new information is therefore ADDED alongside, never folded in.
        """
        return {"ok": True, "leds": self.snapshot()}

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

    # A cached reading older than this is reported as stale. The plug only
    # changes when something acts on it, so an old reading is usually still
    # true -- but "usually true" is exactly the kind of value this rig has been
    # burned by, so the age travels with it and the consumer is told rather
    # than left to assume.
    STALE_S = 120.0

    def __init__(self, *a, **kw):
        super(PowerCapability, self).__init__(*a, **kw)
        self._seen = None      # last successful power_state() result
        self._seen_t = 0.0
        self._seen_host = None
        self._fail = None      # why the most recent refresh did not succeed

    # One request per interval for the WHOLE DAEMON, regardless of how many
    # tabs are open. That distinction is the point: querying the plug per tab
    # per 1.5 s poll was the thing worth avoiding, not querying it at all.
    # Without a heartbeat `stale` latches true after the first idle hour and a
    # permanently-set flag carries no information -- the page would show
    # "unknown" during exactly the quiet periods when someone glances at it.
    REFRESH_S = 60.0

    def start(self):
        # Off the main thread: the plug is on the LAN and a dead plug must not
        # delay or fail daemon startup.
        threading.Thread(target=self._heartbeat, name="power-id",
                         daemon=True).start()

    def _heartbeat(self):
        while True:
            self._refresh()
            time.sleep(self.REFRESH_S)

    def _refresh(self):
        try:
            host = load_config().get("kasa_host")
            if host:
                self._remember(host, power_state(host))
                self._fail = None
        except Exception as exc:
            # Record WHY, and let snapshot() turn `on` into null. A plug that
            # stopped answering and a plug reporting off are opposite facts and
            # must not share a JSON value.
            self._fail = errstr(exc)

    def _remember(self, host, st):
        self._seen, self._seen_t, self._seen_host = st, time.time(), host
        # Tell the epoch, so readings taken before a power transition can be
        # told from readings taken after one. See TargetEpoch.
        TARGET.observe(st.get("on"))

    def snapshot(self):
        """Plug identity and last known relay state, for /state.json.

        EVERY KEY IS ALWAYS PRESENT, null where unknown. A consumer that has to
        branch on which keys exist ends up encoding the daemon's internal
        states in the page, and a key that is sometimes absent has already
        broken the KVM twice this week.

        This is deliberately NOT a live query. state.json is polled every 1.5 s
        by every open tab; querying the plug on that cadence would put the mains
        control of a 1995 machine behind a network request per tab per poll.
        """
        cfg_host = None
        try:
            cfg_host = load_config().get("kasa_host")
        except Exception:
            pass
        st, t = self._seen, self._seen_t
        if not st:
            return {"host": cfg_host, "alias": None, "model": None,
                    "on": None, "age_s": None, "stale": None,
                    "reason": "the plug has not answered since the daemon started"}
        age = round(time.time() - t, 1)
        # `on` is TRI-STATE. If the last refresh failed we no longer know the
        # relay state, and reporting the last-known value as though it were
        # current is how "the machine is off" and "I cannot reach the plug"
        # become the same JSON. Three separate bugs on this rig today were that
        # exact collapse, so it is null and `reason` says why.
        unreachable = self._fail is not None
        return {"host": self._seen_host or cfg_host,
                "alias": st.get("alias"), "model": st.get("model"),
                "on": None if unreachable else st.get("on"),
                "age_s": age,
                "stale": unreachable or age > self.STALE_S,
                "reason": self._fail}

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
            st = power_state(host)
            self._remember(host, st)
            return {"ok": True, "power": st}
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
        self._remember(host, st)
        self._audit(action, req.get("as"), "done on=%s" % st.get("on"))
        return {"ok": True, "power": st}


def _keep_stderr(cap, proc):
    """Keep ffmpeg's own explanation instead of throwing it away.

    Both spawns used stderr=DEVNULL, so a device that would not open produced
    `fast_failures` climbing and NOTHING ELSE -- state.json carried
    last_error: null while ffmpeg was, on the other side of the pipe, saying
    exactly what was wrong. Verified on the Pi 5 with no stick attached:
    video and audio both unavailable, nine failures each, no reason given.

    That is the wrong thing to discard on a rig whose devices are addressed by
    INDEX. "hw:1,0" is not a stick, it is a guess about enumeration order, and
    when the guess is wrong the difference between "cannot open audio device
    hw:1,0" and "cannot open ... hw:2,0 (Device or resource busy)" is the
    difference between one environment variable and an afternoon.

    Drained in a thread because a PIPE nobody reads eventually fills and stops
    the process it was meant to be watching. At -loglevel error the volume is
    a line or two per failed spawn.
    """
    # The FIRST lines, not the last. ffmpeg names the specific fault first and
    # then summarises: "Cannot open video device /dev/videoX: No such file or
    # directory" followed by "Error opening input files: No such file or
    # directory". Keeping the most recent line kept the second one, which is
    # true, useless, and identical for every device that will not open --
    # caught by the test asserting the device name survives.
    kept = []
    try:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line or line in kept:
                continue
            if len(kept) < 3:
                kept.append(line)
                with cap.lock:
                    cap.last_error = " | ".join(kept)[:240]
    except Exception:
        pass
    finally:
        try:
            proc.stderr.close()
        except Exception:
            pass

# ── MJPEG IN AVI ──────────────────────────────────────────────────────────
# The ring already holds JPEGs. Muxing them into a container costs no encode
# and loses no byte: what plays back is exactly what the daemon judged, which
# matters when the file is evidence about why a picture stopped. Re-encoding
# to H.264 would put the Pi's compressor between the fault and the person
# looking at it, and an artefact would then be unattributable.
#
# AVI rather than Matroska because AVI's MJPEG support is universal -- VLC,
# mpv, QuickTime and ffmpeg all open it without a codec pack -- and because
# writing it is 100 lines with no dependency.

def jpeg_dims(buf):
    """Width and height from a JPEG's SOF marker, or None.

    Read rather than assumed. The Gateway's stick emits 640x480 today and a
    Macintosh capture will not, and a container header that disagrees with its
    frames plays as a smear rather than as an error.
    """
    i, n = 2, len(buf)
    while i + 9 < n:
        if buf[i] != 0xFF:
            i += 1
            continue
        m = buf[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seg = (buf[i + 2] << 8) | buf[i + 3]
        # SOF0..SOF15, except DHT (C4), JPG (C8) and DAC (CC), which are not
        # frame headers and would give a plausible wrong answer if read as one.
        if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
            return ((buf[i + 7] << 8) | buf[i + 8],
                    (buf[i + 5] << 8) | buf[i + 6])
        if seg < 2:
            return None
        i += 2 + seg
    return None


def avi_mjpeg(frames, fps, width, height):
    """Mux JPEG frames into an AVI. `frames` is a list of bytes, in order.

    Fixed rate: AVI has one frame interval for the whole file. The ring is
    NOT evenly spaced -- it is thinned when bytes run out -- so the caller
    repeats a frame to cover the time it was on screen. That is what keeps a
    stall looking like a stall instead of being smoothed into motion.
    """
    import struct

    def chunk(fourcc, payload):
        pad = b"\x00" * (len(payload) & 1)
        return fourcc + struct.pack("<I", len(payload)) + payload + pad

    biggest = max((len(f) for f in frames), default=0)
    rate = int(round(fps * 1000))
    avih = struct.pack(
        "<14I",
        int(round(1000000.0 / fps)),      # dwMicroSecPerFrame
        int(biggest * fps),               # dwMaxBytesPerSec
        0,                                # dwPaddingGranularity
        0x10,                             # dwFlags: AVIF_HASINDEX
        len(frames),                      # dwTotalFrames
        0,                                # dwInitialFrames
        1,                                # dwStreams
        biggest,                          # dwSuggestedBufferSize
        width, height, 0, 0, 0, 0)
    strh = (b"vids" + b"MJPG" + struct.pack("<I", 0) + struct.pack("<HH", 0, 0)
            + struct.pack("<7I", 0, 1000, rate, 0, len(frames), biggest,
                          0xFFFFFFFF)
            + struct.pack("<I", 0)
            + struct.pack("<4H", 0, 0, width, height))
    # BITMAPINFOHEADER is eleven fields, not ten, and the two pixels-per-metre
    # ones are SIGNED: biSize, biWidth, biHeight, biPlanes, biBitCount,
    # biCompression, biSizeImage, biXPelsPerMeter, biYPelsPerMeter, biClrUsed,
    # biClrImportant -- 40 bytes.
    strf = struct.pack("<I2i2H2I2i2I", 40, width, height, 1, 24,
                       0x47504A4D,          # 'MJPG' little-endian
                       biggest, 0, 0, 0, 0)
    hdrl = chunk(b"LIST", b"hdrl" + chunk(b"avih", avih)
                 + chunk(b"LIST", b"strl" + chunk(b"strh", strh)
                         + chunk(b"strf", strf)))

    movi, idx, off = [b"movi"], [], 4
    for f in frames:
        pad = len(f) & 1
        movi.append(b"00dc" + struct.pack("<I", len(f)) + f + b"\x00" * pad)
        # 0x10 is AVIIF_KEYFRAME. Every MJPEG frame is a keyframe, which is
        # also why seeking in this file is exact rather than approximate.
        idx.append(b"00dc" + struct.pack("<3I", 0x10, off, len(f)))
        off += 8 + len(f) + pad
    body = (hdrl + chunk(b"LIST", b"".join(movi))
            + chunk(b"idx1", b"".join(idx)))
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"AVI " + body


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

    # Overridable for the same reason the ALSA device is: a device NODE is not
    # a device. /dev/video0 is whatever enumerated first, and a UVC capture
    # stick can present two nodes (capture and metadata) in either order on a
    # machine whose other video devices differ. Named here so a machine that
    # numbers them differently is a systemd Environment= line rather than an
    # edit to this file on the box, at the point in a migration where editing
    # source on hardware is the last thing anyone should be doing.
    # /dev/v4l/by-id/... is the stable name if the index ever moves.
    DEVICE = os.environ.get("VCCTRL_VIDEO", "/dev/video0")
    # 48 MB. 30 s at 30 fps is 900 frames: 13.5 MB of text console but 63 MB of
    # a dense screen, a 4.7x spread. A buffer sized in seconds has no fixed
    # cost and one sized in bytes has no fixed duration, so this is capped in
    # BYTES -- the units the resource is actually measured in -- and the span
    # it currently buys is reported rather than promised.
    RING_BYTES = 48 * 1024 * 1024
    # The ratio those two express: about 1.6 MB per second of buffer at the
    # rate this stick produces. Used to size the byte cap when the span is
    # changed, so a longer buffer keeps its granularity instead of just
    # thinning harder over more seconds.
    BYTES_PER_S = 48 * 1024 * 1024 / 30.0
    # Bounds for a requested span. The floor is short enough to be useless and
    # is there only to stop 0; the ceiling is a guard against a typo, not a
    # capacity claim -- what actually limits it is memory, checked at _cap().
    SPAN_MIN_S = 10.0
    SPAN_MAX_S = 600.0
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
        # DECODE FAILURES, COUNTED. Four call sites hand ring bytes to Pillow
        # and all four swallow the exception -- correctly, since one bad frame
        # must not take down a timeline. But swallowed with no counter means
        # the daemon has never been asked whether malformed frames arrive at
        # all, and that question is now load-bearing: a glibc "double free" is
        # heap metadata damage, the classic shape is a decoder overrunning on
        # bad input, and the only validation between the capture pipe and
        # Image.open is "starts FFD8, ends FFD9, at least 128 bytes".
        #
        # If this reads zero after a week, the malformed-frame hypothesis is
        # dead on the evidence rather than on argument. If it reads several an
        # hour, it becomes the first thing to chase.
        self.decode_errs = 0
        self.decode_last = None
        # _chg is read and written by concurrent /timeline.json requests --
        # two open tabs is enough -- and the prune iterates it while another
        # thread may insert. That raises RuntimeError rather than corrupting
        # anything (the GIL is on; checked, not assumed), but a 500 on a
        # timeline is a real fault and this is a cheap fix.
        self._chg_lock = threading.Lock()
        # WHO is holding the ring, not merely THAT it is held. One boolean
        # could not tell two holders apart, and the consequence was reported
        # from a phone: a reviewer scrubbing a capture got a broken image
        # where the frame should be, because a second browser tab took the
        # auto-pin on losing the picture and dropped it 45 s later. The
        # release was correct for the tab that made it and wrong for
        # everybody else, and neither side could have known -- the flag
        # carried no owner. Held while ANY holder holds; each lease expires
        # on its own.
        self.pin_holders = {}
        # Per-frame change scores, by sequence number. Filled lazily when a
        # timeline is asked for, never on the capture path.
        self._chg = {}
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
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=0)
            except Exception as exc:
                self.last_error = errstr(exc)
                return False
            self.owned = True
            self.spawns += 1
            self.spawn_t = time.time()
            self.state = "starting"
            # Cleared per attempt, so the field means "what went wrong with
            # THIS spawn" rather than accumulating the history of a machine.
            self.last_error = None
            threading.Thread(target=_keep_stderr, args=(self, self.proc),
                             daemon=True).start()
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
                self.last_error = errstr(exc, "reader: ")
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
            pinned = bool(self.pin_holders)
            cap = self._cap()
            # Counted BEFORE the pinned early return, deliberately. The field
            # is published as "frames" and rendered as "frames seen": a frame
            # that arrived and was then dropped WAS seen, and the drop is
            # separately counted in dropped_while_pinned. Counting arrivals is
            # the honest semantics for both readers.
            #
            # It used to sit after the return, so the counter stalled exactly
            # while the ring was pinned -- and the KVM auto-pins whenever the
            # picture is lost. The state where a source rate is most
            # interesting was the state that stopped measuring it, and a
            # consumer computing a rate from the difference saw zero and kept
            # showing the last good figure as current. Retained value rendered
            # as live, which is sec. 33 again in a third place.
            self.frames_total += 1
            if pinned and self.ring_bytes + len(frame) > cap:
                # Pinned: the buffer is being examined, so drop the NEW frame
                # rather than free one somebody may be looking at.
                self.dropped_pinned += 1
                self.last_frame_t = now
                return
            self.seq += 1
            self.ring.append((now, self.seq, frame))
            self.ring_bytes += len(frame)
            # A DURATION HAS TO BE ABLE TO END A FRAME'S LIFE ON ITS OWN.
            #
            # Eviction used to run only when BYTES exceeded the cap, so the
            # setting bought a byte budget -- want * BYTES_PER_S -- and the
            # span was whatever that budget happened to buy. BYTES_PER_S is a
            # pessimistic 1.6 MB/s, so on content that compresses better the
            # ring simply kept more: asked for 480 s, measured holding 1193
            # at 25 KB a frame. Nobody chose twenty minutes.
            #
            # The menu offers seconds, so seconds must be a ceiling as well as
            # a floor. Cheap content now costs LESS than its budget instead of
            # silently spending all of it; expensive content is unchanged,
            # because the byte loop below still thins to protect the span.
            while len(self.ring) > 1 and \
                    now - self.ring[0][0] > self.TARGET_SPAN_S * 1.05:
                _t, _s, old = self.ring.popleft()
                self.ring_bytes -= len(old) if old else 0
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
                cut = time.time() - self.PIN_TIMEOUT_S
                expired = sorted(h for h, t in self.pin_holders.items()
                                 if t < cut)
                for h in expired:
                    del self.pin_holders[h]
                still = bool(self.pin_holders)
            if expired:
                # Names them, because "the pin expired" is not actionable when
                # more than one thing can hold it -- and says whether the ring
                # is actually moving again, which is the part a reader cares
                # about.
                self._publish("video.pin", state="expired",
                              holders=expired, pinned=still)

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
                "buffer": self._buffer,
                "framestats": self._framestats, "shot": self._shot,
                "lastgood": self._lastgood, "pin": self._pin,
                "timeline": self._timeline, "frame": self._frame}

    # -- scrub --------------------------------------------------------------

    def _pin(self, req):
        """Stop eviction so a frame cannot be freed while it is examined.

        Without this the scrub feature is subtly broken in exactly the case it
        exists for: the live stream keeps writing while you look at something
        interesting, and the frame under the cursor gets evicted from under it.

        HELD BY NAME. The ring is one object and several things want it still
        at once -- a KVM tab reviewing a capture, a second tab that auto-pinned
        on losing the picture, `vcctrl record` muxing an AVI, a sweep pinning
        at the moment a cell loses lock. With a single flag the last release
        won, whoever it belonged to. Each caller now takes its own lease and
        can only drop its own; the ring moves again when the last one lets go.

        `off` with no holder named clears EVERY lease. That asymmetry is
        deliberate: it is the operator's escape hatch, it is what the recovery
        note in vcctrl-cell already tells someone to type, and a person at a
        terminal asking for the pin off means the ring, not their share of it.
        Programs name themselves and get the narrow behaviour by default.
        """
        action = req.get("action", "status")
        holder = str(req.get("holder") or "")[:64]
        now = time.time()
        with self.lock:
            if action == "on":
                self.pin_holders[holder or "cli"] = now
            elif action == "off":
                if holder:
                    self.pin_holders.pop(holder, None)
                else:
                    self.pin_holders.clear()
                if not self.pin_holders:
                    self.dropped_pinned = 0
            taken = self.pin_holders.values()
            # Two different questions, so two different numbers: how long the
            # ring has been standing still (the OLDEST lease) and how long
            # before it starts moving on its own (the NEWEST one to lapse).
            held = (now - min(taken)) if taken else None
            left = (self.PIN_TIMEOUT_S - (now - max(taken))) if taken else None
            out = {"ok": True, "pinned": bool(self.pin_holders),
                   "holders": sorted(self.pin_holders),
                   "held_s": round(held, 1) if held is not None else None,
                   "dropped_while_pinned": self.dropped_pinned,
                   "expires_in_s": round(left, 1) if left is not None else None}
        if action in ("on", "off"):
            self._publish("video.pin", state=action,
                          holder=holder or None, pinned=out["pinned"],
                          holders=out["holders"])
        return out

    def _buffer(self, req):
        """Read or set how much history the scrub buffer keeps.

        Sized in BYTES and reported in SECONDS, for the reason the constants
        already say: a buffer sized in seconds has no fixed cost and one sized
        in bytes has no fixed duration. Asking for a span therefore sets a
        byte cap that buys it at the current rate, and the span actually
        achieved is reported rather than promised.

        The 48 MB / 30 s default was chosen for a Pi 3 with 920 MB and no
        swap. This machine has 4 GB, so the constraint that shaped it is gone
        -- but MEM_FLOOR_MB still applies, and a request that memory cannot
        honour is clamped and SAID to be clamped rather than silently obeyed.

        Not persisted: a daemon restart returns to the default. That is a
        deliberate choice over writing config from a web request, and the page
        reads the live value back rather than remembering its own.
        """
        want = req.get("seconds")
        with self.lock:
            if want is not None:
                try:
                    want = float(want)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "seconds must be a number"}
                want = max(self.SPAN_MIN_S, min(self.SPAN_MAX_S, want))
                self.TARGET_SPAN_S = want
                self.RING_BYTES = int(want * self.BYTES_PER_S)
            span = self.TARGET_SPAN_S
            asked = self.RING_BYTES
            used = self.ring_bytes
            frames = len(self.ring)
            actual = (self.ring[-1][0] - self.ring[0][0]) if frames > 1 else 0.0
        cap = self._cap()
        if want is not None:
            self._publish("video.buffer", target_span_s=span,
                          cap_bytes=cap, limited=cap < asked)
        return {"ok": True, "target_span_s": span, "cap_bytes": cap,
                "asked_bytes": asked, "ring_bytes": used,
                "ring_frames": frames, "span_s": round(actual, 2),
                "mem_limited": cap < asked}

    def _note_decode_error(self, where, exc, seq=None):
        """Record that a frame would not decode. Never raises, never blocks."""
        with self.lock:
            self.decode_errs += 1
            self.decode_last = {
                "where": where, "seq": seq,
                "error": errstr(exc)[:160],
                "t": round(time.time(), 3),
            }

    def _score_changes(self, items):
        """How much each frame differs from the one before it.

        WHAT THE SCRUB BAR SHOULD ANSWER. It used to colour by how far apart
        frames were in the ring, which is a fact about the RECORDER -- byte
        pressure, thinning -- and not about the machine. Reported exactly that
        way: "the colours don't mean anything to me, I ran DIR for ten seconds
        and it didn't change". Quite right. Where the screen changed is the
        question a timeline exists to answer.

        Decoded at 1/8 scale through draft(), which uses the JPEG's own DCT
        scaling rather than decoding and resizing -- about half a millisecond
        a frame instead of eight. Scoring a full ring costs a third of a
        second, once, and only when somebody opens the timeline: putting this
        on the capture path would spend 2% of a core forever to save it.

        Cached by sequence number. The ring is append-only, so a frame's score
        cannot change; a second open scores only what has arrived since.
        """
        try:
            from PIL import Image, ImageChops, ImageStat
        except Exception:
            return {}                      # no Pillow: no waveform, not a crash
        import io as _io
        # EVERY touch of the shared cache is under the lock, and only the
        # touches -- the decode, which is the expensive part, stays outside
        # it. A first version guarded the prune alone, and a test with a
        # control caught that being wrong within the minute: the prune
        # iterates while ANOTHER thread inserts, so locking the pruners
        # against each other and not against the writers changes nothing.
        cache = self._chg
        with self._chg_lock:
            known = frozenset(cache)
        start = len(items)
        for i, (_t, sq, _f) in enumerate(items):
            if sq not in known:
                # One frame EARLIER than the first unscored one, because a
                # difference needs something to differ from.
                start = max(0, i - 1)
                break
        prev = None
        for i in range(start, len(items)):
            _t, sq, f = items[i]
            try:
                im = Image.open(_io.BytesIO(f))
                im.draft("L", (max(1, im.size[0] // 8), max(1, im.size[1] // 8)))
                im = im.convert("L")
            except Exception as exc:
                self._note_decode_error("timeline", exc, sq)
                prev = None
                with self._chg_lock:
                    cache.setdefault(sq, None)
                continue
            if prev is not None and prev.size == im.size:
                v = round(
                    ImageStat.Stat(ImageChops.difference(prev, im)).mean[0], 2)
                with self._chg_lock:
                    cache[sq] = v
            else:
                with self._chg_lock:
                    cache.setdefault(sq, None)
            prev = im
        live = set(sq for _t, sq, _f in items)
        with self._chg_lock:
            for k in [k for k in cache if k not in live]:
                cache.pop(k, None)
            # A COPY out. The caller then reads a snapshot rather than a dict
            # another request thread is still writing into.
            return dict(cache)

    def _timeline(self, req):
        """Index of what is in the buffer: one entry per frame, no pixels.

        Carries the gap to the previous frame so the UI can draw where the
        buffer has been thinned. A scrub bar that looks uniform while stepping
        1/30 s in one place and 1/7.5 s in another is a lying interface.
        """
        with self.lock:
            items = list(self.ring)
            pinned = bool(self.pin_holders)
            span = (items[-1][0] - items[0][0]) if len(items) > 1 else 0.0
            cap = self._cap()
            used = self.ring_bytes
            thins = self.thin_passes
            memlim = self.mem_limited
        chg = self._score_changes(items) if req.get("change", True) else {}
        out, prev = [], None
        for t, sq, f in items:
            out.append({"seq": sq, "t": round(t, 3), "bytes": len(f),
                        "change": chg.get(sq),
                        "gap_ms": None if prev is None
                        else round((t - prev) * 1000.0, 1)})
            prev = t
        return {"ok": True, "frames": out, "count": len(out),
                "span_s": round(span, 2), "pinned": pinned,
                "ring_bytes": used, "cap_bytes": cap,
                "thin_passes": thins, "mem_limited": memlim,
                "target_span_s": self.TARGET_SPAN_S}

    def buffer_avi(self, first=None, last=None):
        """Snapshot the ring and mux it into a playable AVI.

        Returns (bytes, meta). Called directly rather than through the command
        table because the result is forty megabytes of binary and the command
        path is JSON -- base64 would inflate it by a third for no reason.

        THE SNAPSHOT IS THE WHOLE TRICK. One `list()` under the lock takes a
        reference to every frame, so the ring may roll, thin and evict for the
        rest of this call and nothing under us can be freed. The web page's
        old version walked the ring one HTTP request per frame and lost the
        race the moment the signal came back: eviction ran ahead of the copy
        and each remaining fetch 404'd, so the download quietly produced
        almost nothing. A pin would also have worked; not needing one is
        better, because it cannot be forgotten.
        """
        with self.lock:
            items = list(self.ring)
        if first is not None:
            items = [i for i in items if i[1] >= first]
        if last is not None:
            items = [i for i in items if i[1] <= last]
        if not items:
            return None, {"error": "the buffer is empty"}

        dims = None
        for _t, _s, f in items:
            dims = jpeg_dims(f)
            if dims:
                break
        if not dims:
            return None, {"error": "no frame carried a readable JPEG header"}

        # THE NOMINAL RATE COMES FROM THE DATA. Picking 30 would repeat every
        # frame of a ring thinned to 17 fps and double the file for nothing;
        # picking the average would quantise away the very stalls this file
        # exists to show. The median gap is the rate at which the buffer is
        # actually spaced, so an evenly-thinned buffer produces no repeats at
        # all and a stall produces exactly as many as it lasted.
        gaps = sorted(items[i + 1][0] - items[i][0]
                      for i in range(len(items) - 1))
        med = gaps[len(gaps) // 2] if gaps else 1 / 15.0
        fps = min(30.0, max(1.0, round(1.0 / med) if med > 0 else 15.0))

        out, repeats = [], 0
        for i, (t, _sq, f) in enumerate(items):
            dur = (items[i + 1][0] - t) if i + 1 < len(items) else med
            n = max(1, int(round(dur * fps)))
            n = min(n, int(fps * 5))      # a gap longer than 5s is a gap, not
                                          # 400 copies of one frame
            out.extend([f] * n)
            repeats += n - 1
        blob = avi_mjpeg(out, fps, dims[0], dims[1])
        span = items[-1][0] - items[0][0]
        return blob, {
            "frames": len(items), "written": len(out), "repeated": repeats,
            "fps": round(fps, 2), "width": dims[0], "height": dims[1],
            "span_s": round(span, 2), "bytes": len(blob),
            "first_seq": items[0][1], "last_seq": items[-1][1],
            "first_t": round(items[0][0], 3), "last_t": round(items[-1][0], 3),
        }

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
        except Exception as exc:
            self._note_decode_error("is_picture", exc)
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
            except Exception as exc:
                self._note_decode_error("lastgood", exc)
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
                    # THREE situations currently render identically as "no
                    # signal": the stick is unplugged (someone's hand is on the
                    # cable), the stick is present but will not open (a fault),
                    # and the stick is open on a dark target (the machine is
                    # off). Only the third is "no signal". This separates the
                    # first, and last_error separates the second.
                    #
                    # It matters most during a board swap, when the capture
                    # stick is swapped with the board and the device genuinely
                    # is absent for a minute -- reporting that as a fault would
                    # be the instrument's own state reported as the target's.
                    "device_present": os.path.exists(self.DEVICE),
                    "device": self.DEVICE,
                    "pinned": bool(self.pin_holders),
                    # Frames that would not decode. Zero is a real answer here
                    # and an interesting one: it says the capture pipe has
                    # never handed Pillow anything malformed, which is the
                    # standing hypothesis for an abort nobody has explained.
                    "decode_errs": self.decode_errs,
                    "decode_last": self.decode_last,
                    "span_s": round(self.ring[-1][0] - self.ring[0][0], 2)
                    if len(self.ring) > 1 else 0.0,
                    # What the buffer is ASKED to keep, beside what it is
                    # actually keeping. A page that offers to change this has
                    # to read the live value back rather than remember its own,
                    # because the buffer belongs to the daemon and every viewer
                    # shares one.
                    "target_span_s": self.TARGET_SPAN_S,
                    "mem_limited": self.mem_limited,
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
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    bufsize=0)
            except Exception as exc:
                self.last_error = errstr(exc)
                return False
            self.owned = True
            self.spawns += 1
            self.spawn_t = time.time()
            self.state = "starting"
            # Cleared per attempt, so the field means "what went wrong with
            # THIS spawn" rather than accumulating the history of a machine.
            self.last_error = None
            threading.Thread(target=_keep_stderr, args=(self, self.proc),
                             daemon=True).start()
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
                self.last_error = errstr(exc, "reader: ")
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
                    # Same three-way distinction as video. ALSA has no path to
                    # stat, so presence is "does the card list still name it".
                    # hw:N,D is an index and indices are not stable across
                    # hardware, so an absent card is exactly what a swap looks
                    # like from here.
                    "device_present": self._device_present(),
                    "device": self.DEVICE,
                    "fast_failures": self.fast_failures,
                    "last_error": self.last_error}

    def _device_present(self):
        """Is the configured ALSA device still in the card list?

        Deliberately tolerant: any parse failure returns None rather than
        False, because "I could not tell" and "it is gone" are different
        answers and this rig has paid for collapsing them before.
        """
        dev = self.DEVICE or ""
        try:
            with open("/proc/asound/cards") as f:
                cards = f.read()
        except OSError:
            return None
        if dev.startswith("hw:CARD="):
            name = dev[len("hw:CARD="):].split(",")[0]
            return ("[" + name) in cards.replace(" ", "") or name in cards
        if dev.startswith("hw:"):
            idx = dev[3:].split(",")[0]
            if not idx.isdigit():
                return None
            return any(line.strip().startswith(idx + " ")
                       for line in cards.splitlines())
        return None

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



_UNSET = object()


class BoardCapability(Capability):
    """Which USB4VC protocol board is installed, and therefore which machine.

    The rig drives a Gateway 2000 over the IBM PC board and a Macintosh Plus
    over the Lisa/Mac/ADB board, through ONE USB4VC with the boards swapped by
    hand. Several capabilities mean different things depending on which is in,
    so the daemon has to be able to answer rather than assume.

    NOT read: /home/pi/usb4vc/config/config.json. It is keyed by board id and
    looks authoritative. On the Pi 3 it read {"3": ...} -- the Mac board -- for
    the entire time that machine was driving the Gateway over PS/2, because
    rpi_app only writes a section when settings are CHANGED. It records boards
    once configured, not the board present.
    """

    name = "board"

    FILE = "/run/usb4vc/board.json"

    # Rig-specific: which computer each board implies. Lives here rather than
    # in the page so there is one table instead of two that drift. Override
    # with "board_targets": {"1": "..."} in the daemon config.
    TARGETS = {1: "Gateway 2000", 2: None, 3: "Macintosh Plus"}

    def __init__(self, *a, **kw):
        super(BoardCapability, self).__init__(*a, **kw)
        self._last_id = _UNSET

    def commands(self):
        return {"board": self._board}

    def start(self):
        self.snapshot()          # publishes board.changed on first read

    def _targets(self):
        try:
            over = load_config().get("board_targets") or {}
        except Exception:
            over = {}
        out = dict(self.TARGETS)
        for k, v in over.items():
            try:
                out[int(k)] = v
            except (TypeError, ValueError):
                continue
        return out

    def _from_file(self):
        """The primary source: written by our local patch to rpi_app.

        /run is tmpfs, which is the whole reason this is trusted -- the record
        cannot outlive the boot that wrote it, so a stale value reading as
        current is structurally impossible rather than merely unlikely.
        """
        with open(self.FILE) as f:
            return json.load(f), "status-file"

    def _from_journal(self):
        """Fallback: rpi_app prints the SPI status frame at startup.

        Bounded to THIS BOOT (-b). Without that bound the journal happily
        returns a frame from a previous boot, which is exactly the stale-record
        failure the status file was chosen to avoid.
        """
        out = subprocess.run(
            ["journalctl", "-b", "-u", "usb4vc", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=5).stdout
        last = None
        for line in out.splitlines():
            if "PB INFO:" in line and "[" in line:
                last = line
        if not last:
            raise LookupError("no PB INFO frame in this boot's journal")
        nums = last[last.index("[") + 1:last.index("]")].split(",")
        return {"id": int(nums[3]), "name": None,
                "fw_ver": None, "hw_rev": None, "t": None}, "journal"

    def snapshot(self):
        """The contract. EVERY KEY ALWAYS PRESENT, null where unknown.

        A consumer that branches on which keys exist ends up re-encoding the
        daemon's internal states, and a key that is sometimes absent has broken
        the page more than once. Unknown is a first-class answer here, never a
        default to IBMPC -- "could not look" and "looked, and it is a Mac" must
        not collapse into one value, because the caller acts differently on
        each.
        """
        rec = src_name = None
        reason = None
        for reader in (self._from_file, self._from_journal):
            try:
                rec, src_name = reader()
                break
            except Exception as exc:
                reason = errstr(exc)
        if not rec:
            out = {"id": None, "name": None, "target": None, "source": None,
                   "stale": None,
                   "reason": ("usb4vc has not reported a board (%s)"
                              % reason)[:ERR_MAX]}
        else:
            bid = rec.get("id")
            age = (round(time.time() - rec["t"], 1)
                   if rec.get("t") else None)
            out = {"id": bid,
                   "name": rec.get("name"),
                   "target": self._targets().get(bid),
                   "source": src_name,
                   # The journal path cannot prove the frame belongs to the
                   # currently running rpi_app -- only to this boot -- so it is
                   # marked stale. The file path is written by the running
                   # instance into tmpfs, so it is not.
                   "stale": src_name != "status-file",
                   "reason": None}
            if age is not None:
                out["age_s"] = age
        if out["id"] != self._last_id:
            if self._last_id is not _UNSET and self.bus:
                self.bus.publish("board.changed", **out)
            self._last_id = out["id"]
        return out

    def _board(self, req):
        return {"ok": True, "board": self.snapshot()}


# The registry. A table in the source, in load order. Video, web, audio, reset
# and files join this list; each is one entry and touches nothing above it.
CAPABILITIES = [InputCapability, LedsCapability, PowerCapability,
                VideoCapability, AudioCapability, BoardCapability]

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
                try:
                    cap = cls(devs, self.bus)
                except TypeError:
                    cap = cls(devs)
                # Set unconditionally, so every capability has one whether or
                # not its constructor asked for it.
                cap.bus = self.bus
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
    # THE NEXT ABORT MUST NAME ITSELF.
    #
    # 2026-08-20 23:09:03 this process died with one line -- "double free or
    # corruption (top)" -- and status 6/ABRT. That message is glibc's heap
    # allocator, so it cannot come from Python code; it comes from a C
    # extension, and the only ones on the frame path are Pillow's JPEG decode
    # and hashlib. Which one, and doing what, was unrecoverable: no core (this
    # host has no systemd-coredump), no traceback, nothing in the journal
    # either side of it. A daemon that aborts and says nothing is a daemon
    # whose fault cannot be found, and systemd restarted it three seconds
    # later so from the outside it looked perfectly healthy -- it looked
    # healthy BECAUSE it restarted, which is the state this tool exists to
    # make visible rather than reproduce.
    #
    # faulthandler catches SIGABRT and SIGSEGV among others and writes every
    # thread's Python stack to stderr, which systemd puts in the journal. It
    # costs nothing until the process is already dying. It cannot say which C
    # frame corrupted the heap, but it says which Python line was running in
    # each thread when it happened, and with a capture thread, a watchdog and
    # several web threads all touching Pillow that is most of the answer.
    faulthandler.enable(file=sys.stderr, all_threads=True)
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
