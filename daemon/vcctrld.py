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
import contextlib
import errno
import faulthandler
import glob
import base64
import hashlib
import io
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from evdev import UInput, ecodes as e

# Same directory, deployed alongside this file -- so a plain import works
# once vcctrld itself is on the path. It is not always: tests/test_core.py
# loads this file directly by path via SourceFileLoader, which puts
# neither this file's own directory nor daemon/ on sys.path, so the same
# fallback vcconfig/audio_bands need below is needed here too -- sourced
# from THIS file's directory rather than common/, since that is where
# vcsysinfo.py actually lives.
try:
    from vcsysinfo import parse_dinspect_report
except ImportError:                                        # loaded by path
    import importlib.util as _ilu
    _vs = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "vcsysinfo.py")
    _spec = _ilu.spec_from_file_location("vcsysinfo", _vs)
    vcsysinfo = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(vcsysinfo)
    parse_dinspect_report = vcsysinfo.parse_dinspect_report

# The configuration loader. Deployed beside this file on the daemon host; in a
# source checkout it is one directory up. Imported by path rather than by
# package so neither layout needs a sys.path entry set by whoever launched us.
try:
    import vcconfig
except ImportError:                                        # source checkout
    import importlib.util as _ilu
    _vc = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "common", "vcconfig.py")
    _spec = _ilu.spec_from_file_location("vcconfig", _vc)
    vcconfig = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(vcconfig)

# Same two-layout reasoning as vcconfig immediately above. Shared with the
# control host (bin/, agent/) so AudioCapability's band math and a
# reference-file analysis on the control host are the same implementation,
# not two that have to be kept in agreement by hand.
try:
    import audio_bands
except ImportError:                                        # source checkout
    import importlib.util as _ilu
    _ab = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "common", "audio_bands.py")
    _spec = _ilu.spec_from_file_location("audio_bands", _ab)
    audio_bands = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(audio_bands)

# Same two-layout reasoning once more. Page-level Ogg parsing for the Opus
# audio side-stream (AudioCapability), shared with tests so they exercise the
# implementation the daemon runs rather than a copy.
try:
    import ogg_pages
except ImportError:                                        # source checkout
    import importlib.util as _ilu
    _op = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "common", "ogg_pages.py")
    _spec = _ilu.spec_from_file_location("ogg_pages", _op)
    ogg_pages = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(ogg_pages)

# READ ONCE, AND NEVER FATAL.
#
# A malformed config must not stop the daemon: that is the same rule as Rule 2
# for capabilities, and for the same reason -- a rig that will not start cannot
# be looked at, and looking at it is the whole point of this program. So a
# broken file degrades to built-in defaults and the reason is recorded where
# `caps` and `status` will show it, rather than raising out of main().
#
# The consequence is the reason phase 1 of docs/CONFIG-PLAN.md shipped with a
# control that must fail: THIS FALLBACK MAKES A WORKING LOADER AND A BROKEN ONE
# PRODUCE IDENTICAL OUTPUT on a rig whose values happen to match the defaults.
CFG_ERROR = None
try:
    _PRIMARY_CFG = vcconfig.load()
except vcconfig.ConfigError as _exc:
    CFG_ERROR = str(_exc)
    _PRIMARY_CFG = vcconfig.Config(vcconfig.DEFAULTS, source=None)
    sys.stderr.write("config: %s\n  -- continuing on built-in defaults\n"
                     % CFG_ERROR)


class _ProfileConfigContext(threading.local):
    """Which profile's Config (and load error) the CFG name below resolves
    to, for the CALLING THREAD.

    Phase B (one vcctrld process serving multiple profiles, e.g. the
    primary DOS/Mac target plus modernpc) is what makes this exist. Every
    one of this file's `CFG.xxx` call sites -- from module/class scope
    down through a capability's start()/dispatch methods to handle() -- was
    written assuming exactly one Config exists for the process's whole
    life, before more than one profile was a possibility. Rewriting every
    one of those call sites to take an explicit `cfg` parameter would touch
    most of this file for no behavioral gain; instead, CFG stays the exact
    same name and object every call site already reads, and THIS decides
    which profile's Config it actually resolves to right now. Bind it with
    _profile_scope() once where a profile's Devices/Registry are built, and
    again at the top of that profile's own per-connection thread (a
    threading.local does not inherit across a `threading.Thread(...)`
    boundary) -- every existing call site downstream needs no other change.

    Defaults, on every new thread including the main one at import, to the
    PRIMARY profile -- so a thread that never explicitly bound a profile
    (a stray background thread, a test importing this file directly)
    behaves exactly as every profile-unaware line here always has. This is
    also the hard backward-compatibility floor for a rig with no second
    profile at all: nothing ever calls _profile_scope() for one, so CFG
    always resolves to _PRIMARY_CFG, unconditionally, exactly as before
    this class existed.
    """
    def __init__(self):
        self.cfg = _PRIMARY_CFG
        self.error = CFG_ERROR


_CFG_CTX = _ProfileConfigContext()


class _CFGProxy(object):
    """Stands in for a single global vcconfig.Config. See
    _ProfileConfigContext's docstring for why this indirection exists
    instead of threading a `cfg` parameter through the whole file."""

    def __getattr__(self, name):
        return getattr(_CFG_CTX.cfg, name)


CFG = _CFGProxy()


def _profile_thread(target, name=None, daemon=True):
    """A thread that keeps the profile scope of whoever started it.

    `_ProfileConfigContext` is a threading.local, so a freshly spawned thread
    re-runs its `__init__` and binds `_PRIMARY_CFG` -- NOT the config of the
    profile whose capability spawned it. Every `CFG.xxx` read on that thread
    then answers for the primary profile, silently and correctly-looking.

    MEASURED, not reasoned (2026-09-02, docs/FINDINGS.md sec. 48): a second
    profile's `PowerCapability` heartbeat resolved `power_host()` to the
    PRIMARY's plug address. With a primary that has no plug the second
    profile's plug was never polled at all; with one that does, the second
    profile's real relay reading got stamped with the primary's address and
    `snapshot()` published it -- a true reading under another machine's name,
    on the capability whose whole job is knowing which machine it is talking
    about.

    So: capture the caller's binding HERE, on the caller's thread, and rebind
    it inside the new one. Callers that never had a second profile are
    unaffected -- they capture the primary and rebind the primary.
    """
    cfg, error = _CFG_CTX.cfg, _CFG_CTX.error

    def run():
        with _profile_scope(cfg, error):
            target()

    return threading.Thread(target=run, name=name, daemon=daemon)


@contextlib.contextmanager
def _profile_scope(cfg, error=None):
    """Bind `cfg` (and its load error, if any) to CFG for the life of this
    `with` block on the calling thread, restoring whatever was bound
    before on exit -- so profile construction and connection handling can
    nest, or reuse a thread (e.g. an idle-timer callback), without one
    profile's config leaking into another's for the rest of that thread's
    life."""
    prev_cfg, prev_err = _CFG_CTX.cfg, _CFG_CTX.error
    _CFG_CTX.cfg, _CFG_CTX.error = cfg, error
    try:
        yield
    finally:
        _CFG_CTX.cfg, _CFG_CTX.error = prev_cfg, prev_err


SOCKET_PATH = CFG.default("daemon.socket", "/run/vcctrl.sock")
USB4VC_LOG = CFG.default("daemon.usb4vc.debug_log",
                         "/home/pi/usb4vc/usb4vc_debug_log.txt")
STATE_DIR = CFG.default("daemon.state_dir", "/var/lib/vcctrl")

# THE VENDORED FTP SERVER, PUT ON THE PATH HERE RATHER THAN IN PYTHONPATH.
#
# The daemon is started by systemd, by hand, and from tests, and only one of
# those three reliably carries an environment somebody set up. A dependency
# that resolves under one launcher and not the others is a feature that works
# for whoever wrote it -- which is the same class as --out meaning a path on
# the wrong machine.
#
# APPENDED, NOT PREPENDED, and that is deliberate. Anything genuinely
# installed on the host wins; this is the fallback that makes a clone
# self-sufficient, not an override that quietly shadows a newer package
# somebody installed on purpose.
#
# Absent or broken is survivable and is NOT handled here: nothing imports
# pyftpdlib at module scope, so a missing vendor/ costs the file-transfer
# capability and nothing else. It reports the absence in its own words.
# TWO LAYOUTS, BECAUSE THERE ARE TWO. In the checkout this file is
# <repo>/daemon/vcctrld.py, so vendor/ is one level up. Deployed it is
# /opt/vcctrl/vcctrld.py -- FLAT, no daemon/ directory -- so vendor/ sits
# beside it and "one level up" is /opt.
#
# The first version only knew the checkout, which is why it worked in every
# test and failed on the rig with "No module named 'pyftpdlib'". The tests
# asserted the repo layout and were right about it; nothing asserted the
# deployed one, which is the layout that matters.
_here = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(_here, "vendor"),
              os.path.join(os.path.dirname(_here), "vendor")):
    if os.path.isdir(_cand):
        if _cand not in sys.path:
            # APPENDED: anything genuinely installed on the host wins, and
            # this is the fallback rather than an override.
            sys.path.append(_cand)
        _VENDOR = _cand
        break
else:
    _VENDOR = None
CONFIG_PATH = os.path.join(CFG.default("daemon.prefix", "/opt/vcctrl"),
                           "config.json")

# Seconds the rails stay down during a power cycle. The g2k is an AT-style
# PicoRC setup with no soft-off, so it boots as soon as power returns; the delay
# is only to let the supply drain rather than to satisfy any handshake.
#
# Raised from 6 s after measuring POST at 26 s from a cold start but 44 s after
# a 6 s cycle -- most likely the 12 V brick and picoPSU had not fully
# discharged. This is the recovery path of last resort, so it should be the
# most reliable thing in the system rather than the fastest.
POWER_CYCLE_OFF_S = float(CFG.default(
    "capabilities.power.settings.cycle_off_s", 15.0))

VENDOR = int(CFG.default("capabilities.input.settings.vendor", 0x1209))
KBD_PRODUCT = int(CFG.default(
    "capabilities.input.settings.keyboard_product", 0xDEA1))
MOUSE_PRODUCT = int(CFG.default(
    "capabilities.input.settings.mouse_product", 0xDEA2))

# Minimum gap between input events. USB4VC drains one event per device per
# loop pass and sleeps 5 ms when idle; PS/2 wire time adds ~1 ms per byte.
DEFAULT_PACE_S = float(CFG.default(
    "capabilities.input.settings.pace_s", 0.012))

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

# ------------------------------------------------------- HID gadget usage IDs
#
# The `hid-gadget` input backend (Devices, below) does not get its own copy of
# NAMED_KEYS/CHARMAP -- it translates the SAME evdev codes those tables
# already resolve names and characters into, via this one table. That is
# deliberate: it is what guarantees the two backends accept exactly the same
# key names and characters, rather than two hand-maintained tables that are
# free to quietly disagree about what "f9" means.
#
# Modifiers are NOT here. A USB HID boot-protocol keyboard report has no
# keycode for Ctrl/Alt/Shift/Meta at all -- they are eight bits in the
# report's own first byte (HID_MODIFIER_BITS, below), not entries in its
# 6-key array. A modifier evdev code is looked up there first and never
# reaches this table.
#
# Values are USB HID Usage Tables 1.12, page 0x07 (Keyboard/Keypad), the same
# numbers every USB keyboard on earth reports.
EVDEV_TO_HID_USAGE = {
    e.KEY_A: 0x04, e.KEY_B: 0x05, e.KEY_C: 0x06, e.KEY_D: 0x07,
    e.KEY_E: 0x08, e.KEY_F: 0x09, e.KEY_G: 0x0A, e.KEY_H: 0x0B,
    e.KEY_I: 0x0C, e.KEY_J: 0x0D, e.KEY_K: 0x0E, e.KEY_L: 0x0F,
    e.KEY_M: 0x10, e.KEY_N: 0x11, e.KEY_O: 0x12, e.KEY_P: 0x13,
    e.KEY_Q: 0x14, e.KEY_R: 0x15, e.KEY_S: 0x16, e.KEY_T: 0x17,
    e.KEY_U: 0x18, e.KEY_V: 0x19, e.KEY_W: 0x1A, e.KEY_X: 0x1B,
    e.KEY_Y: 0x1C, e.KEY_Z: 0x1D,
    e.KEY_1: 0x1E, e.KEY_2: 0x1F, e.KEY_3: 0x20, e.KEY_4: 0x21,
    e.KEY_5: 0x22, e.KEY_6: 0x23, e.KEY_7: 0x24, e.KEY_8: 0x25,
    e.KEY_9: 0x26, e.KEY_0: 0x27,
    e.KEY_ENTER: 0x28, e.KEY_ESC: 0x29, e.KEY_BACKSPACE: 0x2A,
    e.KEY_TAB: 0x2B, e.KEY_SPACE: 0x2C,
    e.KEY_MINUS: 0x2D, e.KEY_EQUAL: 0x2E, e.KEY_LEFTBRACE: 0x2F,
    e.KEY_RIGHTBRACE: 0x30, e.KEY_BACKSLASH: 0x31,
    e.KEY_SEMICOLON: 0x33, e.KEY_APOSTROPHE: 0x34, e.KEY_GRAVE: 0x35,
    e.KEY_COMMA: 0x36, e.KEY_DOT: 0x37, e.KEY_SLASH: 0x38,
    e.KEY_CAPSLOCK: 0x39,
    e.KEY_F1: 0x3A, e.KEY_F2: 0x3B, e.KEY_F3: 0x3C, e.KEY_F4: 0x3D,
    e.KEY_F5: 0x3E, e.KEY_F6: 0x3F, e.KEY_F7: 0x40, e.KEY_F8: 0x41,
    e.KEY_F9: 0x42, e.KEY_F10: 0x43, e.KEY_F11: 0x44, e.KEY_F12: 0x45,
    e.KEY_SYSRQ: 0x46, e.KEY_SCROLLLOCK: 0x47, e.KEY_PAUSE: 0x48,
    e.KEY_INSERT: 0x49, e.KEY_HOME: 0x4A, e.KEY_PAGEUP: 0x4B,
    e.KEY_DELETE: 0x4C, e.KEY_END: 0x4D, e.KEY_PAGEDOWN: 0x4E,
    e.KEY_RIGHT: 0x4F, e.KEY_LEFT: 0x50, e.KEY_DOWN: 0x51, e.KEY_UP: 0x52,
    e.KEY_NUMLOCK: 0x53, e.KEY_KPSLASH: 0x54, e.KEY_KPASTERISK: 0x55,
    e.KEY_KPMINUS: 0x56, e.KEY_KPPLUS: 0x57, e.KEY_KPENTER: 0x58,
    e.KEY_KP1: 0x59, e.KEY_KP2: 0x5A, e.KEY_KP3: 0x5B, e.KEY_KP4: 0x5C,
    e.KEY_KP5: 0x5D, e.KEY_KP6: 0x5E, e.KEY_KP7: 0x5F, e.KEY_KP8: 0x60,
    e.KEY_KP9: 0x61, e.KEY_KP0: 0x62, e.KEY_KPDOT: 0x63,
    e.KEY_102ND: 0x64, e.KEY_COMPOSE: 0x65,
}

# Bit position in a HID keyboard report's modifier byte (report[0]), per USB
# HID 1.11 sec 8.3. Checked BEFORE EVDEV_TO_HID_USAGE by every hid-gadget
# press/release path -- see the comment above that table for why a modifier
# must never reach it.
HID_MODIFIER_BITS = {
    e.KEY_LEFTCTRL: 0x01, e.KEY_LEFTSHIFT: 0x02, e.KEY_LEFTALT: 0x04,
    e.KEY_LEFTMETA: 0x08, e.KEY_RIGHTCTRL: 0x10, e.KEY_RIGHTSHIFT: 0x20,
    e.KEY_RIGHTALT: 0x40, e.KEY_RIGHTMETA: 0x80,
}

# Bit position in a HID mouse report's button byte (report[0]).
HID_MOUSE_BUTTON_BITS = {
    e.BTN_LEFT: 0x01, e.BTN_RIGHT: 0x02, e.BTN_MIDDLE: 0x04,
}

# THE ALIASES ARE THE POINT. NAMED_KEYS gives the same physical key several
# names -- `ctrl`, `lctrl` and `rctrl` are three names for two keys that both
# mean Ctrl to a chord -- so a check written against one spelling is a check
# that a different spelling walks straight past.
#
# That was live: the reboot detector below tested `{"ctrl", "alt"} <= keys`,
# and the web KVM's modifier buttons send `lctrl` and `lalt`. A Ctrl-Alt-Del
# assembled from those buttons rebooted the machine and left PROFILE holding a
# reading from the boot before it -- a real value, about a machine that is no
# longer running, which is the failure PROFILE.invalidate() exists to prevent.
# It was unreachable only because the page had no Delete key to finish the
# chord with. It has one now.
_CHORD_ALIASES = {
    "lctrl": "ctrl", "rctrl": "ctrl",
    "lalt": "alt", "ralt": "alt",
    "lshift": "shift", "rshift": "shift",
    "leftmeta": "meta", "rightmeta": "meta",
    "del": "delete", "escape": "esc", "return": "enter", "bs": "backspace",
    "caps": "capslock", "period": "dot", "break": "pause",
    "printscreen": "sysrq", "prtsc": "sysrq", "compose": "menu",
}


def chord_set(keys):
    """A combo's keys as a set of canonical names, aliases collapsed.

    For asking questions ABOUT a chord -- is this the reboot? -- never for
    sending one. Sending goes through NAMED_KEYS unchanged: `lctrl` and
    `rctrl` are genuinely different keycodes on the wire and collapsing them
    there would send the wrong one.
    """
    out = set()
    for k in (keys or []):
        n = str(k).lower()
        out.add(_CHORD_ALIASES.get(n, n))
    return out


REBOOT_CHORD = ("ctrl", "alt", "delete")


def is_reboot_combo(keys):
    """Does this combo warm-boot a PC?

    A SUPERSET test, not equality: Ctrl-Alt-Shift-Del is still Ctrl-Alt-Del
    with a spare finger on it, and the BIOS does not care about the extra.

    NOTE this is a fact about the IBM PC boards. A Macintosh reboots by a
    route that does not exist on this keyboard at all, so a false here means
    "not the PC reboot chord", not "harmless".
    """
    return set(REBOOT_CHORD) <= chord_set(keys)


# The order a chord's keys must be PRESSED in. Anything not a modifier keeps
# its position after these, in the order the caller gave.
MOD_ORDER = ("ctrl", "alt", "shift", "meta")


def order_chord(keys):
    """A combo's keys, modifiers first, ready to press.

    THIS IS PROTOCOL, NOT TIDINESS. Devices.combo() presses in the order it is
    given and releases in reverse, so `combo delete ctrl alt` presses Delete
    BEFORE either modifier arrives -- the target sees a keystroke, and then two
    modifiers going down after it. The chord silently becomes something else,
    and at a DOS prompt the something else is a character in the buffer.

    It lives here rather than in the caller because there are two callers --
    the CLI over the unix socket and the web KVM over /cmd -- and the browser
    having a guarantee the command line does not is exactly the asymmetry that
    makes one of them wrong. Every caller of the `combo` COMMAND gets it.

    Deliberately NOT in Devices.combo(). That is the primitive: it presses what
    it is handed, in the order it is handed, and something that genuinely wants
    a raw press sequence must still be able to say so. The reordering belongs
    to the meaning of "chord", which is what the command means and the
    primitive does not.

    Stable: keys that rank equal keep the caller's order, so a chord with two
    non-modifiers in it still types them the way it was written.
    """
    ranked = []
    for i, k in enumerate(keys or []):
        c = _CHORD_ALIASES.get(str(k).lower(), str(k).lower())
        r = MOD_ORDER.index(c) if c in MOD_ORDER else len(MOD_ORDER)
        ranked.append((r, i, k))
    return [k for _, _, k in sorted(ranked, key=lambda t: (t[0], t[1]))]


def canonical_key_names():
    """{every accepted name: the one canonical name for its keycode}.

    NAMED_KEYS gives 129 names to 105 keycodes, so any table keyed by name has
    more rows than facts and is free to disagree with itself -- `printscreen`
    and `sysrq` are ONE key and could carry opposite verdicts. Coverage is
    therefore keyed by ONE name per keycode, and this is how a caller resolves
    whatever spelling it happens to hold into that name.

    Published rather than reimplemented, for the reason the alias table is:
    the web KVM draws keys by the names in its layout -- printed keycaps like
    `-` and `[` -- and the coverage table is keyed by evdev-ish ones like
    `minus` and `leftbrace`. Two independent guesses at "the same key" is how
    a lookup silently misses and a measured key reads as unmeasured.

    The pick is deterministic and boring: the longest name made only of
    letters, ties broken alphabetically; and if a keycode has no such name --
    the punctuation whose only spelling IS the keycap -- the keycap itself.
    Which name wins does not matter. That both sides agree does.
    """
    by_code = {}
    for name, code in NAMED_KEYS.items():
        by_code.setdefault(code, []).append(name)
    out = {}
    for code, names in by_code.items():
        wordy = [n for n in names if n.isalnum()]
        pick = (max(sorted(wordy), key=len) if wordy
                else max(sorted(names), key=len))
        for n in names:
            out[n] = pick
    return out


COVERAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "keycoverage.json")


def key_coverage(board_id):
    """Measured coverage for one protocol board, or None if there is none.

    None means "nothing has been measured for this board", which is a real
    answer and NOT the same as "measured, and nothing works". A board with no
    entry gets no coverage published and the page greys nothing -- greying on
    absence would assert a negative from a measurement that was never taken.

    KEYED BY BOARD because coverage is a fact about a board's firmware and a
    machine's BIOS, not about the daemon. The IBM PC board's table says
    nothing whatever about what an ADB board delivers.

    Returns (row, reason). `reason` is None when a row was found, and
    otherwise says WHY -- because "not deployed with the file" and "this board
    has never been swept" are different facts and the caller acts differently
    on each.

    Rows are keyed by ONE CANONICAL NAME PER KEYCODE and aliases resolve into
    them -- `ctrl` and `lctrl` are one physical key, and a row per name would
    be 129 rows for 105 facts, free to disagree with itself.
    """
    if board_id is None:
        return None, "the protocol board is not identified"
    try:
        with open(COVERAGE_FILE) as f:
            doc = json.load(f)
    except FileNotFoundError:
        # A DEPLOY FAULT, NOT A MEASUREMENT FACT, and they looked identical
        # for one deploy. install.sh names each daemon/ file explicitly, this
        # one was added and the install line was not, and the daemon answered
        # `coverage: null` -- which is precisely what a board nobody has swept
        # looks like. Two different facts sharing one value, in the object
        # built to stop exactly that.
        return None, ("no coverage file on this host (%s) -- the daemon was "
                      "deployed without it" % COVERAGE_FILE)
    except Exception as exc:
        return None, "coverage file unreadable: %s" % errstr(exc)
    row = (doc.get("boards") or {}).get(str(board_id))
    if row is None:
        return None, "no keys have been measured for board %s" % board_id
    return row, None


def keymap():
    """The key tables a client needs to reason about chords, as data.

    THE POINT IS THAT THERE IS ONE COPY. The web KVM has to decide whether a
    chord is the reboot BEFORE it sends it -- that is what the confirmation is
    -- so it needs the alias table and the reboot definition. It used to carry
    its own transcription of both, kept honest by a test that read the two
    files and compared them. A test that two tables agree is a good answer to
    a question that should not have been asked.

    So the daemon publishes and the page consumes: `/keymap.json` over HTTP,
    `vcctrl keymap` on the command line, one `keymap` command underneath both.

    `keys` is every name `key`, `combo`, `keydown` and `keyup` will accept.
    Note what it does NOT say: whether the protocol board turns any of them
    into a scancode the target sees. That is not knowable from this side --
    the Pi forwards raw evdev codes and the mapping lives in the STM32's
    firmware -- and a list published by the daemon must not be mistaken for a
    coverage table. See docs/WEBKVM.md sec. 5.2.
    """
    bid = installed_board_id()
    cov, cov_why = key_coverage(bid)
    return {
        "aliases": dict(_CHORD_ALIASES),
        "mod_order": list(MOD_ORDER),
        "reboot": list(REBOOT_CHORD),
        "keys": sorted(NAMED_KEYS),
        # WHICH BOARD THE COVERAGE IS ABOUT, always present. A consumer that
        # took the table without checking would apply an IBM PC measurement to
        # an ADB board, which is the "real value, wrong question" shape this
        # whole table exists to avoid.
        "board_id": bid,
        # HOW TO RESOLVE A NAME INTO A COVERAGE ROW. Without this the page
        # looks up `-` in a table keyed `minus`, misses, and reports a
        # measured key as having no verdict -- which is the quiet direction:
        # it understates what is known rather than overstating it, so nothing
        # looks wrong.
        "canonical": canonical_key_names(),
        "coverage": (cov or {}).get("keys") or None,
        # Chords are their own table: they are not keys and cannot be keyed by
        # keycode. Keyed by the canonical names joined with "+", in the order
        # `combo` sends them -- which is why every chord row here begins
        # `lctrl`: measured on 2026-08-25, `combo` sends the LEFT ctrl.
        "chords": (cov or {}).get("chords") or None,
        "coverage_meta": {k: v for k, v in (cov or {}).items() if k != "keys"}
                         or None,
        # WHY there is no coverage, in the daemon's own words. Null when there
        # is some. A consumer showing "nothing measured" for a missing file
        # would be reporting the deploy's state as the target's.
        "coverage_reason": cov_why,
        # `measured` was False and unconditional while nothing had been swept.
        # It is now a fact about THIS BOARD: true when a table exists for it,
        # false when none does. It never meant "every key works" and still
        # does not -- the per-key rows carry that, and some of them have no
        # verdict at all.
        "measured": cov is not None,
    }


# ---------------------------------------------------------------- power

_LEGACY_WARNED = [False]


def load_config():
    """The pre-YAML config.json, kept readable for one release.

    Superseded by vcctrl.yaml and common/vcconfig.py. It is still read so an
    existing rig keeps working across the upgrade without a flag day, but it
    now LOSES to the YAML file rather than winning, and it says once that it
    is deprecated. Removing it silently would take out power control on any
    rig that had not migrated, at the moment of deploying -- which is exactly
    when nobody is looking at stderr.
    """
    try:
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    except Exception:
        return {}
    if data and not _LEGACY_WARNED[0]:
        _LEGACY_WARNED[0] = True
        sys.stderr.write(
            "config: %s is DEPRECATED and will be removed. Move its values "
            "into vcctrl.yaml (see vcctrl.example.yaml); the YAML file wins "
            "where both are set.\n" % CONFIG_PATH)
    return data


def power_host():
    """Where the smart plug is, YAML first and legacy JSON second.

    BACKEND-INDEPENDENT, and named for that after `wemo` became the third
    backend: this reads `capabilities.power.settings.host`, which is whatever
    the configured backend dials -- a Kasa plug on 9999, a Wemo on 4915x, or
    nothing at all. It was called `kasa_host` while Kasa was the only thing
    with a host, and a name that states one backend while serving three is
    exactly the comment-that-stopped-being-true this repo keeps getting bitten
    by. The LEGACY JSON KEY below is still spelled `kasa_host`, because that
    is a fact about a file already on disk and renaming it here would not
    rename it there.

    Returns None when neither names one, and None must stay a real answer:
    a rig with no plug configured has NO POWER CONTROL, and reporting that
    honestly is the difference between "go and configure it" and a power
    command that quietly does nothing.
    """
    host = CFG.optional("capabilities.power.settings.host")
    if host is not vcconfig.ABSENT and host is not vcconfig.NONE:
        return host
    return load_config().get("kasa_host") or None


def kasa_port():
    return int(CFG.default("capabilities.power.settings.port", 9999))


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


def kasa_send(host, payload, timeout=5.0, port=None):
    """Legacy TP-Link smart-home protocol on port 9999.

    4-byte big-endian length prefix plus an XOR-autokey cipher seeded at 171.
    No dependency and no cloud account -- the EP10 speaks this directly on the
    LAN. Newer Kasa firmware may move to KLAP on port 80, in which case this
    stops working and needs the python-kasa library instead.
    """
    sock = socket.create_connection((host, port or kasa_port()), timeout)
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
            "rssi": info.get("rssi"),
            # ALWAYS None HERE, and the gap is named rather than left as an
            # absence. Energy metering is a per-model hardware fact that
            # `get_sysinfo` reports in `feature`: this rig's EP10 says `TIM`
            # (timer only) and answers both emeter namespaces with
            # `err_code -1, "module not support"` (measured 2026-09-02), so
            # there is nothing to read. A METERED MODEL -- HS110, KP115,
            # KP125M, which say `TIM:ENE` -- would answer
            # `{"emeter": {"get_realtime": {}}}` and could fill this, and
            # nobody has written that because no such plug has been on this
            # rig. That is a missing feature with a known shape, not a
            # property of the protocol. See WemoPower.state() for what the
            # field is for.
            "power_mw": None}


def power_set(host, on):
    resp = kasa_send(host, {"system": {"set_relay_state": {"state": 1 if on else 0}}})
    err = resp.get("system", {}).get("set_relay_state", {}).get("err_code")
    if err not in (0, None):
        raise IOError("kasa set_relay_state err_code=%s" % err)
    return err


# ---------------------------------------------------------------- wemo transport
#
# Belkin's Wemo plugs speak SOAP over plain HTTP on the LAN. No cloud account
# and no dependency, same as the Kasa path above and for the same reason: a
# power backend that needs an internet round trip is one that stops working on
# exactly the day the rig's uplink is the thing being debugged.
#
# FOUR THINGS ABOUT THIS PROTOCOL THAT READ AS BUGS AND ARE NOT. All four were
# measured 2026-09-01 against a Wemo Insight running firmware
# WeMo_WW_2.00.11532.PVT-OWRT-Insight, on a flat LAN with the daemon host; see
# docs/FINDINGS.md sec. 47 for the readings and the conditions.
#
# 1. THE PORT MOVES. The device picks its HTTP port at boot; 49153 is only the
#    usual answer and 49152/49154/49155 are the others it takes. A literal here
#    would be the same defect this file already records against `kasa_send`, so
#    `port:` is obeyed where the operator pins one and the candidates are
#    walked where they do not -- see `wemo_call`.
#
# 2. `BinaryState` 8 MEANS ON. On an Insight it is "relay closed, load below
#    the standby threshold" -- a real machine, powered, idling. Read as False
#    it reports a running target as off, which is the exact collapse the
#    tri-state `on` exists to prevent. Some firmwares also answer with the
#    state pipe-joined onto the Insight counters (`1|1788297732|0|...`), so the
#    first field is the state and the rest of the string is not.
#
# 3. THE REPLY'S XML NAMESPACE IS WRONG, so it cannot be matched on. A call to
#    `urn:Belkin:service:insight:1#GetInsightParams` came back in an envelope
#    declaring `xmlns:u="urn:Belkin:service:metainfo:1"` -- the wrong service
#    entirely. A namespace-aware parse of that reply finds nothing and reports
#    a healthy plug as unreachable. Local tag name, therefore, and nothing
#    else: `_wemo_tag`.
#
# 4. ITS HTTP SERVER DROPS OFF WHILE SSDP KEEPS ADVERTISING IT, FOR AS LONG AS
#    17.5 MINUTES, AND THEN COMES BACK BY ITSELF. Measured: ICMP answered 45/45
#    samples over three minutes, SSDP kept announcing
#    `LOCATION: http://<device>:49153/setup.xml`, and every connection to that
#    port came back ECONNREFUSED -- an active RST, so the device's TCP stack
#    was alive and NOTHING WAS BOUND. A full 1-65535 sweep during the outage
#    found no other listening port, so it had not moved either. It then cleared
#    with nothing touching it. Three consequences, all load-bearing:
#    `wemo_call` walks the candidate ports ONE AT A TIME (the first such outage
#    followed a 200-thread scan -- suspicious enough to design around, not
#    enough to call a cause, since a later one followed a handful of ordinary
#    requests); "discovery says it is there and the socket says it is not" is a
#    real state of this device rather than a wrong address to go looking past;
#    and a power backend on one of these is UNAVAILABLE FOR MINUTES AT A TIME
#    by its own nature, so `PowerCapability`'s tri-state `on` and its `reason`
#    are doing real work here -- an outage must read as "could not ask", never
#    as "the machine is off".

WEMO_PORTS = (49153, 49152, 49154, 49155)

# host -> the port that last answered. Module-level, not per-instance, because
# `PowerCapability._protocol()` deliberately builds a FRESH backend object on
# every call so a settings change takes effect without a daemon restart. A
# cache on the instance would be thrown away before it was ever read, and the
# port walk would then run on every single poll of a device that note 4 says
# must not be hammered.
_WEMO_PORT = {}

WEMO_ENVELOPE = ('<?xml version="1.0" encoding="utf-8"?>'
                 '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
                 ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                 '<s:Body>%s</s:Body></s:Envelope>')


def wemo_call(host, call, port=None, timeout=5.0):
    """Run `call(port)` against whichever port this Wemo is answering on.

    A CONFIGURED PORT IS NEVER SECOND-GUESSED. Where the operator pinned one, a
    failure there is a fault to report -- knocking on three more ports would
    turn "the plug you named is not answering" into "no Wemo anywhere", which
    is a different and much less useful sentence to hand someone.

    AN HTTP ERROR ENDS THE WALK, because it is a real answer: something at that
    port spoke back, so that is the device and its complaint is the finding.
    Only a connection that never landed moves on to the next candidate. Without
    this split, a plug returning 500 would be reported as absent, and the port
    it is plainly sitting on would be listed as one of four that "did not
    answer".
    """
    if port:
        return call(int(port))
    tried, seen = [], _WEMO_PORT.get(host)
    order = ([seen] if seen else []) + [p for p in WEMO_PORTS if p != seen]
    for cand in order:
        try:
            out = call(cand)
        except urllib.error.HTTPError:
            _WEMO_PORT[host] = cand
            raise
        except (urllib.error.URLError, OSError) as exc:
            tried.append("%d %s" % (cand, type(exc).__name__))
            continue
        _WEMO_PORT[host] = cand
        return out
    raise IOError("no Wemo answered at %s on ports %s (%s)"
                  % (host, "/".join(str(x) for x in order), "; ".join(tried)))


def wemo_soap(host, service, path, action, body="", port=None, timeout=5.0):
    """One SOAP call to a Wemo. Returns the reply document as text."""
    env = (WEMO_ENVELOPE % ('<u:%s xmlns:u="urn:Belkin:service:%s">%s</u:%s>'
                            % (action, service, body, action))).encode()

    def call(cand):
        req = urllib.request.Request(
            "http://%s:%d%s" % (host, cand, path), data=env,
            headers={"Content-Type": 'text/xml; charset="utf-8"',
                     "SOAPACTION": '"urn:Belkin:service:%s#%s"'
                                   % (service, action)})
        with contextlib.closing(
                urllib.request.urlopen(req, timeout=timeout)) as resp:
            return resp.read().decode("utf-8", "replace")

    return wemo_call(host, call, port=port, timeout=timeout)


def wemo_setup_xml(host, port=None, timeout=5.0):
    """The device description document: friendly name, model, firmware."""
    def call(cand):
        with contextlib.closing(urllib.request.urlopen(
                "http://%s:%d/setup.xml" % (host, cand), timeout=timeout)) as r:
            return r.read().decode("utf-8", "replace")

    return wemo_call(host, call, port=port, timeout=timeout)


def _wemo_tag(xml, tag):
    """The text of the first `<tag>`, whatever namespace prefix it wears.

    Note 3 above: the prefix on a Wemo reply is not reliable and has been
    measured naming the wrong service outright, so this matches the LOCAL NAME
    and ignores any `foo:` in front of it.
    """
    pat = r"<(?:[\w.-]+:)?%s\b[^>]*>(.*?)</(?:[\w.-]+:)?%s\s*>" % (tag, tag)
    m = re.search(pat, xml or "", re.S)
    return m.group(1).strip() if m else None


def wemo_on(raw):
    """True / False / None from a Wemo `BinaryState` value.

    None is "answered without saying" and it has to survive: see note 2 above,
    and `power_state`'s own comment on why bool(None) is the wrong answer to
    give about a machine's mains.
    """
    if raw is None:
        return None
    head = raw.split("|")[0].strip()
    if head in ("1", "8"):
        return True
    if head == "0":
        return False
    return None


# ---------------------------------------------------------------- klap transport
#
# TP-Link's second-generation LAN protocol, on port 80. Newer Kasa firmware and
# the whole Tapo line speak it, and it shares NOTHING with the port-9999
# protocol above: different port, different framing, real cryptography, and it
# requires the credentials of a TP-Link cloud account -- checked locally, but
# the account has to exist. A plug speaks one or the other, and they cannot
# negotiate; that is why `kasa` and `kasa-klap` are separate backends rather
# than one that tries both.
#
# HOW THE SESSION IS ESTABLISHED, since none of it is guessable from the wire:
#
#   auth_hash = sha256(sha1(username) + sha1(password))
#   handshake1  POST /app/handshake1, body = 16 random bytes (local_seed).
#               Reply = remote_seed (16 bytes) + server_hash (32), and a
#               TP_SESSIONID cookie that every later request must carry.
#               server_hash must equal sha256(local_seed + remote_seed +
#               auth_hash) -- that is the device proving it holds the same
#               credential, and a mismatch means WRONG CREDENTIALS, not a
#               network fault. They are worth telling apart: one is fixed in a
#               config file and the other by looking at a cable.
#   handshake2  POST /app/handshake2, body = sha256(remote_seed + local_seed +
#               auth_hash). That is us proving the same thing back.
#
# Then every request is AES-128-CBC with keys derived from the same three
# values, and a sequence number that increments per request and appears both
# in the IV and in the query string. A signature prefix covers the sequence and
# the ciphertext.
#
# THE DEPENDENCY IS DELIBERATE AND ISOLATED. `cryptography` is imported inside
# the class, not at module scope: this daemon runs rigs that will never own a
# KLAP plug, and an ImportError at the top would take down input, video, audio
# and LEDs on a machine whose only sin was not installing a crypto library for
# a power backend it does not use. Selecting `kasa-klap` without it gives a
# message naming the package; selecting anything else never touches it.
#
# NOT VERIFIED AGAINST HARDWARE. No KLAP device has ever been on this rig -- the
# plug here is an EP10 speaking the port-9999 protocol, and there is nothing to
# point this at. The tests exercise it against a fake device written from the
# same understanding of the protocol as the code, which proves the two halves
# agree and CANNOT catch a misreading of the real thing: a port inherits the
# premise of its source. Treat this as untested code that is expected to work,
# and the first person with a real KLAP plug should expect to debug it.


KLAP_HANDSHAKE1 = "/app/handshake1"
KLAP_HANDSHAKE2 = "/app/handshake2"
KLAP_REQUEST = "/app/request"


def klap_auth_hash(username, password):
    """sha256(sha1(user) + sha1(pass)) -- the credential, never sent as such."""
    return hashlib.sha256(
        hashlib.sha1((username or "").encode()).digest()
        + hashlib.sha1((password or "").encode()).digest()).digest()


def _klap_derive(prefix, local_seed, remote_seed, auth_hash):
    return hashlib.sha256(prefix + local_seed + remote_seed + auth_hash).digest()


class KlapSession(object):
    """One handshaken KLAP session: the keys, and the sequence number.

    The sequence number is STATE, and that is the whole reason this is an
    object rather than a function. It increments per request and the device
    tracks it; replaying or skipping one invalidates the session, which is why
    a session cannot be rebuilt per call the way the Kasa and Wemo backends
    rebuild their protocol objects.
    """

    def __init__(self, local_seed, remote_seed, auth_hash, cookie):
        self.cookie = cookie
        self.key = _klap_derive(b"lsk", local_seed, remote_seed, auth_hash)[:16]
        ivseq = _klap_derive(b"iv", local_seed, remote_seed, auth_hash)
        self.iv = ivseq[:12]
        # Signed, big-endian, from the LAST FOUR BYTES -- and signed matters:
        # the derivation is a hash, so roughly half of all sessions start with
        # the high bit set. Read unsigned it would still count up, but from a
        # number the device does not agree with, and every request in that
        # session would be rejected. A bug that fires on half of all
        # handshakes and none of the others is the kind that gets called
        # "flaky hardware".
        self.seq = int.from_bytes(ivseq[-4:], "big", signed=True)
        self.sig = _klap_derive(b"ldk", local_seed, remote_seed, auth_hash)[:28]

    def _cipher(self, seq):
        from cryptography.hazmat.primitives.ciphers import (
            Cipher, algorithms, modes)
        return Cipher(algorithms.AES(self.key),
                      modes.CBC(self.iv + seq.to_bytes(4, "big", signed=True)))

    def seal(self, seq, payload):
        """signature + ciphertext for one payload at one sequence number.

        Separate from `encrypt` because the device seals its REPLY at the same
        sequence number the request used, so the operation is not "the next
        message" -- it is "this message, at this number". Keeping one
        implementation for both directions is also what lets a test drive the
        device side without writing a second encryptor to disagree with this
        one.
        """
        from cryptography.hazmat.primitives import padding
        pad = padding.PKCS7(128).padder()
        enc = self._cipher(seq).encryptor()
        ct = enc.update(pad.update(payload) + pad.finalize()) + enc.finalize()
        sig = hashlib.sha256(
            self.sig + seq.to_bytes(4, "big", signed=True) + ct).digest()
        return sig + ct

    def encrypt(self, payload):
        """-> (body, seq) for the NEXT message, advancing the sequence."""
        self.seq += 1
        return self.seal(self.seq, payload), self.seq

    def decrypt(self, body, seq):
        """The plaintext of a sealed message.

        THE SIGNATURE IS NOT CHECKED, and that is a known gap rather than an
        oversight. The first 32 bytes are the device's signature over the
        ciphertext; verifying it would be strictly better, and it is left out
        because nothing here has ever been run against a real KLAP device, so
        a rejection could not be told from a misunderstanding of the scheme --
        and a check that refuses valid traffic is worse than an absent one. A
        tampered body still fails: it will not decrypt to valid padding or to
        JSON. Close this the day real hardware is available.
        """
        from cryptography.hazmat.primitives import padding
        dec = self._cipher(seq).decryptor()
        plain = dec.update(body[32:]) + dec.finalize()
        unpad = padding.PKCS7(128).unpadder()
        return unpad.update(plain) + unpad.finalize()


# --------------------------------------------------------------- power backends
#
# Three real implementations, not one plus an interface. An interface with a
# single implementation is a guess about what varies, and this one was wrong
# twice before it was written down: `kasa_send` had the port as a literal, and
# `power_state` is shaped like the Kasa reply rather than like a plug. The
# third one, `wemo`, found the same literal-port mistake waiting to be made
# again and a second Kasa shape baked in besides -- `on_time_s` and `rssi` are
# free in Kasa's one `get_sysinfo` reply and cost a round trip each on a Wemo.
#
# All three return the SAME three-valued `on`: True, False, or None for "answered
# without saying". None must survive to the caller -- bool(None) is False,
# which reports "the machine is off" on the word of a reply that never
# mentioned it.


class KasaPower(object):
    """TP-Link's original LAN protocol. No cloud account, no dependency.

    NAMED FOR THE PROTOCOL GENERATION, not for being old: port 9999 with the
    XOR-autokey cipher above. `kasa-klap` below is the other generation, and
    they are different wire protocols on different ports -- not versions of
    one thing that could negotiate.

    Plain `kasa` is this one because it is what a Kasa plug on a home LAN
    usually speaks; the EP10 on this rig does. That is a bet on the installed
    base and it is worth knowing it is a bet: if TP-Link ever makes KLAP
    universal, `kasa` becomes the name of the unusual case. The config value
    `kasa-legacy` still selects this and always will -- see POWER_BACKENDS.
    """

    name = "kasa"

    def __init__(self, settings):
        self.settings = settings or {}

    def host(self):
        return self.settings.get("host") or power_host()

    def state(self):
        return power_state(self.host())

    def set(self, on):
        return power_set(self.host(), on)


class WemoPower(object):
    """Belkin Wemo over its LAN SOAP API. No cloud account, no dependency."""

    name = "wemo"

    # setup.xml is IDENTITY, not state: the friendly name and model do not
    # change while the daemon runs, and this device's HTTP server is the
    # flakiest thing on the LAN (note 4 in the transport section above), so it
    # is fetched once per host per process. Module-level for the same reason
    # `_WEMO_PORT` is -- the backend object is rebuilt on every call and cannot
    # hold a cache of its own.
    #
    # A FAILED IDENTITY FETCH IS NOT CACHED AND NEVER FAILS THE RELAY READ.
    # Not knowing what the plug is called is a different fact from not knowing
    # whether it is on, and only one of those is worth reporting as a fault.
    _IDENT = {}

    def __init__(self, settings):
        self.settings = settings or {}

    def host(self):
        return self.settings.get("host") or power_host()

    def _timeout(self):
        return float(self.settings.get("timeout_s", 5.0))

    def _soap(self, action, body="", service="basicevent:1",
              path="/upnp/control/basicevent1"):
        return wemo_soap(self.host(), service, path, action, body,
                         port=self.settings.get("port"),
                         timeout=self._timeout())

    def _meter(self):
        """The Insight energy meter, or {} where there is not one.

        SEPARATE CALL, SEPARATE FAILURE. This is a second round trip, to a
        device that goes unreachable for minutes at a stretch (note 4 above),
        so it must not be able to take the relay reading down with it: the
        machine's mains state is the answer people need in an outage, and the
        wattage is the answer they would like.

        GATED ON THE MODEL, because the meter is a hardware fact. Only the
        Insight has one; a Wemo Switch or Mini answers this call with a fault
        and would spend the round trip to learn that every single poll. `meter:
        false` in settings turns it off on an Insight too, for a rig that would
        rather not pay the second request at all.
        """
        if self.settings.get("meter") is False:
            return {}
        if "insight" not in (self._ident().get("model") or "").lower():
            return {}
        try:
            raw = _wemo_tag(self._soap("GetInsightParams", "",
                                       "insight:1", "/upnp/control/insight1"),
                            "InsightParams")
        except Exception:
            return {}
        # `state|lastchange|onfor|ontoday|ontotal|timespan|avgpower|
        #  currentpower|todaymw|totalmw|threshold`. Read POSITIONALLY and
        # defensively: a short list is a firmware answering a shape this does
        # not know, and guessing at it would be worse than saying nothing.
        f = (raw or "").split("|")
        if len(f) < 8:
            return {}

        def num(i, cast):
            try:
                return cast(f[i])
            except (ValueError, TypeError):
                return None

        return {"power_mw": num(7, lambda v: int(float(v))),
                # NOT `on_time_s`, and that distinction was measured rather
                # than assumed. Field 2 held 0 across a 90 s sample taken with
                # the relay CLOSED the whole time (2026-09-02) -- so it is not
                # counting relay-on seconds, or it would have read 90. It is
                # the Insight's LOAD-on counter: time the attached machine
                # spent drawing above `standby_threshold_mw`, which had never
                # happened on this plug.
                #
                # Kasa's `on_time` is relay-on seconds. Putting this in the
                # same field would have published "powered for 0 s" about a
                # machine whose mains had been on for a day, in a key another
                # backend fills with a different quantity -- so it gets its own
                # name and `on_time_s` stays null, which is what "this backend
                # does not report it" is for.
                "load_on_s": num(2, int),
                # The reason a running machine reads `1` and an idle outlet
                # reads `8`: the device compares the draw above against this.
                # Carried so a reader can check that arithmetic rather than
                # take note 2 on faith.
                "standby_threshold_mw": num(10, lambda v: int(float(v)))}

    def _ident(self):
        host = self.host()
        if host not in self._IDENT:
            try:
                xml = wemo_setup_xml(host, self.settings.get("port"),
                                     self._timeout())
            except Exception:
                return {}
            self._IDENT[host] = {"alias": _wemo_tag(xml, "friendlyName"),
                                 "model": _wemo_tag(xml, "modelName"),
                                 "firmware": _wemo_tag(xml, "firmwareVersion")}
        return self._IDENT[host]

    def state(self):
        xml = self._soap("GetBinaryState", "<BinaryState>0</BinaryState>")
        raw = _wemo_tag(xml, "BinaryState")
        on = wemo_on(raw)
        ident = self._ident()
        meter = self._meter()
        return {"on": on,
                # The device's own name wins over the configured one: `alias`
                # in the config is a label somebody typed, and the plug knows
                # what it was actually named.
                "alias": ident.get("alias") or self.settings.get("alias"),
                "model": ident.get("model"),
                # THE DRAW, WHERE THE HARDWARE HAS A METER. This is not a nicer
                # `on`: it answers a different question. `on` says the relay is
                # closed; `power_mw` says whether anything on the other end is
                # actually pulling current, and those come apart exactly when
                # it matters -- a machine wedged at its own power switch, a
                # cable out, a PSU that did not come up. It is the one witness
                # of target state on this rig that does not route through video
                # capture, which is worth something given how much of
                # docs/FINDINGS.md is about video lying.
                #
                # NULL, NEVER ZERO, WHERE THERE IS NO METER: a Wemo Switch has
                # no measuring hardware and 0 mW would read as "plugged in and
                # drawing nothing", which is a finding rather than a gap. The
                # Kasa EP10 on this rig is the same story from the other
                # protocol -- `feature: TIM`, and both emeter namespaces answer
                # "module not support" (measured 2026-09-02).
                "power_mw": meter.get("power_mw"),
                "standby_threshold_mw": meter.get("standby_threshold_mw"),
                # HOW LONG THE LOAD HAS BEEN DRAWING, not how long the relay
                # has been closed -- see `_meter`. The two come apart on
                # exactly the plug this was written against.
                "load_on_s": meter.get("load_on_s"),
                # NULL EVEN ON A METERED INSIGHT: no Wemo call reports relay-on
                # seconds, and the counter that looks like it is measuring
                # something else. Filling it from `load_on_s` would put two
                # different quantities in one key across backends.
                "on_time_s": None,
                # No LAN call reports it. Kasa gets it free inside the one
                # `get_sysinfo` it already makes; here it would be another
                # round trip to a flaky device for a field nothing reads.
                "rssi": None,
                "reason": None if on is not None else
                          ("the plug answered without a usable BinaryState "
                           "(%r)" % (raw,))}

    def set(self, on):
        got = _wemo_tag(self._soap("SetBinaryState",
                                   "<BinaryState>%d</BinaryState>"
                                   % (1 if on else 0)), "BinaryState")
        if wemo_on(got) is bool(on):
            return 0
        # ANYTHING ELSE IS NOT YET A FAILURE, so read the relay back and let
        # the hardware settle it rather than trusting the reply's word. One
        # request, on the unhappy path only, and it is the only thing here that
        # can tell "it was already in that state" from "it refused to switch".
        #
        # WHAT A REDUNDANT SET ACTUALLY ANSWERS, measured 2026-09-01 on the
        # Insight: `SetBinaryState(1)` against a plug already on came back
        # `<BinaryState>8|1788395720|0|0|0|1209600|9|0|0|0</BinaryState>` -- the
        # state pipe-joined onto the Insight counters, on a WRITE reply, where
        # notes 2 and 3 above were written expecting them only on reads. Both
        # defences fired at once and this returned success on the line above:
        # without the pipe-split, or without 8 meaning on, a plug that did
        # exactly what was asked would have raised.
        #
        # OTHER FIRMWARES ARE REPORTED TO ANSWER THE LITERAL WORD `Error` in
        # that same case. NOT OBSERVED HERE, and carried as hearsay on purpose
        # rather than as a property of the device -- the read-back covers it
        # either way, which is why it does not need to be true to be handled.
        st = self.state()
        if st.get("on") is bool(on):
            return 0
        raise IOError("wemo SetBinaryState(%s) answered %r and the relay then "
                      "read %r" % ("on" if on else "off", got, st.get("on")))


class KasaKlapPower(object):
    """TP-Link KLAP on port 80: newer Kasa firmware and the Tapo line.

    Needs a TP-Link account's credentials. They are checked by the device on
    the LAN and never leave it, but the account must exist -- there is no
    "local only" mode, which is the real cost of this generation and the reason
    `kasa` above is still worth having.

    THE PASSWORD IS NAMED, NOT WRITTEN. `password_env` gives the name of an
    environment variable, matching what `control.fileserver` already does in
    this config; a literal in vcctrl.yaml would be a credential in a file that
    gets copied to the Pi by every deploy. `password` is accepted too, because
    refusing it outright would just push someone to put it in the environment
    of a wrapper script where nothing documents it -- but it is second.
    """

    name = "kasa-klap"

    # Sessions are keyed by host and survive across the short-lived backend
    # objects `_protocol()` builds, because a KLAP session carries a sequence
    # number the device tracks. Rebuilding it per call would re-handshake on
    # every poll -- two extra round trips a minute, and a device that may well
    # rate-limit them.
    _SESSIONS = {}

    def __init__(self, settings):
        self.settings = settings or {}

    def host(self):
        return self.settings.get("host") or power_host()

    def _timeout(self):
        return float(self.settings.get("timeout_s", 5.0))

    def _credentials(self):
        user = self.settings.get("username")
        env = self.settings.get("password_env")
        pw = os.environ.get(env) if env else None
        if env and pw is None:
            raise IOError(
                "power backend 'kasa-klap' has password_env=%r and that "
                "variable is not set in the daemon's environment" % (env,))
        if pw is None:
            pw = self.settings.get("password")
        if not user or pw is None:
            raise IOError(
                "power backend 'kasa-klap' needs `username` and either "
                "`password_env` (preferred) or `password` in "
                "capabilities.power.settings")
        return user, pw

    def _post(self, path, body, cookie=None, query=""):
        req = urllib.request.Request(
            "http://%s%s%s" % (self.host(), path, query), data=body,
            headers={"Content-Type": "application/octet-stream"})
        if cookie:
            req.add_header("Cookie", cookie)
        with contextlib.closing(
                urllib.request.urlopen(req, timeout=self._timeout())) as r:
            return r.read(), r.headers.get("Set-Cookie") or ""

    def _handshake(self):
        try:
            import cryptography       # noqa: F401
        except ImportError:
            raise IOError(
                "power backend 'kasa-klap' needs the `cryptography` package "
                "(pip install cryptography); the `kasa` backend needs no "
                "dependency but only speaks the older port-9999 protocol")
        user, pw = self._credentials()
        auth = klap_auth_hash(user, pw)
        local_seed = os.urandom(16)
        reply, setcookie = self._post(KLAP_HANDSHAKE1, local_seed)
        if len(reply) < 48:
            raise IOError("klap handshake1 returned %d bytes, expected 48"
                          % len(reply))
        remote_seed, server_hash = reply[:16], reply[16:48]
        expect = hashlib.sha256(local_seed + remote_seed + auth).digest()
        if server_hash != expect:
            # A CREDENTIAL FAULT, and it must not be reported as a network one.
            # The device answered, on time, with a well-formed reply -- it is
            # simply holding a different account's hash. Someone told "the plug
            # is unreachable" checks cables; someone told this checks the
            # config.
            raise IOError(
                "klap handshake1 rejected the credentials for %s -- the plug "
                "answered but holds a different TP-Link account's hash "
                "(username %r)" % (self.host(), user))
        cookie = setcookie.split(";")[0] if setcookie else None
        self._post(KLAP_HANDSHAKE2,
                   hashlib.sha256(remote_seed + local_seed + auth).digest(),
                   cookie=cookie)
        return KlapSession(local_seed, remote_seed, auth, cookie)

    def _session(self):
        sess = self._SESSIONS.get(self.host())
        if sess is None:
            sess = self._SESSIONS[self.host()] = self._handshake()
        return sess

    def _call(self, method, params=None):
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        blob = json.dumps(payload).encode()
        for attempt in (1, 2):
            sess = self._session()
            try:
                body, seq = sess.encrypt(blob)
                reply, _ = self._post(KLAP_REQUEST, body, cookie=sess.cookie,
                                      query="?seq=%d" % seq)
                return json.loads(sess.decrypt(reply, seq))
            except Exception:
                # ONE retry, and only by throwing the session away. A KLAP
                # session dies from the device's side -- it reboots, it times
                # the cookie out, it loses track of the sequence number -- and
                # every one of those looks like a transport error here. Retry
                # WITHOUT re-handshaking would replay a sequence number the
                # device has already rejected and fail identically forever.
                self._SESSIONS.pop(self.host(), None)
                if attempt == 2:
                    raise

    def state(self):
        info = (self._call("get_device_info") or {}).get("result") or {}
        on = info.get("device_on")
        # `nickname` is base64 in this protocol, unlike every other field.
        alias = info.get("nickname")
        if alias:
            try:
                alias = base64.b64decode(alias).decode("utf-8", "replace")
            except Exception:
                alias = None
        return {"on": None if on is None else bool(on),
                "alias": alias or self.settings.get("alias"),
                "model": info.get("model"),
                # Relay-on seconds, and the same quantity Kasa's `on_time`
                # carries -- unlike the Wemo Insight's load-on counter. See
                # WemoPower.state() for why that distinction has its own key.
                "on_time_s": info.get("on_time"),
                "rssi": info.get("rssi"),
                "power_mw": self._draw(),
                "reason": None if on is not None else
                          ("get_device_info answered without `device_on`")}

    def _draw(self):
        """Live draw in mW on a metered model, None everywhere else.

        Same rule as the Wemo: NEVER 0 for "no meter". A plug without energy
        monitoring answers this method with an error, and a zero here would
        read as "plugged in and drawing nothing", which is a finding rather
        than a gap.
        """
        if self.settings.get("meter") is False:
            return None
        try:
            usage = (self._call("get_energy_usage") or {}).get("result") or {}
        except Exception:
            return None
        mw = usage.get("current_power")
        return None if mw is None else int(mw)

    def set(self, on):
        resp = self._call("set_device_info", {"device_on": bool(on)}) or {}
        err = resp.get("error_code")
        if err not in (0, None):
            raise IOError("klap set_device_info error_code=%s" % err)
        return 0


class ShellPower(object):
    """Run the operator's own commands.

    The escape hatch for every plug this project will never support: a Zigbee
    bridge, a relay board, a PDU with a web form, a person with a switch and a
    script. Settings are `on_cmd`, `off_cmd` and `state_cmd`; state_cmd must
    print `on` or `off`.

    ANYTHING ELSE ON STDOUT IS `None`, NOT AN ERROR AND NOT `off`. A script
    that prints nothing, or prints a warning, has failed to answer -- and
    "failed to answer" is a different fact from "the plug is off". Collapsing
    them here would hand a confident False to the LED epoch logic, which would
    then report every reading as belonging to a powered-down machine.
    """

    name = "shell"

    def __init__(self, settings):
        self.settings = settings or {}

    def host(self):
        # There is no host; the commands are the mechanism. Returned so the
        # capability's "is anything configured at all" check still works.
        return self.settings.get("state_cmd") or self.settings.get("on_cmd")

    def _run(self, key):
        cmd = self.settings.get(key)
        if not cmd:
            raise IOError("power backend 'shell' has no %s configured" % key)
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=float(self.settings.get("timeout_s", 20)))

    def state(self):
        p = self._run("state_cmd")
        word = (p.stdout or "").strip().lower()
        on = True if word == "on" else False if word == "off" else None
        return {"on": on, "alias": self.settings.get("alias"),
                "model": "shell", "on_time_s": None, "rssi": None,
                # No shape to read one out of: `state_cmd` promises the word
                # `on` or `off` and nothing else. A rig that can measure its
                # own draw already has somewhere better to put the number.
                "power_mw": None,
                "reason": None if on is not None else
                          ("state_cmd printed %r, which is neither 'on' nor "
                           "'off'" % word[:40])}

    def set(self, on):
        p = self._run("on_cmd" if on else "off_cmd")
        if p.returncode != 0:
            raise IOError("power %s_cmd exited %d: %s"
                          % ("on" if on else "off", p.returncode,
                             (p.stderr or "").strip()[:200]))
        return 0


# THE CONFIG NAMES. One of them is an alias rather than a backend.
#
# `kasa-legacy` was this project's original name for the port-9999 protocol,
# and it is the value written in the operator's live vcctrl.yaml. Dropping it
# would not produce a gentle failure: the registry REFUSES an unknown backend
# name -- correct behaviour, and here it would mean the rig quietly loses power
# control at the next daemon restart, discovered whenever someone next needed
# to reboot the target. So it keeps working, permanently, and `_backend_alias`
# says so once on stderr instead of leaving the config silently misdescribing
# itself.
POWER_BACKENDS = {"kasa": KasaPower,
                  "kasa-legacy": KasaPower,     # deprecated spelling of `kasa`
                  "kasa-klap": KasaKlapPower,
                  "wemo": WemoPower,
                  "shell": ShellPower}

# Config value -> what it actually selects.
POWER_BACKEND_ALIASES = {"kasa-legacy": "kasa"}

_ALIAS_WARNED = set()


def _backend_alias(name):
    """The real backend name for a config value, noting a deprecated spelling.

    Once per name per process. This is consulted from `_protocol()`, which runs
    on every power call and on every 60 s heartbeat; warning each time would
    bury the journal under a message about a config that is working fine.
    """
    real = POWER_BACKEND_ALIASES.get(name)
    if real and name not in _ALIAS_WARNED:
        _ALIAS_WARNED.add(name)
        sys.stderr.write(
            "config: capabilities.power.backend: %r is the old spelling of "
            "%r and still works. Rename it when convenient -- it selects the "
            "same protocol.\n" % (name, real))
    return real or name


HID_KBD_DEVICE = CFG.default("capabilities.input.settings.hid_keyboard_device",
                             "/dev/hidg0")
HID_MOUSE_DEVICE = CFG.default("capabilities.input.settings.hid_mouse_device",
                               "/dev/hidg1")


class Devices(object):
    """Owns the keyboard/mouse OUTPUT for the life of the process.

    Two backends, chosen by `capabilities.input.backend` and never mixed:

      usb4vc-uinput (default) -- two persistent uinput devices. USB4VC
        discovers them on its 0.75s scan and relays their events over SPI to
        a PS/2/ADB protocol board. This is the ONLY backend that existed
        before the one below, and everything about it is unchanged.

      hid-gadget -- the Pi's own USB-C port acting as a USB HID keyboard and
        mouse (dwc2 peripheral mode + configfs; the gadget itself is built by
        pi/files/vcctrl-hid-gadget-setup.sh, independently of this daemon --
        see that script's own docstring for why). Raw HID reports are written
        directly to /dev/hidg0/1. No USB4VC, no SPI, no protocol board: the
        target here has a real USB port and needs none of that translation.

    Both backends answer the SAME method surface below. InputCapability and
    LedsCapability call into `self.devs` without knowing or caring which one
    is underneath -- InputCapability's own Rule 1 depends on that.
    """

    def __init__(self):
        self.hid_mode = (CFG.default("capabilities.input.backend",
                                      "usb4vc-uinput") == "hid-gadget")
        self.lock = threading.Lock()
        # Keys currently held by keydown with no matching keyup. Tracked so a
        # disconnecting client cannot strand one down (see release_all).
        # Holds evdev codes in BOTH backends -- translation to a HID usage ID
        # or modifier bit happens only at the point of writing a report, in
        # _press/_release below, so this set means the same thing either way.
        self.held = set()
        # SAME REASONING, FOR MOUSE BUTTONS HELD BY mouse_down WITH NO
        # MATCHING mouse_up -- a dropped connection mid-drag must not leave
        # a button down at the target any more than mid-keypress may.
        # Holds MOUSE_BUTTONS evdev codes in both backends, same as `held`.
        self.held_mouse = set()

        if self.hid_mode:
            self.kbd = None
            self.mouse = None
            # Resolved FRESH from CFG here, not read from the HID_KBD_DEVICE/
            # HID_MOUSE_DEVICE module constants directly -- those are computed
            # once at import time from whichever profile's config happened to
            # be bound to CFG at that moment (the primary's), so a SECOND
            # profile with its own hid_keyboard_device/hid_mouse_device
            # setting would otherwise silently get the primary's paths
            # instead of its own. Devices() is always constructed inside
            # _profile_scope(that profile's cfg) (see _build_instance), so
            # this CFG.default() call resolves correctly per profile. Stored
            # on self rather than re-read later: every other place that needs
            # these paths (status/serve's log line) reads devs.hid_kbd_device/
            # devs.hid_mouse_device instead of a module global, so they stay
            # correct regardless of which thread asks.
            self.hid_kbd_device = CFG.default(
                "capabilities.input.settings.hid_keyboard_device",
                HID_KBD_DEVICE)
            self.hid_mouse_device = CFG.default(
                "capabilities.input.settings.hid_mouse_device",
                HID_MOUSE_DEVICE)
            self._hid_kbd_fd = open(self.hid_kbd_device, "wb", buffering=0)
            self._hid_mouse_fd = open(self.hid_mouse_device, "wb", buffering=0)
            self._hid_mods = 0
            self._hid_keys = []            # up to 6 pressed HID usage IDs
            self._hid_mouse_buttons = 0
            # No PS/2 return channel over a generic HID gadget -- there is no
            # protocol message a HID host sends back that means "the lock
            # light changed" the way USB4VC's PS/2 bridge has one. See
            # read_leds() below.
            self.led_paths = {}
            # BEST-EFFORT ONLY, HERE. This is clearing state on a device
            # that may not have a host listening yet -- modernpc's gadget
            # link can be unattached for reasons that have nothing to do
            # with this daemon (the connected machine asleep or rebooting,
            # a cable reseated) and are expected to clear on their own.
            # Found live 2026-09-02: with the host not listening, this
            # write raised BrokenPipeError (errno 108, "transport endpoint
            # shutdown") UNCAUGHT, in Devices.__init__, before any
            # Capability's own start() ever ran -- so Rule 2 ("a capability
            # that fails to start is recorded as failed, the daemon
            # carries on") never got a chance to apply, and the whole
            # daemon crash-looped. A GATEWAY2000 restart during that
            # window inherits the same crash, because one `Devices` object
            # serves every profile's `main()` build step -- modernpc's own
            # hardware taking down a daemon restart that has nothing to do
            # with modernpc is exactly the failure this guards. The actual
            # first real report (a keypress, a mouse move) still raises
            # normally -- only this init-time "no keys held yet" write is
            # tolerated, because there is nothing for its failure to be
            # attributed to but "no host was listening at the moment the
            # daemon started", which is not this daemon's fault to fix.
            try:
                self._write_hid_kbd_report()
                self._write_hid_mouse_report()
            except OSError as exc:
                sys.stderr.write(
                    "Devices: could not clear the HID gadget's initial "
                    "report (%s) -- no host listening yet? Will retry on "
                    "the first real keystroke.\n" % errstr(exc))
            return

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
        # No PS/2 return channel exists over a generic HID gadget -- see the
        # comment in __init__. Absent, not zero: LedsCapability's own contract
        # (Capability's docstring, "value keys ABSENT when unavailable") is
        # that a channel with nothing to read publishes no key at all, rather
        # than a plausible-looking `{"capslock": 0}` nobody actually measured.
        if self.hid_mode:
            return {}
        out = {}
        for name, path in self.led_paths.items():
            try:
                with open(path) as f:
                    out[name] = int(f.read().strip())
            except Exception:
                out[name] = None
        return out

    # -- HID gadget report I/O -----------------------------------------------
    #
    # One write() per report, unbuffered (Devices.__init__ opens both fds with
    # buffering=0) -- a HID gadget report is a fixed-size datagram-like unit,
    # not a byte stream, and a short or coalesced write would send a
    # different report than the one built here.

    def _write_hid_kbd_report(self):
        keys = (self._hid_keys + [0, 0, 0, 0, 0, 0])[:6]
        self._hid_kbd_fd.write(bytes([self._hid_mods, 0] + keys))

    def _write_hid_mouse_report(self, dx=0, dy=0, wheel=0):
        def s8(v):
            return max(-127, min(127, int(v))) & 0xff
        self._hid_mouse_fd.write(
            bytes([self._hid_mouse_buttons, s8(dx), s8(dy), s8(wheel)]))

    # -- emission -------------------------------------------------------------
    #
    # _press/_release take an EVDEV code -- the same ones NAMED_KEYS/CHARMAP
    # already resolve names and characters to -- and are the only place that
    # branches on which backend is active. Every method below them (key,
    # type_text, hold, combo, keydown, keyup, release_all) is backend-agnostic
    # and UNCHANGED in shape from before hid-gadget existed; only the two
    # primitives underneath it differ.

    def _press(self, code):
        if not self.hid_mode:
            self.kbd.write(e.EV_KEY, code, 1)
            self.kbd.syn()
            return
        bit = HID_MODIFIER_BITS.get(code)
        if bit is not None:
            self._hid_mods |= bit
        else:
            usage = EVDEV_TO_HID_USAGE.get(code)
            if usage is None:
                raise ValueError(
                    "key has no HID usage mapping: evdev code %d" % code)
            if usage not in self._hid_keys:
                if len(self._hid_keys) >= 6:
                    # A real keyboard reports all-1s ("phantom") on true
                    # 6-key rollover; dropping the oldest held key instead is
                    # a deliberate simplification -- nothing this daemon's
                    # key/combo/keydown surface sends holds more than a
                    # handful of keys at once, and phantom-state has no
                    # meaning to relay to a caller anyway.
                    self._hid_keys.pop(0)
                self._hid_keys.append(usage)
        self._write_hid_kbd_report()

    def _release(self, code):
        if not self.hid_mode:
            self.kbd.write(e.EV_KEY, code, 0)
            self.kbd.syn()
            return
        bit = HID_MODIFIER_BITS.get(code)
        if bit is not None:
            self._hid_mods &= ~bit
        else:
            usage = EVDEV_TO_HID_USAGE.get(code)
            if usage in self._hid_keys:
                self._hid_keys.remove(usage)
        self._write_hid_kbd_report()

    def _tap(self, code, pace):
        self._press(code)
        time.sleep(pace)
        self._release(code)
        time.sleep(pace)

    def key(self, names, pace=DEFAULT_PACE_S):
        with self.lock:
            for n in names:
                code = NAMED_KEYS.get(n.lower())
                if code is None:
                    raise ValueError("unknown key: %s" % n)
                self._tap(code, pace)

    def type_text(self, text, pace=DEFAULT_PACE_S):
        with self.lock:
            for ch in text:
                ent = CHARMAP.get(ch)
                if ent is None:
                    raise ValueError("untypable character: %r" % ch)
                code, shift = ent
                if shift:
                    self._press(e.KEY_LEFTSHIFT)
                    time.sleep(pace)
                self._tap(code, pace)
                if shift:
                    self._release(e.KEY_LEFTSHIFT)
                    time.sleep(pace)

    def hold(self, name, ms, pace=DEFAULT_PACE_S):
        code = NAMED_KEYS.get(name.lower())
        if code is None:
            raise ValueError("unknown key: %s" % name)
        with self.lock:
            self._press(code)
            time.sleep(ms / 1000.0)
            self._release(code)
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
                self._press(c)
                time.sleep(pace)
            for c in reversed(codes):
                self._release(c)
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
            self._press(code)
            self.held.add(code)

    def keyup(self, name):
        code = NAMED_KEYS.get(name.lower())
        if code is None:
            raise ValueError("unknown key: %s" % name)
        with self.lock:
            self._release(code)
            self.held.discard(code)

    def release_all(self, pace=DEFAULT_PACE_S):
        """Release every key held via keydown. Returns how many.

        The web KVM must call this when a viewer's socket closes, or gives
        up keyboard capture. A dropped wifi connection mid-keypress would
        otherwise leave a key down at the g2k forever, which at a DOS
        prompt types until the buffer fills.

        KEYS ONLY, DELIBERATELY -- see mouse_release_all for the mouse's
        own version and why the two are not one verb. Keyboard capture and
        mouse capture are independent controls in the web KVM; releasing
        one must not reach across and end whatever the other is
        mid-operation on (a latched on-screen modifier held for a chord
        that has not been sent yet, or a mouse button held for a drag).
        """
        with self.lock:
            codes = sorted(self.held)
            for code in codes:
                self._release(code)
                time.sleep(pace)
            self.held.clear()
        return len(codes)

    def mouse_release_all(self, pace=DEFAULT_PACE_S):
        """release_all's mouse-button counterpart. Returns how many.

        A separate verb rather than release_all covering both: a viewer can
        hold keyboard capture and mouse capture independently, and giving
        up one must not silently drop whatever the other is mid-operation
        on. See release_all's own docstring.
        """
        with self.lock:
            codes = sorted(self.held_mouse)
            for code in codes:
                self._mouse_button_release(code)
                time.sleep(pace)
            self.held_mouse.clear()
        return len(codes)

    def mouse_move(self, dx, dy, pace=DEFAULT_PACE_S):
        with self.lock:
            if not self.hid_mode:
                if dx:
                    self.mouse.write(e.EV_REL, e.REL_X, int(dx))
                if dy:
                    self.mouse.write(e.EV_REL, e.REL_Y, int(dy))
                self.mouse.syn()
                time.sleep(pace)
                return
            # A HID relative report is one SIGNED BYTE per axis (-127..127),
            # unlike EV_REL which takes any int -- so a move bigger than that
            # is split into several reports summing to the same total
            # distance, rather than being silently clamped to a fifth of what
            # was asked for.
            rx, ry = int(dx), int(dy)
            while rx or ry:
                sx = max(-127, min(127, rx))
                sy = max(-127, min(127, ry))
                self._write_hid_mouse_report(sx, sy)
                rx -= sx
                ry -= sy
                time.sleep(pace)

    def mouse_wheel(self, dy, pace=DEFAULT_PACE_S):
        """Scroll. Chunked into signed-byte HID reports the same way
        mouse_move chunks a delta too large for one -- see its own comment;
        _write_hid_mouse_report's own clamp would otherwise silently throw
        away everything past +-127 instead of sending it as more than one
        report.

        UNMEASURED ON PS/2 (docs/MOUSE.md sec 7): the uinput mouse device
        declares REL_WHEEL and USB4VC's own bridge may or may not carry it
        through to a PS/2 IntelliMouse-style packet, and DOS's CTMOUSE may
        or may not honour one if it does. This sends the event either way;
        nothing has confirmed a DOS program's own window scrolls because of
        it. Sign/direction is passed through exactly as given, unverified
        against either path -- see the browser's own wheel handler.
        """
        with self.lock:
            if not self.hid_mode:
                if dy:
                    self.mouse.write(e.EV_REL, e.REL_WHEEL, int(dy))
                    self.mouse.syn()
                    time.sleep(pace)
                return
            ry = int(dy)
            while ry:
                sy = max(-127, min(127, ry))
                self._write_hid_mouse_report(0, 0, sy)
                ry -= sy
                time.sleep(pace)

    def _mouse_button_press(self, code):
        """The lower half of a click: press one MOUSE_BUTTONS code and leave
        it down. Caller holds self.lock. Shared by mouse_click and
        mouse_down so the two backends' report-writing logic exists once."""
        if not self.hid_mode:
            self.mouse.write(e.EV_KEY, code, 1)
            self.mouse.syn()
            return
        self._hid_mouse_buttons |= HID_MOUSE_BUTTON_BITS[code]
        self._write_hid_mouse_report()

    def _mouse_button_release(self, code):
        """The upper half of a click. Caller holds self.lock."""
        if not self.hid_mode:
            self.mouse.write(e.EV_KEY, code, 0)
            self.mouse.syn()
            return
        self._hid_mouse_buttons &= ~HID_MOUSE_BUTTON_BITS[code]
        self._write_hid_mouse_report()

    def mouse_click(self, button, pace=DEFAULT_PACE_S):
        code = MOUSE_BUTTONS.get(button.lower())
        if code is None:
            raise ValueError("unknown button: %s" % button)
        with self.lock:
            self._mouse_button_press(code)
            time.sleep(pace)
            self._mouse_button_release(code)
            time.sleep(pace)

    def mouse_down(self, button):
        """Press and hold, for a drag. The matching mouse_up may never
        come -- see release_all. Unlike mouse_click this does not bracket
        a complete operation, so (mirroring keydown) it takes the lock only
        for the single event."""
        code = MOUSE_BUTTONS.get(button.lower())
        if code is None:
            raise ValueError("unknown button: %s" % button)
        with self.lock:
            self._mouse_button_press(code)
            self.held_mouse.add(code)

    def mouse_up(self, button):
        code = MOUSE_BUTTONS.get(button.lower())
        if code is None:
            raise ValueError("unknown button: %s" % button)
        with self.lock:
            self._mouse_button_release(code)
            self.held_mouse.discard(code)


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
    "mouse_move", "mouse_click", "mouse_down", "mouse_up",
    "mouse_release_all", "mouse_wheel",
])

# Power ACTIONS that change the target's state. Gated like input, because
# cutting mains under a running cell destroys it exactly as surely as typing
# into it -- and until now the most destructive control on the rig was the one
# the arbiter did not cover, which is the wrong way round.
#
# `state` IS NOT HERE, DELIBERATELY. It is a read, and the arbiter gates input
# only, never observation: gating it would mean a held lock stops everyone
# else from finding out whether the machine is on, including `preflight` and
# `power_on()` in the harness library. Diagnosing a stuck run must never
# require taking the lock away from it.
GATED_POWER_ACTIONS = frozenset(["on", "off", "cycle"])


def _gated(cmd, req):
    """Does this specific request need the input lock?

    Command-level for input, action-level for power. A set of command names
    could not express "on but not state", and the alternative -- gating the
    whole `power` verb -- trades a destructive-action hole for an observation
    outage.
    """
    if cmd in GATED_COMMANDS:
        return True
    if cmd == "power":
        return req.get("action", "state") in GATED_POWER_ACTIONS
    return False


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
    """The input lock, and the destructive power actions. Never observation.

    Unheld by default, and while unheld every gated command behaves exactly as
    it did before this existed.

    NO LONGER OPT-IN. The docstring used to say "nothing in the existing
    tooling acquires it", which was true when written and stopped being true
    without the sentence changing: vcctrl-sweep takes it and refuses outright
    if somebody else holds it, and vcctrl-cell takes it best-effort and says so
    when it cannot. A comment that states a fact reads as one, and this one
    was cited in a discussion about why a running round showed no owner.

    What it gates is input plus power on/off/cycle -- see _gated(). `power
    state` is excluded on purpose: gating a read would mean a held lock stops
    anyone else discovering whether the machine is on, and diagnosing a stuck
    run must never require taking the lock away from it.
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

    ----------------------------------------------------------------------
    WRITING A `snapshot()`? THE ABSENCE RULE IS NOT WHICHEVER SIBLING YOU
    HAPPENED TO OPEN FIRST.

    Two rules are in force in this file and they are OPPOSITE, so a schema
    copied from one sibling alone is half wrong with no way to tell which
    half. It is not an even split -- it is four to one:

        every key always present, null where unknown
            PowerCapability, BoardCapability, TargetProfile, FilesCapability
        value keys ABSENT when unavailable, never zero
            LedsCapability -- and only LedsCapability

    Both are correct, and the line between them is DESCRIPTIVE versus
    MEASURED:

      * A descriptive or status field keeps its key and carries null, because
        null is a real answer somebody wants. "The plug did not answer", "no
        layout is declared for this board", "the profile is not known" are
        information, and a consumer that has to branch on which keys EXIST
        ends up re-encoding this daemon's internal states -- which has broken
        the KVM more than once.

      * A SAMPLED MEASUREMENT loses its key entirely, because a plausible
        default is indistinguishable from data. LedsCapability is the only
        capability here that publishes sampled values at all, which is why it
        is the only exception: `{"capslock": 0}` on a channel that was never
        read says "the lamp is off" to a consumer that forgot to check
        `available`, and it is confidently wrong. Absent gives undefined and
        a dash.

    A surface that needs BOTH -- enumerability and absence-means-absence --
    gets both rather than choosing: every expected entry is present so a
    consumer can enumerate and a missing one is a bug, and an entry with no
    measurement carries no measured field. The keyboard coverage table is the
    first of these; docs/WEBKVM.md sec. 5.2b works it through.

    Found writing that table, recorded here instead, because this is where it
    bites: the next person to hit the fork will be writing a snapshot, not a
    coverage schema.
    ----------------------------------------------------------------------
    """

    name = None
    bus = None

    # {config name: implementation}. Empty means this capability has exactly
    # one implementation and `backend:` may only be omitted or set to `none`.
    # `none` is never listed here -- it is handled by the registry, because a
    # not-configured capability must not be an object that could accidentally
    # answer.
    BACKENDS = {}

    # The implementation used when `backend:` is absent. Named so a config
    # that says nothing behaves exactly as the code did before backends
    # existed, rather than becoming unconfigured by omission.
    DEFAULT_BACKEND = None

    # The NAME of that default. Separate from the class because several
    # backends share one class -- power's kasa, wemo and shell are the same
    # capability with a different protocol object -- so the class cannot say
    # which one was chosen. Reporting the class's own name here is what broke
    # power on the rig: `backend_name` came back as "power", and the protocol
    # lookup for "power" found nothing.
    DEFAULT_BACKEND_NAME = None

    # Set by the registry after construction: which backend was chosen, and
    # that backend's `settings` mapping from config.
    backend_name = None
    settings = None

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
            "mouse_down": self._mouse_down, "mouse_up": self._mouse_up,
            "mouse_release_all": self._mouse_release_all,
            "mouse_wheel": self._mouse_wheel,
            # Read-only, and deliberately a COMMAND rather than a field on
            # /state.json: it is a constant, and every open tab polls state
            # every 1.5 s. Same argument as /wslog.json.
            "keymap": self._keymap,
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
        # Ctrl-Alt-Del is a reboot, and after it the profile reading describes
        # a boot that is no longer running. Matched on the SET of keys, not
        # their order, because the caller may send them in any.
        if is_reboot_combo(req.get("keys")):
            PROFILE.invalidate("ctrl-alt-del")
        # MODIFIERS FIRST, for every caller. See order_chord(): the primitive
        # presses in the order it is handed, so an unordered chord arrives at
        # the target as a keystroke followed by its modifiers. The browser used
        # to sort before posting and the CLI did not, which meant `vcctrl combo
        # delete ctrl alt` was quietly a different command from the same chord
        # built in the page. Sorting here is what makes them one command.
        keys = order_chord(req["keys"])
        self.devs.combo(keys, _pace(req))
        return {"ok": True, "keys": keys}

    def _keymap(self, req):
        return {"ok": True, "keymap": keymap()}

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

    def _mouse_down(self, req):
        self.devs.mouse_down(req.get("button", "left"))
        return {"ok": True}

    def _mouse_up(self, req):
        self.devs.mouse_up(req.get("button", "left"))
        return {"ok": True}

    def _mouse_release_all(self, req):
        return {"ok": True, "released": self.devs.mouse_release_all(_pace(req))}

    def _mouse_wheel(self, req):
        self.devs.mouse_wheel(req.get("dy", 0), _pace(req))
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


def _configured_targets():
    """The `targets:` list from config as {board_id: name}, or None if absent.

    None means "not configured", which is why this returns None rather than an
    empty dict: an empty mapping would read as "configured, and no board maps
    to anything", and the caller must be able to tell those apart.
    """
    t = CFG.optional("targets")
    if t is vcconfig.ABSENT or t is vcconfig.NONE:
        return None
    out = {}
    for row in t:
        try:
            out[int(row["board_id"])] = row.get("name")
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _configured_keyboards():
    """`targets:` as {board_id: keyboard layout id}, or None if absent.

    A SEPARATE function rather than widening _configured_targets(), which
    flattens to {id: name} and has another caller. Two small readers over the
    same list is the shape _configured_led_boards() already set, and it keeps
    each one's "absent" answer its own.

    The value is a LAYOUT ID -- an opaque string the page resolves against its
    own table of drawn keyboards -- not a machine name and not a boolean. A
    row with no `keyboard:` maps to None, which is a real answer: this board
    is known and no keyboard layout has been declared for it. The page must
    not fall back to the PC layout on it, because a Macintosh drawn as a PC is
    a picture of a keyboard that is not in the building.
    """
    t = CFG.optional("targets")
    if t is vcconfig.ABSENT or t is vcconfig.NONE:
        return None
    out = {}
    for row in t:
        try:
            kb = row.get("keyboard")
            out[int(row["board_id"])] = str(kb) if kb else None
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _configured_led_boards():
    """Board ids whose target declares `leds: supported`, or None if absent.

    The word matters. `unsupported` is a fact about the protocol -- a
    Macintosh has no message in which it reports lock-key state back to a
    keyboard -- and it is NOT a fault. Anything that is not the literal word
    `supported` is treated as not-supported here, so a typo fails closed: the
    channel reports unsupported rather than being trusted and returning
    nothing.
    """
    t = CFG.optional("targets")
    if t is vcconfig.ABSENT or t is vcconfig.NONE:
        return None
    out = []
    for row in t:
        try:
            if row.get("leds") == "supported":
                out.append(int(row["board_id"]))
        except (TypeError, ValueError, KeyError):
            continue
    return tuple(out)


# Boards that have a PS/2 LED return channel. PBID 1 is the IBM PC board; 2
# and 3 are ADB, which has no equivalent -- there is no protocol message in
# which a Macintosh reports its lock-key state back to a keyboard.
#
# The built-in is the reference rig's; a configured `targets:` list replaces it.
LED_BOARDS = _configured_led_boards()
if LED_BOARDS is None:
    LED_BOARDS = (1,)


def _configured_power_boards():
    """Board ids the configured power plug actually controls, or None.

    NOT part of `targets:` -- that list describes facts about a MACHINE
    (does it have LEDs, can it receive files); this describes a fact about
    the CONFIGURED PLUG, which board's mains it is wired to. Lives beside the
    plug's own settings for that reason: `capabilities.power.settings.boards`,
    read the same defensive way as `_configured_led_boards()` -- anything
    that is not a clean list of ints is treated as "not configured" rather
    than trusted partially, so a typo here fails toward refusing power
    actions rather than toward silently accepting an unmapped board.
    """
    boards = CFG.optional("capabilities.power.settings.boards")
    if boards is vcconfig.ABSENT or boards is vcconfig.NONE:
        return None
    out = []
    for b in boards:
        try:
            out.append(int(b))
        except (TypeError, ValueError):
            continue
    return tuple(out)


# Which board(s) the configured plug is wired to. docs/BOARD-IDENTITY.md
# sec. 5: `power cycle` drives ONE plug regardless of which USB4VC protocol
# board is seated, so with a Mac Plus installed it cut the Gateway's mains --
# a machine nobody asked about and possibly mid-run on a peer session. This is
# the fix: a gated power action refuses on a board this plug is not declared
# to control, rather than running unconditionally (see PowerCapability.support
# and _power below).
#
# The built-in default is the reference rig's single plug, wired to the g2k
# (board 1) -- same shape as LED_BOARDS. A rig with more than one powered
# machine MUST configure `capabilities.power.settings.boards` explicitly; an
# unmapped board is refused, never assumed to be this one.
POWER_BOARDS = _configured_power_boards()
if POWER_BOARDS is None:
    POWER_BOARDS = (1,)


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

    A `board: none` INSTANCE RETURNS None UNCONDITIONALLY, without even
    trying the file. Not an optimization: /run/usb4vc/board.json is a path on
    the HOST filesystem, not scoped to one vcctrld process, and a second
    instance on the same Pi with no protocol board of its own (the
    `hid-gadget` input backend has no USB4VC/SPI board at all) would
    otherwise silently read whatever the OTHER instance's board happens to
    be -- a real value about a real board, attributed to a target that has
    none. `board: none` is this daemon's own declaration that the question
    does not apply here, so the answer must not come from a file that was
    never about this process to begin with.
    """
    _board_backend = CFG.optional("capabilities.board.backend")
    if _board_backend is vcconfig.NONE or _board_backend == "none":
        return None
    # BoardCapability.FILE, the class attribute -- NOT CFG.default(
    # "daemon.usb4vc.board_file", ...), which was tried and reverted: that
    # key has a built-in DEFAULT ("/run/usb4vc/board.json" in vcconfig.py's
    # own DEFAULTS), so it is never actually ABSENT and CFG.default() would
    # always return the SAME value regardless of the fallback argument --
    # silently ignoring per-profile overrides, and breaking every test that
    # monkeypatches BoardCapability.FILE directly to point at a temp file
    # (tests/test_core.py: test_installed_board_id_never_guesses and
    # others). Left as a known limitation instead: a future board-aware
    # profile with its own board_file would share this class attribute with
    # the primary's, exactly like LED_BOARDS/POWER_BOARDS below already do
    # for the same reason -- not exercised by any profile that exists
    # today, since modernpc's board: none already returns above before this
    # line is ever reached.
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
        if LedsCapability._changes is None:
            # `start()` creates this, and `_sample()` is reachable without it
            # -- `snapshot()` calls us, and so does `verify_input` now. The
            # deque was only ever absent on a path that could not reach the
            # append below, so this was a crash waiting on a call order rather
            # than on a fault. Created here as well so the record exists
            # wherever a transition can be observed.
            LedsCapability._changes = collections.deque(maxlen=self.CHANGES_MAX)
        prev = LedsCapability._seen_values
        if values != prev:
            epoch, _powered, _at = TARGET.state()
            LedsCapability._seen_values = dict(values)
            if prev is not None:
                # The first sample of a daemon's life is not a transition --
                # there is no prior state for it to have moved from, and
                # recording one would put a fictitious change at every start.
                #
                # `_proven_epoch` LIVES IN HERE FOR THE SAME REASON, and it
                # used to live three lines up where it was set by the first
                # sample. That is how `capslock: 1` came to sit beside
                # `changes: 0` with `available: true`: the change record was
                # guarded by this test and the proof field was not, so the
                # gate compared a proof written at startup against the epoch
                # it was written in, matched, and published nodes nothing had
                # ever been observed to move. The field's name said proven;
                # its value meant "a sample differed from the one before it",
                # and the first sample always differs from None.
                #
                # Measured 2026-08-24: the target's Caps Lock was OFF while
                # this channel reported 1, and unshifted text typed at the
                # prompt came back lowercase on the glass.
                LedsCapability._proven_epoch = epoch
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
                # THE UNDRIVEN-REBOOT SIGNAL. POST clears the LEDs whatever
                # caused the reset, and RDYPULSE is the last line of every
                # boot path in AUTOEXEC -- so a front-panel reset and a crash
                # reboot produce the same two edges as a driven one, which
                # `power` and ctrl-alt-del alone could never see.
                #
                #     scroll 1 -> 0    a reset HAPPENED
                #     scroll 0 -> 1    a boot COMPLETED  (~16.4 s later)
                #
                # The video lock was the obvious alternative and is wrong: the
                # capture loses lock on every 640x480-to-text transition, so a
                # game starting looks exactly like a reboot (OPEN-FAULTS
                # sec. 9). Scroll Lock has one other cause and it means the
                # same thing.
                was, now = prev.get("scrolllock"), values.get("scrolllock")
                if was and not now:
                    PROFILE.reset_seen()
                elif now and not was:
                    PROFILE.ready_pulse()
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

        **AND THAT RULE IS THIS CLASS'S ALONE -- DO NOT COPY IT BLIND.** The
        other four snapshots here (power, board, profile, files) do the
        OPPOSITE and are right to: every key always present, null where
        unknown. This one differs because it is the only capability that
        publishes SAMPLED MEASUREMENTS rather than status, and a plausible
        default in a measured field is indistinguishable from data. It was
        offered to another session as the house style; it is the exception to
        it. See Capability's docstring for the fork and which side a new
        surface belongs on.
        """
        supported, reason = self.support()
        if supported is False:
            return {"available": False, "why": "unsupported", "reason": reason}

        if supported is None:
            # Readable, but we cannot say the reading MEANS anything, because
            # we do not know which board is in. Values are withheld rather
            # than published with a caveat -- a caveat next to a number gets
            # dropped and the number does not.
            #
            # ANSWERED HERE, AHEAD OF EVERY CURRENCY QUESTION, because it used
            # to sit below them and the never-proven branch swallowed it: an
            # unidentified board reported `unproven`, which is a STRONGER
            # claim than this daemon can make -- it says the channel exists
            # and merely lacks proof. `unknown` says we cannot tell which
            # machine this is. A consumer told `unproven` goes looking for
            # something to press; told `unknown` it goes and looks at the
            # board. You cannot prove a channel you cannot say exists.
            return {"available": False, "why": "unknown", "reason": reason}
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

        if LedsCapability._proven_epoch is None:
            # NEVER PROVEN ON ANY EPOCH, which is not staleness and is the
            # case this gate missed entirely. A stale reading was real once
            # and belongs to an earlier epoch; this one belongs to NO epoch --
            # nothing ever wrote it, and re-reading cannot improve it. That is
            # the practical test for telling the two apart.
            #
            # `verify_input` is the way out and it reads the nodes directly
            # rather than through this gate, so it still works while this
            # branch is refusing. Without that there would be no way to prove
            # a channel this branch has closed.
            return {"available": False, "why": "unproven",
                    "reason": ("not proven since the daemon started -- "
                               "startup values, unconfirmed. "
                               "`verify_input` proves it")}

        if LedsCapability._proven_epoch != epoch:
            # The sharpest case, and the one a power-off check alone misses:
            # just after power returns, the nodes still hold the PREVIOUS
            # boot's values and the machine is on. This is the moment
            # wait_cold_boot() exists for -- readiness is the last thing a
            # healthy boot sets, so a level check for "ready" reads TRUE
            # 2.5 s after power-on on a machine that has not begun to POST.
            return {"available": False, "why": "unproven",
                    "reason": ("power changed at %s, unconfirmed since -- "
                               "values are from before that"
                               % (time.strftime("%H:%M:%S",
                                                time.localtime(changed_at))
                                  if changed_at else "an unknown time"))}

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

        # REFUSE ON UNPOWERED TOO -- MEASURED, not hypothetical. snapshot()
        # already gates on this (the "unpowered" branch below), but this
        # probe did not, and toggling Caps Lock with the target's mains cut
        # is not a no-op: it MOVES OUR OWN sysfs LED node regardless, because
        # that node belongs to the Pi's uinput device and something in the
        # Pi's own input stack echoes a keyboard's lock-key LED locally,
        # independent of whatever the external protocol board is doing.
        # `_sample()` cannot tell that local echo apart from a genuine
        # Set-LEDs reply carried back over PS/2 -- both are "the value moved
        # and moved back" on the same node.
        #
        # Caught live, 2026-08-25: the g2k's plug was off (`power state`:
        # on=false), `leds` correctly reported `why: unpowered`, and this
        # probe still returned `verified: true` -- `led-changes` showed the
        # exact toggle-then-restore it produced, recorded at epoch 0, the
        # unpowered epoch. The round trip this docstring promises -- "the
        # value returns only if the target's keyboard controller received
        # the key" -- was false for that reading, which is precisely the
        # class of bug this command exists to catch everywhere else.
        #
        # Same TARGET.state() snapshot.py already reads; not the _proven_epoch
        # gate just below in snapshot() (deliberately bypassed, per that
        # gate's own comment -- this probe is what proves a channel that gate
        # would otherwise never let out of "unproven"). Power is different:
        # an unpowered target can never legitimately move this node, so there
        # is no bootstrapping problem in refusing here the way there would be
        # for "unproven".
        _epoch, powered, _changed_at = TARGET.state()
        if powered is False:
            return {"ok": True, "verified": None,
                    "available": False,
                    "why": "unpowered",
                    "reason": ("the target has no power, so a round trip "
                               "cannot prove anything reached it -- see this "
                               "method's docstring for the measured false "
                               "positive this refusal closes"),
                    "note": ("not attempted -- toggling the key regardless "
                             "would still move our own uinput device's LED "
                             "node locally and could be misread as the "
                             "target's answer")}

        # THROUGH `_sample()`, NOT `read_leds()`, and that is the whole point
        # of this change. This probe is the only thing that can prove a
        # channel the gate has closed, but it used to observe the transition
        # with a raw read and tell nobody -- it set `verified_ok` and left
        # `_proven_epoch` alone. The 1 Hz poller is the only other writer, and
        # a press-then-press-back inside one poll interval is invisible to it:
        # same value either side, nothing recorded. So a successful proof
        # could leave the gate shut forever.
        #
        # `_sample()` is documented as the one place that decides what counts
        # as a change. Routing the probe through it means the proof is
        # recorded exactly like any other transition -- `_seen_values`,
        # `_changes` and `_proven_epoch` together -- instead of in a second
        # place that can disagree with the first.
        before = self._sample()
        try:
            self.devs.key(["capslock"])
        except Exception as exc:
            return {"ok": False, "error": "could not send: %s" % exc}
        changed, toggled, deadline = False, None, time.time() + 1.5
        while time.time() < deadline:
            cur = self._sample()
            if cur != before:
                # KEEP THE TOGGLED WORD. It is the first reading of this
                # sequence that is known to post-date a Set-LEDs, so it is the
                # only one we can compare against without trusting `before`.
                changed, toggled = True, cur
                break
            time.sleep(0.02)
        try:
            self.devs.key(["capslock"])          # put it back
        except Exception:
            pass
        # WAIT FOR THE RESTORE, DO NOT SNAPSHOT THROUGH IT. `after` used to be
        # a bare read taken immediately after sending the restore keystroke,
        # with nothing between the two. The VERDICT was never at risk --
        # `changed` comes from the polled loop above and its 1.5 s deadline --
        # but `after` is the field a reader quotes as "what the LEDs were left
        # at", and on a target slower than tonight's it would capture the
        # state before the restore landed and report the TOGGLED value as the
        # resting one.
        #
        # Same shape as the fault this whole function exists to expose: the
        # verdict waits for evidence and the number printed beside it does
        # not. Polled back to `before` with a deadline, and whatever the last
        # sample says is reported either way -- a restore that genuinely did
        # not land must show as not landed, not be waited into looking fine.
        # AND `restored` COMPARES AGAINST THE TOGGLED READING, NOT `before`.
        #
        # The first version of this waited for the word to come back to
        # `before`, which is wrong for the exact reason the fault above
        # documents: `before` may be STALE. Measured on hardware 2026-08-24,
        # first run of this code on the rig -- before {1,0,1}, after {0,1,1},
        # `restored: false` on a probe whose keystroke had been restored
        # perfectly. The round trip REFRESHED a stale word, so the settled
        # word could never equal `before` and the poll burned its whole
        # deadline to report a false alarm.
        #
        # `toggled` is a reading taken after the target answered, so it is not
        # stale. The caps bit moving AWAY from its value there is what "the
        # key went back" means, and it is checkable without trusting anything
        # read before the target spoke.
        #
        # `None` when nothing was ever seen to move: we cannot say a key came
        # back if we never saw it leave, and False would claim we could.
        settled = self._sample() or {}
        restored = None
        if toggled is not None:
            was = toggled.get("capslock")
            deadline = time.time() + 1.5
            while settled.get("capslock") == was and time.time() < deadline:
                time.sleep(0.02)
                settled = self._sample() or settled
            restored = settled.get("capslock") != was
        LedsCapability.verified_at = time.time()
        LedsCapability.verified_ok = changed
        if self.bus:
            self.bus.publish("input.verify", ok=changed)
        return {"ok": True, "verified": changed, "before": before,
                "after": settled,
                "restored": restored,
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
    """Mains control for the target.

    Touches no device and holds no lock, so it runs fully concurrently with
    input -- which matters, because `cycle` blocks for 15 s.

    The PROTOCOL is pluggable; the capability is not. Both entries below map to
    this same class, and `self.backend_name` selects which protocol object it
    builds. That is the honest factoring: switching from a Kasa plug to a Wemo
    or to a shell command changes how a relay is toggled, not what mains
    control means.

    BOARD-SCOPED, as of the fix docs/BOARD-IDENTITY.md sec. 5 named and left
    open: the configured plug is wired to ONE machine, and with a different
    USB4VC protocol board seated, `power cycle` used to cut that machine's
    mains regardless -- a Mac Plus installed on the rig still cycled the
    g2k's plug, a machine nobody asked about and possibly mid-run on a peer
    session. `support()` and the check at the top of `_power()` close this:
    a gated action (`on`/`off`/`cycle`) refuses unless the installed board is
    one `POWER_BOARDS` names, and an unknown board refuses too rather than
    assuming. `state` is unaffected -- it is never gated (see `_gated()`),
    and diagnosing a stuck run must never require knowing which board is
    seated.
    """

    name = "power"
    BACKENDS = {"kasa": None, "kasa-legacy": None, "kasa-klap": None,
                "wemo": None, "shell": None}          # filled in below

    # Mains control is the most consequential thing this rig can do, and the
    # event bus is in MEMORY. A daemon restart erases it -- and a restart is
    # exactly the event most likely to be happening around an unexplained power
    # change, so the record disappears precisely when it is needed.
    #
    # Found the hard way: the g2k was discovered powered off, and answering
    # "did anything here turn it off" required reasoning from absence rather
    # than reading a line. An append-only file survives restarts, reboots and
    # the ring wrapping.
    AUDIT = os.path.join(STATE_DIR, "power.log")

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

    def _protocol(self):
        """The protocol object for the chosen backend.

        Built lazily and not cached, so a settings change takes effect on the
        next call rather than at the next daemon restart -- these objects hold
        no connection and cost nothing to make.
        """
        impl = POWER_BACKENDS.get(
            _backend_alias(self.backend_name or "kasa"))
        if impl is None:
            raise IOError("power backend %r is not implemented"
                          % (self.backend_name,))
        return impl(self.settings or _cap_settings("power"))

    def start(self):
        # Off the main thread: the plug is on the LAN and a dead plug must not
        # delay or fail daemon startup.
        #
        # _profile_thread, NOT threading.Thread: this capability may belong to
        # a profile that is not the primary, and a bare thread would read the
        # PRIMARY's plug address for the rest of the daemon's life. See that
        # helper for what was measured.
        _profile_thread(self._heartbeat, name="power-id").start()

    def _heartbeat(self):
        while True:
            self._refresh()
            time.sleep(self.REFRESH_S)

    def _refresh(self):
        try:
            host = power_host()
            if host:
                self._remember(host, self._protocol().state())
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

    def support(self):
        """Is the configured plug wired to the INSTALLED board?

        Tri-state, same shape as LedsCapability.support() and for the same
        reason: `False` (known board, not this plug's) and `None` (board
        unknown) are different facts that need different responses, and
        collapsing them would mean an unknown board either blocks every
        power action forever or -- far worse -- defaults to allowing one.

        Read-only, and called from `_power()` only for the GATED actions.
        `power state` must keep working with no board known at all, because
        diagnosing a stuck run must never depend on already knowing which
        board is seated.
        """
        bid = installed_board_id()
        if bid is None:
            return None, ("board unknown, so it cannot be confirmed that "
                           "the configured power plug controls it -- "
                           "refusing rather than guessing which machine "
                           "this would power-cycle")
        if bid in POWER_BOARDS:
            return True, None
        return False, ("the configured power plug controls board(s) %s, "
                        "not the installed board %d -- refusing rather "
                        "than cycling the wrong machine's mains (see "
                        "docs/BOARD-IDENTITY.md sec. 5)"
                        % (list(POWER_BOARDS), bid))

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
            cfg_host = power_host()
        except Exception:
            pass
        # Informational, never gating: `board_match` tells a caller (a
        # preflight, a human reading /state.json) whether a WRITE to this
        # plug would be honoured right now, without making the READ above
        # depend on the board being known. See support().
        board_match, board_reason = self.support()
        st, t = self._seen, self._seen_t
        if not st:
            return {"host": cfg_host, "alias": None, "model": None,
                    "on": None, "power_mw": None,
                    "age_s": None, "stale": None,
                    "reason": "the plug has not answered since the daemon started",
                    "board_match": board_match, "board_reason": board_reason}
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
                # SAME NULLING RULE AS `on`, for the same reason. A wattage
                # from the last successful poll is a reading about a moment
                # that has passed, and a stale 45 W presented as current is a
                # worse lie than no number -- it says the machine is running
                # now. `age_s` is right there for anyone who wants the last
                # known value with its age; this field means "now".
                "power_mw": None if unreachable else st.get("power_mw"),
                "age_s": age,
                "stale": unreachable or age > self.STALE_S,
                "reason": self._fail,
                "board_match": board_match, "board_reason": board_reason}

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
        action = req.get("action", "state")
        if action in GATED_POWER_ACTIONS:
            # BOARD-SCOPED, CHECKED FIRST -- before the host is resolved,
            # before PROFILE is invalidated, before the plug is touched.
            # docs/BOARD-IDENTITY.md sec. 5: this plug is wired to one
            # machine, and cycling it while a different board is installed
            # is a power cycle of a machine nobody asked about, possibly
            # mid-run on a peer session. See PowerCapability's docstring and
            # support() for the two refusal reasons this can return.
            supported, why = self.support()
            if supported is not True:
                self._audit(action, req.get("as"), "REFUSED (board scope): %s" % why)
                return {"ok": False, "error": why,
                        "board_id": installed_board_id(),
                        "power_boards": list(POWER_BOARDS)}
        # ANY power action may have rebooted the target, so the profile
        # reading stops being about the machine that is running. Done here
        # rather than in a listener because the invalidation must not be able
        # to arrive after the reboot it describes.
        PROFILE.invalidate("power %s" % (req.get("action") or "action"))
        host = req.get("host") or power_host()
        if not host:
            return {"ok": False, "error":
                    "no power host configured -- set "
                    "capabilities.power.settings.host in %s"
                    % (CFG.source or "vcctrl.yaml (see vcctrl.example.yaml)")}
        if action == "state":
            st = dict(self._protocol().state())
            self._remember(host, st)
            # SAME TWO FIELDS snapshot() carries, on the SAME call a caller
            # would make right before attempting `on`/`off`/`cycle` -- not
            # only on /state.json's poll. Without this, `vcctrl power state`
            # (what a harness or MCP caller actually calls) answered a
            # different, thinner question than the web UI did, and the one
            # place this information is most useful -- deciding whether a
            # gated action is even worth attempting -- was the one place it
            # was missing.
            board_match, board_reason = self.support()
            st["board_match"], st["board_reason"] = board_match, board_reason
            return {"ok": True, "power": st}
        # Reads are not audited -- they happen on a timer from every open
        # browser tab and would bury the two lines that matter.
        self._audit(action, req.get("as"), "requested")
        if action == "on":
            self._protocol().set(True)
        elif action == "off":
            self._protocol().set(False)
        elif action == "cycle":
            # Deliberately unconditional: a wedged machine may report on while
            # being useless, so cycle means cycle rather than "on if off".
            self._protocol().set(False)
            time.sleep(float(req.get("off_seconds", POWER_CYCLE_OFF_S)))
            self._protocol().set(True)
        else:
            return {"ok": False, "error": "unknown power action: %r" % action}
        time.sleep(0.5)
        st = self._protocol().state()
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


def _split_mjpeg(buf):
    """Split complete JPEGs out of an MJPEG byte stream.

    Frames are delimited by SOI (FFD8) and EOI (FFD9). This is safe rather
    than merely usual: inside entropy-coded data every 0xFF is byte-stuffed
    as FF 00, and restart markers are FFD0-FFD7, so FFD9 appears only as a
    genuine EOI.

    Length is still validated, because partial writes happen on this path --
    one of the existing shot files on the Pi is zero bytes.

    Shared by every capability that owns a v4l2-ffmpeg MJPEG passthrough
    (VideoCapability, CameraCapability) -- the parsing is identical for any
    such device; only what each does with a finished frame differs.

    Returns (frames, remaining_buf): frames is a list of complete JPEG byte
    strings found in buf, in order; remaining_buf is what's left to prepend
    to the next read.
    """
    frames = []
    while True:
        i = buf.find(b"\xff\xd8")
        if i < 0:
            # No SOI in hand: nothing here is the start of a frame. Keep one
            # trailing byte in case FF and D8 straddle reads.
            buf = buf[-1:]
            break
        j = buf.find(b"\xff\xd9", i + 2)
        if j < 0:
            buf = buf[i:]
            break
        frame, buf = buf[i:j + 2], buf[j + 2:]
        if len(frame) >= 128:
            frames.append(frame)
    return frames, buf


def _split_h264_annexb(buf):
    """Split complete ACCESS UNITS (one decoded picture's worth of NALs)
    out of an Annex-B H.264 byte stream. Same (items, remaining) contract
    as `_split_mjpeg`, and shares its reason for existing: WebCodecs'
    `EncodedVideoChunk` is one picture per chunk, so whatever feeds it has
    to draw the same boundary libx264's own stdout does not draw for you.

    NAL START CODES are 3 bytes (`00 00 01`) or 4 (`00 00 00 01`); both
    appear in real ffmpeg output, sometimes in the SAME stream (measured:
    x264 emits 4-byte codes before SPS/PPS and 3-byte before SEI/slices).
    Safe to scan for byte-for-byte the same way `_split_mjpeg`'s SOI/EOI
    scan is: Annex-B's own emulation-prevention rule (a `03` byte inserted
    after any `00 00` followed by `00`/`01`/`02`/`03` inside a NAL's RBSP)
    guarantees a real `00 00 01` never occurs except at a genuine start
    code, so this never has to look inside entropy-coded slice data to
    tell a start code from picture content.

    ONE NAL IS NOT ONE PICTURE, and finding that out cost a false start:
    `-tune zerolatency` forces x264 into SLICED multi-threading (it cannot
    use frame-threading, which buffers whole frames and so adds the exact
    latency zerolatency exists to remove) -- measured against a real
    2-second/60-frame encode, ordinary slice NALs (type 1) outnumbered
    frames roughly 4:1, one run of four consecutive type-5 (IDR) NALs
    appeared for what was ONE picture, and naively treating every slice
    NAL as its own access unit produced 240 "pictures" from 60 real ones.
    The fix -- and this is the one bit of the spec this function actually
    leans on -- is `first_mb_in_slice`: a slice NAL's RBSP begins with
    this field as an Exp-Golomb code, and Exp-Golomb 0 is encoded as a
    single `1` bit, so the TOP BIT of the byte right after a slice NAL's
    1-byte header is 1 if and only if this slice is the FIRST slice of a
    new picture (`first_mb_in_slice == 0`) rather than a continuation
    slice of the picture already in progress. Checked against the same
    real encode: exactly 60 slice NALs have that bit set, matching the
    true frame count.

    HEADERS BELONG TO THE PICTURE THAT FOLLOWS THEM, NOT THE ONE BEFORE.
    SPS/PPS/SEI (`-x264-params repeat-headers=1` puts a fresh SPS+PPS
    before every IDR, so a joining viewer never waits past the next
    keyframe) arrive immediately BEFORE the slice they belong to, so an
    access unit boundary is the start of that NAL run, not the start of
    the slice itself -- getting this backwards silently drops every
    leading SPS/PPS/SEI into the PRECEDING access unit's tail instead of
    the one it configures, which is invisible until a decoder is actually
    fed the result and fails to configure from the first keyframe.
    """
    n = len(buf)
    starts = []
    i = 0
    while i < n - 3:
        if buf[i] == 0 and buf[i + 1] == 0:
            if buf[i + 2] == 1:
                starts.append((i, i + 3))
                i += 3
                continue
            elif buf[i + 2] == 0 and buf[i + 3] == 1:
                starts.append((i, i + 4))
                i += 4
                continue
        i += 1

    boundaries = []
    pending_run_start = None
    for (sc, payload_off) in starts:
        if payload_off + 1 >= n:
            # The NAL's own type byte, or the byte after it a slice needs
            # to read first_mb_in_slice, has not fully arrived. Stop here
            # rather than guess -- everything from this NAL's start code
            # onward stays in `remaining` until a later call has more.
            break
        nal_type = buf[payload_off] & 0x1F
        if nal_type in (1, 5):                       # non-IDR / IDR slice
            first_mb_bit = (buf[payload_off + 1] >> 7) & 1
            if first_mb_bit == 1:
                boundaries.append(pending_run_start
                                  if pending_run_start is not None else sc)
            pending_run_start = None
        elif pending_run_start is None:
            pending_run_start = sc

    aus = []
    if len(boundaries) >= 2:
        for i in range(len(boundaries) - 1):
            aus.append(bytes(buf[boundaries[i]:boundaries[i + 1]]))
        tail_start = boundaries[-1]
    elif len(boundaries) == 1:
        tail_start = boundaries[0]
    else:
        tail_start = 0
    return aus, bytes(buf[tail_start:])


def _au_is_keyframe(au):
    """Does this access unit (as `_split_h264_annexb` cuts them) contain an
    IDR slice (NAL type 5)? A joining viewer has to start on one -- feeding
    a decoder a delta frame first is feeding it a picture defined in terms
    of a reference frame it was never given."""
    n = len(au)
    i = 0
    while i < n - 3:
        if au[i] == 0 and au[i + 1] == 0:
            if au[i + 2] == 1:
                payload_off = i + 3
            elif au[i + 2] == 0 and au[i + 3] == 1:
                payload_off = i + 4
            else:
                i += 1
                continue
            if payload_off < n and (au[payload_off] & 0x1F) == 5:
                return True
            i = payload_off
            continue
        i += 1
    return False


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

    NOT YET FIXED, LEFT AS A NOTE RATHER THAN A CHANGE (docs/FINDINGS.md #45):
    the very first frame read after a v4l2 device is freshly opened can be a
    stale "locked, no source" placeholder even when a real signal is present
    -- measured on the SAME MacroSilicon chip family this class already
    reads, resyncing correctly about 2s later. This class's own multi-frame
    FROZEN_RUN check (8 consecutive identical frames) almost certainly
    already absorbs this for the steady-running case -- one stale frame
    right after a respawn is not 8 in a row -- but that has not been proven
    against an actual respawn, only reasoned about, and a caller that does
    ITS OWN single-shot open-and-read-one-frame (as this repo's own ad hoc
    Phase 0 testing did) gets no such protection at all. Left alone here
    rather than changed blind, in code this heavily measured already.
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
    DEVICE = CFG.default("capabilities.video.settings.device", "/dev/video0")
    # Class-level default, same reasoning as DEVICE above (and shadowed by
    # an instance attribute in start() the same way) -- so anything that
    # builds a VideoCapability without going through start() (tests, most
    # likely) still has a value here instead of raising on the first
    # _state() call.
    ANALOG = bool(CFG.default("capabilities.video.settings.analog", True))
    # Defaults to 640x480 -- the historical hardcoded value, correct for
    # the primary's VGA capture because it MATCHES the DOS target's own
    # native mode (see this file's own "the Gateway's stick emits 640x480
    # today" comment elsewhere), not because it's a good default in
    # general. A digital-source profile capturing a modern display's own
    # native resolution needs its own value here -- unlike
    # CameraCapability's equivalent (always the same one physical room
    # camera, correctly left hardcoded), this class now serves genuinely
    # different physical devices per profile, so it has to be a setting.
    # `-c:v copy` means ffmpeg does not scale or re-encode: this must be a
    # size the DEVICE ITSELF produces MJPEG at natively, not an arbitrary
    # request -- check with `v4l2-ctl --list-formats-ext` before changing
    # it for a given profile, the same way CameraCapability's own 1920x1080
    # was confirmed rather than assumed.
    RESOLUTION = CFG.default("capabilities.video.settings.resolution",
                             "640x480")
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
    # How many undecodable frames to keep on disk, and where. Twenty 60 KB
    # frames is about a megabyte -- enough to see a pattern, small enough that
    # a cable fault producing a steady stream of them cannot fill the card.
    # Twelve, not twenty: the observed rate is about one per 100,000 decode
    # attempts, so a dozen is many days of ordinary operation, and a degraded
    # cable producing a steady stream still cannot fill the card.
    BAD_KEEP = 12
    BAD_DIR = os.path.join(STATE_DIR, "badframes")
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
        #
        # ZERO WITH NO SIGNAL IS NOT EVIDENCE. When nothing is plugged in, the
        # capture stick emits a well-formed JPEG of its own no-lock constant,
        # so there is nothing malformed for this to count and it will sit at 0
        # no matter how long it runs. The counter only says anything while a
        # real source is being captured. Anyone reading a clean 0 the morning
        # after a dark night and closing the question has read the instrument's
        # state as the target's -- which is the failure this whole tool exists
        # to prevent, and it has caught four of us this evening already.
        self.decode_errs = 0
        # THE DENOMINATOR. A failure count with nothing counting attempts is
        # uninterpretable -- "zero failures" and "nothing was tried" are the
        # same reading, and I quoted the first while the second was closer to
        # true. It is emphatically not out of `frames`: most captured frames
        # are never decoded at all. They arrive, sit in the ring, and are
        # evicted without Pillow ever touching them.
        #
        # Per site as well as in total, because coverage is uneven by design:
        # with no browser connected only the watchdog's _is_picture runs, at
        # about two decodes a second, while the timeline path does 945 in one
        # request and only when a tab opens the reviewer.
        self.decode_attempts = 0
        self.decode_sites = {}
        self.decode_last = None
        # AND KEEP THE FRAME THAT BROKE.
        #
        # 2026-08-21 17:29:12 the first decode failure ever recorded arrived
        # -- "broken data stream when reading image file", seq 78658, off the
        # live capture with no stress and no fuzzing involved. By the time
        # anyone asked for that frame it had been evicted: the ring is 31
        # seconds deep and the counter had taken 140 s to be read.
        #
        # A counter says a frame broke. The frame itself can be examined,
        # replayed through the decoder, fuzzed against, and sent upstream. The
        # whole malformed-frame hypothesis has run on frames we MADE UP,
        # because no real one had ever been captured.
        #
        # Bounded hard, because this writes to the card from the capture path
        # and a degraded cable could produce these continuously.
        self.bad_frames = collections.deque(maxlen=self.BAD_KEEP)
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
        # WP7: a second, OPTIONAL consumer of this same JPEG ring, live only
        # while an H.264 viewer is connected (H264Sidecar.acquire/release,
        # called from vcweb.py's WS pump). Created unconditionally -- cheap,
        # no subprocess until the first viewer -- so `caps.video` reporting
        # and vcweb.py's own lookups never have to special-case its absence.
        self.h264 = H264Sidecar(self)

    # -- device lifecycle ---------------------------------------------------

    def start(self):
        # Resolved fresh HERE, shadowing the class attribute above -- not
        # because the class attribute is wrong for the primary profile (it
        # is, by construction: it's what gets baked in at import time from
        # whichever profile is bound to CFG then), but because a SECOND
        # profile's VideoCapability instance would otherwise share that same
        # class attribute and silently read the PRIMARY's video device.
        # Registry.__init__ constructs this inside _profile_scope(that
        # profile's cfg) (see _build_instance), so this resolves correctly
        # per profile; self.DEVICE (an instance attribute) then shadows
        # VideoCapability.DEVICE (the class attribute) for every other
        # method on THIS instance, which all already read `self.DEVICE`.
        self.DEVICE = CFG.default("capabilities.video.settings.device",
                                  "/dev/video0")
        # WHETHER "frozen" MEANS "no signal" DEPENDS ON THE SOURCE. The
        # "frozen -> NO SIGNAL" reading above (see the watchdog's own
        # comment) is a MEASURED fact about the analog VGA capture stick
        # specifically: analog sampling noise means a live, connected
        # signal never produces byte-identical frames, mean absolute
        # difference ~1 with peaks near 40 on a still DOS prompt -- so
        # identical frames are the stick's own "locked, no source" tell.
        # A DIGITAL capture (modernpc's HDMI dongle) has no such noise
        # floor: a genuinely static, fully-connected picture can produce
        # byte-identical frames forever, which this same page (daemon/
        # kvm.html) was labelling "NO SIGNAL" regardless -- caught live
        # 2026-09-01 when a real, responsive HDMI target sat at an idle
        # browser tab and the page insisted nothing was connected.
        # Defaults to True (the historical, still-correct behavior for
        # every VGA-stick profile that already exists) so nothing already
        # deployed changes; a digital-source profile's own config sets
        # this False and the page adjusts what "frozen" is allowed to mean
        # for it (see kvm.html's own use of this field).
        self.ANALOG = bool(CFG.default("capabilities.video.settings.analog",
                                       True))
        # Resolved fresh here too, same reasoning as DEVICE/ANALOG above --
        # this profile's own capture resolution, not whichever profile's
        # class attribute happened to be baked in at import time.
        self.RESOLUTION = CFG.default("capabilities.video.settings.resolution",
                                      "640x480")
        self.running = True
        self._acquire()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def stop(self):
        self.running = False
        self._release()
        self.h264.shutdown()

    def _acquire(self):
        with self.lock:
            if self.owned:
                return True
            try:
                self.proc = subprocess.Popen(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error",
                     "-f", "v4l2", "-input_format", "mjpeg",
                     "-video_size", self.RESOLUTION, "-framerate", "30",
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
        """Split JPEGs out of the mjpeg stream. See _split_mjpeg for why the
        SOI/EOI approach is safe."""
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
            frames, buf = _split_mjpeg(buf)
            for frame in frames:
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

    def _decoded(self, where, exc=None, seq=None, frame=None):
        """Record one decode ATTEMPT and whether it failed.

        Both halves, always. The first version of this counted only failures,
        and the zero it produced was quoted as "no decode failures across
        103,657 frames" -- a denominator that was never the denominator. Most
        frames are never decoded; and with no browser attached only one of the
        four call sites runs at all. Counting attempts is what turns the zero
        from a feeling into a measurement.
        """
        with self.lock:
            self.decode_attempts += 1
            st = self.decode_sites.get(where)
            if st is None:
                st = self.decode_sites[where] = {"n": 0, "err": 0}
            st["n"] += 1
            if exc is not None:
                self.decode_errs += 1
                st["err"] += 1
                self.decode_last = {
                    "where": where, "seq": seq,
                    "error": errstr(exc)[:160],
                    "t": round(time.time(), 3),
                }
        # Outside the lock: this touches the disk, and the capture path takes
        # the same lock thirty times a second.
        if exc is not None and frame:
            self._keep_bad_frame(where, seq, exc, frame)

    def _keep_bad_frame(self, where, seq, exc, frame):
        """Write an undecodable frame to disk. Bounded, and never fatal.

        A counter says a frame broke; the frame itself can be replayed through
        the decoder, fuzzed against, and sent upstream. Every malformed frame
        this project has ever examined was one we manufactured, because no real
        one had ever been kept -- the first real failure was evicted 140
        seconds after it happened, before anyone could ask for it.

        Failures here are swallowed on purpose. A full disk or a bad path must
        not take down capture to preserve a diagnostic about capture.
        """
        try:
            os.makedirs(self.BAD_DIR, exist_ok=True)
            now = time.time()
            # A MONOTONIC ORDINAL, not a finer clock. Seconds collided, so
            # two failures in one second overwrote each other and the on-disk
            # count fell behind the in-memory record -- which then unlinked a
            # file a live record still pointed at. Milliseconds collided too:
            # a burst runs faster than 1 ms, and twelve records shared three
            # files. Any time-based name is a guess about the arrival rate,
            # and the rate that matters is the one during a cable fault --
            # exactly when nobody wants to debug the diagnostic.
            #
            # decode_errs is strictly increasing and already incremented by
            # the caller, so it cannot repeat however fast they arrive.
            with self.lock:
                ordinal = self.decode_errs
            name = "%s/%06d_%d_%s_%s.jpg" % (
                self.BAD_DIR, ordinal, int(now),
                seq if seq is not None else "noseq", where)
            with open(name, "wb") as f:
                f.write(frame)
            # PROVENANCE TRAVELS WITH THE ARTIFACT. A bare .jpg in a directory
            # in three weeks is an orphan; the same file with its seq, wall
            # time, site and exception beside it is evidence. This is the
            # GMQ3 lesson -- a recording that could not say which cell it
            # belonged to produced a confident, wrong refutation -- applied
            # before it costs anything instead of after.
            with open(name[:-4] + ".json", "w") as f:
                json.dump({
                    "file": os.path.basename(name),
                    "seq": seq, "where": where,
                    "t": round(now, 3),
                    "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime(now)),
                    "bytes": len(frame),
                    "error": errstr(exc)[:300],
                    "sha256": hashlib.sha256(frame).hexdigest(),
                    "starts_soi": frame[:2] == b"\xff\xd8",
                    "ends_eoi": frame[-2:] == b"\xff\xd9",
                }, f, indent=1, sort_keys=True)
            with self.lock:
                old = None
                if len(self.bad_frames) == self.bad_frames.maxlen:
                    old = self.bad_frames[0]
                self.bad_frames.append({
                    "file": name, "seq": seq, "where": where,
                    "bytes": len(frame), "t": round(time.time(), 3),
                    "error": errstr(exc)[:160]})
            # The deque bounds the RECORD; the directory needs bounding too, or
            # the count is a fiction and the card fills anyway.
            if old:
                for path in (old["file"], old["file"][:-4] + ".json"):
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        except Exception:
            pass

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
                self._decoded("timeline")
            except Exception as exc:
                self._decoded("timeline", exc, sq, frame=f)
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

    def buffer_avi(self, first=None, last=None, since=None, clip=False):
        """Snapshot the ring and mux it into a playable AVI.

        `since` IS THE CALLER'S OWN START TIME, and passing it turns this from
        a dump of whatever the ring holds into a recording of YOUR run.

        GMQ3-glass.avi is 815 MB and looks like a recording of cell MQ3.
        18,871 of its 19,976 frame packets are byte-identical to frames in
        GMQ2-glass.avi -- 94.5%, because the ring was holding 1,193 s against
        the 480 s it was asked for, so consecutive dumps necessarily overlapped.
        A peer sampled it, found real game frames, and reported a finding
        refuted. The frames were real, correctly identified, and the PREVIOUS
        CELL'S.

        The metadata said so the whole time: first_seq and first_t have always
        been in the returned meta, and a reader could have compared them
        against the cell's own start. Nobody did, because nothing made them.
        That is the same defect as a lock check whose answer nothing consumes
        -- the information existed, was correct, and was not load-bearing.

        So the check moves to the point of WRITING. Pass `since` and a window
        that opens before it is refused, with the overlap measured. Pass
        `clip=True` to take the part that is yours and be told what was cut.
        Pass neither and you get the old behaviour, which is correct for "show
        me the buffer" and wrong for "record my run".

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

        # PROVENANCE, BEFORE ANY BYTES ARE WRITTEN.
        if since is not None and items[0][0] < since:
            older = [i for i in items if i[0] < since]
            gap = round(since - items[0][0], 3)
            if not clip:
                return None, {
                    "error": "the buffer opens %.3fs before the caller's own "
                             "start: %d of %d frames predate it and belong to "
                             "whatever ran before. Pass clip to take only "
                             "yours." % (gap, len(older), len(items)),
                    "refused": "window_precedes_caller",
                    "since": round(since, 3),
                    "first_t": round(items[0][0], 3),
                    "older_by_s": gap,
                    "foreign_frames": len(older),
                    "frames": len(items),
                    "first_seq": items[0][1], "last_seq": items[-1][1],
                }
            items = [i for i in items if i[0] >= since]
            clipped = len(older)
            if not items:
                return None, {"error": "nothing in the buffer is newer than "
                                       "the caller's start time",
                              "refused": "nothing_after_since",
                              "clipped_frames": clipped}
        else:
            clipped = 0

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
            # Always present, so a reader never has to know whether to look.
            # 0 is a fact -- "nothing was cut" -- and null would be a question.
            "clipped_frames": clipped,
            "since": round(since, 3) if since is not None else None,
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

        Ported from grab() in bin/vcctrl_common.py so the two cannot drift,
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

        NONE OF THAT HOLDS ON A DIGITAL SOURCE (`self.ANALOG` False, see
        start()'s own comment on the setting). HDMI has no sampling noise
        floor -- a genuinely static, fully-connected picture sits
        byte-identical for as long as nothing on screen changes, so a
        digest match there is evidence of a still picture, not evidence of
        no signal. Commit 0b8a051 already fixed this for the STATUS label
        (`frozen` vs `no signal`); this method still applied the analog
        rule to the shot judgement itself, which is what made `shot`
        refuse a perfectly healthy static digital screen with "every frame
        in the window was a duplicate" -- caught live 2026-09-02 against
        modernpc's locked desktop. On a digital source every frame in the
        window is a live candidate; `_is_picture`'s flat-frame check below
        (MIN_RANGE) is still what catches a genuinely blank capture.
        """
        if self.ANALOG:
            digests = {}
            for t, _sq, f in items:
                digests.setdefault(hashlib.md5(f).hexdigest(), []).append((t, f))
            live = [tf for group in digests.values() if len(group) == 1
                    for tf in group]
            if not live:
                return None, None, "every frame in the window was a duplicate"
        else:
            live = [(t, f) for t, _sq, f in items]
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
                self._decoded("shot")
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
                #
                # THREE TIMES. The local `errs` list dies with the call, so
                # when the decode counter was added this block kept losing the
                # very thing the counter exists to count -- and I described
                # the coverage as "all four call sites" while this one was
                # never wired. It is the path the harness hits on every
                # mid-cell shot, so it was also the busiest one missing.
                self._decoded("shot", exc, frame=f)
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
            self._decoded("is_picture")
            return (hi - lo) >= self.MIN_RANGE
        except Exception as exc:
            self._decoded("is_picture", exc, frame=frame)
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
                self._decoded("lastgood")
            except Exception as exc:
                self._decoded("lastgood", exc, frame=frame)
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
                    # Whether "frozen" (below) is trustworthy evidence of no
                    # signal for THIS source -- see start()'s own comment.
                    "analog": self.ANALOG,
                    "pinned": bool(self.pin_holders),
                    # Frames that would not decode. Zero is a real answer here
                    # and an interesting one: it says the capture pipe has
                    # never handed Pillow anything malformed, which is the
                    # standing hypothesis for an abort nobody has explained.
                    "decode_errs": self.decode_errs,
                    # WITH ITS DENOMINATOR, always, and per site -- because
                    # coverage is uneven and a total hides that. With no
                    # browser attached only is_picture runs.
                    "decode_attempts": self.decode_attempts,
                    "decode_sites": dict(self.decode_sites),
                    "decode_last": self.decode_last,
                    # What is actually ON DISK, so nobody has to guess whether
                    # the keeping worked. Names the files; the bytes are in
                    # BAD_DIR on the Pi.
                    "bad_frames_kept": len(self.bad_frames),
                    "bad_frames": list(self.bad_frames)[-5:],
                    "span_s": round(self.ring[-1][0] - self.ring[0][0], 2)
                    if len(self.ring) > 1 else 0.0,
                    # What the buffer is ASKED to keep, beside what it is
                    # actually keeping. A page that offers to change this has
                    # to read the live value back rather than remember its own,
                    # because the buffer belongs to the daemon and every viewer
                    # shares one.
                    "target_span_s": self.TARGET_SPAN_S,
                    "mem_limited": self.mem_limited,
                    "last_frame_age_s": round(age, 3) if age else None,
                    "h264": self.h264.state()}

    def _latest(self):
        """(timestamp, jpeg_bytes) of the newest ring frame, or (None, None).

        The uniform read `_mjpeg()` in vcweb.py uses for any capability that
        can hand it a most-recent frame -- CameraCapability implements the
        same method over its own, smaller state.
        """
        with self.lock:
            return (self.ring[-1][0], self.ring[-1][2]) if self.ring \
                else (None, None)

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


_THROTTLE_CACHE = [0.0, False]     # [checked_at, currently_throttled]
_THROTTLE_CACHE_S = 5.0            # matches vcweb.py's own host-facts cache


def _currently_throttled():
    """Bit 2 of `vcgencmd get_throttled` -- "throttled now", not "ever
    since boot" (bit 18). A cheap, independent read: WP7's guard rail does
    not reach into vcweb.py's own host-facts cache (a request-driven cache
    in a different module, for a human-facing readout) for the same reason
    MsdCapability resolves its own settings instead of sharing a class
    attribute -- two callers with different questions sharing one cache is
    how one of them ends up reading the other's answer.
    """
    now = time.time()
    if now - _THROTTLE_CACHE[0] < _THROTTLE_CACHE_S:
        return _THROTTLE_CACHE[1]
    throttled = False
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"],
                             capture_output=True, text=True,
                             timeout=2).stdout.strip()
        if "=" in out:
            throttled = bool(int(out.split("=", 1)[1], 16) & 0x4)
    except Exception:
        pass       # non-Pi host, or vcgencmd absent: reads as not-throttled
    _THROTTLE_CACHE[0] = now
    _THROTTLE_CACHE[1] = throttled
    return throttled


class H264Sidecar(object):
    """Transcodes a VideoCapability's own JPEG ring to H.264, live only
    while at least one viewer wants it (`acquire()`/`release()`,
    ref-counted -- called from vcweb.py's WS pump on connect/disconnect).

    A SECOND CONSUMER OF THE RING, NOT A SECOND CAPTURE. Feeds from
    `video.ring[-1]`, the exact bytes the MJPEG path already reads --
    `-c:v copy` on the primary capture means nothing here ever asks the
    stick for more than one encode's worth of work. FINDINGS #48 measured
    libx264 ultrafast/zerolatency at up to ~2/3 of a core on synthetic
    1080p30 content; that cost is not worth paying with nobody watching,
    the same reasoning Video/CameraCapability's own subprocess only exists
    while `owned`, just gated on viewer count here instead of on daemon
    lifetime.

    `-tune zerolatency` (forces sliced multi-threading, not frame-
    threading, so latency does not grow with thread count) and
    `-x264-params repeat-headers=1` (a fresh SPS+PPS before every IDR, so
    ANY joining viewer's wait is bounded by the GOP, not by whether they
    happened to connect before the first one) are both load-bearing, not
    tuning knobs -- see `_split_h264_annexb`'s own docstring for what the
    first actually does to the output shape.

    ONE GUARD RAIL, NOT TWO. The plan
    (internal/KVM-MACHINES-PLAN.md WP7) asks for a two-stage drop (720p,
    then MJPEG) when `vcgencmd get_throttled` reports the Pi currently
    throttled. This implements the second stage only -- `_watchdog` kills
    the encoder outright and refuses new viewers while throttled, which a
    connected viewer's own page reads as an encoder that stopped sending
    and falls back to MJPEG the same way it already does for a stalled
    WebSocket. A resolution step in between is real, useful polish that
    did not make this pass -- recorded here rather than silently dropped.
    """

    AU_RING_LEN = 90           # ~3s at 30fps -- a joining viewer's own window
    FEED_POLL_S = 1.0 / 60     # finer than the 30fps source so a new ring
                               # frame is picked up within one tick, not one
                               # source frame late
    WATCHDOG_POLL_S = 2.0

    def __init__(self, video):
        self.video = video
        self.lock = threading.Lock()
        self.viewers = 0
        self.proc = None
        self.running = False
        self.au_ring = collections.deque(maxlen=self.AU_RING_LEN)
        self.seq = 0
        self.spawns = 0
        self.last_error = None
        self.throttled_refusal = False
        self._watchdog_started = False

    def state(self):
        with self.lock:
            return {"active": self.running, "viewers": self.viewers,
                    "spawns": self.spawns, "last_error": self.last_error,
                    "throttled_refusal": self.throttled_refusal,
                    "au_ring_len": len(self.au_ring)}

    # -- lifecycle, ref-counted by connected viewers -----------------------

    def acquire(self):
        """True if a viewer may proceed; False (with `last_error` set) if
        refused -- currently throttled, or the spawn itself failed."""
        if not self._watchdog_started:
            self._watchdog_started = True
            threading.Thread(target=self._watchdog, daemon=True).start()
        with self.lock:
            if _currently_throttled():
                self.throttled_refusal = True
                self.last_error = ("Pi currently throttled "
                                   "(vcgencmd get_throttled) -- refusing a "
                                   "new H.264 viewer rather than adding load")
                return False
            self.throttled_refusal = False
            self.viewers += 1
            # NOT gated on the viewer count crossing 0 -> 1: a viewer that
            # is still nominally "acquired" (never called release()) after
            # the watchdog kills the encoder for thermal reasons must not
            # permanently wedge this sidecar into "never respawns" just
            # because the count never returned to zero. Gated on whether an
            # encoder is actually alive instead, which is the real question.
            if not self.running:
                self._spawn_locked()
            return self.proc is not None

    def release(self):
        with self.lock:
            self.viewers = max(0, self.viewers - 1)
            if self.viewers == 0:
                self._kill_locked()

    def shutdown(self):
        """Called from VideoCapability.stop() -- the daemon is going away,
        not merely "no viewers right now"."""
        with self.lock:
            self.viewers = 0
            self._kill_locked()

    def _spawn_locked(self):
        """Caller holds self.lock."""
        try:
            self.proc = subprocess.Popen(
                ["nice", "-n", "5", "ffmpeg", "-hide_banner",
                 "-loglevel", "error", "-f", "mjpeg", "-i", "pipe:0",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-tune", "zerolatency", "-g", "30", "-pix_fmt", "yuv420p",
                 "-x264-params", "repeat-headers=1",
                 "-f", "h264", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
        except Exception as exc:
            self.last_error = errstr(exc)
            self.proc = None
            return
        self.running = True
        self.spawns += 1
        self.last_error = None
        self.au_ring.clear()
        threading.Thread(target=_keep_stderr, args=(self, self.proc),
                         daemon=True).start()
        threading.Thread(target=self._feed, args=(self.proc,),
                         daemon=True).start()
        threading.Thread(target=self._read_nals, args=(self.proc,),
                         daemon=True).start()

    def _kill_locked(self):
        """Caller holds self.lock."""
        self.running = False
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass

    # -- feeding JPEGs in, reading H.264 out --------------------------------

    def _feed(self, proc):
        """Tails video.ring, writing each frame ONCE to ffmpeg's stdin as
        it arrives -- not a re-read of frames already sent. `last_t` is
        this thread's own bookmark, independent of the MJPEG path's."""
        last_t = 0.0
        while self.running and proc.poll() is None:
            with self.video.lock:
                item = self.video.ring[-1] if self.video.ring else None
            if item is not None and item[0] > last_t:
                last_t = item[0]
                try:
                    proc.stdin.write(item[2])
                except Exception:
                    break
            else:
                time.sleep(self.FEED_POLL_S)

    def _read_nals(self, proc):
        buf = b""
        fd = proc.stdout.fileno()
        while self.running:
            try:
                chunk = os.read(fd, 65536)
            except Exception as exc:
                with self.lock:
                    self.last_error = errstr(exc, "reader: ")
                break
            if not chunk:
                break
            buf += chunk
            aus, buf = _split_h264_annexb(buf)
            for au in aus:
                self._push(au)
        with self.lock:
            self.running = False

    def _push(self, au):
        with self.lock:
            self.seq += 1
            self.au_ring.append((time.time(), self.seq, au,
                                 _au_is_keyframe(au)))

    def latest(self):
        """(seq, keyframe) of the newest AU, or (0, False)."""
        with self.lock:
            if not self.au_ring:
                return 0, False
            t, seq, au, kf = self.au_ring[-1]
            return seq, kf

    def au_after(self, since_seq):
        """The oldest AU newer than `since_seq`, and whether it is a
        keyframe -- (seq, au_bytes, is_keyframe) or None. A viewer that has
        never received anything (since_seq == 0) is handed the ring's own
        most recent KEYFRAME rather than its most recent AU: starting a
        brand-new decoder on a delta frame is starting it on a picture
        defined relative to a reference frame it was never given."""
        with self.lock:
            if since_seq == 0:
                for (_t, seq, au, kf) in reversed(self.au_ring):
                    if kf:
                        return seq, au, True
                return None
            for (_t, seq, au, kf) in self.au_ring:
                if seq > since_seq:
                    return seq, au, kf
        return None

    # -- the guard rail -------------------------------------------------

    def _watchdog(self):
        """Runs for the life of the daemon (the thread is only started
        once, on the first acquire()), not just while a viewer is
        connected -- a throttled-now Pi with an encoder mid-spawn needs
        this to fire even if nobody is polling `state()`."""
        while True:
            time.sleep(self.WATCHDOG_POLL_S)
            with self.lock:
                if self.running and _currently_throttled():
                    self.throttled_refusal = True
                    self.last_error = ("Pi currently throttled -- stopping "
                                       "the H.264 encoder")
                    self._kill_locked()


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

    DEVICE = CFG.default("capabilities.audio.settings.device", "hw:1,0")
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
    # The Opus side-stream. 128k is transparent-with-margin for this source
    # (FM/SB output through an analog path with a -30 dB noise floor) and
    # still ~12x under raw PCM; the public mirror is the intended consumer.
    # A string because it goes straight into ffmpeg's -b:a.
    OPUS_BITRATE = str(CFG.default("capabilities.audio.settings.opus_bitrate",
                                   "128k"))
    # ~32 s at 128 kbit/s. Same bytes-not-count bounding as the PCM ring.
    OPUS_RING_BYTES = 512 * 1024
    # The frequency-analysis constants and math now live in
    # common/audio_bands.py, shared with the control host (bin/, agent/) --
    # see that module's own comments for what each one means and how it was
    # calibrated (docs/FINDINGS.md sec 42). These stay as class attributes
    # so existing call sites (and tests/test_core.py) don't need to change
    # what they reference.
    BAND_HZ = audio_bands.BAND_HZ
    SPECTRUM_FFT_N = audio_bands.SPECTRUM_FFT_N
    BAND_ACTIVE_MARGIN_DB = audio_bands.BAND_ACTIVE_MARGIN_DB
    BAND_FLOOR_DB = audio_bands.BAND_FLOOR_DB

    def __init__(self, devs, bus=None):
        Capability.__init__(self, devs)
        self._hann_cache = audio_bands.hann(self.SPECTRUM_FFT_N)
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
        # The Opus side-stream, all under its own lock (never nested inside
        # self.lock by any thread that also takes self.lock -- the feed
        # thread takes only self.lock, the read thread only opus_lock).
        self.opus_lock = threading.Lock()
        self.opus_ring = collections.deque()
        self.opus_ring_bytes = 0
        self.opus_seq = 0
        self.opus_headers = []       # OpusHead/OpusTags pages, current stream
        self.opus_headers_done = False
        self.opus_generation = 0     # bumped per encoder spawn
        self.opus_proc = None
        self.opus_listeners = 0
        self.opus_last_error = None

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        self.running = True
        self._acquire()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def stop(self):
        self.running = False
        self._release()
        with self.opus_lock:
            self._opus_kill_locked()

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

    # -- the Opus side-stream -----------------------------------------------
    #
    # A second, opt-in encoding of the same capture, for consumers where raw
    # PCM's 1.536 Mbit/s actually costs something -- in practice the public
    # mirror, whose every listener pays that over the funnel. One encoder,
    # spawned on the first Opus listener and killed on the last, feeding a
    # page ring the same way the capture ffmpeg feeds the PCM ring. The
    # capture device is NOT touched: input is the existing PCM ring, so
    # `hw:1,0` stays single-open and `bin/vcctrl-audio`'s shim contract holds.
    #
    # One WHOLE Ogg page per ring entry, because the two things this stream
    # needs beyond PCM only work at page granularity: replaying the
    # OpusHead/OpusTags pages to a listener who joins a running stream, and
    # skipping a listener who falls behind without corrupting the stream
    # (a missing page is a resyncable discontinuity; verified against the
    # browser-side decoder, docs/WEBKVM-AUDIO.md's Opus spike). -page_duration
    # matters: the Ogg muxer's default is one full second of buffering per
    # page, which would put a second of latency between the machine and every
    # public listener.

    def opus_attach(self):
        """Register an Opus listener; the first one spawns the encoder.
        Returns the encoder generation this listener must follow -- a
        listener that sees the generation move on must drop, because pages
        from a new encoder cannot follow another stream's headers."""
        with self.opus_lock:
            self.opus_listeners += 1
            if self.opus_proc is None:
                self._opus_spawn_locked()
            return self.opus_generation

    def opus_detach(self):
        with self.opus_lock:
            self.opus_listeners = max(0, self.opus_listeners - 1)
            if self.opus_listeners == 0:
                self._opus_kill_locked()

    def _opus_spawn_locked(self):
        """Caller holds opus_lock."""
        try:
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error",
                 # WITHOUT THESE TWO, THE FIRST PAGE TAKES ~4.5 SECONDS:
                 # ffmpeg runs stream analysis (analyzeduration, default
                 # 5 s) on every input, even raw PCM whose format this
                 # command fully specifies -- and because the feeder pipes
                 # input at CAPTURE PACE, "analyze 5 s of input" means
                 # "sit for ~5 real seconds before writing even the Ogg
                 # header". Measured 2026-08-28, first page 4.5 s -> 0.09 s
                 # with these flags; every fresh-encoder unmute paid that
                 # wait as silence. There is nothing to analyze: rate,
                 # channels and sample format are all given.
                 "-probesize", "32", "-analyzeduration", "0",
                 "-f", "s16le", "-ar", str(self.RATE),
                 "-ac", str(self.CHANNELS), "-i", "pipe:0",
                 "-c:a", "libopus", "-b:a", self.OPUS_BITRATE,
                 "-frame_duration", "20",
                 "-f", "ogg", "-page_duration", "20000", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0)
        except Exception as exc:
            self.opus_last_error = errstr(exc)
            return
        self.opus_proc = proc
        self.opus_generation += 1
        self.opus_headers = []
        self.opus_headers_done = False
        self.opus_ring.clear()
        self.opus_ring_bytes = 0
        gen = self.opus_generation
        threading.Thread(target=self._opus_feed, args=(proc,),
                         daemon=True, name="opus-feed").start()
        threading.Thread(target=self._opus_read, args=(proc, gen),
                         daemon=True, name="opus-read").start()
        threading.Thread(target=self._opus_stderr, args=(proc,),
                         daemon=True, name="opus-stderr").start()
        self._publish("audio.opus_started", generation=gen,
                      bitrate=self.OPUS_BITRATE)

    def _opus_kill_locked(self):
        """Caller holds opus_lock.

        BUMPS THE GENERATION, same as a spawn does: the reader thread gates
        every ring/header write on the generation it was born with, and
        without the bump it kept flushing its buffered pages into a ring
        this method had just cleared -- caught by the encoder test, as stale
        pages sitting where the next attach expects nothing."""
        self.opus_generation += 1
        proc, self.opus_proc = self.opus_proc, None
        self.opus_ring.clear()
        self.opus_ring_bytes = 0
        self.opus_headers = []
        self.opus_headers_done = False
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass
            self._publish("audio.opus_stopped")

    def _opus_feed(self, proc):
        """Follow the PCM ring from the live edge into the encoder's stdin.

        Applies the same skip-ahead rule as serve_ws_audio (vcweb.py): an
        encoder that fell badly behind is never allowed to catch up by
        encoding a backlog, because late audio is worth nothing for
        monitoring -- skip to near-now and let the discontinuity through.
        """
        with self.lock:
            last = self.seq
        while proc.poll() is None:
            with self.lock:
                pending = [(sq, c) for _t, sq, c in self.ring if sq > last]
            if not pending:
                time.sleep(0.005)
                continue
            if len(pending) > 40:          # ~0.8 s behind
                pending = pending[-10:]
            try:
                for sq, chunk in pending:
                    proc.stdin.write(chunk)
                    last = sq
            except Exception:
                return

    def _opus_read(self, proc, gen):
        """Split the encoder's stdout into whole Ogg pages; cache the header
        pages, ring the rest. On EOF with listeners still attached, respawn
        -- that EOF was a crash, not the last-listener shutdown."""
        splitter = ogg_pages.OggPageSplitter()
        fd = proc.stdout.fileno()
        while True:
            try:
                data = os.read(fd, 8192)
            except Exception:
                data = b""
            if not data:
                break
            for page in splitter.feed(data):
                now = time.time()
                with self.opus_lock:
                    if self.opus_generation != gen:
                        return
                    if not self.opus_headers_done:
                        # Header pages carry granule 0 (OpusHead, OpusTags);
                        # the first page with a real granule position is the
                        # first audio page and closes the header set.
                        if ogg_pages.page_granule(page) == 0:
                            self.opus_headers.append(page)
                            continue
                        self.opus_headers_done = True
                    self.opus_seq += 1
                    self.opus_ring.append((now, self.opus_seq, page))
                    self.opus_ring_bytes += len(page)
                    while (self.opus_ring_bytes > self.OPUS_RING_BYTES
                           and len(self.opus_ring) > 1):
                        _t, _s, old = self.opus_ring.popleft()
                        self.opus_ring_bytes -= len(old)
        with self.opus_lock:
            if self.opus_proc is proc:
                self._opus_kill_locked()
                if self.opus_listeners > 0 and self.running:
                    self._opus_spawn_locked()

    def _opus_stderr(self, proc):
        """First lines, not last -- same reasoning as _keep_stderr, but into
        its own field: this is the ENCODER's account, and it must not
        overwrite last_error, which is the CAPTURE's."""
        kept = []
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line and line not in kept:
                    kept.append(line)
                    if len(kept) >= 4:
                        break
        except Exception:
            pass
        if kept:
            self.opus_last_error = " | ".join(kept)[:240]

    # -- levels, computed on demand -----------------------------------------

    def _ring_window(self, ms):
        """The last `ms` worth of the PCM ring, joined into one fragment.

        Shared by `_levels` and `_spectrum` -- both need the identical slice
        of recent audio, and reading it twice with two hand-written loops is
        how they'd quietly drift apart (one gets fixed for an edge case, the
        other doesn't).
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
        return b"".join(reversed(chunks))

    def _levels(self, ms=3000):
        """RMS and peak in dBFS over the last `ms`, plus a bucket histogram.

        Matches what `ffmpeg -af volumedetect` reports, because
        `bin/vcctrl-audio` reads mean_volume, max_volume and the histogram
        bucket count, and its verdicts are tuned to those numbers. Changing the
        scale would silently invalidate every reference level in FINDINGS.
        """
        frag = self._ring_window(ms)
        if not frag:
            return None

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

    def _spectrum(self, ms=3000):
        """Per-band energy over the last `ms`, and how many bands are active.

        A COMPLEMENT to `_levels`, not a replacement: amplitude alone cannot
        tell a music/SFX signal apart from a steady tone or hum that happens
        to sit at a similar level -- `bin/vcctrl-audio`'s existing crest-
        factor note is an indirect proxy for exactly that gap. This measures
        it directly: real content spreads energy across multiple frequency
        bands, a pure tone or mains hum concentrates it in one.

        Unlike mean_db/peak_db, `active_bands` is a comparison of bands to
        EACH OTHER, not to an absolute scale -- so unlike the rest of this
        file, it stays meaningful regardless of where the physical volume
        knob sits (WEBKVM-AUDIO.md sec 4 / FINDINGS sec 11). The per-band
        dB figures are still informational-only, same as the histogram.
        """
        frag = self._ring_window(ms)
        if not frag:
            return None
        import array as _array
        a = _array.array("h")
        a.frombytes(frag[:len(frag) // 2 * 2])
        if sys.byteorder == "big":
            a.byteswap()
        frames = len(a) // self.CHANNELS
        if frames < 2:
            return None

        # Downmix to mono. _levels can treat interleaved L/R as one long
        # sequence because a scalar RMS/peak doesn't care about sample
        # ORDER -- a spectrum absolutely does, and alternating L/R samples
        # into one sequence would alias real content into fake high-
        # frequency energy here. Hardware is fixed at 2ch (class docstring),
        # so this pairs samples directly rather than looping CHANNELS times.
        mono = [(a[2 * i] + a[2 * i + 1]) * 0.5 for i in range(frames)]

        # The FFT/Hann/band-binning math itself lives in common/audio_bands.py
        # now, shared with the control host -- see that module for how a
        # chunk becomes one band_db figure. getattr, not a bare attribute
        # reference: a test double that skips __init__ (see
        # tests/test_core.py's Fake(AudioCapability)) would otherwise raise
        # here instead of exercising the real computation.
        window = getattr(self, "_hann_cache", None) or audio_bands.hann(self.SPECTRUM_FFT_N)
        band_db = audio_bands.band_db_from_mono(
            mono, rate=self.RATE, n_fft=self.SPECTRUM_FFT_N,
            band_hz=self.BAND_HZ, window=window)
        if band_db is None:
            return None
        active = audio_bands.active_bands(
            band_db, margin_db=self.BAND_ACTIVE_MARGIN_DB,
            floor_db=self.BAND_FLOOR_DB)
        return {"bands_hz": list(self.BAND_HZ), "band_db": band_db,
                "active_bands": active,
                "window_ms": round(frames * 1000.0 / self.RATE, 1)}

    # -- commands -----------------------------------------------------------

    def commands(self):
        return {"audio": self._audio, "level": self._level,
                "spectrum": self._spectrum_cmd}

    def _state(self):
        # opus_lock before self.lock, never nested: the two dicts describe
        # two independent processes and a torn read between them is fine.
        with self.opus_lock:
            opus = {"running": self.opus_proc is not None,
                    "listeners": self.opus_listeners,
                    "generation": self.opus_generation,
                    "bitrate": self.OPUS_BITRATE,
                    "ring_pages": len(self.opus_ring),
                    "last_error": self.opus_last_error}
        with self.lock:
            age = (time.time() - self.last_chunk_t) if self.last_chunk_t else None
            return {"state": self.state, "owned": self.owned,
                    "opus": opus,
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

    def _spectrum_cmd(self, req):
        sp = self._spectrum(int(req.get("ms", 3000)))
        if sp is None:
            return {"ok": True, "spectrum": None, "state": self.state,
                    "reason": "no audio in the ring"}
        return dict({"ok": True, "state": self.state}, **sp)


class CameraCapability(Capability):
    """A second, independent UVC camera pointed at the physical target, not
    its captured video signal -- a photographic "is the room really doing
    what the emulated capture says" companion view, not a replacement for it.

    Deliberately a slimmed-down sibling of VideoCapability, not a copy.
    VideoCapability's ring buffer, frozen-frame detection, uniform-frame
    rejection and pin/span/scrub machinery are all calibrated to the ANALOG
    capture stick's specific failure modes -- a relock that produces a
    no-lock constant-color frame, a text mode that legitimately never
    changes. None of that applies here: a UVC webcam either delivers frames
    or ffmpeg exits, closer to AudioCapability's "no data is a fault"
    watchdog than to VideoCapability's "no data can be correct" one. So
    there is no ring, no frozen-run detection, and no scrub/pin state --
    only the latest frame, because nothing here needs to look backward.

    Still a full Capability (config-driven, registered, always-on from
    daemon startup) rather than a bypass process, so it gets the same
    device-by-id discipline and liveness watchdog as every other capture
    device in this file -- just without the machinery that exists only to
    survive the analog stick's own hazards.
    """

    name = "camera"

    # Same by-id discipline as VideoCapability.DEVICE, and doubly so here:
    # this Pi can carry TWO UVC devices that renumber independently of each
    # other, so /dev/videoN is not even a good guess.
    DEVICE = CFG.default("capabilities.camera.settings.device", "/dev/video0")

    # How long the process can go without a frame before it's reported as
    # nosignal. Unlike VideoCapability's NOSIGNAL_AFTER_S, this never
    # triggers a respawn -- see _watchdog.
    NOSIGNAL_AFTER_S = 2.0

    def __init__(self, devs, bus=None):
        Capability.__init__(self, devs)
        self.bus = bus
        self.lock = threading.Lock()
        self.proc = None
        self.reader = None
        self.running = False
        self.owned = False
        self.last_frame = None
        self.last_frame_t = 0.0
        self.frames_total = 0
        self.state = "starting"
        self.spawns = 0
        self.last_error = None
        self.spawn_t = 0.0
        self.fast_failures = 0
        self.seq = 0

    # -- device lifecycle -----------------------------------------------

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
                     # 1920x1080, not 640x480 like the primary capture: that
                     # size was picked when this only ever rendered as a
                     # ~200px thumbnail, but the swap feature can make this
                     # the full main view -- and -c:v copy means capturing at
                     # the sensor's native detail costs nothing extra here
                     # (no decode either way, Rule 3), only bytes on the wire,
                     # which the thumbnail case already pays for by scaling
                     # down in CSS rather than by asking for less source.
                     # Confirmed MJPG 1920x1080@30fps via v4l2-ctl
                     # --list-formats-ext on this device.
                     "-video_size", "1920x1080", "-framerate", "30",
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
            self.last_error = None
            threading.Thread(target=_keep_stderr, args=(self, self.proc),
                             daemon=True).start()
            self.reader = threading.Thread(target=self._read_frames,
                                           args=(self.proc,), daemon=True)
            self.reader.start()
        self._publish("camera.acquired", spawns=self.spawns)
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
        self._publish("camera.released")

    def _publish(self, kind, **kw):
        if self.bus is not None:
            self.bus.publish(kind, **kw)

    # -- the per-frame path -----------------------------------------------

    def _read_frames(self, proc):
        """Split JPEGs out of the mjpeg stream. See _split_mjpeg for why the
        SOI/EOI approach is safe -- identical reasoning to VideoCapability's
        own reader, just not re-derived a second time."""
        buf = b""
        fd = proc.stdout.fileno()
        while self.running and proc.poll() is None:
            try:
                chunk = os.read(fd, 65536)
            except Exception as exc:
                self.last_error = errstr(exc, "reader: ")
                self._publish("camera.reader_error", error=self.last_error)
                break
            if not chunk:
                break
            buf += chunk
            frames, buf = _split_mjpeg(buf)
            for frame in frames:
                self._push(frame)

    def _push(self, frame):
        now = time.time()
        with self.lock:
            self.frames_total += 1
            self.seq += 1
            self.last_frame = frame
            self.last_frame_t = now

    def _latest(self):
        """(timestamp, jpeg_bytes) of the newest frame, or (None, None) --
        same shape as VideoCapability._latest, so vcweb.py's _mjpeg() can
        read either capability uniformly."""
        with self.lock:
            return (self.last_frame_t, self.last_frame) if self.last_frame \
                else (None, None)

    def _watchdog(self):
        """Liveness, not a clock -- same rule as video/audio (sec. 4.5).

        Unlike VideoCapability, there is no legitimate steady state where
        this process stays alive and produces nothing: a UVC webcam that is
        open delivers frames whether or not anything interesting is in
        frame, the same way AudioCapability's device delivers samples
        whether or not anything is playing. So an alive-but-quiet stream is
        reported (state "nosignal") but, deliberately, never forces a
        respawn on its own -- only a process exit does, exactly as for video
        and audio.
        """
        while self.running:
            time.sleep(0.5)
            with self.lock:
                owned, proc = self.owned, self.proc
                age = (time.time() - self.last_frame_t) if self.last_frame_t \
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
                self._publish("camera.wedged", rc=proc.returncode,
                              lifetime_s=round(lifetime, 2),
                              fast_failures=fails)
                self._release()
                if fails:
                    with self.lock:
                        self.state = "unavailable"
                    time.sleep(min(30.0, 2.0 ** min(fails, 5)))
                if self.running:
                    self._acquire()
                continue
            new = "nosignal" if (age is not None
                                 and age > self.NOSIGNAL_AFTER_S) else "capturing"
            if new != state:
                with self.lock:
                    self.state = new
                self._publish("camera.%s" % new)

    def _state(self):
        with self.lock:
            age = (time.time() - self.last_frame_t) if self.last_frame_t \
                else None
            return {"state": self.state, "owned": self.owned,
                    "frames": self.frames_total, "spawns": self.spawns,
                    "fast_failures": self.fast_failures,
                    "last_error": self.last_error,
                    "device_present": os.path.exists(self.DEVICE),
                    "device": self.DEVICE,
                    "last_frame_age_s": round(age, 3) if age else None}

    # -- commands -----------------------------------------------------------

    def commands(self):
        return {"camera": self._camera}

    def _camera(self, req):
        """action="state" (default) mirrors VideoCapability's own "video
        state"; action="shot" hands back the single latest frame this
        capability holds, base64-encoded, raw="True" -- there is no ring, no
        picture judgement and no "considered/live" sampling here (see the
        class docstring for why none of that applies to a UVC webcam), so
        this is closer in shape to _frame's raw contract than to _shot's
        judged one. Two-valued the same way: "jpeg" present iff a frame has
        ever arrived, so bin/vcctrl-client's existing raw-frame write path
        (write_frame, gated on resp["raw"]) handles it unchanged.
        """
        action = req.get("action", "state")
        if action == "state":
            return {"ok": True, **self._state()}
        if action == "shot":
            import base64
            t, frame = self._latest()
            if frame is None:
                return {"ok": True, "raw": True,
                        "reason": "no frame received yet from the camera"}
            return {"ok": True, "raw": True, "t": t,
                    "age_s": round(time.time() - t, 3),
                    "bytes": len(frame),
                    "jpeg": base64.b64encode(frame).decode()}
        return {"ok": False, "error": "camera: unknown action %r" % action}


class MsdCapability(Capability):
    """A USB mass-storage LUN presented to the target over the same gadget
    the HID keyboard/mouse/absolute-pointer functions use
    (pi/files/vcctrl-hid-gadget-setup.sh). "Mount" is a configfs write to
    lun.0/file naming an image already on THIS host's disk; the LUN exists
    from boot with no file assigned, so mounting or ejecting is never a USB
    re-enumeration once the gadget has been built once with this function
    present (internal/KVM-MACHINES-PLAN.md WP4).

    A copy, not a live share -- USB mass storage is block-level, so whichever
    side has the image open owns the filesystem. `msd_build` makes that copy
    at build time, from a directory already on this host (scp'd there; see
    WP4 sec. 3) -- a FAT image via mkfs.vfat+mtools, or an ISO9660 image via
    xorriso.

    Synchronous filesystem/configfs operations throughout -- no ring, no
    watchdog, nothing that needs a background thread the way a capture
    device does.

    ONLY THE INSTANCE READS self.settings, NEVER A CLASS ATTRIBUTE, unlike
    CameraCapability.DEVICE/AudioCapability's own settings-derived class
    attributes -- OPEN-FAULTS #21 already names those as not profile-fresh
    (baked in at module import time, under whichever profile's CFG happened
    to be bound then, "safe today only because modernpc disables them"). This
    is new code with no such precedent to preserve, so it resolves its paths
    in start(), from `self.settings` (set by the registry, fresh per
    profile, before start() runs) -- the same thing PowerCapability's own
    `_protocol()` already does for the identical reason.

    The `why` set here is NOT CLOSED:

        no_lun      the mass-storage LUN is not present on the gadget --
                    built without this function, or not rebuilt since
        host_busy   the connected host has the drive open; the kernel
                    refuses a `lun.0/file` write while that holds
    """

    name = "msd"
    BACKENDS = {}                          # filled in below

    DEFAULT_LUN_DIR = ("/sys/kernel/config/usb_gadget/vcctrl-hid-km/"
                       "functions/mass_storage.usb0/lun.0")
    DEFAULT_IMAGE_DIR_NAME = "msd/images"  # under daemon.state_dir

    # image library extension -> what a LUN can present it as
    IMAGE_KINDS = {".img": "drive", ".vfat": "drive", ".iso": "cdrom"}

    # Same ceiling as FilesCapability.STAGE_CHUNK_MAX and kvm.html's own
    # CHUNK constant -- one number, not three that have to be kept equal by
    # hand.
    STAGE_CHUNK_MAX = 4 * 1024 * 1024

    def __init__(self, devs, bus=None):
        Capability.__init__(self, devs)
        self.bus = bus
        self.lock = threading.Lock()
        self.lun_dir = self.DEFAULT_LUN_DIR
        self.image_dir = os.path.join(STATE_DIR, self.DEFAULT_IMAGE_DIR_NAME)

    def start(self):
        settings = self.settings or {}
        self.lun_dir = settings.get("lun_dir", self.DEFAULT_LUN_DIR)
        self.image_dir = settings.get(
            "image_dir", os.path.join(STATE_DIR, self.DEFAULT_IMAGE_DIR_NAME))
        try:
            os.makedirs(self.image_dir, exist_ok=True)
        except OSError as exc:
            sys.stderr.write("msd: could not create image dir %s: %s\n"
                             % (self.image_dir, exc))

    def _publish(self, kind, **kw):
        if self.bus is not None:
            self.bus.publish(kind, **kw)

    # -- configfs LUN plumbing -------------------------------------------

    def _lun_present(self):
        return os.path.isdir(self.lun_dir)

    def _read_lun(self, name, default=""):
        try:
            with open(os.path.join(self.lun_dir, name)) as f:
                return f.read().strip()
        except (IOError, OSError):
            return default

    def _write_lun(self, name, value):
        with open(os.path.join(self.lun_dir, name), "w") as f:
            f.write(value)

    # -- the image library -------------------------------------------------

    def _resolve_image(self, image_name):
        """A basename only, resolved strictly inside image_dir -- refuses
        anything that would leave it (`../`, an absolute path) rather than
        silently collapsing it, because `mount` writes the result straight
        into a kernel interface with no sandboxing of its own."""
        if not image_name or os.path.basename(image_name) != image_name:
            return None
        path = os.path.join(self.image_dir, image_name)
        if not os.path.isfile(path):
            return None
        return path

    def _list_images(self):
        out = []
        try:
            names = sorted(os.listdir(self.image_dir))
        except OSError:
            names = []
        for name in names:
            path = os.path.join(self.image_dir, name)
            if not os.path.isfile(path):
                continue
            kind = self.IMAGE_KINDS.get(os.path.splitext(name)[1].lower())
            if kind is None:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            out.append({"name": name, "size": size, "kind": kind})
        return out

    # -- commands -----------------------------------------------------------

    def commands(self):
        return {"msd_list": self._msd_list, "msd_mount": self._msd_mount,
                "msd_eject": self._msd_eject, "msd_status": self._msd_status,
                "msd_build": self._msd_build, "msd_stage": self._msd_stage}

    def _msd_list(self, req):
        # THE COST IS PART OF THE CHOICE, same rule as the capture-length
        # menu: free space travels with the list so a browser upload or a
        # build can be judged against it before either is started, not
        # discovered as a failure partway through.
        try:
            free_bytes = shutil.disk_usage(self.image_dir).free
        except OSError:
            free_bytes = None
        return {"ok": True, "images": self._list_images(),
                "free_bytes": free_bytes}

    def _msd_status(self, req):
        if not self._lun_present():
            return {"ok": False, "why": "no_lun",
                    "error": "the mass-storage LUN is not present -- the "
                    "gadget was built without it; rebuild with "
                    "pi/install.sh --hid-gadget-only"}
        current = self._read_lun("file")
        return {"ok": True,
                "mounted": os.path.basename(current) if current else None,
                "mode": "cdrom" if self._read_lun("cdrom") == "1" else "drive",
                "ro": self._read_lun("ro") == "1"}

    def _msd_mount(self, req):
        if not self._lun_present():
            return {"ok": False, "why": "no_lun",
                    "error": "the mass-storage LUN is not present -- the "
                    "gadget was built without it; rebuild with "
                    "pi/install.sh --hid-gadget-only"}
        image = req.get("image")
        path = self._resolve_image(image)
        if path is None:
            return {"ok": False, "error": "no such image: %r" % (image,)}
        mode = req.get("mode", "drive")
        if mode not in ("drive", "cdrom"):
            return {"ok": False,
                    "error": "mode must be drive or cdrom, got %r" % (mode,)}
        ro = bool(req.get("ro", mode == "cdrom"))
        with self.lock:
            try:
                # DETACH FIRST. cdrom/ro are refused by the kernel's mass
                # storage function while a file is already attached to the
                # LUN -- this is the write order, not just a style choice.
                self._write_lun("file", "")
                self._write_lun("cdrom", "1" if mode == "cdrom" else "0")
                self._write_lun("ro", "1" if ro else "0")
                self._write_lun("file", path)
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    return {"ok": False, "why": "host_busy",
                            "error": "the connected host has this drive "
                            "open -- eject it there first"}
                return {"ok": False, "error": "mount failed: %s" % exc}
        name = os.path.basename(path)
        self._publish("msd.mounted", image=name, mode=mode, ro=ro)
        return {"ok": True, "mounted": name, "mode": mode, "ro": ro}

    def _msd_eject(self, req):
        if not self._lun_present():
            return {"ok": False, "why": "no_lun",
                    "error": "the mass-storage LUN is not present -- the "
                    "gadget was built without it; rebuild with "
                    "pi/install.sh --hid-gadget-only"}
        with self.lock:
            prev = self._read_lun("file")
            try:
                self._write_lun("file", "")
            except OSError as exc:
                if exc.errno == errno.EBUSY:
                    return {"ok": False, "why": "host_busy",
                            "error": "the connected host has this drive "
                            "open -- eject it there first"}
                return {"ok": False, "error": "eject failed: %s" % exc}
        name = os.path.basename(prev) if prev else None
        self._publish("msd.ejected", image=name)
        return {"ok": True, "ejected": name}

    def _msd_build(self, req):
        """FAT (mode=fat) via mkfs.vfat+mtools, or ISO9660 (mode=iso) via
        xorriso, from a directory this HOST already has on disk -- scp is
        how it gets there (WP4 sec. 3 -- a browser cannot hand the daemon a
        whole directory tree, only individual files). Blocking: this runs
        on the request thread, same as VideoCapability's own `shot`, and a
        multi-hundred-MB image is seconds, not the kind of long-running job
        this daemon otherwise backgrounds (`vcctrl_job_status` and friends).
        """
        source = req.get("source_dir")
        name = req.get("name")
        mode = req.get("mode", "fat")
        label = (req.get("label") or "VCCTRL")[:32]
        if not source or not os.path.isdir(source):
            return {"ok": False,
                    "error": "source_dir does not exist: %r" % (source,)}
        if not name or os.path.basename(name) != name:
            return {"ok": False,
                    "error": "name must be a plain filename, no path"}
        if mode not in ("fat", "iso"):
            return {"ok": False,
                    "error": "mode must be fat or iso, got %r" % (mode,)}
        ext = ".img" if mode == "fat" else ".iso"
        if not name.lower().endswith(ext):
            name = name + ext
        dest = os.path.join(self.image_dir, name)
        if os.path.exists(dest):
            return {"ok": False,
                    "error": "an image named %r already exists" % (name,)}
        try:
            if mode == "iso":
                subprocess.run(
                    ["xorriso", "-as", "mkisofs", "-J", "-R",
                     "-V", label, "-o", dest, source],
                    check=True, capture_output=True, timeout=300)
            else:
                size_bytes = 0
                for root, _dirs, files in os.walk(source):
                    for fn in files:
                        try:
                            size_bytes += os.path.getsize(os.path.join(root, fn))
                        except OSError:
                            pass
                # 15% headroom for FAT overhead/directory entries, floored at
                # 16 MiB so a near-empty source still makes a mountable
                # filesystem -- mkfs.vfat refuses a volume below its own
                # cluster-count minimum otherwise.
                mb = max(16, int(size_bytes / (1024 * 1024) * 1.15) + 4)
                subprocess.run(
                    ["dd", "if=/dev/zero", "of=" + dest, "bs=1M",
                     "count=%d" % mb],
                    check=True, capture_output=True, timeout=120)
                subprocess.run(["mkfs.vfat", "-n", label[:11].upper(), dest],
                               check=True, capture_output=True, timeout=60)
                entries = sorted(os.path.join(source, n)
                                 for n in os.listdir(source))
                if entries:
                    subprocess.run(
                        ["mcopy", "-s", "-i", dest] + entries + ["::"],
                        check=True, capture_output=True, timeout=300)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            try:
                os.remove(dest)
            except OSError:
                pass
            if isinstance(exc, subprocess.TimeoutExpired):
                return {"ok": False, "error": "image build timed out"}
            detail = (exc.stderr or b"").decode("utf-8", "replace")[-500:]
            return {"ok": False,
                    "error": "%s failed: %s" % (exc.cmd[0], detail)}
        size = os.path.getsize(dest)
        self._publish("msd.built", name=name, mode=mode, size=size)
        return {"ok": True, "name": name, "mode": mode, "size": size}

    def _msd_stage(self, req):
        """Chunked upload straight into the image library -- same
        discipline as FilesCapability._file_stage (an `offset` that must
        equal what has already arrived, a final chunk checked against a
        declared sha256, promotion by atomic rename) with no DOS 8.3
        conversion: an image name is whatever the browser sent it as, not
        a name a DOS target has to open.

        The `why` set here is NOT CLOSED:

            bad-name          not a plain filename (a path, or empty)
            name-taken        an image already exists under this name
            offset-mismatch   a chunk did not start where the last one ended
            short             the final chunk arrived and the file is
                              undersized
            sha-mismatch      the assembled bytes are not the declared ones
        """
        name = req.get("name")
        if not name or os.path.basename(name) != name:
            return {"ok": False, "why": "bad-name",
                    "error": "name must be a plain filename, no path"}
        try:
            total = int(req.get("total"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "total (the whole file's size in "
                                          "bytes) is required"}
        try:
            offset = int(req.get("offset") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "offset must be a byte offset"}

        dest = os.path.join(self.image_dir, name)
        part = dest + ".part"
        if offset == 0 and os.path.exists(dest):
            return {"ok": False, "why": "name-taken",
                    "error": "an image named %r already exists" % (name,)}

        try:
            chunk = base64.b64decode(req.get("data") or "", validate=True)
        except Exception as exc:
            return {"ok": False, "error": "data is not valid base64: %s" % exc}
        if len(chunk) > self.STAGE_CHUNK_MAX:
            return {"ok": False, "error": "chunk is %d bytes; the limit is %d"
                                          % (len(chunk), self.STAGE_CHUNK_MAX)}

        have = os.path.getsize(part) if os.path.exists(part) else 0
        if offset == 0 and have:
            try:
                os.remove(part)
            except OSError:
                pass
            have = 0
        if offset != have:
            # LOUD, NOT PATCHED -- same reasoning as _file_stage's own
            # comment: seeking to the offset would leave a hole full of
            # zeroes, and the sha would fail at the end with nothing saying
            # which chunk was lost.
            return {"ok": False, "why": "offset-mismatch",
                    "error": ("this chunk starts at %d and %d bytes have "
                              "arrived" % (offset, have)), "have": have}
        if have + len(chunk) > total:
            return {"ok": False,
                    "error": ("this chunk would take %s past its declared "
                              "total of %d bytes" % (name, total))}
        try:
            with open(part, "ab") as f:
                f.write(chunk)
            have += len(chunk)
        except OSError as exc:
            return {"ok": False, "error": "cannot write %s: %s" % (part, exc)}

        if not req.get("final"):
            return {"ok": True, "name": name, "have": have, "total": total,
                    "complete": False}

        if have != total:
            return {"ok": False, "why": "short",
                    "error": ("final chunk received but %s is %d bytes, not "
                              "the declared %d" % (name, have, total)),
                    "have": have}

        h = hashlib.sha256()
        try:
            with open(part, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
        except OSError as exc:
            return {"ok": False, "error": "cannot read back %s: %s"
                                          % (part, exc)}
        digest = h.hexdigest()
        want = (req.get("sha256") or "").strip().lower()
        if want and want != digest:
            try:
                os.remove(part)
            except OSError:
                pass
            return {"ok": False, "why": "sha-mismatch",
                    "error": ("%s arrived with sha256 %s, not the declared "
                              "%s -- discarded rather than staged"
                              % (name, digest[:16], want[:16]))}

        os.rename(part, dest)
        self._publish("msd.staged", name=name, bytes=have)
        return {"ok": True, "name": name, "bytes": have, "complete": True}


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

    FILE = CFG.default("daemon.usb4vc.board_file", "/run/usb4vc/board.json")

    # Which computer each board implies. Lives in ONE place so there is not a
    # second table in the page to drift against this one; the page is fed from
    # state.json rather than carrying its own.
    #
    # These built-ins are the reference rig's and are a FALLBACK, not a
    # default to rely on: a `targets:` list in vcctrl.yaml replaces them
    # wholesale. Note 2 maps to None deliberately -- a board that exists and
    # implies no known machine is a different answer from a board that is not
    # installed, and both are different from "we could not look".
    TARGETS = {1: "Gateway 2000", 2: None, 3: "Macintosh Plus"}

    # Which KEYBOARD each board implies, by the same rule and for the same
    # reason: one table, here, so the page is fed rather than carrying a
    # second copy to drift against this one.
    #
    # The values are layout ids the page resolves; the daemon never draws a
    # key and deliberately knows nothing about what is on one. That split is
    # what lets a page older than its config say "board 3 asks for a layout I
    # do not have" instead of quietly showing the wrong keyboard.
    #
    # 2 maps to None for the same reason it does above -- a board that exists
    # and implies no known keyboard is a different answer from a board that is
    # not installed, and both differ from "we could not look".
    KEYBOARDS = {1: "pc-at-101", 2: None, 3: "mac-plus"}

    def __init__(self, *a, **kw):
        super(BoardCapability, self).__init__(*a, **kw)
        self._last_id = _UNSET

    def commands(self):
        return {"board": self._board, "profile": self._profile}

    def _profile(self, req):
        """Read, establish or forget the target's boot-profile reading.

        `profile` alone reads it. `profile set NAME` records one somebody
        established by other means -- a cell, a hand-run SET. `profile blaster
        VALUE` maps a BLASTER string onto a profile, which is the only form
        that is a MEASUREMENT rather than an assertion, and it is recorded as
        such in `how`.

        It lives on the board capability because both answer "what is on the
        other end of the wire" -- one the protocol board, one the boot it came
        up on. Neither is a fact about the Pi.
        """
        action = (req.get("action") or "state").lower()
        if action == "state":
            return {"ok": True, "profile": PROFILE.snapshot()}
        if action == "clear":
            PROFILE.invalidate(req.get("why") or "cleared by hand")
            return {"ok": True, "profile": PROFILE.snapshot()}
        if action == "blaster":
            value = req.get("value")
            name = PROFILE.from_blaster(value, "BLASTER value read from SET")
            if name is None:
                # NOT an error, and not a guess either. Four of the six
                # profiles set no BLASTER, so an unrecognised or absent value
                # rules two out and says nothing about the rest.
                return {"ok": True, "name": None, "profile": PROFILE.snapshot(),
                        "note": ("no profile sets BLASTER=%r; that rules out "
                                 "PGSB and VIBRA and identifies nothing"
                                 % (value,))}
            return {"ok": True, "name": name, "profile": PROFILE.snapshot()}
        if action == "set":
            name = req.get("name")
            if not name:
                return {"ok": False, "error": "profile set needs a name"}
            PROFILE.establish(str(name), req.get("how") or "asserted by hand")
            return {"ok": True, "profile": PROFILE.snapshot()}
        return {"ok": False, "error": "unknown profile action: %r" % action}

    def start(self):
        self.snapshot()          # publishes board.changed on first read

    def _targets(self):
        """board id -> machine name, from config where configured.

        A configured `targets:` list REPLACES the built-in table rather than
        merging into it. Merging would mean a rig that configures board 1
        silently inherits this rig's board 3, and would then report a
        Macintosh Plus that is not in the building.
        """
        cfgd = _configured_targets()
        if cfgd is not None:
            return dict(cfgd)
        # Legacy JSON override, one release only.
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

    def _keyboards(self):
        """board id -> keyboard layout id, from config where configured.

        REPLACES the built-in table rather than merging, exactly as _targets()
        does and for the identical reason: a rig that configures board 1 must
        not silently inherit this rig's board 3 and offer a Macintosh keyboard
        for a machine that is not in the building.

        No legacy JSON override here -- `board_targets` predates this field
        and never carried one, so there is nothing to be backward-compatible
        with. An empty branch kept "for symmetry" would be a code path that
        cannot run, which is worse than an asymmetry that is explained.
        """
        cfgd = _configured_keyboards()
        if cfgd is not None:
            return dict(cfgd)
        return dict(self.KEYBOARDS)

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
        # STATIC BACKEND: the operator asserts which board is installed.
        #
        # Reported with source "configured" and stale=None, and NEVER as
        # "status-file". This is a DECLARED fact in the sense of the harness
        # standard sec. 6.4 -- a human said so, the machine did not observe it,
        # and it goes stale the moment somebody swaps a board without editing
        # the config. Dressing an assertion up as a detection is worse than
        # having no detection, because it resembles evidence.
        #
        # It exists for rigs with one permanently-installed board and no
        # USB4VC to ask, where the alternative is `unknown` forever.
        if self.backend_name == "static":
            bid = (self.settings or {}).get("board_id")
            if bid is None:
                out = {"id": None, "name": None, "target": None,
                       "keyboard": None,
                       "source": "configured", "stale": None,
                       "reason": "backend is `static` but no board_id is set"}
            else:
                bid = int(bid)
                out = {"id": bid, "name": (self.settings or {}).get("name"),
                       "target": self._targets().get(bid),
                       "keyboard": self._keyboards().get(bid),
                       "source": "configured", "stale": None,
                       "reason": "asserted by configuration, not detected -- "
                                 "it cannot notice a board swap"}
            self._publish_if_changed(out)
            return out

        rec = src_name = None
        reason = None
        for reader in (self._from_file, self._from_journal):
            try:
                rec, src_name = reader()
                break
            except Exception as exc:
                reason = errstr(exc)
        if not rec:
            out = {"id": None, "name": None, "target": None, "keyboard": None,
                   "source": None, "stale": None,
                   "reason": ("usb4vc has not reported a board (%s)"
                              % reason)[:ERR_MAX]}
        else:
            bid = rec.get("id")
            age = (round(time.time() - rec["t"], 1)
                   if rec.get("t") else None)
            out = {"id": bid,
                   "name": rec.get("name"),
                   "target": self._targets().get(bid),
                   "keyboard": self._keyboards().get(bid),
                   "source": src_name,
                   # The journal path cannot prove the frame belongs to the
                   # currently running rpi_app -- only to this boot -- so it is
                   # marked stale. The file path is written by the running
                   # instance into tmpfs, so it is not.
                   "stale": src_name != "status-file",
                   "reason": None}
            if age is not None:
                out["age_s"] = age
        self._publish_if_changed(out)
        return out

    def _publish_if_changed(self, out):
        """Emit board.changed on a transition, never on the first reading.

        Extracted so the static backend uses the SAME transition logic rather
        than a second copy of it -- two copies of "has this changed" is how a
        board swap gets announced twice on one path and not at all on the
        other.
        """
        if out["id"] != self._last_id:
            if self._last_id is not _UNSET and self.bus:
                self.bus.publish("board.changed", **out)
            self._last_id = out["id"]

    def _board(self, req):
        return {"ok": True, "board": self.snapshot()}


class SysinfoCapability(Capability):
    """The DOS target's own hardware, as dinspect measured it -- CPU,
    memory, video, sound -- not what the boot profile implies and not
    what a config file asserts. Modeled directly on BoardCapability
    (above): every key always present, `source` says where the value
    came from, `reason` says why when something is unknown.

    READS, NEVER DRIVES. The scan itself -- the reboot, the typing, the
    fetch -- is FilesCapability.file_scan's job (see ScanJob); this
    capability only ever looks at the report file that job's PullJob
    already verified and wrote to the pulled directory, via
    FilesCapability._pulled() -- a query, the same kind vcweb.py's
    state builder already makes across capabilities for presentation.
    Nothing here calls registry.execute() or anything that would type at
    the target.

    `stale` IS NEVER A POSITIVE "STILL ACCURATE" CLAIM, because it
    cannot be one -- unlike a USB4VC board swap, this daemon has no
    channel that would notice a CPU, sound card or video adapter being
    physically swapped. `stale` is computed from ONE corroborating
    signal: has `board.changed` fired on the bus since this reading was
    taken? If so, the board capability at least saw *something* change
    at the wire, which is reason to distrust an inventory taken before
    it. If not, `stale: false` says exactly that and no more -- see
    `_staleness` for the sentence this carries into `reason`.
    """

    name = "sysinfo"
    REPORT_NAME = "SYSINFO.TXT"

    def __init__(self, *a, **kw):
        super(SysinfoCapability, self).__init__(*a, **kw)
        self._cache_mtime = None
        self._cache = None
        self._last_fields = _UNSET

    def commands(self):
        return {"sysinfo": self._sysinfo}

    def _pulled_record(self):
        """The pulled SYSINFO.TXT's own record, or None if none exists.

        Name matched case-insensitively: FilesCapability promotes names
        through dos_filename(), which upper-cases -- this does not repeat
        that logic, it just does not assume a particular case out of it.
        """
        files = (self.registry.caps.get("files") if self.registry else None)
        if files is None:
            return None
        for rec in files._pulled():
            if (rec.get("name") or "").upper() == self.REPORT_NAME:
                return rec
        return None

    def _staleness(self, since):
        """(stale, reason) from ONE corroborating signal: a board swap
        seen on the bus after `since`. See the class docstring."""
        if self.bus is None:
            return None, "no event bus to check for a board change since"
        for ev in list(self.bus.events):
            if ev.get("kind") == "board.changed" and ev.get("t", 0) > since:
                return True, ("the USB4VC board changed after this reading "
                              "was taken -- it is very likely describing a "
                              "machine that is no longer connected")
        return False, ("no board change seen since this reading was taken "
                       "-- that does NOT mean nothing changed, only that "
                       "nothing this daemon can observe did")

    def snapshot(self):
        rec = self._pulled_record()
        if rec is None:
            out = {"fields": None, "other": None, "source": None,
                   "age_s": None, "stale": None,
                   "reason": "no dinspect report has been pulled yet -- "
                             "run file_scan"}
            self._publish_if_changed(out)
            return out
        path = rec.get("path")
        try:
            mtime = os.path.getmtime(path)
        except OSError as exc:
            out = {"fields": None, "other": None, "source": "pulled",
                   "age_s": None, "stale": None,
                   "reason": "the pulled report is recorded but unreadable "
                             "on this host: %s" % errstr(exc)}
            self._publish_if_changed(out)
            return out
        if mtime != self._cache_mtime:
            try:
                with open(path) as f:
                    text = f.read()
            except OSError as exc:
                out = {"fields": None, "other": None, "source": "pulled",
                       "age_s": None, "stale": None,
                       "reason": "the pulled report could not be read: %s"
                                 % errstr(exc)}
                self._publish_if_changed(out)
                return out
            self._cache = parse_dinspect_report(text)
            self._cache_mtime = mtime
        stale, reason = self._staleness(mtime)
        out = {"fields": self._cache["fields"], "other": self._cache["other"],
               "source": "pulled", "age_s": round(time.time() - mtime, 1),
               "stale": stale, "reason": reason}
        self._publish_if_changed(out)
        return out

    def _publish_if_changed(self, out):
        """Emit sysinfo.changed on a transition, never on the first
        reading -- same contract as BoardCapability._publish_if_changed."""
        if out["fields"] != self._last_fields:
            if self._last_fields is not _UNSET and self.bus:
                self.bus.publish("sysinfo.changed",
                                 source=out["source"], stale=out["stale"])
            self._last_fields = out["fields"]

    def _sysinfo(self, req):
        return {"ok": True, "sysinfo": self.snapshot()}


# The registry. A table in the source, in load order. Video, web, audio, reset
# and files join this list; each is one entry and touches nothing above it.
class _Disabled(object):
    def __repr__(self):
        return "DISABLED"


_DISABLED = _Disabled()


def _cap_settings(name):
    """A capability's `settings` mapping, or {} when there is none.

    {} rather than None because every consumer indexes it, and a None here
    would turn "no settings" into an AttributeError at the first read -- in a
    constructor, which Rule 2 would then record as the capability having
    failed to start.
    """
    v = CFG.optional("capabilities.%s.settings" % name)
    if v is vcconfig.ABSENT or v is vcconfig.NONE or not isinstance(v, dict):
        return {}
    return v


# Every protocol backend is served by the one capability class; the registry
# only needs to know the NAMES are valid, so a typo is refused rather than
# silently falling back to the default. Derived from POWER_BACKENDS rather
# than written out, so adding a protocol cannot leave the registry behind.
PowerCapability.BACKENDS = {k: PowerCapability for k in POWER_BACKENDS}
PowerCapability.DEFAULT_BACKEND = PowerCapability
PowerCapability.DEFAULT_BACKEND_NAME = "kasa"

# Board identity has two genuinely different implementations, below.
BoardCapability.BACKENDS = {"usb4vc-runfile": BoardCapability,
                            "static": BoardCapability}
BoardCapability.DEFAULT_BACKEND = BoardCapability
BoardCapability.DEFAULT_BACKEND_NAME = 'usb4vc-runfile'

# THE REST HAVE ONE IMPLEMENTATION AND STILL MUST NAME IT.
#
# Every capability that a config can name needs its canonical backend name
# registered here, even where there is only one. Without this the registry
# reads `backend: v4l2-ffmpeg` as a typo and refuses to start the capability
# -- which is the correct treatment of an unknown name, and would have taken
# input, leds, video and audio down together on the reference rig. Caught by
# resolving every capability against the real config before deploying rather
# than after.
#
# The single-name maps are also the extension point: a second implementation
# is one more entry, and `none` already works for all of them.
InputCapability.BACKENDS = {"usb4vc-uinput": InputCapability,
                            "hid-gadget": InputCapability}
InputCapability.DEFAULT_BACKEND = InputCapability
InputCapability.DEFAULT_BACKEND_NAME = 'usb4vc-uinput'
LedsCapability.BACKENDS = {"ps2-sysfs": LedsCapability}
LedsCapability.DEFAULT_BACKEND = LedsCapability
LedsCapability.DEFAULT_BACKEND_NAME = 'ps2-sysfs'
# ---------------------------------------------------------------------------
# FILES -- putting a file ONTO the target.
# ---------------------------------------------------------------------------
#
# The transport is the target's own FTP client pulling from a server on the
# control host; the DOS side always initiates, because mTCP's FTP is a client
# and nothing can be pushed to it unsolicited. That single fact shapes
# everything here, including why this capability spends most of its code
# saying when it does NOT apply.

# DOS 6.22 reserves these names, and it reserves them REGARDLESS OF EXTENSION:
# `CON.TXT` is the console, not a file. A transfer to one of these does not
# fail loudly -- it writes to a device and reports success, which is the exact
# shape of failure this rig keeps producing.
_DOS_DEVICES = frozenset([
    "CON", "PRN", "AUX", "NUL", "CLOCK$",
] + ["COM%d" % n for n in range(1, 10)] + ["LPT%d" % n for n in range(1, 10)])

# Everything DOS will not accept in a filename. The space is in here
# deliberately: FAT permits it in some tools and the BATs that carry these
# names do not, because a space ends the argument.
_DOS_ILLEGAL = set('"*+,/:;<=>?[]|\\ ') | set(chr(c) for c in range(0, 32))

# Where the size policy sits. Both are bytes, both apply to a single file AND
# to a queue total -- without the second, three 30 MB files walk past a 64 MB
# refusal one at a time.
# WHERE PULLED FILES LAND ON THE TARGET, and deliberately not the directory
# the program under test lives in. C:\DOSKUTSU holds DOSKUTSU.EXE, CLRENV.BAT
# and LOGS\ -- a pulled file whose name collides silently replaces something
# every measured run calls, which happened to CLRENV.BAT on 2026-08-24 and was
# survivable only because a .BAK was taken first.
#
# The sibling OUT is the return direction, reserved and created so the
# convention is discoverable from a DIR rather than only from a document.
#
# THE WORDS ARE FROM THE TARGET'S POINT OF VIEW AND THEY INVERT ACROSS THE
# WIRE: the server's stage/ feeds the target's IN, and the target's OUT feeds
# the server's incoming/. IN and OUT are deliberately terse so nobody reads
# them as matching the server's names.
DEFAULT_DEST = "C:\\XFER\\IN"
DEFAULT_OUT = "C:\\XFER\\OUT"

# THE LARGEST TRANSFER THIS TOOL HAS ACTUALLY VERIFIED, end to end, on the
# rig: 10 MB out and back, byte-for-byte, 2026-08-24. Measured by the file
# server rather than read off the target's screen --
#
#     RETR 10,485,760 bytes in 14.952 s   =  685 KiB/s   (server -> card)
#     STOR 10,485,760 bytes in 10.781 s   =  950 KiB/s   (card -> server)
#
# A CONSTANT WITH PROVENANCE rather than a number inside a sentence, because
# the warning text quoted "7.8 MB" long after that was beaten -- a figure from
# a different tool, cited as though it were this one's limit. Beat it and
# update it here; nothing else should carry the number.
LARGEST_VERIFIED_BYTES = 10 * 1024 * 1024

# WARN AT WHAT HAS BEEN PROVEN, not at a round number. Below this somebody has
# watched it work; above it nobody has, and that is the whole content of the
# warning. Tying them together means the threshold cannot drift away from the
# evidence for it.
WARN_BYTES = LARGEST_VERIFIED_BYTES
REFUSE_BYTES = 64 * 1024 * 1024


def dos_filename(name):
    """A local filename -> (DOS 8.3 name, [notes]), or raise ValueError.

    Renaming happens HERE, on the staging side, because the DOS side cannot
    rename in transit: `GET.BAT` and its successor write the name they
    fetched. So `my-photo-2026.jpg` has to become `MY_PHOTO.JPG` before the
    bytes ever move, and the operator has to be shown that it did -- silently
    truncating a name is how two files become one.

    Returns the notes rather than logging them, so the caller can put them in
    front of a person before the transfer rather than in a log afterwards.

    THIS IS ALSO THE PATH-TRAVERSAL GUARD, AND RELAXING IT IS A SECURITY
    CHANGE. `vcctrld` runs as root and the staged path is
    `os.path.join(root, name)` with `name` supplied by whoever is driving the
    page. Nothing downstream checks it again. What contains it is here:
    `basename()` strips every path component before anything else, the
    backslash is in the illegal set so it becomes an underscore rather than a
    separator, and the result is rebuilt from a whitelist of surviving
    characters rather than filtered against a blacklist of bad patterns.
    `../../../etc/cron.d/pwn` becomes `PWN`.

    It reads like a usability function. It is not only one. Anybody loosening
    it for long filenames on a future non-DOS target is also loosening a
    boundary, and needs to put an explicit containment check back.
    """
    notes = []
    raw = os.path.basename(str(name or "").strip())
    if not raw or raw in (".", ".."):
        raise ValueError("no filename in %r" % (name,))
    base, dot, ext = raw.rpartition(".")
    if not dot:
        base, ext = raw, ""
    up = lambda s: "".join(("_" if c in _DOS_ILLEGAL else c) for c in s).upper()
    # THE STEM'S OWN DOTS HAVE TO GO. An 8.3 name holds exactly one separator,
    # and `archive.tar.gz` splits into a stem that still contains one --
    # which came out as `ARCHIVE..GZ`, a name DOS will not open and which
    # nothing downstream would have questioned. Caught by a test, not by
    # reading: the truncation to eight characters happened to land on the
    # stray dot and made it look deliberate.
    base = up(base).replace(".", "_")
    ext = up(ext)
    if not base:
        # ".bashrc" has no stem. DOS has no concept of a leading-dot file.
        raise ValueError("%r has no name before its extension" % (raw,))
    if len(base) > 8:
        base = base[:8]
        notes.append("name shortened to 8 characters")
    # A stem that truncated onto a separator would end in one. Valid, ugly,
    # and it makes two different files look like the same typo.
    base = base.rstrip("_")
    if not base:
        raise ValueError("%r leaves no usable name once DOS-legal" % (raw,))
    if len(ext) > 3:
        ext = ext[:3]
        # NAMED, not just reported. The headline case is a phone photo:
        # `.jpeg` becomes `.JPE`, which is a real JPEG extension and an
        # unusual one that some DOS viewers do not associate. "Shortened to
        # three characters" does not tell the operator that; the actual
        # extension does, before they go looking for it on the target.
        notes.append("extension shortened to .%s" % ext)
    if base in _DOS_DEVICES:
        # NOT silently renamed. A file the operator called CON.TXT is a file
        # they will look for under that name, and DOS would have written it to
        # the console and said nothing.
        raise ValueError("%s is a reserved DOS device name, whatever the "
                         "extension -- rename it before sending" % base)
    out = base + ("." + ext if ext else "")
    if out != raw.upper():
        notes.append("sent as %s" % out)
    return out, notes


def size_verdict(sizes):
    """[bytes] -> {"ok", "why", "reason", "total"}. Three answers, not two.

    `warn` is a real third state and the reason this is not a boolean: past
    8 MB the honest statement is that the regime is UNMEASURED, not that it
    will fail. The largest transfer on record over this path is a 7.8 MB
    binary (FINDINGS sec. 15), so above that nobody has looked -- which is a
    different fact from "too big", and the operator is entitled to proceed.

    The refusal at 64 MB is not about size either. It is about there being no
    safe interruption: a cancel lands between files and never inside
    FTP.EXE, so a wedged transfer is a wedged machine whose only exit is a
    power cycle.

    The `why` set here is NOT CLOSED -- `too-large` and `unmeasured` are what
    exists today, and a future ceiling (free space on the target, a queue
    length) would add a word rather than overload one of these.
    """
    total = sum(int(s) for s in sizes)
    biggest = max([int(s) for s in sizes] or [0])
    if biggest > REFUSE_BYTES or total > REFUSE_BYTES:
        return {"ok": False, "why": "too-large", "total": total,
                "reason": ("over the %d MB ceiling. Nothing this large has "
                           "been transferred here, and a transfer that wedges "
                           "cannot be interrupted -- a cancel lands between "
                           "files, never inside FTP.EXE."
                           % (REFUSE_BYTES // (1024 * 1024)))}
    if biggest > WARN_BYTES or total > WARN_BYTES:
        return {"ok": True, "why": "unmeasured", "total": total,
                "reason": ("over %d MB, which is the largest this tool has "
                           "verified end to end on this rig. Untested rather "
                           "than known to be too big."
                           % (LARGEST_VERIFIED_BYTES // (1024 * 1024)))}
    return {"ok": True, "why": None, "total": total, "reason": None}


# The target's own account of what is in C:\XFER\OUT, and the ONLY view of it
# that does not go through the glass. `VCLIST.BAT` redirects a DIR into a file
# and sends that file here, so what is parsed below arrived over FTP byte for
# byte rather than off a console that reads 13,800 as 13,808.
#
#     Volume in drive C is DOSKUTSU
#     Directory of C:\XFER\OUT
#
#    .            <DIR>        08-24-26  10:12a
#    ..           <DIR>        08-24-26  10:12a
#    SCORES   DAT         1310 08-24-26  10:13a
#            3 file(s)          1310 bytes
#                           8994816 bytes free
#
# STRICT, WHERE bin/vcctrl-cfclean PARSES THE SAME LINES LENIENTLY, and the
# difference is not taste -- it is where the bytes came from. cfclean reads a
# screen through OCR and must survive `file<s)`; this reads a file. Importing
# that tolerance here would buy nothing and would hide the one thing this is
# actually checked for: a listing that arrived short.
def dos_dir_path(path):
    r"""A DOS directory a command may be built around -> canonical, or raise.

    THIS EXISTS BECAUSE THE STRING IS TYPED AT THE TARGET. `out_dir` came from
    config and was trusted; the moment a caller can name a directory -- the
    harness fetching C:\DOSKUTSU\LOGS, a browser asking for anything -- it is
    input, and it ends up inside `VCLIST.BAT %s` and `VCCHK.BAT %s\%s` on a
    machine with no quoting whatsoever.

    DOS HAS NO ESCAPE CHARACTER. The caret escapes nothing on 6.22 -- that is
    an established fact about this rig, learned the expensive way -- so there
    is no such thing as quoting a bad name safely. The only defence is a
    whitelist, and this is it: a drive letter, a colon, and 8.3 components
    made of characters DOS accepts. Anything else is refused rather than
    repaired, because a path this had to alter is a path that will not name
    what the caller meant.

    A SPACE IS THE CASE THAT LOOKS INNOCENT AND IS NOT: FAT permits one and a
    command line does not, so `DIR C:\MY DIR` lists C:\MY. Refused here.

    Redirection characters are refused for the same reason but with a louder
    consequence -- `> file` inside a typed command writes to the card.

    Returns the path uppercased with no trailing separator. Raises ValueError
    with a reason a person can act on.
    """
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("no directory given")
    if len(raw) > 64:
        # DOS's own limit is 66 for a full path; well before that, a long
        # command line is the thing the BIOS buffer truncates.
        raise ValueError("%r is too long to type safely at this machine" % raw)
    # NOT rstrip()ed BEFORE THE MATCH: "C:\\" would become "C:" and fail as
    # "not an absolute path", which is a confusing thing to say about a path
    # that is absolutely fine and merely names the root. The root gets its own
    # refusal below, in its own words.
    up = raw.upper().replace("/", "\\")
    m = re.match(r"^([A-Z]):\\(.*)$", up)
    if not m:
        raise ValueError("%r is not an absolute DOS path like C:\\XFER\\OUT"
                         % raw)
    drive, rest = m.group(1), m.group(2).rstrip("\\")
    parts = [p for p in rest.split("\\") if p != ""]
    if not parts:
        # THE ROOT IS REFUSED DELIBERATELY. Listing C:\ is a legitimate wish
        # and this is not the tool for it: everything downstream fetches what
        # it lists, and a fetch-everything against the root of the boot drive
        # is not an operation anybody should reach by accident.
        raise ValueError("refusing the root of drive %s: name a directory "
                         "under it" % drive)
    for part in parts:
        if part in (".", ".."):
            raise ValueError("%r contains a relative component" % raw)
        base, dot, ext = part.partition(".")
        if not base or len(base) > 8 or len(ext) > 3 or "." in ext:
            raise ValueError("%r is not a DOS 8.3 directory name" % part)
        if base.rstrip(".") in _DOS_DEVICES:
            raise ValueError("%r is a DOS device name, not a directory" % part)
        bad = set(part) & (_DOS_ILLEGAL | {"*", "?"})
        if bad:
            raise ValueError("%r contains %s, which cannot appear in a typed "
                             "command on this machine"
                             % (part, " ".join(sorted(repr(c) for c in bad))))
    return "%s:\\%s" % (drive, "\\".join(parts))


def dos_dir_listing(text):
    """DOS 6.22 `DIR` output -> what is in that directory, or why not.

        {"ok": True,  "why": None, "dir": "C:\\XFER\\OUT",
         "entries": [...], "files": [...], "bytes": 1310,
         "reported": {"count": 3, "bytes": 1310}}

    THE TRAILER IS THE CHECK, and it is the reason this returns a verdict
    rather than a list. `DIR` states its own totals, so a listing can be
    reconciled against itself: bytes that do not add up, or a trailer that is
    not there at all, mean the text is not a whole listing -- and a SHORT
    LISTING IS THE DANGEROUS FAILURE, because it reads as a directory with
    fewer files in it and nothing looks wrong. That is the same shape as the
    truncated transfer the staging side exists to prevent, one direction over.

    THE COUNT IS RECONCILED PERMISSIVELY AND THE BYTES ARE NOT. `.` and `..`
    ARE counted by `file(s)` -- measured on the card 2026-08-24, where a
    directory holding two files reported `4 file(s)`. That was an assumption
    when this was written and is now a reading, and the permissive check is
    kept anyway: it costs nothing, and the byte total is the one that can
    refuse. Individual file sizes carry thousands separators on this DOS
    (`2,434`), which was the other thing this had to guess and no longer does.

    NAMES ARE NOT TRUSTED, and this is the boundary. The text arrives from the
    target, and every name in it is about to be joined onto a path on this
    host and typed into a command on that one. So each is put through
    `dos_filename()` and must come back UNCHANGED; anything that does not is
    listed and marked unfetchable rather than silently corrected, because a
    name we had to alter is a name that will not match the file on the card.

    The `why` set here is NOT CLOSED:

        unreadable    nothing that looks like DIR output
        no-dir        DIR said File not found -- the directory is not there
        truncated     no `N file(s)` trailer, so the listing may be short
        unreconciled  the sizes do not add up to the total DIR reported
        unsafe-name   (per entry) the name does not survive the 8.3 round
                      trip, so it is shown but cannot be fetched
    """
    raw = text.decode("cp437", "replace") if isinstance(text, bytes) else \
        (text or "")
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    where, entries = None, []
    reported = {"count": None, "bytes": None}
    not_found = False
    for line in lines:
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("directory of"):
            where = s.split(None, 2)[-1].strip()
            continue
        if low.startswith("file not found"):
            not_found = True
            continue
        m = re.match(r"^([\d,]+)\s+file\(s\)\s+([\d,]+)\s+bytes\s*$", s, re.I)
        if m:
            reported = {"count": int(m.group(1).replace(",", "")),
                        "bytes": int(m.group(2).replace(",", ""))}
            continue
        if low.endswith("bytes free") or low.startswith("volume in drive") \
                or low.startswith("volume serial"):
            continue
        rec = _dos_dir_entry(s)
        if rec is not None:
            entries.append(rec)

    files = [e for e in entries if e["kind"] == "file"]
    total = sum(e["bytes"] for e in files)
    out = {"ok": False, "why": None, "reason": None, "dir": where,
           "entries": entries, "files": files, "bytes": total,
           "reported": reported}

    if (where is None and not entries and reported["count"] is None
            and not not_found):
        # NOT A SHORT LISTING -- NOT A LISTING AT ALL. `DIR` failing writes its
        # complaint into the same file the good output would have gone to, so
        # what arrives is a sentence rather than a truncated table. Calling
        # that `truncated` would send somebody looking for the missing half of
        # something that was never there.
        out["why"] = "unreadable"
        out["reason"] = ("nothing in this file looks like DIR output -- no "
                         "directory header, no entries and no total. Whatever "
                         "ran on the target, it was not a listing")
        return out
    if not_found and not entries:
        out["why"] = "no-dir"
        out["reason"] = ("DIR said File not found: %s does not exist on the "
                         "target" % (where or "the directory"))
        return out
    if where is not None and not entries and reported["count"] is None:
        # A HEADER AND NOTHING ELSE, WHICH IS WHAT A FAILED DIR LEAVES BEHIND.
        # Measured on the rig 2026-08-24: `DIR C:\NOSUCH > file` where NOSUCH
        # does not exist wrote exactly this and no more --
        #
        #     Volume in drive C is DOS
        #     Volume Serial Number is 0000-0000
        #     Directory of C:\
        #
        # DOS 6.22 HAS NO STDERR REDIRECTION, so `File not found` goes to the
        # console and never reaches the file. The absence of the error message
        # IS the error message. Note also that DOS re-read the argument as a
        # FILENAME PATTERN in the root and reported the directory as `C:\` --
        # which is why the caller's own path, not the text's claim, is what
        # this reading gets filed under.
        #
        # NAMED AS THE LIKELY CAUSE WITHOUT CLAIMING TO KNOW IT. A transfer
        # truncated at exactly the header boundary would look identical from
        # here, and nothing in the bytes can separate them -- so the reason
        # says both and the advice covers both.
        out["why"] = "no-dir"
        out["reason"] = ("the listing holds a header and no entries at all, "
                         "which is what DIR leaves when it fails: its error "
                         "goes to the console and DOS 6.22 cannot redirect "
                         "that into the file. So the directory very likely "
                         "does not exist -- check the path. A transfer cut "
                         "off at the header would look the same, so if the "
                         "path is right, try again")
        return out
    if reported["count"] is None:
        # NOT "an empty directory". An empty one still prints its trailer --
        # `2 file(s) 0 bytes` for the . and .. entries -- so a listing with no
        # trailer is a listing that stopped early, and treating it as empty
        # would report a directory of files as containing none.
        out["why"] = "truncated"
        out["reason"] = ("no `N file(s)` line, so this is not a whole DIR "
                         "listing -- it stopped early rather than being empty")
        return out
    if total != reported["bytes"]:
        out["why"] = "unreconciled"
        out["reason"] = ("the file sizes add up to %d bytes and DIR reported "
                         "%d -- a line is missing or was misparsed"
                         % (total, reported["bytes"]))
        return out
    # PERMISSIVE, AND SAID SO. Whether `.` and `..` are inside DIR's own count
    # is a fact about MS-DOS 6.22 that is asserted here rather than measured,
    # so both readings pass and neither is called wrong.
    dirs = len(entries) - len(files)
    if reported["count"] not in (len(entries), len(files),
                                 len(entries) - dirs):
        out["why"] = "unreconciled"
        out["reason"] = ("DIR counted %d entries and %d were parsed"
                         % (reported["count"], len(entries)))
        return out
    out["ok"] = True
    return out


def _dos_dir_entry(s):
    """One DIR line -> a record, or None if the line is not one.

    Tokens rather than columns. The column layout is fixed on 6.22 and a
    fixed-column parser is still the more brittle choice: it fails silently by
    slicing a name in half, where a token parser that does not recognise a
    line returns None and the reconciliation above then refuses the listing.
    """
    parts = s.split()
    if len(parts) < 3:
        return None
    # The last two tokens are the date and the time. Anchoring on the END is
    # what lets `.` and `..` parse with the same rule as everything else.
    if not re.match(r"^\d\d-\d\d-\d\d$", parts[-2]):
        return None
    if not re.match(r"^\d{1,2}:\d\d[apAP]?m?$", parts[-1]):
        return None
    size_tok = parts[-3]
    head = parts[:-3]
    if not head:
        return None
    if size_tok.upper() == "<DIR>":
        kind, nbytes = "dir", 0
    elif re.match(r"^[\d,]+$", size_tok):
        kind, nbytes = "file", int(size_tok.replace(",", ""))
    else:
        return None
    base = head[0]
    ext = head[1] if len(head) > 1 else ""
    if len(head) > 2:
        # A name with a space in it. FAT permits one and every command line
        # that carries the name does not, so this is reported as seen and
        # refused as fetchable rather than being joined back up into a name
        # that would arrive as two arguments.
        base, ext = " ".join(head[:-1]), head[-1]
    name = base + ("." + ext if ext else "")
    rec = {"name": name, "bytes": nbytes, "kind": kind,
           "date": parts[-2], "time": parts[-1],
           "fetchable": kind == "file", "why": None}
    if rec["fetchable"]:
        try:
            safe, _notes = dos_filename(name)
        except ValueError:
            safe = None
        if safe != name:
            rec["fetchable"] = False
            rec["why"] = "unsafe-name"
    return rec


class TargetProfile(object):
    """The boot profile the target is currently running -- as a READING.

    THIS CANNOT BE POLLED, and that fact shapes the whole class. Establishing
    it means typing `SET` at the target's prompt and reading the answer back
    off the screen; there is no passive channel that carries it. So it is not
    a status the daemon observes, it is a reading somebody took, and the only
    honest way to hold one is with when it was taken and what took it.

    The dangerous direction is obvious once stated. A cell died on 2026-08-20
    with `sdl_init failed: No BLASTER environment variable` because the
    machine was still in NET from a hand-run transfer an hour earlier, and NET
    and the sound profiles are indistinguishable from the harness -- same
    prompt, same `at_prompt`, same screen (OPEN-FAULTS sec. 7). A header that
    kept displaying `PGSB` across a reboot would be that same failure, printed
    in the one place everybody looks and wearing the authority of a live
    indicator.

    So: any reboot invalidates it, and an invalidated reading is ABSENT rather
    than old. Nothing here ages into a softer version of itself.
    """

    # Two of the six CONFIG.SYS profiles set BLASTER and their strings differ,
    # so the VALUE names the profile uniquely where its presence does not.
    # PGADLIB, PGGUS, NET and CLEAN set nothing at all -- which is why an
    # absence is not evidence of any particular one of them.
    #
    # SAME TWO STRINGS ARE HARDCODED AGAIN in harness/vcctrl-cell's profile
    # witness (docs/OPEN-FAULTS.md sec 15) -- that check runs on the CONTROL
    # host against a value OCR'd live off the DOS prompt (never the file-based
    # channel this class assumes), so it cannot import this dict across the
    # host boundary. If either set of strings changes, update BOTH.
    BLASTER_PROFILES = {
        "A220 I7 D3 P330 T3": "PGSB",
        "A220 I5 D1 H5 T6 P330": "VIBRA",
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._name = None
        self._at = None
        self._how = None
        self._reason = "never read"

    def establish(self, name, how):
        """Record a reading. `how` says what took it, and is not decoration --
        it is the difference between a value somebody typed in and one the
        machine answered with."""
        with self._lock:
            self._name, self._at, self._how = name, time.time(), how
            self._reason = None

    def invalidate(self, why):
        """Forget it. Called on anything that could have rebooted the target.

        ABSENT, not stale. A stale profile is worse than none: it is the same
        string, in the same place, with nothing on screen to say the machine
        underneath it changed. The reading's whole value is that it was true
        of the boot that is running, and after a reboot it is true of a boot
        that is not.
        """
        with self._lock:
            if self._name is not None:
                self._name = self._at = self._how = None
            self._reason = why

    def reset_seen(self):
        """Scroll Lock went 1 -> 0: the target reset, however it was caused.

        DELIBERATELY NOT ATTRIBUTED, and that is the design rather than a
        shortcut. The instinct is that this is a shared flag with several
        writers -- `arm_leds()` clears Scroll, `verify_input` leaves it set,
        POST clears it, `RDYPULSE` sets it -- and that the daemon must know
        which transitions it caused.

        It does not, because **every writer that clears Scroll Lock is either
        a reboot or a prelude to one.** `arm_leds()` clears it BECAUSE it is
        about to reboot. So `1 -> 0` means invalidate whoever did it: a driven
        reboot invalidates, an undriven one invalidates, and an arming that is
        not followed by a reboot invalidates a reading that was still
        technically good -- a false positive in the safe direction.

        That is what separates this from a shared flag with no owner, where
        two writers mean different things and every release is locally right
        and globally wrong. Here both writers mean the same thing, so
        attribution machinery would buy a way to be wrong in exchange for
        suppressing a conservative refusal.
        """
        self.invalidate("the target reset, and no readiness pulse since POST")

    def ready_pulse(self):
        """Scroll Lock went 0 -> 1: RDYPULSE ran, so AUTOEXEC completed.

        A DIFFERENT FACT FROM THE RESET, and it gets its own words. The reset
        edge says the reading is void; this one says the machine is back and
        the profile can be read again. Without the distinction, a boot that
        never completes and a boot that completed but was never read look
        identical to somebody reading the header.

        RDYPULSE is guarded by `IF EXIST C:\\DRIVERS\\RDYPULSE.COM` in
        AUTOEXEC, so a missing file makes that line a silent no-op: Scroll
        stays at 0 and this never fires. The failure is a permanently
        invalidated reading rather than a falsely valid one -- the right
        direction -- but it reads as a bug rather than as a missing file,
        which is why reset_seen() names the pulse in its own message.
        """
        with self._lock:
            if self._name is None:
                self._reason = ("booted, and the profile has not been read "
                                "since")

    def from_blaster(self, value, how):
        """A BLASTER string -> a profile name, or None.

        Returns None for an unrecognised value rather than guessing. Four of
        the six profiles set no BLASTER at all, so an ABSENT one is not
        evidence of any particular profile -- it rules out two and says
        nothing about the other four, which is a genuinely different statement
        from naming one.

        IN PARTICULAR IT CAN NEVER NAME `NET`, and a file transfer is the
        thing most likely to want it to. NET, PGADLIB, PGGUS and CLEAN all set
        no BLASTER, so "no BLASTER, therefore we booted into networking" is
        wrong three ways -- and one of those ways is CLEAN, which has no
        network stack at all. The profile witness in `profiles/doskutsu.yaml`
        was built to prove a cell is NOT in NET; it does not run backwards.

        Proving NET wants a POSITIVE witness of the thing the transfer
        actually needs: `C:\\MTCP\\PKTTOOL.EXE scan` reporting the packet
        driver. That attests the capability rather than the label, and it is
        letters rather than digits, which matters on a console whose OCR drops
        digits.
        """
        name = self.BLASTER_PROFILES.get((value or "").strip().upper())
        if name:
            self.establish(name, how)
        return name

    def snapshot(self):
        """EVERY KEY ALWAYS PRESENT, null where unknown.

            {"name": "PGSB", "at": 1787270112.4, "how": "SET at a prompt",
             "reason": null}
            {"name": null, "at": null, "how": null, "reason": "power cycled"}

        `reason` AND NOT `why`, deliberately. Everywhere else in this daemon
        `why` is a controlled word a consumer branches on and `reason` is the
        sentence a person reads; this held free text under `why` for an hour
        and the guard that checks the word vocabulary caught it. There is no
        word vocabulary here -- an invalidation is described by what happened,
        which is an open set of sentences rather than of tokens. A consumer shows the machine's name
        alone when `name` is null; it must not substitute a likely profile,
        because the default being PGSB is exactly the assumption that made the
        cell-log label accidentally true for weeks.
        """
        with self._lock:
            return {"name": self._name, "at": self._at, "how": self._how,
                    "reason": self._reason}


PROFILE = TargetProfile()


# PKTTOOL's own words, observed on the card 2026-08-24 in the PGSB profile:
#
#     Scanning :
#     No packet drivers found - did you load one?
#
# Letters, not digits, which is what makes it usable here: this console's OCR
# turns 0 into 8 and `10 file(s)` into `18 file(s)`, and a check keyed on a
# number would inherit that.
_PKT_NONE = "no packet drivers found"

# A POSITIVE marker. OBSERVED on the card 2026-08-24, in NET:
#
#     Scanning :
#     Details for driver at software interrupt: 0x7E
#     Name: ODIPKT
#     Entry point: 1256:08DD
#     Version: 21 Class: 1 Type: 71 Interface Number: 0
#     ...
#
# `Name:` is chosen over `Details for driver at software interrupt:` for one
# reason: the latter carries a hex value, and `0x7E` came back off the glass as
# `@x7E`. EVERY MARKER HERE IS LETTERS. On this console a check that reads a
# number has a failure rate rather than a result.
_PKT_FOUND = ("name:", "odipkt")

# `C:\MTCP` IS NOT ON THE PATH IN THE NET PROFILE. `PKTTOOL SCAN` gives
# "Bad command or file name" on a machine whose driver is loaded and whose EXE
# is present -- it only appears to work after a `CD \MTCP`. Use the full path.
#
# This string contains no driver marker and no failure marker, so the matcher
# correctly answers "cannot tell" and the gate refuses. Correct, and maddening
# to debug: a perfectly configured machine, refused because the command was
# spelled without its directory. Recognised separately so the answer names the
# fix instead of leaving somebody to find it.
_PKT_NOT_RUN = "bad command or file name"


def packet_driver_seen(text, expect=None):
    """Did PKTTOOL find a packet driver? True / False / None.

    THREE VALUES, AND THE THIRD IS THE POINT. The tempting implementation is
    two-valued -- "no failure string, therefore a driver" -- and it is wrong in
    the direction that costs a reboot and an environment: a PKTTOOL that dies
    early, prints nothing, is missing from the card, or whose output the OCR
    mangles all produce no failure string, and all would read as success.

    So a driver is reported ONLY on a positive marker. The explicit failure
    line gives False. Everything else -- empty, truncated, unrecognised -- is
    None, which the caller must treat as "do not transfer", not as "probably
    fine". Same discipline as every other could-not-look on this rig: the
    instrument's silence is not the target's answer.

    `expect` names the driver this rig should see, so a profile can be
    specific where the generic marker is loose. It only ever ADDS a way to
    recognise a driver; it never turns an unrecognised answer into a refusal,
    because a driver by another name is still a driver -- `ODIPKT` is the ODI
    shim specifically, and a rig with a different NIC reports a different name.

    Pair a None with packet_driver_reason(), which names the causes it can
    recognise. Chief among them: the command not having run at all.
    """
    if not text:
        return None
    low = " ".join(str(text).split()).lower()
    if expect and str(expect).lower() in low:
        return True
    if any(m in low for m in _PKT_FOUND):
        return True
    if _PKT_NONE in low:
        return False
    return None


def ftp_log_suppressed(msg, authenticated):
    """Should this server log line be dropped? True to drop.

    THE LIVENESS PROBE IS A BARE TCP CONNECT, every few seconds, from every
    open tab. pyftpdlib logs an opened and a closed for each, so the journal
    fills with pairs from this host and the TARGET's own sessions -- the ones
    that say what actually happened -- are buried among them.

    SUPPRESSED BY AUTHENTICATION, NOT BY SOURCE ADDRESS. A probe connects and
    closes without a USER; the target always logs in. Filtering on the address
    would have hidden a real client that happened to run on this host, and
    would have said nothing about why it was hidden.

    Nothing meaningful goes. A successful login is still logged, and so is
    every RETR and STOR. What goes is a pair of lines about a socket that did
    nothing -- and the "opened" line is dropped unconditionally because at the
    moment it is written nobody has authenticated yet, so keeping it would
    mean deciding on information that does not exist.
    """
    if msg.startswith("FTP session opened"):
        return True
    if msg.startswith("FTP session closed") and not authenticated:
        return True
    return False


def packet_driver_reason(text):
    """Why packet_driver_seen() could not tell, where that is recognisable.

    A None from that function is a refusal, and a refusal whose cause is
    invisible is an outage with good manners. This turns the one cause we have
    actually met into an instruction.

    Returns None when there is nothing useful to add -- deliberately, rather
    than inventing a plausible explanation for output nobody has seen.
    """
    if not text or not str(text).strip():
        return ("nothing came back from the check at all, so this says the "
                "screen could not be read rather than anything about the "
                "target")
    low = " ".join(str(text).split()).lower()
    if _PKT_NOT_RUN in low:
        return ("the command did not run: C:\\MTCP is not on the PATH in the "
                "NET profile. Use the full path, C:\\MTCP\\PKTTOOL.EXE SCAN "
                "-- the driver may well be loaded")
    return None


def _configured_transfer_boards():
    """{board id: the literal word} from `targets:`, or None if unconfigured.

    Returns the WORD rather than a boolean, because the caller has to tell
    `unsupported` from `unknown` and from a target that is simply not listed.
    Anything that is not one of the three known words is returned as it was
    written, so a typo surfaces as a typo instead of failing open.
    """
    t = CFG.optional("targets")
    if t is vcconfig.ABSENT or t is vcconfig.NONE:
        return None
    out = {}
    for row in t:
        try:
            word = row.get("transfer")
            if word is not None:
                out[int(row["board_id"])] = str(word)
        except (TypeError, ValueError, KeyError):
            continue
    return out


class RegistryDriver(object):
    """TransferJob's driver, over the registry rather than a capability.

    RULE 1: A CAPABILITY MAY NOT CALL INTO ANOTHER. This job needs input and
    the LED channel, so it goes through `registry.execute()` -- the same door
    a client uses -- rather than reaching sideways into InputCapability. That
    is what makes it a sequence over capabilities instead of a capability with
    tentacles.

    Every wait here is closed-loop on the LED return channel. Nothing is a
    fixed sleep waiting for a machine to be ready, because a fixed sleep that
    is long enough on a good day is a race on a bad one, and this rig has
    produced four separate timing bugs of exactly that shape.
    """

    # Cold start to a prompt is ~90 s; the peer measured reset -> RDYPULSE at
    # ~16 s and prompt ~17 s after that. Generous, because the failure of
    # waiting too long is a slow refusal and the failure of waiting too little
    # is typing into a machine that is not listening.
    BOOT_TIMEOUT_S = 150.0
    # HOW LONG ONE SENT CHORD IS GIVEN TO START A RESET, before this class
    # concludes it needs a resend. NOT the same claim as "how long a reset
    # takes" -- REVISED 2026-08-26, UPWARD, ON REAL EVIDENCE THAT OVERTURNED
    # THE EARLIER READING OF THIS SAME RIG. Three Ctrl-Alt-Del sends in one
    # sitting each produced a clean, correctly-timed cycle -- Scroll Lock
    # clears, then sets again ~11-12 s later (RDYPULSE), matching FINDINGS
    # sec. 7 exactly -- so the chord was never being swallowed. What varied
    # was the gap between SENDING the chord and the clear STARTING: as long
    # as 85 s in one case. A 30 s window (this constant's value that same
    # day, before this fix) reads a chord that is genuinely working as a
    # failure and resends into a reset already under way -- which is a race,
    # not a recovery. Set past the worst delay actually measured, with real
    # margin, because the failure of waiting too long is a slow refusal and
    # the failure of waiting too little is a second chord landing mid-boot.
    MENU_TIMEOUT_S = 100.0
    PROMPT_TIMEOUT_S = 120.0
    TRANSFER_TIMEOUT_S = 180.0
    LED_POLL_S = 0.5
    # ONE resend, not a loop of short ones -- see NetJob._reboot_edge. The
    # 2026-08-26 evidence above points at DELAY, not loss, so eagerly
    # resending every few seconds was the wrong instinct: it fires a second
    # chord while the first may still be pending. This stays as a genuine
    # last resort, given a further RESEND_TIMEOUT_S once the first attempt's
    # full MENU_TIMEOUT_S has genuinely elapsed with no edge.
    RESET_ATTEMPTS = 2
    RESEND_TIMEOUT_S = 30.0

    def __init__(self, registry, pace=None):
        self.reg = registry
        self.pace = pace
        # Set by type_line when the LED says Caps Lock is on. NOT acted on --
        # carried, so the job can put it beside a result that may have been
        # typed in the wrong case. See type_line.
        self.caps_seen_on = False

    # -- typing ---------------------------------------------------------------

    def _do(self, cmd, **kw):
        req = dict(kw, cmd=cmd)
        req.setdefault("as", "transfer")
        if self.pace is not None:
            req["pace"] = self.pace
        return self.reg.execute(req)

    def combo(self, keys):
        return self._do("combo", keys=list(keys))

    def shot(self):
        """One diagnostic frame plus its own metadata, or None.

        DIAGNOSIS ONLY, the same seam as screen() above -- no verdict here
        ever depends on it. Unlike screen() this has a real body: the frame
        ring is a daemon-side capability this driver already reaches through
        the registry, the same door combo()/type_line() use, so there is
        nothing to inject from outside. Exists because a refusal with no
        picture beside it sends whoever reads the log back to the rig cold --
        see NetJob._fail().
        """
        r = self._do("shot")
        return r if isinstance(r, dict) else None

    def type_line(self, text):
        """Type a line, and RECORD the Caps Lock reading without acting on it.

        Caps Lock inverts what this harness types -- an established fact about
        this rig -- and wait_prompt() PROBES BY TOGGLING CAPS LOCK. So the
        readiness check corrupts the case of the command typed immediately
        after it, and it does so silently: DOS is case-insensitive about
        commands and paths, so `c:\\mtcp\\vcchk.bat` runs perfectly and only
        the ARGUMENTS come out wrong.

        Measured: a verification copy asked for as HELLO.TXT.CHK arrived as
        hello.txt.chk, and the wait timed out on a transfer the server log
        showed completing. The probe and the payload were fighting over one
        piece of global state.

        Checked rather than assumed off, because the failure is invisible in
        every other respect.

        IT USED TO PRESS THE KEY, AND THE PRESS BROKE A TRANSFER. 2026-08-24,
        measured end to end: the LED read `capslock: 1` on an `available`
        channel, the value was STALE, this "corrected" it by pressing -- which
        turned Caps Lock ON, because it was really off -- and every command
        typed afterwards came out inverted. The proof file landed as
        `netproof.txt` instead of `NETPROOF.TXT`, met a stale uppercase copy
        from an earlier session, and the run reported `no-net`: the target is
        not on the network, about a machine whose transfer was sitting
        completed in the server's own log. **The compensation caused the
        fault it existed to prevent.**

        THE PRESS IS ASYMMETRIC AND THE READ IS NOT. A wrong read reports
        something false; a wrong press CHANGES THE TARGET, in exactly the
        direction that breaks what follows. It helps only when the value is
        right, harms when the value is wrongly high, and does nothing when it
        is wrongly low -- two of three outcomes neutral or worse.

        AND ITS PURPOSE SHRANK THE SAME EVENING. While `at_prompt()` probed by
        toggling Caps Lock, the harness was itself the main reason caps was
        ever on, and this existed largely to clean up after our own probe.
        That probe moved to Num Lock. What is left is the operator at the KVM
        and a DOS program, which is a far smaller population.

        SO IT REPORTS AND DOES NOT ACT. The reading is recorded on the driver
        and surfaced by the job, so a transfer that comes out case-flipped has
        the reading sitting beside it and the diagnosis is thirty seconds
        rather than a 230-second `no-net` hunt. What consumes the information
        is `_await_incoming`, which is case-INSENSITIVE: the right kind of
        insensitivity is not needing caps to be right, rather than fixing it.

        DO NOT REPLACE THIS WITH "REFRESH, THEN PRESS". It is the obvious next
        move and it is a trap. The refresh hypothesis has three observations
        and no mechanism, and OPEN-FAULTS already prescribed one fix built on
        an untrusted lock-key value -- read the LED and invert the shift --
        which would have made things worse silently, in exactly the conditions
        where the value is least trustworthy. A compensation needs a
        precondition somebody can sign for, and no press has one today.
        """
        # READ AND RECORD. DO NOT PRESS. See the docstring: the press is what
        # broke a transfer tonight.
        if self._led("capslock") is True:
            self.caps_seen_on = True
        r = self._do("type", text=text)
        if not r.get("ok"):
            return r
        return self._do("key", keys=["enter"])

    def sleep(self, s):
        time.sleep(s)

    def menu_attempts(self):
        # The window is target physics and lives in the profile; the number of
        # attempts is derived from it rather than configured, because a
        # derived value presented as configuration is a value with its
        # reasoning deleted.
        window = 14.0
        try:
            prof = CFG.optional("harness.profile")
            if prof not in (vcconfig.ABSENT, vcconfig.NONE, None):
                pass
        except Exception:
            pass
        return max(2, int(window / 2.0))

    def transfer_timeout(self):
        return self.TRANSFER_TIMEOUT_S

    # -- the LED return channel -----------------------------------------------

    def _led(self, name):
        """One LED's level, or None when it cannot be read.

        THE VALUES ARE NESTED UNDER "leds" AND THIS READ THEM FROM THE TOP
        LEVEL, so every lookup returned None and arm() concluded Scroll Lock
        could not be set -- on a machine where it works perfectly. The
        transfer then refused with `no-witness`, which was the RIGHT refusal
        for a wrong reason: it declined to reboot into a state it could not
        witness, and the thing it could not witness was its own bug.

        A guard that fails closed still has to be right about what it saw.
        """
        r = self._do("leds") or {}
        leds = r.get("leds")
        if not isinstance(leds, dict):
            return None
        if leds.get("available") is not True:
            return None
        v = leds.get(name)
        return None if v is None else bool(v)

    def _await_led(self, name, want, timeout):
        end = time.time() + timeout
        while time.time() < end:
            v = self._led(name)
            if v is not None and v == want:
                return True
            time.sleep(self.LED_POLL_S)
        return False

    def arm(self):
        """Set Scroll Lock so POST clearing it is an EDGE.

        Without this the reset is invisible: if Scroll is already 0, POST
        clearing it changes nothing and the wait for 1 -> 0 times out on a
        machine that rebooted perfectly. Same reason arm_leds() exists for
        Caps Lock -- a level cannot show a transition.

        IT VERIFIES ITS OWN PRESS NOW, RATHER THAN ASSUMING IT LANDED, and
        that is a fix for a real failure of this feature rather than tidiness.
        The old shape read once, pressed if the read was not True, and
        returned whatever the next read said. Both of those reads can be wrong
        about the target:

        THE VALUE MAY NEVER HAVE BEEN A READING. The daemon publishes the
        lock LEDs as `available` on a channel it has never seen move -- the
        first sample of its life is recorded as proof, so retained sysfs
        values are served as current. If that phantom says Scroll Lock is
        already set, this SKIPS THE PRESS, POST clears a bit that was never
        set, no edge occurs, and the run refuses `no-reset`: "the machine
        never reset" about a machine that rebooted perfectly. That is the
        second time this exact sentence has been produced by an instrument
        fault rather than a target fault -- see the note above about reading
        the values from the wrong level.

        AND A PRESS CAN MOVE THE BIT THE WRONG WAY. If the read said not-set
        and the truth was set, one press clears it, and waiting for it to
        become set then times out on a machine that is fine.

        So: read, and press only if the reading disagrees -- then LOOK AGAIN.
        Two attempts, because the second one corrects a first press that went
        the wrong way, and stopping there bounds the stray keystrokes sent to
        a target we may not be able to observe at all.
        """
        for _ in range(2):
            if self._led("scrolllock") is True:
                return True
            self._do("key", keys=["scrolllock"])
            self._await_led("scrolllock", True, 5.0)
        return self._led("scrolllock") is True

    def wait_menu(self, timeout=None):
        """Scroll Lock 1 -> 0: POST ran, so the machine really reset.

        `timeout` lets a caller use a different window than the class
        default for one attempt -- NetJob._reboot_edge gives the first send
        the full MENU_TIMEOUT_S and a resend, if one is needed at all, only
        RESEND_TIMEOUT_S. Omit it for the plain one-shot behavior.
        """
        return self._await_led("scrolllock", False,
                               self.MENU_TIMEOUT_S if timeout is None
                               else timeout)

    def wait_boot(self):
        """Scroll Lock 0 -> 1: RDYPULSE ran, so AUTOEXEC finished."""
        return self._await_led("scrolllock", True, self.BOOT_TIMEOUT_S)

    def wait_prompt(self):
        """Is the BIOS keyboard ISR alive? NOT "is DOS at a prompt".

        A lock key is serviced by INT 09h and DOS is not involved, so this is
        True during an FTP transfer as well as at a prompt. It is a NECESSARY
        condition and never a sufficient one: what it genuinely detects is a
        program that HOOKS the vector, which is why it correctly reports the
        game as not-at-a-prompt.

        The transfer does not lean on it the way its name invites -- the thing
        that actually proves a transfer finished is the file arriving here.

        IT PROBES NUM LOCK, AND IT USED TO PROBE CAPS LOCK. Two reasons, and
        the first one already cost this project a transfer:

        THE PROBE CORRUPTED WHAT WAS TYPED NEXT. Caps Lock inverts the case of
        every letter this harness sends, so a readiness check run before a
        command silently changed that command's ARGUMENTS -- `HELLO.TXT.CHK`
        was asked for and `hello.txt.chk` arrived, and the wait timed out on a
        transfer the server log showed completing. type_line() defends against
        that by proving Caps Lock off first, which is a compensation for a
        hazard this probe created. Probing a bit that changes nothing removes
        the dependency instead. Num Lock is safe here because the char map
        contains no keypad codes at all, so nothing typed by `type` is
        sensitive to it -- and the boot-menu digit is a number-row key.
        (`kp*` keys ARE reachable by name. Anything that sends one becomes
        sensitive to Num Lock where it was not before; nothing on this path
        does.)

        THE CAPS LOCK NODE IS THE ONE MEASURED TO MISBEHAVE. On the rig,
        2026-08-24: the node named `capslock` read 1 while the target's Caps
        Lock was OFF, stayed 1 when it came ON, and moved only on a second
        press -- on a channel that was PROVEN, so this is not the phantom.
        Which node moved was not consistent between two identical presses. The
        num lock node moved cleanly under the same test. A probe that reads a
        bit to decide whether the machine is responsive should not be reading
        the one bit known to lie about itself.

        WHAT THAT MEANS HERE, STATED BECAUSE IT IS NOT FIXED: this probe is a
        tiebreaker after something has already gone wrong, and a false
        negative turns a per-file `no-return` into `no-prompt`, which stops
        the rest of the run. On the old bit that was a live risk. On this one
        it is smaller and it is not zero.
        """
        before = self._led("numlock")
        if before is None:
            return False
        end = time.time() + self.PROMPT_TIMEOUT_S
        while time.time() < end:
            self._do("key", keys=["numlock"])
            if self._await_led("numlock", not before, 8.0):
                # Restore, ALWAYS. On the failure path the keystroke was
                # usually only buffered, so leaving it unrestored means a
                # failed probe corrupts the state the next probe reads.
                self._do("key", keys=["numlock"])
                self._await_led("numlock", before, 8.0)
                return True
            time.sleep(1.0)
        self._do("key", keys=["numlock"])
        return False

    def screen(self):
        """Whatever text the screen holds, or None.

        DIAGNOSIS ONLY. The daemon has frames and no OCR -- that lives in the
        client -- so this returns None here and the job's gate does not depend
        on it. It is a seam for a caller that can read the screen to pass one
        in, not a capability being claimed.
        """
        return None


class NetJob(object):
    """The spine both directions share: reboot into NET, prove it, come back.

    NO OCR IN ANY VERDICT, AND THAT IS THE DESIGN RATHER THAN AN ECONOMY.
    Every witness here is either a hardware LED edge or a byte arriving on
    this host:

        the machine reset          Scroll Lock 1 -> 0   (POST clears it)
        the boot completed         Scroll Lock 0 -> 1   (RDYPULSE, ~16 s)
        NET really booted          a file arrives in incoming/
        the payload really landed  its bytes come back and the sha matches
        what is on the card        a DIR arrives in incoming/ as a FILE

    The earlier design gated on reading `PKTTOOL SCAN` off the screen. That
    needed OCR in the daemon -- which lives in the client, not here -- and it
    read a console whose OCR turns 0 into 8. The replacement is already this
    project's own idea, stated in harness/vcctrl-collect: **a file landing in
    incoming/ proves NET booted, and nothing else can produce one.**

    It is also a STRONGER claim than the screen read was. One arrival proves
    the packet driver is loaded, the address in the BAT is right, the
    credentials are right and the FTP client works -- four things a `PKTTOOL`
    line says nothing about, established together, from the side that counts.

    `PKTTOOL` keeps a job: it is the DIAGNOSIS when the arrival does not
    happen, not the gate. Which is the right way round -- a diagnostic that
    cannot fail the run cannot mislead it either.

    ONE CLASS BECAUSE THE SEQUENCE IS ONE FACT. Sending files and fetching
    them differ only in what gets typed once the prompt is there: the arming,
    the reboot, the blind menu selection, the readiness pulse, the arrival
    that proves the network and the reboot back are identical in both. Held as
    two copies, the next fix would land in one of them -- and the half that
    did not get it would be the half nobody ran that week.

    The driver is injected so the whole sequence is testable without a rig.
    """

    # The proof file is one that MUST already exist for a transfer to be
    # possible at all: mTCP's own config, which every BAT points MTCPCFG at.
    # Sending something we placed there would prove less -- it would have to
    # get there first, which is the thing being tested.
    PROOF_SOURCE = "C:\\MTCP\\TCP.CFG"
    PROOF_NAME = "NETPROOF.TXT"

    def __init__(self, cap, driver, do_return=True, log=None):
        self.cap = cap
        self.d = driver
        self.do_return = do_return
        # THE CALLER'S LIST, NOT A PRIVATE ONE. This kept its own and the job
        # record was only updated when run() returned -- so file_status showed
        # an empty log for the entire three minutes and then everything at
        # once. Live progress is the whole reason this is a job rather than a
        # blocking call, and it was the one thing it did not do.
        self.log = [] if log is None else log
        # WHETHER THE MACHINE IS IN NET RIGHT NOW, tracked rather than
        # inferred from `do_return`. The successful paths already reported it;
        # the REFUSALS did not, and half of them can only happen after the
        # reboot -- a listing that never came back, a selection over the
        # ceiling. "The run failed" and "the target is sitting in the profile
        # no measured run may start from" are two facts, and the second one is
        # the one that costs somebody a cell.
        self.in_net = False

    def _say(self, phase, text, **kw):
        rec = dict(kw, phase=phase, text=text, t=time.time())
        self.log.append(rec)
        return rec

    def _reboot_edge(self):
        """Send Ctrl-Alt-Del and wait for the reset edge, resending the
        chord ONCE, as a last resort, if a full MENU_TIMEOUT_S genuinely
        finds no edge -- not on a short window, and not repeatedly.

        REVISED 2026-08-26, SAME DAY AS THE FIRST VERSION OF THIS METHOD,
        ON EVIDENCE THAT OVERTURNED ITS OWN DIAGNOSIS. The first version of
        this fix (see git history) read two "no-reset" refusals as a
        swallowed chord and started resending every ~10 s. Then a chord sent
        with the operator watching the physical screen was seen to actually
        reboot the machine, live, while `vcctrl_led_changes` still showed no
        transition at all -- and a few checks later it had: `led_changes`
        for that sitting shows THREE separate Scroll Lock clear -> set
        cycles, each ~11-12 s apart, exactly the FINDINGS sec. 7 shape,
        confirming three clean resets, not zero. What actually happened is
        that ONE of the cycles started 85 SECONDS after its chord was sent.
        A 30 s window split three ways (10 s each) reads a chord that is
        genuinely working as a failure and resends INTO a reset already
        under way -- a race the harness was creating for itself, not a
        recovery from anything the target did.

        So the fix is not "resend eagerly", it is "wait long enough for one
        send to prove itself" -- MENU_TIMEOUT_S is now set well past the
        worst delay actually measured, and only a single resend follows if
        that entire window truly finds nothing, itself given a further
        RESEND_TIMEOUT_S rather than a slice of the first window.
        """
        self.d.combo(["ctrl", "alt", "delete"])
        if self.d.wait_menu(getattr(self.d, "MENU_TIMEOUT_S", 100.0)):
            return True
        attempts = getattr(self.d, "RESET_ATTEMPTS", 2)
        resend_s = getattr(self.d, "RESEND_TIMEOUT_S", 30.0)
        for _ in range(attempts - 1):
            self.d.combo(["ctrl", "alt", "delete"])
            if self.d.wait_menu(resend_s):
                return True
        return False

    # -- getting there, and back ----------------------------------------------

    def _enter_net(self):
        """True, or the refusal that stopped it. Leaves a proved NET prompt.

        THE ORDER IS THE POINT RATHER THAN THE LIST. Everything checkable from
        this host is checked from this host, because a refusal after the
        reboot has already cost two minutes and the target's entire
        environment for a fault a shell command would have fixed.
        """
        snap = self.cap.snapshot()
        if not snap["available"]:
            return self._fail(snap["why"], snap["reason"])

        # THE EXPENSIVE CHECK BEFORE THE EXPENSIVE ACTION. A server that is not
        # answering costs two minutes and the target's whole environment if it
        # is discovered after the reboot, and a shell command if before.
        live, why = self.cap._reachable(timeout=5.0)
        if live is not True:
            return self._fail("unreachable" if live is False else "unchecked",
                              why)

        # ARM FIRST. POST clears Scroll Lock, so the reset is only visible as
        # an edge if it was set beforehand -- otherwise the wait for 1 -> 0
        # times out on a machine that rebooted perfectly.
        if hasattr(self.d, "arm") and not self.d.arm():
            return self._fail("no-witness",
                              "could not set Scroll Lock, so a reboot would "
                              "be invisible -- refusing rather than rebooting "
                              "blind")
        self._say("reboot", "rebooting into the NET profile")
        PROFILE.invalidate("rebooting for a file transfer")
        # SET BEFORE THE KEYSTROKE, NOT AFTER THE BOOT. From here on the
        # machine is rebooting towards NET, and every refusal below this line
        # leaves it there or somewhere unknown -- which is the state worth
        # reporting. Setting it once the boot succeeded would have said False
        # on exactly the paths where it matters most.
        self.in_net = True

        # BLIND, AND TIMED. The CONFIG.SYS menu is text mode 03h at 70 Hz and
        # cannot be captured, so nothing can confirm the selection while it is
        # happening -- FINDINGS sec. 8: the digit alone does not work, the
        # digit AND Enter does, repeated across the window because the POST
        # edge is not observable either. The chord that gets it there is
        # retried the same way -- see _reboot_edge.
        if not self._reboot_edge():
            return self._fail("no-reset",
                              "the machine never reset -- Scroll Lock did not "
                              "clear, so the reboot did not happen")
        self._say("select", "selecting NET, blind")
        for _ in range(self.d.menu_attempts()):
            self.d.type_line("5")
            self.d.sleep(2.0)

        if not self.d.wait_boot():
            return self._fail("no-boot",
                              "no readiness pulse after the reboot: the "
                              "machine did not finish booting, or "
                              "RDYPULSE.COM is missing from the card")

        return self._prove_net()

    def _prove_net(self):
        """One arrival proves four things. Nothing else here proves any."""
        self._say("attest", "proving NET by making the target send a file back")
        before = time.time()
        self.d.type_line("C:\\MTCP\\VCCHK.BAT %s %s"
                         % (self.PROOF_SOURCE, self.PROOF_NAME))
        if self.cap._await_incoming(self.PROOF_NAME, before,
                                    self.d.transfer_timeout()):
            self._say("attest", "NET confirmed: the target reached this host")
            # CONSUMED, LIKE THE LISTING. It has served its whole purpose the
            # instant it is seen, and leaving it behind is what put a
            # half-hour-old NETPROOF.TXT in the way of a fresh netproof.txt.
            # The finder no longer trips over that, but a run should not be
            # leaving landmines for the next one either -- removing the
            # collision source and fixing the finder are different repairs and
            # this feature needs both.
            self.cap._drop_incoming(self.cap._incoming_path(self.PROOF_NAME))
            return True
        # The gate has failed. NOW ask the screen why -- as a diagnosis, which
        # cannot promote a failure into a pass because it runs only on this
        # path and its answer is never a verdict.
        detail = ""
        try:
            text = self.d.screen()
        except Exception:
            text = None
        seen = packet_driver_seen(text)
        if seen is False:
            detail = (" PKTTOOL says no packet driver is loaded, so the menu "
                      "selection did not land on NET.")
        else:
            hint = packet_driver_reason(text)
            if hint:
                detail = " " + hint
        return self._fail("no-net",
                          "nothing arrived from the target, so NET is not up, "
                          "or it cannot reach this host, or the credentials on "
                          "the card do not match.%s" % detail)

    def _leave_net(self):
        """The reboot back, or the sentence that says why there was not one.

        ARMED, LIKE THE OUTWARD LEG, AND IT WAS NOT -- WHICH MADE THIS WHOLE
        BRANCH A CHECK THAT COULD NOT FAIL. Measured on the rig 2026-08-24, in
        the first real fetch:

            +69.5 s  return   returning to the menu default
            +69.5 s  return   the machine booted; which profile is unread

        Zero seconds apart, on a machine that takes about forty to come back.
        `wait_boot()` waits for Scroll Lock to READ 1, and RDYPULSE from the
        boot we were already in had left it at 1 -- so it returned on its first
        poll, before the reset had even happened. The line said the machine had
        booted; nothing had been observed at all.

        The cost is not the wrong log line. It is that `left_in_net` was set
        false on the strength of it, and that the failure branch below --
        the one that warns the target may still be in NET -- was unreachable
        for as long as this code has existed. A witness that cannot fail is
        not a witness, and this one was reporting on the ONE state the whole
        feature is careful about.

        So the return leg now uses the same two edges the outward leg does:
        arm, then 1 -> 0 (POST cleared it, so a reset really happened), then
        0 -> 1 (RDYPULSE ran, so a boot really finished). Nothing is typed at
        the menu -- the timeout landing on the default is the point.

        WHEN IT CANNOT SEE, IT SAYS SO AND STAYS PESSIMISTIC. `in_net` is only
        cleared by a boot that was actually witnessed; an unarmed or unseen
        return leaves it standing, because "I could not look" is not a reading
        of "the machine came back".
        """
        self._note_caps()
        if not self.do_return:
            self._say("stay", "left in NET at your request -- a measured run "
                              "must not start from here")
            return
        armed = self.d.arm() if hasattr(self.d, "arm") else False
        self._say("return", "returning to the menu default")
        PROFILE.invalidate("returning from a file transfer")
        if not armed:
            # UNWITNESSABLE, SO ONE SHOT. Resending a chord we cannot see the
            # result of would not buy anything -- see _reboot_edge for why a
            # resend loop needs the LED edge to know when to stop.
            self.d.combo(["ctrl", "alt", "delete"])
            self._say("return", "COULD NOT SET SCROLL LOCK BEFORE THE RETURN "
                                "REBOOT, so nothing here can see whether the "
                                "machine came back. It was told to; that is "
                                "all this can say", warn=True)
            return
        if not self._reboot_edge():
            self._say("return", "NO RESET AFTER THE RETURN REBOOT -- Scroll "
                                "Lock never cleared, so the machine may still "
                                "be in NET, which no measured run may start "
                                "from", warn=True)
            return
        if self.d.wait_boot():
            # DELIBERATELY NOT ASSERTING WHICH PROFILE. The timeout lands on
            # the default and that is what we typed nothing to change -- but
            # nothing here READ it, and a name written down without a reading
            # behind it is the hardcoded "(profile is PGSB)" all over again.
            #
            # WHAT IT DOES SAY is that the machine is no longer in NET, which
            # is a weaker claim and an honest one: it rebooted, and nothing
            # selected NET this time. A missing pulse leaves in_net standing,
            # which is what the warning below is about.
            self.in_net = False
            self._say("return", "the machine booted; which profile is unread")
        else:
            self._say("return", "NO READINESS PULSE AFTER THE RETURN REBOOT -- "
                                "the machine may still be in NET, which no "
                                "measured run may start from", warn=True)

    def _note_caps(self):
        """Put the Caps Lock reading beside the result, if it was ever on.

        THE OTHER HALF OF "REPORT, DO NOT ACT". type_line stopped pressing the
        key because a stale reading made the press CAUSE an inverted case --
        but the reading is still worth having, because a transfer that comes
        out case-flipped is otherwise diagnosed from scratch. It cost 230
        seconds and a screenshot to work out that a `no-net` refusal meant
        "the file arrived under a different name"; this line would have said
        so in the log.

        Emitted once, on the way out, and only when it was seen ON. A note
        that appears on every run is a note nobody reads.
        """
        if getattr(self.d, "caps_seen_on", False):
            self._say("caps", "THE CAPS LOCK LED READ ON WHILE TYPING. Not "
                              "acted on -- a press would change the target on "
                              "the strength of a value that may be stale. If "
                              "anything here came back under an unexpected "
                              "name, this is the first thing to suspect",
                      warn=True)

    def _cancelled(self):
        job = FilesCapability._job
        return bool(job and job.get("cancel"))

    def _fail(self, why, reason):
        """Refuse the run, naming which gate stopped it.

        THE VOCABULARY, AND IT IS NOT CLOSED. What stopped the RUN:

            empty-queue   nothing staged -- a run that rebooted and reported
                          success on an empty queue would be the purest form
                          of a check passing on nothing
            unreachable   the file server is not answering (checked BEFORE
                          anything reboots)
            unchecked     that check could not be made
            no-witness    Scroll Lock could not be set, so a reboot would be
                          invisible -- refusing beats rebooting blind
            no-reset      Scroll Lock never cleared, so the reboot did not
                          happen at all
            no-boot       no readiness pulse: the machine did not finish
                          booting, or RDYPULSE.COM is missing
            no-net        nothing arrived from the target -- NET is not up, or
                          it cannot reach this host, or the card's credentials
                          do not match. One arrival would have proved all three
            no-listing    the DIR of the target's OUT directory never came
                          back, so what is on the card is unknown -- and an
                          unknown directory is not an empty one
            too-large     the selection is over the ceiling (checked BEFORE
                          anything is typed, and against the total)
            busy          a transfer is already running -- refused rather than
                          queued, because two runs interleaving their reboots
                          would each misread the other's machine state
            nothing-asked a fetch that named nothing. It will not guess
                          between "everything" and "the listing", because both
                          readings cost a reboot
            bad-dir       the directory asked for cannot be typed at this
                          machine -- not an absolute DOS path, not 8.3, or it
                          contains something a command line would eat. Refused
                          rather than repaired: a path we had to alter is not
                          the path the caller meant
            unsequenced   no registry to drive input through
            crashed       the job raised; the target is very likely still in
                          NET, which nothing downstream can otherwise tell

        And what stopped ONE FILE, which never stops the run on its own:

            no-prompt     the prompt did not come back; the machine's state is
                          unknown and the next command would be typed blind.
                          This one DOES stop the run, because everything after
                          it would be typed into the dark
            no-return     a file did not come back, so whether it arrived on
                          the target is unknown
            sha-mismatch  it came back different: the copy on the target is
                          not the file that was staged
            not-listed    asked for by name, and the target's own DIR does not
                          have it. Never typed at the machine
            unsafe-name   the name does not survive the 8.3 round trip, so it
                          cannot be joined onto a path here or typed there
            empty         zero bytes on the card. A zero-byte arrival cannot
                          be told from no arrival at all, so it is refused
                          rather than reported as a success
            size-mismatch what came back is not the size the target's DIR said
                          it was -- a short transfer, which is the failure
                          GET.BAT could never see
            unstable      two fetches of the same file disagree

        `unsupported`, `unknown` and `not_configured` reach here unchanged
        from FilesCapability.snapshot(), which is where they are defined, and
        the listing's own words are defined on dos_dir_listing().

        A DIAGNOSTIC FRAME RIDES ALONG, IF ONE IS AVAILABLE. Every `why` above
        is "something we expected did not happen" -- the exact class of event
        where an image of the screen at that instant is worth more than
        another line of reasoning about why it should have worked. Attached
        here rather than only shown live, because whoever reads this later
        (a peer session, a human the next morning) was not necessarily
        watching when it happened. Never a verdict: `shot()` can come back
        None -- no picture, or the call itself failing -- and the refusal is
        reported exactly the same either way.
        """
        self._note_caps()
        shot = None
        if hasattr(self.d, "shot"):
            try:
                shot = self.d.shot()
            except Exception:
                shot = None
        self._say("refused", reason, why=why, shot=shot)
        return {"ok": False, "why": why, "reason": reason, "files": [],
                "left_in_net": self.in_net, "log": self.log}


class TransferJob(NetJob):
    """One transfer: reboot into NET, send the queue, verify, come back.

    THE VERIFICATION IS A ROUND TRIP AND IT IS THE STRONG DIRECTION. The
    staged copy was sha256'd when it landed here, so pulling the target's copy
    back and comparing proves the payload rather than the transport. PullJob
    is the same sequence with a weaker verdict available to it, and says so.
    """

    def __init__(self, cap, driver, dest=None, do_return=True, log=None):
        NetJob.__init__(self, cap, driver, do_return=do_return, log=log)
        self.dest = dest or (cap.settings or {}).get("dest", DEFAULT_DEST)

    def run(self):
        """{"ok", "why", "files": [...], "log": [...]}."""
        queue = self.cap._queued()
        if not queue:
            # A CHECK MAY NOT PASS ON NOTHING. An empty queue that rebooted the
            # machine and reported success would be the purest form of it.
            return self._fail("empty-queue",
                              "nothing is staged, so there is nothing to send")

        gate = self._enter_net()
        if gate is not True:
            return gate

        results = []
        for rec in queue:
            if self._cancelled():
                self._say("cancel", "stopped between files, as asked")
                break
            results.append(self._send_one(rec))
            # BOTH why VALUES STOP THE RUN, NOT ONLY "no-prompt" -- same
            # reasoning as PullJob's identical loop, and the same measured
            # cause (docs/OPEN-FAULTS.md sec 16). at_prompt()/wait_prompt()
            # tests the BIOS ISR, not "DOS is at a clean prompt": a VCGET.BAT
            # command truncated by the keyboard buffer leaves DOS sitting on
            # an unfinished input line while the ISR stays responsive, which
            # reports as "no-return" here -- indistinguishable, from this
            # signal alone, from a prompt that is genuinely fine but a file
            # that genuinely failed. Typing the next VCGET over that unread
            # line is exactly the concatenation this function already refuses
            # for "no-prompt"; there is no cheaper way to tell the two apart.
            if results[-1].get("why") in ("no-prompt", "no-return"):
                why = results[-1]["why"]
                self._say("abort", "%s -- stopping rather than typing blind"
                                   % ("the prompt did not come back"
                                      if why == "no-prompt" else
                                      "a file did not come back and the "
                                      "prompt's state cannot be trusted"))
                break

        cancelled = self._cancelled()
        self._finish(results)
        left = [q["name"] for q in self.cap._queued()]
        # TWO DIFFERENT FACTS, AND THEY WERE ONE FLAG. `ok` answers "did
        # everything I attempted succeed"; `complete` answers "was everything
        # you asked for done". A cancelled run reported ok=True with a file
        # still sitting in the queue -- true, and read by anything branching
        # on it as "all sent".
        #
        # ok stays as it was rather than being folded into completion: the
        # operator ASKED to stop, and reporting a deliberate act as a failure
        # is the mirror of the same mistake.
        return {"ok": all(r["ok"] for r in results), "why": None,
                "complete": bool(results) and not cancelled and not left,
                "cancelled": cancelled, "remaining": left,
                # Returned for a DIRECT caller, and popped by _file_send
                # because there it is already the live shared list. Dropping
                # it here made run() incomplete for anyone not going through
                # the job wrapper.
                "files": results, "log": self.log,
                # THE TRACKED FACT, NOT THE INTENTION. This was
                # `not self.do_return`, which reports a run that asked to come
                # back and got no readiness pulse as one that came back -- in
                # the same result whose log says the machine may still be in
                # NET. Two statements, one of them read by consumers, and they
                # disagreed exactly when it mattered.
                "left_in_net": self.in_net}

    def _send_one(self, rec):
        name = rec["name"]
        self._say("send", "sending %s" % name, name=name)

        # ONE TYPED COMMAND. The BAT fetches and then sends the file straight
        # back, so nothing here has to decide when DOS is ready for a second
        # one -- a question with no honest answer on this machine, since the
        # only non-OCR readiness signal tests whether the BIOS keyboard ISR is
        # alive and that is true all the way through FTP.EXE. Asking it cost a
        # command truncated to fifteen characters, the BIOS buffer depth.
        #
        # ARRIVAL PROVES THE TRANSFER AND SAYS NOTHING ABOUT THE PAYLOAD. What
        # closes that is the bytes coming back and matching -- and that only
        # means anything because the staged copy was sha-verified when it
        # landed, or this would compare a wrong file against itself and pass.
        back = name + ".CHK"
        before = time.time()
        self.d.type_line("C:\\MTCP\\VCGET.BAT %s %s" % (name, self.dest))
        if not self.cap._await_incoming(back, before,
                                        self.d.transfer_timeout()):
            # NOTHING CAME BACK, AND THAT IS TWO DIFFERENT FACTS: the
            # transfer failed, or the machine is wedged. Only the second one
            # means the next file would be typed into the dark.
            #
            # So the readiness probe is used HERE and nowhere else -- as a
            # tiebreaker after something has already gone wrong, never as a
            # gate in the happy path. It is a weak signal (it tests whether
            # the BIOS keyboard ISR is alive, not whether DOS is reading), and
            # a weak signal is worth having when the alternative is guessing
            # between two very different situations.
            alive = self.d.wait_prompt()
            return {"name": name, "ok": False,
                    "why": "no-return" if alive else "no-prompt",
                    "reason": ("%s did not come back, so whether it arrived on "
                               "the target is unknown" % name) if alive else
                              ("%s did not come back and the machine is not "
                               "responding to a keystroke either" % name)}
        got = self.cap._incoming_sha(back)
        if got != rec.get("sha256"):
            return {"name": name, "ok": False, "why": "sha-mismatch",
                    "reason": "%s came back with a different sha256, so the "
                              "copy on the target is not the file that was "
                              "staged" % name, "sha256": got}
        self._say("verify", "%s verified byte for byte" % name, name=name)
        # Compared, therefore spent. Same reason as the proof file above: two
        # spellings of one name in this directory is what a case-unstable far
        # end produces across two runs, and the cheapest way not to have that
        # problem is not to keep the first one.
        self.cap._drop_incoming(self.cap._incoming_path(back))
        return {"name": name, "ok": True, "why": None, "sha256": got}

    def _finish(self, results):
        # CLEAR WHAT IS VERIFIED, KEEP WHAT FAILED. A queue is one transfer's
        # worth and not a library: a verified file's bytes exist on the target,
        # and a failed one is exactly what somebody wants to retry without
        # uploading it again from a phone.
        for r in results:
            if r["ok"]:
                self.cap._file_queue({"action": "clear", "name": r["name"]})
        self._leave_net()


class PullJob(NetJob):
    """One fetch: reboot into NET, read C:\\XFER\\OUT, bring files back, return.

    THE MIRROR OF TransferJob, AND ITS VERDICT IS WEAKER BY ONE STEP. That is
    written here rather than left to be inferred, because the two directions
    look symmetric and are not. A push is proved against a known quantity: the
    staged copy was sha256'd when it landed on this host, so the round trip
    compares the target's copy against something whose bytes are certain.
    Nothing on a DOS 6.22 machine can hash a file, so a pull has no such
    quantity to compare against. What it has instead:

        THE LISTING'S SIZE. `DIR` states the file's length, and that number
        reaches this host AS A FILE -- not off a console that reads 13,800 as
        13,808. A transfer that arrives short cannot match it, and short is
        the failure that otherwise looks exactly like success.

        OPTIONALLY, A SECOND FETCH, compared against the first. That proves
        the path is repeatable. It does NOT prove either copy equals what is
        on the card, and calling it "byte for byte" would be borrowing a
        phrase from the other direction, where it is earned.

    So the result says WHICH check ran -- `size` or `size+repeat` -- rather
    than letting one word cover both. The card's own account of the file is
    the strongest witness available in this direction; it is not the same
    claim as the upload path's, and the vocabulary keeps them apart.

    THE LISTING IS ALSO THE SAFETY. Nothing is typed at the target that did
    not come out of its own DIR: a name asked for and not listed is refused
    here, never sent, so a typo cannot become an FTP session for a file that
    does not exist -- and a name that does not survive the 8.3 round trip is
    shown to the operator and refused, because a name we had to alter is one
    that would not match the file on the card anyway.
    """

    LISTING_NAME = "VCLIST.TXT"

    def __init__(self, cap, driver, names=None, want_all=False,
                 refresh_only=False, paranoid=False, do_return=True, log=None,
                 out_dir=None, already_net=False):
        NetJob.__init__(self, cap, driver, do_return=do_return, log=log)
        self.names = list(names or ())
        self.want_all = bool(want_all)
        self.refresh_only = bool(refresh_only)
        self.paranoid = bool(paranoid)
        # VALIDATED BY THE CALLER, NOT HERE, and the caller is `_file_pull`.
        # This constructor is also used directly by tests, so it accepts what
        # it is given -- but every path a request can reach runs the string
        # through dos_dir_path() first, because it is about to be typed at a
        # machine with no quoting.
        self.out_dir = out_dir or cap._out_dir()
        self.already_net = bool(already_net)

    def run(self):
        """{"ok", "why", "files": [...], "listing": {...}, "log": [...]}."""
        if self.already_net:
            # THE REBOOT IS SKIPPED. THE PROOF IS NOT. `--already-net` says
            # where the caller believes the machine is, and a belief is not a
            # reading -- so the arrival gate still runs, and it is the same
            # gate: a file landing here proves the packet driver, the address,
            # the credentials and the client, whether or not we rebooted to
            # get them. A skipped reboot must not become a skipped check.
            self._say("attest", "told the machine is already in NET; "
                                "not rebooting, still proving it")
            gate = self._prove_net()
            if gate is True:
                # PROVED, THEREFORE IN NET, and `in_net` has to say so or the
                # return leg would reason from "we never rebooted in" and
                # report a machine it left in NET as one that was never there.
                self.in_net = True
        else:
            gate = self._enter_net()
        if gate is not True:
            return gate

        listing = self._read_listing()
        if listing is None:
            self._leave_net()
            return self._fail("no-listing",
                              "the target never sent back a DIR of %s, so "
                              "what is on the card is unknown -- which is not "
                              "the same as it being empty" % self.out_dir)
        # STORED WHETHER OR NOT IT PARSED, and the store keeps the last
        # readable one separately. A failed read must not delete a good
        # listing (it is older, not wrong), and it must not be hidden either
        # -- "could not look" is a fact about this attempt and belongs beside
        # the answer it failed to refresh.
        #
        # FILED UNDER THE DIRECTORY WE ASKED FOR, NEVER THE ONE THE TEXT
        # CLAIMS. That distinction looked pedantic until the hardware made it:
        # `DIR C:\NOSUCH` on a path that does not exist is re-read by DOS as a
        # filename pattern, and it printed `Directory of C:\`. The failure was
        # therefore filed under `C:\` -- a directory nobody asked about -- and
        # `file-list --from C:\NOSUCH` could not find the record of its own
        # failure. A reading belongs to the thing it is a reading OF, and on
        # the failure path the text is the least reliable witness to that.
        self.cap._save_listing(listing, where=self.out_dir)
        if not listing["ok"]:
            self._leave_net()
            return self._fail(listing["why"], listing["reason"])

        n = len(listing["files"])
        self._say("list", "%s holds %d file%s" % (self.out_dir, n,
                                                  "" if n == 1 else "s"),
                  listing=True)
        if self.refresh_only:
            # A LISTING IS A RESULT. Nothing was fetched because nothing was
            # asked for, so this is complete rather than empty-handed.
            self._leave_net()
            return {"ok": True, "why": None, "complete": True,
                    "cancelled": False, "remaining": [], "files": [],
                    "listing": listing, "log": self.log,
                    "left_in_net": self.in_net}

        selected, refused = self._select(listing)
        if not selected and not refused:
            # NOTHING TO FETCH IS NOT A FAILED FETCH. The run rebooted, read
            # the directory and found it empty; reporting that as ok=False
            # with no failing file in it would be a refusal nobody can act on,
            # and the pressure it creates is to fetch something to make the
            # run look successful.
            self._say("list", "nothing to fetch: %s is empty" % self.out_dir)
            self._leave_net()
            return {"ok": True, "why": None, "complete": True,
                    "cancelled": False, "remaining": [], "files": [],
                    "listing": listing, "log": self.log,
                    "left_in_net": self.in_net}
        if selected:
            verdict = size_verdict([r["bytes"] for r in selected])
            if not verdict["ok"]:
                # BEFORE ANYTHING IS TYPED, and against the total rather than
                # the biggest -- the same rule the staging side applies, for
                # the same reason: files walk past a per-file ceiling one at a
                # time.
                self._leave_net()
                return self._fail("too-large", verdict["reason"])
            if verdict["why"]:
                self._say("size", verdict["reason"], warn=True)

        results = list(refused)
        done = []
        for rec in selected:
            if self._cancelled():
                self._say("cancel", "stopped between files, as asked")
                break
            r = self._fetch_one(rec)
            results.append(r)
            done.append(rec["name"])
            # BOTH why VALUES STOP THE BATCH, NOT ONLY "no-prompt". A leg that
            # times out with "no-return" means wait_prompt() found the BIOS
            # keyboard ISR alive -- which is NOT "DOS is at a clean prompt"
            # (wait_prompt's own docstring: "NOT is DOS at a prompt"). Measured
            # 2026-08-25 (docs/OPEN-FAULTS.md sec 16): a VCCHK.BAT command
            # truncated by the BIOS keyboard buffer leaves its Enter unsent,
            # so the ISR stays responsive (a "no-return" leg) while DOS is
            # still sitting on that unfinished input line. The NEXT file's
            # _fetch_one then typed straight over it -- two commands
            # concatenated into one, DOS answered "Bad command or file name",
            # and neither file arrived. Continuing past "no-return" is exactly
            # the "typing blind" this function already refuses to do for
            # "no-prompt"; there is no cheap way from here to tell "the file
            # genuinely never arrived, prompt is fine" apart from "the prompt
            # has an unconsumed command sitting in it", so both must be
            # treated as unsafe to type over.
            if r.get("why") in ("no-prompt", "no-return"):
                self._say("abort", "%s -- stopping rather than typing blind"
                                   % ("the prompt did not come back"
                                      if r["why"] == "no-prompt" else
                                      "a file did not come back and the "
                                      "prompt's state cannot be trusted"))
                break

        cancelled = self._cancelled()
        self._leave_net()
        left = [r["name"] for r in selected if r["name"] not in done]
        # THE SAME TWO FACTS THE SEND SIDE KEEPS APART. `ok` answers "did
        # everything I attempted succeed" and is vacuously true when nothing
        # was attempted -- a run cancelled before the first file. `complete`
        # is what says the job was done, and it cannot be true on nothing:
        # an empty selection has already returned above, saying so positively.
        return {"ok": all(r["ok"] for r in results), "why": None,
                "complete": not cancelled and not left and not refused,
                "cancelled": cancelled, "remaining": left,
                "files": results, "listing": listing, "log": self.log,
                "left_in_net": self.in_net}

    # -- reading the directory ------------------------------------------------

    def _read_listing(self):
        """The target's DIR of its OUT directory, parsed, or None if none came.

        ONE TYPED COMMAND, for the reason the whole feature is built this way:
        `VCLIST.BAT` redirects the DIR into a file and sends it, so nothing
        here has to decide when DOS is ready for a second command. The answer
        to that question cost this project a command truncated to fifteen
        characters, and there is still no honest way to ask it.
        """
        self._say("list", "reading %s" % self.out_dir, dir=self.out_dir)
        before = time.time()
        self.d.type_line("C:\\MTCP\\VCLIST.BAT %s" % self.out_dir)
        if not self.cap._await_incoming(self.LISTING_NAME, before,
                                        self.d.transfer_timeout()):
            return None
        path = self.cap._incoming_path(self.LISTING_NAME)
        if path is None:
            return None
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        # CONSUMED. incoming/ is inside the FTP root and the target can write
        # it, so a listing left lying there is a file a later read could find
        # and mistake for its own -- the mtime guard already refuses that, and
        # not leaving the trap is cheaper than trusting the guard twice.
        try:
            os.remove(path)
        except OSError:
            pass
        out = dos_dir_listing(raw)
        out["raw"] = raw.decode("cp437", "replace")[:64 * 1024]
        out["read_at"] = time.time()
        return out

    def _select(self, listing):
        """(what to fetch, what is refused before anything is typed).

        THE REFUSALS ARE RESULTS, NOT SILENCE. A name that is not on the card,
        a name that cannot be spelled and a zero-byte file are each reported
        as a failed file rather than quietly dropped -- otherwise a pull of
        four names that fetched two would read as a success with two files in
        it, which is the shape of half-truth this rig keeps producing.
        """
        by_name = {}
        for rec in listing["files"]:
            by_name.setdefault(rec["name"].upper(), rec)

        wanted, refused = [], []
        if self.want_all:
            asked = [r["name"] for r in listing["files"]]
        else:
            asked = self.names
        seen = set()
        for raw in asked:
            key = str(raw or "").strip().upper()
            if not key or key in seen:
                continue
            seen.add(key)
            rec = by_name.get(key)
            if rec is None:
                refused.append({"name": key, "ok": False, "why": "not-listed",
                                "reason": ("%s is not in the target's own DIR "
                                           "of %s, so it was never typed at "
                                           "the machine" % (key, self.out_dir))})
                continue
            if not rec["fetchable"]:
                refused.append({"name": rec["name"], "ok": False,
                                "why": rec["why"] or "unsafe-name",
                                "bytes": rec["bytes"],
                                "reason": ("%r does not survive the DOS 8.3 "
                                           "round trip, so it cannot be "
                                           "joined onto a path here or typed "
                                           "as one argument there"
                                           % rec["name"])})
                continue
            if rec["bytes"] <= 0:
                # A ZERO CLOSES THE QUESTION IN THE WRONG DIRECTION. Arrival
                # is proved by bytes appearing and settling, so a zero-byte
                # file is indistinguishable from one that never came -- there
                # is no reading of the wait that means "it worked".
                refused.append({"name": rec["name"], "ok": False,
                                "why": "empty", "bytes": 0,
                                "reason": ("%s is zero bytes on the card, and "
                                           "a zero-byte arrival cannot be "
                                           "told from no arrival at all"
                                           % rec["name"])})
                continue
            wanted.append(rec)
        return wanted, refused

    # -- bringing one file back -----------------------------------------------

    def _fetch_one(self, rec):
        name, want = rec["name"], rec["bytes"]
        src = self.out_dir.rstrip("\\") + "\\" + name
        self._say("fetch", "fetching %s (%d bytes)" % (name, want), name=name)

        path, why = self._leg(src, name)
        if why:
            return {"name": name, "ok": False, "why": why, "bytes": want,
                    "reason": ("%s did not come back, so whether it left the "
                               "target is unknown" % name) if why == "no-return"
                              else ("%s did not come back and the machine is "
                                    "not responding to a keystroke either"
                                    % name)}
        got = os.path.getsize(path)
        if got != want:
            self.cap._drop_incoming(path)
            return {"name": name, "ok": False, "why": "size-mismatch",
                    "bytes": got, "listed_bytes": want,
                    "reason": ("%s arrived as %d bytes and the target's own "
                               "DIR says it is %d -- a short transfer, which "
                               "is exactly what a copy that looks fine would "
                               "hide" % (name, got, want))}
        sha = self.cap._sha_of(path)
        verified = "size"
        if self.paranoid:
            second, why2 = self._leg(src, name + ".CH2")
            if why2:
                self.cap._drop_incoming(path)
                return {"name": name, "ok": False, "why": why2, "bytes": want,
                        "reason": ("the second fetch of %s never came back, "
                                   "so the pair could not be compared" % name)}
            again = self.cap._sha_of(second)
            self.cap._drop_incoming(second)
            if again != sha:
                self.cap._drop_incoming(path)
                return {"name": name, "ok": False, "why": "unstable",
                        "bytes": got, "sha256": sha, "sha256_second": again,
                        "reason": ("two fetches of %s disagree, so the path "
                                   "is not repeatable and neither copy can be "
                                   "trusted" % name)}
            verified = "size+repeat"
        dest, replaced = self.cap._promote_pulled(path, name, {
            "name": name, "bytes": got, "sha256": sha, "verified": verified,
            "source": src, "listed_bytes": want, "pulled_at": time.time()})
        self._say("verify", "%s came back %s%s"
                  % (name, "the size the card says it is" if verified == "size"
                     else "twice, and the two copies agree",
                     " (replacing an earlier copy)" if replaced else ""),
                  name=name)
        return {"name": name, "ok": True, "why": None, "bytes": got,
                "sha256": sha, "verified": verified, "path": dest,
                "source": src}

    def _leg(self, src, server_name):
        """Type one VCCHK and wait for the bytes. (path, None) or (None, why).

        The readiness probe is used HERE and nowhere else -- as a tiebreaker
        after something has already gone wrong, never as a gate in the happy
        path. It tests whether the BIOS keyboard ISR is alive, not whether DOS
        is reading, which is worth having only when the alternative is
        guessing between "the transfer failed" and "the machine is wedged".
        """
        before = time.time()
        self.d.type_line("C:\\MTCP\\VCCHK.BAT %s %s" % (src, server_name))
        if not self.cap._await_incoming(server_name, before,
                                        self.d.transfer_timeout()):
            return None, ("no-return" if self.d.wait_prompt() else "no-prompt")
        path = self.cap._incoming_path(server_name)
        return (path, None) if path else (None, "no-return")


class ScanJob(NetJob):
    """Run dinspect at whatever profile the menu default boots into, then
    hand off to PullJob for the NET-side fetch of its report.

    TWO REBOOTS, NOT ONE, AND THAT IS DELIBERATE. Typing DINSPECT.EXE
    against whatever profile happens to already be on screen would
    blind-type into unknown machine state -- the same hazard
    vcctrl-cell's guard #3 exists for (confirm the machine is not mid
    something before driving it). So this job's own first reboot lands on
    the menu default the same way NetJob._leave_net's RETURN leg already
    does: nothing is typed at the menu, the timeout picks the default, and
    nothing here asserts which profile that is -- only that a reset really
    happened and a boot really finished. Only THEN is DINSPECT typed. The
    second reboot, into NET to fetch the report back, is the existing,
    unmodified PullJob -- this class adds no new NET logic at all.

    WHAT THIS CANNOT CHECK: whether DINSPECT.EXE is actually staged at
    C:\\XFER\\IN on the card. Unlike a push (sha256'd on this host before
    it is sent) there is no cheap pre-reboot proof of what is already on
    the card -- the closest thing, a VCLIST of C:\\XFER\\OUT, reads the
    OUT directory, not IN. If it is missing, the typed command fails at
    the DOS prompt and PullJob's own listing check below simply never
    finds SYSINFO.TXT -- reported as `not-listed`, not silently as
    success. See the vcctrl-dinspect-sysinfo skill for staging it first.
    """

    REPORT_NAME = "SYSINFO.TXT"
    DINSPECT_CMD = ("C:\\XFER\\IN\\DINSPECT.EXE -o C:\\XFER\\OUT\\%s "
                     "--show-undetected" % REPORT_NAME)

    # BLIND, LIKE THE MENU SELECTION ABOVE IT -- dinspect's own README
    # documents a "Runtime" field precisely so external tooling can
    # calibrate a wait like this one, but that field is INSIDE the report
    # this wait exists to let it finish writing, so it cannot bootstrap
    # itself. This is a placeholder pending a real timed run on the actual
    # Gateway 2000 (see docs/DINSPECT-SYSINFO.md once that run has
    # happened) -- CLAUDE.md's rule applies: a measured number goes to
    # docs/ with its conditions, not silently in as a new default here.
    RUN_WAIT_S = 15.0

    def _boot_to_default(self):
        """Reboot with nothing typed at the menu -- the SAME primitive
        NetJob._leave_net uses for its return leg, run here as the ENTRY
        step instead. Deliberately does not assert which profile it
        landed on; only that a reset really happened and a boot really
        finished.
        """
        armed = self.d.arm() if hasattr(self.d, "arm") else False
        self._say("reboot", "rebooting to the menu default before the scan")
        PROFILE.invalidate("rebooting for a system-inventory scan")
        if not armed:
            self.d.combo(["ctrl", "alt", "delete"])
            return self._fail("no-witness",
                              "could not set Scroll Lock, so a reboot would "
                              "be invisible -- refusing rather than "
                              "rebooting blind")
        if not self._reboot_edge():
            return self._fail("no-reset",
                              "the machine never reset -- Scroll Lock did "
                              "not clear, so the reboot did not happen")
        if not self.d.wait_boot():
            return self._fail("no-boot",
                              "no readiness pulse after the reboot: the "
                              "machine did not finish booting, or "
                              "RDYPULSE.COM is missing from the card")
        self._say("boot", "the machine booted; which profile is unread")
        return True

    def run(self):
        gate = self._boot_to_default()
        if gate is not True:
            return gate
        self._say("scan", "typing the dinspect invocation, blind")
        self.d.type_line(self.DINSPECT_CMD)
        self.d.sleep(self.RUN_WAIT_S)
        self._say("scan", "handing off to the fetch")
        pull = PullJob(self.cap, self.d, names=[self.REPORT_NAME],
                       out_dir=self.cap._out_dir(), do_return=self.do_return,
                       log=self.log)
        return pull.run()


class FilesCapability(Capability):
    """Putting a file onto the target, over the target's own FTP client.

    Rule 1 applies here as everywhere: this class does not call into input,
    power or video. The transfer itself is a SEQUENCE across those three and
    belongs to the registry, the way a sweep does. What lives here is the
    part that is genuinely about files -- whether the feature applies at all,
    what a name has to become, what a size means, and whether the server that
    the target would pull from is actually answering.
    """

    name = "files"

    # Set by the registry after construction, the same way `bus` is. The job
    # sequences input through registry.execute() rather than reaching into
    # InputCapability, which is rule 1: a capability may not call into
    # another. A sequence OVER capabilities is a different object from a
    # capability with tentacles.
    registry = None

    # How long the cheap check waits. It runs on every state poll, so it is
    # not allowed to make the page wait: a server that is slow to answer
    # leaves the previous state standing and says the check timed out. It must
    # NOT report `unsupported`, which would be the instrument's own state
    # reported as the target's.
    PROBE_TIMEOUT_S = 0.4

    def commands(self):
        return {"files": self._files, "file_check": self._file_check,
                "file_name": self._file_name, "file_stage": self._file_stage,
                "file_queue": self._file_queue, "file_send": self._file_send,
                "file_status": self._file_status,
                "file_cancel": self._file_cancel, "file_bats": self._file_bats,
                # The other direction. `file_pull` is the job -- it reboots,
                # and it is the only one here that touches the target;
                # `file_listing` reads what the last one learned, and
                # `file_pulled` is what has already been brought back and
                # lives on this host.
                "file_pull": self._file_pull,
                "file_listing": self._file_listing,
                "file_pulled": self._file_pulled,
                # A third direction, sharing the same slot: reboot to the
                # menu default, run dinspect there, reboot into NET to
                # fetch what it wrote. See ScanJob for why it is not just
                # `file_pull` with an extra step.
                "file_scan": self._file_scan}

    # -- the two BATs the card needs ------------------------------------------
    #
    # GENERATED, NOT SHIPPED AS FILES, because they carry the address and the
    # credentials -- and the address is the same `target_host` the liveness
    # check dials and the server binds. That is the whole point of the key
    # having one meaning: a static BAT in the tree would be a fourth place the
    # address is written down and the first to go stale.
    #
    # Both follow the card's existing BATs exactly: CRLF, `binary`, no `pasv`
    # (mTCP negotiates it and prints "Unknown command" otherwise), no caret
    # anywhere and no `>` inside a REM -- redirection IS parsed in REM on
    # 6.22, and a comment explaining that once created the files it warned
    # about. The success line says "attempted": whether FTP.EXE sets an
    # errorlevel is unverified, and `IF ERRORLEVEL 1` would then read the
    # PREVIOUS command's value, which is a check reporting confidently on a
    # different operation.

    VCGET_BAT = """@ECHO OFF
REM VCGET name [destdir] -- pulls stage\\name from the vcctrl daemon host.
REM NO ANGLE BRACKETS ANYWHERE IN THIS FILE, not even in a comment: DOS 6.22
REM parses redirection INSIDE REM, so a usage line written the obvious way
REM creates a file named after the following word.
REM Generated by `vcctrl file-bats`. Do not hand-edit: the address here must
REM match capabilities.files.settings.target_host, and a copy edited on the
REM card is one nothing can check.
REM Requires the NET boot profile. C:\\MTCP is NOT on the PATH there, so this
REM uses full paths throughout.
SET VGF=%1
SET VGD=%2
IF "%VGF%"=="" GOTO USAGE
IF "%VGD%"=="" SET VGD=@@DEST@@
REM MD ON 6.22 WILL NOT CREATE A NESTED PATH IN ONE GO, so the parent comes
REM first. Both are IF NOT EXIST so a re-run is silent rather than noisy.
IF NOT EXIST @@DESTPARENT@@\\NUL MD @@DESTPARENT@@
IF NOT EXIST %VGD%\\NUL MD %VGD%
REM The return direction, created here so the pair is discoverable from a DIR
REM rather than only from a document. Nothing writes it yet.
IF NOT EXIST @@DESTOUT@@\\NUL MD @@DESTOUT@@
SET MTCPCFG=C:\\MTCP\\TCP.CFG
ECHO @@USER@@> C:\\MTCP\\VCGET.RSP
ECHO @@PASS@@>> C:\\MTCP\\VCGET.RSP
ECHO binary>> C:\\MTCP\\VCGET.RSP
ECHO cd stage>> C:\\MTCP\\VCGET.RSP
ECHO get %VGF% %VGD%\\%VGF%>> C:\\MTCP\\VCGET.RSP
ECHO quit>> C:\\MTCP\\VCGET.RSP
C:\\MTCP\\FTP.EXE -port @@PORT@@ @@HOST@@ < C:\\MTCP\\VCGET.RSP
REM THE VERIFICATION RIDES IN THE SAME BAT, AND THAT IS THE POINT.
REM Typing it as a SECOND command needed the harness to know DOS was back at
REM a prompt, and the only non-OCR readiness signal available tests whether
REM the BIOS keyboard ISR is alive -- which it is, all the way through
REM FTP.EXE. So a 50-character command went into a machine that was not
REM reading, fifteen characters fit in the BIOS buffer, and what executed was
REM C:\\MTCP\\VCCHK.B
REM
REM DOS runs the lines of a batch file in order and needs no help doing it.
REM One typed command, one arrival to wait for, and the readiness question
REM does not arise.
ECHO @@USER@@> C:\\MTCP\\VCGET2.RSP
ECHO @@PASS@@>> C:\\MTCP\\VCGET2.RSP
ECHO binary>> C:\\MTCP\\VCGET2.RSP
ECHO cd incoming>> C:\\MTCP\\VCGET2.RSP
ECHO put %VGD%\\%VGF% %VGF%.CHK>> C:\\MTCP\\VCGET2.RSP
ECHO quit>> C:\\MTCP\\VCGET2.RSP
C:\\MTCP\\FTP.EXE -port @@PORT@@ @@HOST@@ < C:\\MTCP\\VCGET2.RSP
ECHO.
ECHO VCGET attempted: %VGD%\\%VGF%
GOTO END
:USAGE
ECHO Usage: VCGET name [destdir]
:END
SET VGF=
SET VGD=
"""

    VCCHK_BAT = """@ECHO OFF
REM VCCHK source-path name-on-server -- sends a file BACK to the vcctrl
REM daemon host so its bytes can be compared there.
REM NO ANGLE BRACKETS ANYWHERE IN THIS FILE, not even in a comment: DOS 6.22
REM parses redirection INSIDE REM.
REM Generated by `vcctrl file-bats`. Do not hand-edit.
REM This is the verification leg: no OCR is involved in the verdict, because
REM the comparison happens on the Linux side against the staged copy.
REM Requires the NET boot profile.
IF "%1"=="" GOTO USAGE
IF "%2"=="" GOTO USAGE
SET MTCPCFG=C:\\MTCP\\TCP.CFG
ECHO @@USER@@> C:\\MTCP\\VCCHK.RSP
ECHO @@PASS@@>> C:\\MTCP\\VCCHK.RSP
ECHO binary>> C:\\MTCP\\VCCHK.RSP
ECHO cd incoming>> C:\\MTCP\\VCCHK.RSP
ECHO put %1 %2>> C:\\MTCP\\VCCHK.RSP
ECHO quit>> C:\\MTCP\\VCCHK.RSP
C:\\MTCP\\FTP.EXE -port @@PORT@@ @@HOST@@ < C:\\MTCP\\VCCHK.RSP
ECHO.
ECHO VCCHK attempted: %1
GOTO END
:USAGE
ECHO Usage: VCCHK source-path name-on-server
:END
"""

    # THE THIRD BAT, AND THE ONE THAT MAKES A DOWNLOAD POSSIBLE AT ALL.
    #
    # Fetching a file needs no new batch file -- VCCHK already puts a named
    # path back on this host, which is exactly a download. What has no answer
    # without this one is KNOWING WHAT IS THERE. The only other way to read a
    # directory on that machine is to look at the screen, and this console
    # reads `13,800` as `13,808` and `10 file(s)` as `18 file(s)`. A file
    # picker built on those numbers would offer files that do not exist and
    # hide ones that do.
    #
    # So the DIR is redirected into a file and the file is sent. It arrives
    # byte for byte, it states its own totals, and it can be reconciled
    # against itself -- none of which is true of a photograph of a screen.
    #
    # ONE TYPED COMMAND, for the reason every other command here is one: DOS
    # runs a batch file's lines in order and needs no help doing it, and there
    # is still no honest way to ask this machine whether it is ready for a
    # second one.
    VCLIST_BAT = """@ECHO OFF
REM VCLIST [dir] -- writes a DIR of the target's outgoing directory into a
REM file and sends that file to the vcctrl daemon host.
REM NO ANGLE BRACKETS ANYWHERE IN THIS FILE, not even in a comment: DOS 6.22
REM parses redirection INSIDE REM, so a usage line written the obvious way
REM creates a file named after the following word.
REM Generated by `vcctrl file-bats`. Do not hand-edit: the address here must
REM match capabilities.files.settings.target_host.
REM THE LISTING IS A FILE AND NOT A SCREEN, which is the whole point. The
REM harness cannot read this console -- it turns 13,800 into 13,808 -- so the
REM directory has to reach it as bytes or not at all.
REM Requires the NET boot profile. C:\\MTCP is NOT on the PATH there, so this
REM uses full paths throughout.
SET VLD=%1
IF NOT "%VLD%"=="" GOTO LIST
SET VLD=@@DESTOUT@@
REM MD ON 6.22 WILL NOT CREATE A NESTED PATH IN ONE GO, so the parent comes
REM first. Creating the DEFAULT directory rather than reporting File not found
REM is deliberate: an empty directory is an answer, and a missing one is a
REM question.
IF NOT EXIST @@DESTPARENT@@\\NUL MD @@DESTPARENT@@
IF NOT EXIST %VLD%\\NUL MD %VLD%
:LIST
REM A DIRECTORY NAMED BY THE CALLER IS NEVER CREATED, and that asymmetry is
REM the point. MD-ing it would turn a mistyped path into an EMPTY LISTING --
REM which reads as "that directory has nothing in it" when the truth is "that
REM directory does not exist". One of those is an answer and the other is a
REM question, and a tool that cannot tell them apart will confidently report
REM the wrong one.
REM WRITTEN TO THE PARENT, NOT INTO THE DIRECTORY BEING LISTED. A listing
REM that lands inside its own subject appears in the NEXT one, as a file the
REM operator never put there and might well try to fetch.
DIR %VLD% > @@DESTPARENT@@\\VCLIST.TXT
SET MTCPCFG=C:\\MTCP\\TCP.CFG
ECHO @@USER@@> C:\\MTCP\\VCLIST.RSP
ECHO @@PASS@@>> C:\\MTCP\\VCLIST.RSP
ECHO binary>> C:\\MTCP\\VCLIST.RSP
ECHO cd incoming>> C:\\MTCP\\VCLIST.RSP
ECHO put @@DESTPARENT@@\\VCLIST.TXT VCLIST.TXT>> C:\\MTCP\\VCLIST.RSP
ECHO quit>> C:\\MTCP\\VCLIST.RSP
C:\\MTCP\\FTP.EXE -port @@PORT@@ @@HOST@@ < C:\\MTCP\\VCLIST.RSP
ECHO.
ECHO VCLIST attempted: %VLD%
SET VLD=
"""

    def _file_bats(self, req):
        """Render the two BATs the card needs, for this rig's configuration.

        DELIBERATELY NOT EXPOSED TO THE BROWSER. They contain the FTP password
        in plaintext -- which is unavoidable, it is plaintext on the card too
        -- but a password already on a CF card is a different exposure from
        one a web request will hand out.
        """
        srv = self._server()
        if srv is None:
            return {"ok": False, "why": "not_configured",
                    "error": "no capabilities.files.settings.target_host is "
                             "set, so there is no address to write into them"}
        host, port = srv
        user, password = self._credentials()
        if not user or not password:
            return {"ok": False, "why": "not_configured",
                    "error": "no credentials: set control.fileserver.user and "
                             "the variable control.fileserver.password_env "
                             "names"}
        dest = self._dest()
        # The parent is needed because DOS MD takes one level at a time, and
        # the sibling OUT because a convention nobody can see is not one.
        parent = dest.rsplit("\\", 1)[0] if "\\" in dest.rstrip("\\") else dest
        out_dir = self._out_dir()
        out = {}
        for name, tpl in (("VCGET.BAT", self.VCGET_BAT),
                          ("VCCHK.BAT", self.VCCHK_BAT),
                          ("VCLIST.BAT", self.VCLIST_BAT)):
            text = (tpl.replace("@@HOST@@", host)
                       .replace("@@PORT@@", str(port))
                       .replace("@@USER@@", user)
                       .replace("@@PASS@@", password)
                       .replace("@@DEST@@", dest)
                       .replace("@@DESTPARENT@@", parent)
                       .replace("@@DESTOUT@@", out_dir))
            # CRLF, because a DOS batch file with LF endings fails in ways
            # that read as a logic bug rather than a formatting one.
            out[name] = text.replace("\n", "\r\n")
        return {"ok": True, "bats": out, "host": host, "port": port,
                "dest": dest, "out_dir": out_dir,
                "install": [
                    "Put all three files in the CONTROL host's stage/",
                    "  directory (~/doskutsu-netiter/stage/), NOT this host's",
                    "  -- the card's existing GET.BAT is the only way onto the",
                    "  card and it dials the control host.",
                    "At a NET prompt on the target:",
                    "  C:\\MTCP\\GET.BAT VCGET.BAT",
                    "  C:\\MTCP\\GET.BAT VCCHK.BAT",
                    "  C:\\MTCP\\GET.BAT VCLIST.BAT",
                    "NO TRAILING BACKSLASH ON THE DESTINATION. DOS 6.22",
                    "  answers `COPY x C:\\MTCP\\` with Invalid directory,",
                    "  and these lines carried one until it was typed at the",
                    "  real machine on 2026-08-24:",
                    "AND COPY PROMPTS `Overwrite ... (Yes/No/All)?` HERE when",
                    "  the destination exists, which it will for VCGET and",
                    "  VCCHK if you are replacing them. Answer A. Do NOT type",
                    "  the next command into that prompt -- it consumes a",
                    "  typed line looking for a valid answer and finds one",
                    "  inside an ordinary word (the Y in COPY answers Yes).",
                    "  COPY C:\\DOSKUTSU\\VCGET.BAT C:\\MTCP",
                    "  COPY C:\\DOSKUTSU\\VCCHK.BAT C:\\MTCP",
                    "  COPY C:\\DOSKUTSU\\VCLIST.BAT C:\\MTCP",
                    "  DEL C:\\DOSKUTSU\\VCGET.BAT",
                    "  DEL C:\\DOSKUTSU\\VCCHK.BAT",
                    "  DEL C:\\DOSKUTSU\\VCLIST.BAT",
                    "Then verify before relying on any of them:",
                    "  C:\\MTCP\\CHK.BAT C:\\MTCP\\VCGET.BAT VCGET.chk",
                    "  and sha256 that against what this printed.",
                    "VCLIST.BAT IS ONLY NEEDED FOR THE DOWNLOAD DIRECTION.",
                    "  Uploads work without it; `vcctrl file-refresh` is what",
                    "  fails, and it fails as a listing that never arrives.",
                    "NONE OVERWRITES GET.BAT. It stays the known-good",
                    "  bootstrap, and it is the only way to fix these three",
                    "  without a card swap."]}

    # -- the transfer, as a job rather than a call ----------------------------
    #
    # MINUTES LONG, SO NOT A BLOCKING REQUEST. Two reboots at ~60 s each plus
    # the transfers; a POST held open for that is what makes a page look hung,
    # and a browser that closes must not abandon a machine mid-reboot. So the
    # command starts it and returns, and `file_status` is how anyone watches.

    _job = None
    _job_lock = threading.Lock()

    def _file_send(self, req):
        with FilesCapability._job_lock:
            cur = FilesCapability._job
            if cur and cur.get("running"):
                # ONE AT A TIME, AND REFUSED RATHER THAN QUEUED. Two transfers
                # interleaving their reboots would each see the other's
                # machine state and both would be wrong about it.
                return {"ok": False, "why": "busy",
                        "error": "a transfer is already running",
                        "status": cur}
            if self.registry is None:
                return {"ok": False, "why": "unsequenced",
                        "error": "no registry to sequence input through"}
            # `kind` SO A WATCHER CAN TELL THEM APART. One job slot holds
            # either direction -- deliberately, because two runs interleaving
            # their reboots would each misread the other's machine -- and a
            # page polling file_status has to know whether "3 files" means
            # sent or fetched.
            job = {"kind": "send", "running": True, "started_at": time.time(),
                   "log": [], "files": [], "ok": None, "why": None,
                   "reason": None,
                   "dest": req.get("dest") or (self.settings or {}).get(
                       "dest", DEFAULT_DEST),
                   "return": bool(req.get("return", True))}
            FilesCapability._job = job

        def run():
            drv = RegistryDriver(self.registry, pace=req.get("pace"))
            tj = TransferJob(self, drv, dest=job["dest"],
                             do_return=job["return"], log=job["log"])
            try:
                out = tj.run()
            except Exception as exc:
                out = {"ok": False, "why": "crashed",
                       "reason": "%s: %s" % (type(exc).__name__, exc),
                       "files": [], "log": tj.log}
                sys.stderr.write("transfer crashed: %s\n" % exc)
            out.pop("log", None)        # already live in job["log"]
            job.update(out)
            job["running"] = False
            job["finished_at"] = time.time()
            # THE MACHINE'S STATE AFTER A CRASH IS THE PART WORTH SAYING. A
            # transfer that died between the two reboots has left the target
            # in NET, and nothing downstream can tell that from a tidy exit.
            if out.get("why") == "crashed":
                job["left_in_net"] = True

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "started": True, "status": FilesCapability._job}

    def _file_pull(self, req):
        """Fetch from the target's outgoing directory. Same slot, same watcher.

        THE SAME JOB RECORD AS `file_send`, AND THAT IS THE POINT. Both
        directions reboot the machine, so they cannot run at once -- two runs
        interleaving their reboots would each read the other's machine state
        and both would be wrong about it. One slot makes that structural
        rather than remembered, and `file_status` and `file_cancel` work
        unchanged for either.

        WHAT IS ASKED FOR IS EXPLICIT, AND THERE IS NO DEFAULT. `all`,
        `names`, or `refresh` -- a bare call fetching everything would be a
        reboot nobody asked for, and a bare call fetching nothing would be a
        reboot for no reason. Both are refused with the same sentence.
        """
        with FilesCapability._job_lock:
            cur = FilesCapability._job
            if cur and cur.get("running"):
                return {"ok": False, "why": "busy",
                        "error": ("a transfer is already running -- the two "
                                  "directions share one machine"),
                        "status": cur}
            if self.registry is None:
                return {"ok": False, "why": "unsequenced",
                        "error": "no registry to sequence input through"}
            names = [str(n) for n in (req.get("names") or []) if str(n).strip()]
            want_all = bool(req.get("all"))
            refresh = bool(req.get("refresh"))
            # VALIDATED HERE, BEFORE THE JOB EXISTS, because this string is
            # typed at a machine with no quoting and the caller may be a
            # browser. dos_dir_path is a whitelist, not a filter -- see its
            # docstring for why a filter cannot work when the caret escapes
            # nothing.
            try:
                where = (dos_dir_path(req.get("dir")) if req.get("dir")
                         else self._out_dir())
            except ValueError as exc:
                return {"ok": False, "why": "bad-dir", "error": str(exc)}
            if not (names or want_all or refresh):
                return {"ok": False, "why": "nothing-asked",
                        "error": ("say what to fetch: `names`, `all`, or "
                                  "`refresh` for the listing alone. This "
                                  "reboots the target, so it will not guess "
                                  "which of those you meant")}
            job = {"kind": "pull", "running": True, "started_at": time.time(),
                   "log": [], "files": [], "ok": None, "why": None,
                   "reason": None, "out_dir": where,
                   "names": names, "all": want_all, "refresh": refresh,
                   "paranoid": bool(req.get("paranoid")),
                   "already_net": bool(req.get("already_net")),
                   "return": bool(req.get("return", True))}
            FilesCapability._job = job

        def run():
            drv = RegistryDriver(self.registry, pace=req.get("pace"))
            pj = PullJob(self, drv, names=names, want_all=want_all,
                         refresh_only=refresh, out_dir=where,
                         already_net=job["already_net"],
                         paranoid=job["paranoid"], do_return=job["return"],
                         log=job["log"])
            try:
                out = pj.run()
            except Exception as exc:
                out = {"ok": False, "why": "crashed",
                       "reason": "%s: %s" % (type(exc).__name__, exc),
                       "files": [], "log": pj.log}
                sys.stderr.write("pull crashed: %s\n" % exc)
            out.pop("log", None)        # already live in job["log"]
            job.update(out)
            job["running"] = False
            job["finished_at"] = time.time()
            # THE MACHINE'S STATE AFTER A CRASH IS THE PART WORTH SAYING. A
            # run that died between the two reboots has left the target in
            # NET, and nothing downstream can tell that from a tidy exit.
            if out.get("why") == "crashed":
                job["left_in_net"] = True

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "started": True, "status": FilesCapability._job}

    def _file_scan(self, req):
        """Reboot, run dinspect at the menu default, fetch its report.

        SAME SLOT AS SEND AND PULL, for the identical reason: this reboots
        the machine (twice), so it cannot run alongside either direction
        without each misreading the other's machine state.

        Nothing here validates that DINSPECT.EXE is actually staged on the
        card -- see ScanJob's docstring for why that cannot be checked
        cheaply. A scan against a card that never had it pushed reboots
        twice and comes back `not-listed`, same as asking `file_pull` for
        a name the target never had.
        """
        with FilesCapability._job_lock:
            cur = FilesCapability._job
            if cur and cur.get("running"):
                return {"ok": False, "why": "busy",
                        "error": "a transfer is already running -- the two "
                                 "directions share one machine",
                        "status": cur}
            if self.registry is None:
                return {"ok": False, "why": "unsequenced",
                        "error": "no registry to sequence input through"}
            job = {"kind": "scan", "running": True, "started_at": time.time(),
                   "log": [], "files": [], "ok": None, "why": None,
                   "reason": None, "out_dir": self._out_dir(),
                   "names": [ScanJob.REPORT_NAME], "all": False,
                   "refresh": False, "paranoid": False, "already_net": False,
                   "return": bool(req.get("return", True))}
            FilesCapability._job = job

        def run():
            drv = RegistryDriver(self.registry, pace=req.get("pace"))
            sj = ScanJob(self, drv, do_return=job["return"], log=job["log"])
            try:
                out = sj.run()
            except Exception as exc:
                out = {"ok": False, "why": "crashed",
                       "reason": "%s: %s" % (type(exc).__name__, exc),
                       "files": [], "log": sj.log}
                sys.stderr.write("scan crashed: %s\n" % exc)
            out.pop("log", None)        # already live in job["log"]
            job.update(out)
            job["running"] = False
            job["finished_at"] = time.time()
            if out.get("why") == "crashed":
                job["left_in_net"] = True

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True, "started": True, "status": FilesCapability._job}

    def _file_status(self, req):
        job = FilesCapability._job
        if not job:
            return {"ok": True, "job": None,
                    "note": "no transfer has run since the daemon started"}
        out = dict(job)
        # The log is unbounded over a long run and the page polls this; the
        # tail is what anyone watching needs, and the whole thing is in the
        # result of the run for anyone who wants it.
        n = int(req.get("log") or 40)
        out["log"] = job["log"][-n:]
        out["log_total"] = len(job["log"])
        return {"ok": True, "job": out}

    def _file_cancel(self, req):
        """Ask the running transfer to stop at the next FILE boundary.

        NEVER MID-FTP. There is no way to interrupt FTP.EXE on the target from
        here, so a cancel that claimed to stop a transfer in flight would be
        claiming something it cannot do. Between files is the only honest
        boundary, and it is stated rather than implied.
        """
        job = FilesCapability._job
        if not job or not job.get("running"):
            return {"ok": False, "error": "no transfer is running"}
        job["cancel"] = True
        return {"ok": True, "note": ("will stop after the file in flight -- "
                                     "FTP.EXE on the target cannot be "
                                     "interrupted from here")}

    @staticmethod
    def _listed_boards():
        """Board ids that appear in `targets:` at all, whatever they declare."""
        t = CFG.optional("targets")
        if t is vcconfig.ABSENT or t is vcconfig.NONE:
            return ()
        out = []
        for row in t:
            try:
                out.append(int(row["board_id"]))
            except (TypeError, ValueError, KeyError):
                continue
        return tuple(out)

    # -- the server the target pulls from -------------------------------------

    _ftpd = None
    _ftpd_error = None

    def start(self):
        """Report whatever _start_server() left behind, on EVERY path.

        The first version wrote to stderr at the end of the method, so the
        early returns -- missing credentials, an import failure, a directory
        it could not create -- all recorded a reason and left in silence. A
        daemon whose file server declined to start looked exactly like one
        that started it, and the only symptom was "Connection refused" from a
        probe, which points at the network rather than at the thing that chose
        not to listen.

        One exit, so a path cannot be added that skips the telling.
        """
        try:
            self._start_server()
        except Exception as exc:
            FilesCapability._ftpd_error = "%s: %s" % (type(exc).__name__, exc)
        if FilesCapability._ftpd_error:
            sys.stderr.write("file server did not start: %s\n"
                             % FilesCapability._ftpd_error)

    def _start_server(self):
        """Serve the staging root, if this rig is configured for transfers.

        Runs in a daemon thread, which is the house pattern -- video and the
        others do the same, and the registry already records a capability that
        raises during start as failed without taking the daemon with it.

        NOT CONFIGURED IS NOT AN ERROR. A rig with no `target_host` gets no
        server and no complaint; `snapshot()` already reports that state in its
        own word. Only a configured-but-broken server is a failure.
        """
        FilesCapability._ftpd_error = None
        srv = self._server()
        if srv is None:
            return
        host, port = srv
        try:
            stage, _p, _m, root, incoming = self._ensure_dirs()
        except RuntimeError as exc:
            FilesCapability._ftpd_error = str(exc)
            return
        try:
            from pyftpdlib.authorizers import DummyAuthorizer
            from pyftpdlib.handlers import FTPHandler
            from pyftpdlib.servers import FTPServer
        except Exception as exc:
            FilesCapability._ftpd_error = (
                "cannot import the vendored pyftpdlib (%s) -- check vendor/ "
                "reached the deployed tree" % exc)
            return

        user, password = self._credentials()
        if not user or not password:
            # NO DEFAULT CREDENTIALS, the same refusal server.py makes on the
            # control host and for the same reason: a server that falls back
            # to a built-in login comes up working while the configuration it
            # claims to follow was never read. That is a rig that works and a
            # false account of why.
            var = CFG.optional("control.fileserver.password_env")
            FilesCapability._ftpd_error = (
                "refusing to serve without credentials. control.fileserver "
                "names user=%r and password_env=%r; on the daemon host that "
                "variable reaches the service through "
                "/etc/vcctrl/secrets.env (mode 0600, root), which is NOT "
                "created by the installer because it holds a secret. A shell "
                "profile is not a service's environment."
                % (user, var if var not in (vcconfig.ABSENT, vcconfig.NONE)
                   else None))
            return

        try:
            auth = DummyAuthorizer()
            # Full perms on a DEDICATED root with nothing above it. The target
            # reads from stage/ and writes its verification copy to incoming/,
            # and separating those into two logins would buy nothing: both
            # credentials would sit in plaintext in BATs on the same CF card.
            auth.add_user(user, password, root, perm="elradfmwMT")
            # A SUBCLASS, NOT THE SHARED CLASS. server.py on the control
            # host assigns straight onto FTPHandler, which is global mutable
            # state: the authorizer, the masquerade and the passive range
            # would belong to the module rather than to this server. One
            # server makes that harmless and it is one line to not rely on
            # that being true later.
            def _quiet_log(self, msg, *a, **kw):
                """Thin wrapper; the RULE is ftp_log_suppressed(), so it can be
                tested without standing a server up and reading a journal.

                THE LIVENESS PROBE IS A BARE TCP CONNECT, every few seconds,
                from every open tab. pyftpdlib logs an opened and a closed for
                each, so the journal fills with pairs from this host and the
                TARGET's own sessions -- the ones that say what actually
                happened -- are buried among them.

                Suppressed by AUTHENTICATION, not by source address. A probe
                connects and closes without a USER; the target always logs in.
                Filtering on the address would have hidden a real client that
                happened to run here, and would have said nothing about why.

                Nothing meaningful is lost: a successful login is still
                logged, and so is every RETR and STOR. What goes is a pair of
                lines about a socket that did nothing.
                """
                if ftp_log_suppressed(
                        msg, getattr(self, "authenticated", False)):
                    return
                # PASSED THROUGH, NOT RE-DECLARED. The real signature is
                # `log(self, msg, logfun=logger.info)`; re-declaring it with a
                # None default forced None down as the log function and reset
                # the connection -- so the quieting broke the server it was
                # tidying the output of.
                FTPHandler.log(self, msg, *a, **kw)

            handler = type("VcctrlFTPHandler", (FTPHandler,),
                           {"log": _quiet_log})
            handler.authorizer = auth
            # THE MASQUERADE IS THE CONFIGURED ADDRESS, NEVER A DETECTED ONE.
            # Passive mode advertises an address in its reply. This host has
            # two addresses up on the target's subnet, and deriving this from
            # "the first global address" -- which is what the control host's
            # launcher does -- can name the other one. The target would then
            # connect to the control port here and be told to open its data
            # connection somewhere it was never pointed at: control succeeds,
            # data hangs, every check green.
            handler.masquerade_address = host
            handler.passive_ports = range(60000, 60011)
            # BOUND TO THE ONE ADDRESS, never 0.0.0.0. Same value again, so
            # the bind and the advertisement cannot disagree.
            self._ftpd = FTPServer((host, port), handler)
            threading.Thread(target=self._serve_forever, daemon=True).start()
            sys.stderr.write("file server on ftp://%s:%d/ root=%s\n"
                             % (host, port, root))
        except Exception as exc:
            FilesCapability._ftpd_error = "%s: %s" % (type(exc).__name__, exc)
            self._ftpd = None


    def _serve_forever(self):
        try:
            self._ftpd.serve_forever(handle_exit=False)
        except Exception as exc:
            # The thread dying must not be silent, and must not be fatal: the
            # KVM keeps working and the transfer control goes unavailable with
            # a reason, which is exactly the split the five states exist for.
            FilesCapability._ftpd_error = "server stopped: %s" % exc
            sys.stderr.write("file server stopped: %s\n" % exc)

    def stop(self):
        if self._ftpd is not None:
            try:
                self._ftpd.close_all()
            except Exception:
                pass
            self._ftpd = None

    def _credentials(self):
        """(user, password) -- the SAME login the control host's server uses.

        One secret and one rotation. Separate credentials would be isolation
        on paper only: both would live in plaintext in BATs on the same CF
        card, so anyone holding the card holds both.

        The password is reached through the variable `password_env` NAMES and
        is never in any config file.
        """
        user = CFG.optional("control.fileserver.user")
        var = CFG.optional("control.fileserver.password_env")
        if user in (vcconfig.ABSENT, vcconfig.NONE, None, ""):
            return None, None
        if var in (vcconfig.ABSENT, vcconfig.NONE, None, ""):
            return str(user), None
        return str(user), os.environ.get(str(var)) or None

    # -- does this even apply -------------------------------------------------

    def support(self):
        """Is a file transfer MEANINGFUL on the installed board?

        Three values, and the third is load-bearing. `True` for a machine with
        a packet driver and an FTP client; `False` for a Macintosh Plus, which
        has neither and is working hardware; `None` when nobody can say which
        machine is attached.

        `None` refuses rather than proceeding. Every other capability can
        afford to try and fail -- this one reboots the target and writes to
        its disk, and doing that to an unidentified machine is the one case
        where a wrong guess is both expensive and quiet.
        """
        bid = installed_board_id()
        if bid is None:
            return None, ("cannot tell which machine is attached, and a "
                          "transfer reboots the target and writes to its disk")
        table = _configured_transfer_boards()
        if table is None:
            return None, ("no `targets:` are configured, so it is not known "
                          "whether board %d can receive files" % bid)
        word = table.get(bid)
        if word == "supported":
            return True, None
        if word == "unsupported":
            return False, ("board %d has no way to receive a file -- no packet "
                           "driver and no FTP client" % bid)
        if word is None:
            # LISTED AND SILENT IS NOT THE SAME AS ABSENT. This said "board %d
            # is not listed in targets:", which sends somebody looking for a
            # missing entry that is sitting right there with a `leds:` line --
            # the wrong repair for the right refusal. Both cases are `unknown`
            # and they are fixed by different edits, so they say different
            # things.
            if bid in (self._listed_boards() or ()):
                return None, ("board %d is in `targets:` but does not say "
                              "whether it can receive files -- add `transfer: "
                              "supported` or `unsupported` beside its `leds:`"
                              % bid)
            return None, ("board %d is not listed in `targets:` at all, so "
                          "whether it can receive files has never been stated"
                          % bid)
        if word == "unknown":
            return None, ("board %d declares `transfer: unknown`" % bid)
        # A typo. Fails closed as UNKNOWN rather than unsupported: "somebody
        # wrote something we cannot read" is not the same fact as "this
        # machine has no such channel", and only one of them is fixable by
        # editing a line.
        return None, ("board %d declares `transfer: %s`, which is not one of "
                      "supported/unsupported/unknown" % (bid, word))

    # -- the server the target would pull FROM --------------------------------

    def _server(self):
        """(host, port) THE CARD WILL DIAL, or None if not configured.

        NOT `control.fileserver`, and the difference is the whole point of
        this docstring. That block is the control host's server, which is
        what `GET.BAT` and `PUT.BAT` on the card already name and what
        collects every log. This capability's server is a DIFFERENT one, on
        the Pi, named by the `VCGET.BAT` this feature pushes.

        THE ADDRESS IS A LITERAL STRING BAKED INTO A BATCH FILE ON A CF CARD.
        Nothing the daemon can see makes that string true. So a check that
        dials `127.0.0.1`, or the socket the daemon itself opened, or "the
        server I started", proves a server is up and proves NOTHING about the
        address the g2k will call -- and it fails in the worst possible place,
        after the confirmation and after the reboot, with the environment
        already gone.

        `target_host` therefore has exactly one meaning: **the string that
        goes into the BAT**. Absent is `not_configured` rather than a fallback
        to `control.fileserver` -- falling back would silently reinstate the
        drift this exists to prevent, by testing the control host's server and
        reporting on the Pi's, and it would read as robustness.

        FOUR CONSUMERS, ONE KEY, AND THE FOURTH IS THE SUBTLE ONE:

            1. the generator that writes the address into VCGET.BAT
            2. this check, which dials it
            3. the server's BIND_IP -- bound to this address, never 0.0.0.0
            4. the server's PASV masquerade address

        (4) is where a host with two addresses on ONE subnet stops being a
        curiosity and becomes a bug. FTP passive mode advertises an address
        in its reply, so if the masquerade is auto-detected and picks the
        other interface, the card connects to the control port on the address
        the BAT named and is then told to open its data connection somewhere
        else. **Control succeeds and data hangs** -- a half-working transfer,
        which is the worst failure shape to debug across a serial console on a
        1995 machine. Auto-detection is exactly what `serve.sh` does today by
        taking the first global address it finds, which is an ordering, not a
        choice.

        WHICH INTERFACE, DELIBERATELY. The Pi has two addresses on the same
        /24 -- eth0 and wlan0, both DHCP, both up. Whichever one this names,
        the other is an address the card will not be dialling, and a Pi that
        comes up on the other one is perfectly healthy and completely
        unreachable from the card. That is a choice to record, not a default
        to inherit from whatever `ip addr` printed first.
        """
        st = self.settings or {}
        host = st.get("target_host")
        if not host:
            return None
        try:
            return str(host), int(st.get("target_port") or 2121)
        except (TypeError, ValueError):
            return None

    # -- the two directories on the TARGET, and one derivation of each --------

    def _dest(self):
        """Where a pushed file lands on the card."""
        return (self.settings or {}).get("dest", DEFAULT_DEST)

    def _out_dir(self):
        """Where the card leaves files for us -- DERIVED ONCE, USED EVERYWHERE.

        THREE CONSUMERS AND THEY MUST NOT DISAGREE:

            1. the `MD` and the default argument inside VCLIST.BAT
            2. the DIR that batch file takes, which is the file picker
            3. the path typed at VCCHK for each file fetched

        This was computed inline in `_file_bats` and nowhere else, which was
        fine while the BAT was the only thing that had an opinion about the
        directory. The moment a second caller needed it, the choice was
        between deriving it twice -- the shape of drift this project has been
        bitten by more than once, with `target_host` and again with the 7.8 MB
        figure -- and moving it here. It moved.

        The default is the sibling of `dest` rather than a constant with the
        same value, so a rig that moves its incoming directory takes its
        outgoing one with it instead of splitting the pair silently.
        """
        st = self.settings or {}
        configured = st.get("out_dir")
        if configured:
            return str(configured)
        dest = self._dest()
        parent = dest.rsplit("\\", 1)[0] if "\\" in dest.rstrip("\\") else dest
        return parent + "\\OUT" if parent != dest else DEFAULT_OUT

    # The cheap probe runs on every state poll, from every open tab. Cached
    # for a few seconds for the same reason host_facts() is: a 1.5 s poll from
    # four tabs must not become four TCP connects a second at a machine whose
    # only job is to sit there. Short enough that a server coming up is
    # noticed within a few seconds, which is the timescale a person starting
    # serve.sh is working on.
    _probe_cache = (0.0, None)
    PROBE_CACHE_S = 5.0

    def _reachable(self, timeout=None):
        """(True|False|None, reason). None means the CHECK failed, not the server.

        A config file is not the configuration: a `control.fileserver` block
        proves somebody wrote an address down, not that `serve.sh` is running
        behind it. This is the cheap half -- a TCP connect, no login -- and it
        decides whether the control is offered.

        The third value exists because a timeout is a statement about this
        probe, not about the far end, and reporting it as `False` would turn
        a slow network into a machine that "cannot receive files".

        WHAT THIS STILL CANNOT PROVE, stated because the gap is narrow enough
        to be forgotten. It dials the address the card dials, which catches
        the failure that matters -- a moved lease, the wrong interface, a
        server that is not running. It does NOT dial it from the g2k's side of
        the network, so a route that is broken only between the card and this
        host still passes. There is no way to test that without the target,
        and the target is what the test exists to avoid rebooting.
        """
        srv = self._server()
        if srv is None:
            return None, ("no capabilities.files.settings.target_host is set, "
                          "so there is no address to check")
        host, port = srv
        # An explicit timeout is a CALLER asking a real question -- file_check
        # before a reboot -- and must never be served from a cache filled by a
        # background poll. That is the expensive check guarding the expensive
        # action; answering it with a five-second-old result would put the
        # staleness back exactly where it was designed out.
        if timeout is None:
            ts, cached = FilesCapability._probe_cache
            if cached and time.time() - ts < self.PROBE_CACHE_S:
                return cached
        wait = timeout or self.PROBE_TIMEOUT_S
        s = None
        try:
            s = socket.create_connection((host, port), wait)
            verdict = (True, None)
        except socket.timeout:
            verdict = (None,
                       "the file server at %s:%d did not answer within %.1fs "
                       "-- this says the check timed out, not that the server "
                       "is down" % (host, port, wait))
        except OSError as exc:
            # NAME THE LIKELY CAUSE WITHOUT CLAIMING TO KNOW IT. The server is
            # meant to run on this host, so a missing pyftpdlib is very
            # probably why nothing is listening -- but `target_host` is a
            # configured address and nothing here proves it points at this
            # machine. So the observation is offered conditionally: it is a
            # fact about THIS host, stated as one, and left to the reader to
            # apply. An unconditional "install pyftpdlib" would send somebody
            # to the wrong machine whenever the server is remote.
            hint = ""
            try:
                __import__("pyftpdlib")
            except Exception as ierr:
                # The library is vendored, so this is not "install something".
                # It means the tree is incomplete or the path bootstrap did
                # not run -- a different fault with a different fix, and
                # saying "install pyftpdlib" would send somebody to apt for a
                # file that is supposed to be sitting in the checkout.
                hint = (" This host cannot import the vendored pyftpdlib "
                        "(%s), so if the server is meant to run here, that is "
                        "why -- check vendor/ is present in the deployed tree."
                        % ierr)
            verdict = (False,
                       "the file server at %s:%d is not answering (%s). Start "
                       "it on the daemon host.%s"
                       % (host, port, exc.strerror or exc, hint))
        finally:
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        # ONLY THE BACKGROUND PROBE FILLS THE CACHE. A caller that passed a
        # timeout asked a real question and its answer is about that moment,
        # not about the next five seconds -- storing it would let the cheap
        # poll serve a stale copy of an expensive check.
        if timeout is None:
            FilesCapability._probe_cache = (time.time(), verdict)
        return verdict

    # -- staging --------------------------------------------------------------
    #
    # THREE DIRECTORIES, AND THE SEPARATION IS THE SAFETY.
    #
    #   stage/          THE FTP ROOT. Served to the target. Nothing incomplete
    #                   or unverified ever appears here.
    #   stage-partial/  bytes arriving. NOT served, NOT under the root.
    #   stage-meta/     sha and size per staged file. NOT served either.
    #
    # A partial file inside the served root is a truncated transfer waiting to
    # happen: the target GETs by name, and a name that exists is a name it will
    # fetch. `GET.BAT` already cannot tell a short file from a whole one -- its
    # own comment concedes it reports an attempt -- so the only place that
    # distinction can be enforced is here, before the file is reachable.

    STAGE_CHUNK_MAX = 4 * 1024 * 1024

    def _dirs(self):
        """(stage, partial, meta, ftp_root, incoming).

        THE FTP ROOT IS NOT THE STAGE DIRECTORY, and getting that wrong is
        what this layout exists to prevent. The card's BATs `cd stage` and
        `cd incoming` relative to their login home, so the home has to contain
        both -- the target GETs from one and PUTs its verification copy back
        into the other.

            <root>/stage/       served. GET. complete, verified files only.
            <root>/incoming/    served. PUT. the round-trip copies come back.
            <root>-partial/     NOT under the root. bytes still arriving.
            <root>-meta/        NOT under the root. sha and size.

        The first version made `stage` the root and derived the partial
        directory as its sibling. Adding `incoming` inside the root would then
        have put `<root>-partial` INSIDE the FTP home -- reachable by anything
        that can `cd ..`, which is the exact guarantee the separation is for.
        Partial and meta are siblings of the ROOT, so they are outside it
        however the root is configured.

        AND THEY ARE STILL SIBLINGS, which is the atomicity precondition.

        THAT IS A PRECONDITION, NOT A NAMING CONVENTION. Promotion is
        `os.replace()`, which is atomic only within one filesystem and raises
        EXDEV across devices; `shutil.move()` would instead degrade silently
        to copy-then-delete, which is precisely the non-atomic window this
        design exists to eliminate. Deriving the partial directory from the
        configured root guarantees they share a filesystem whatever
        `stage_dir` is set to -- a mount, a tmpfs, a separate disk.

        So the obvious future refactor -- a `partial_dir` config key, for
        somebody who wants the arriving bytes on faster storage -- would break
        the atomicity guarantee without touching the line that depends on it.
        If that is ever wanted, the promotion has to change with it.
        """
        st = self.settings or {}
        root = st.get("root_dir") or os.path.join(STATE_DIR, "fileserver")
        return (os.path.join(root, "stage"), root + "-partial",
                root + "-meta", root, os.path.join(root, "incoming"))

    def _ensure_dirs(self):
        dirs = self._dirs()
        for d in dirs:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as exc:
                raise RuntimeError("cannot create %s: %s" % (d, exc))
        return dirs

    def _queued(self):
        """What is staged and ready, read FROM DISK rather than remembered.

        One source of truth, deliberately. An in-memory queue beside a
        directory of files is two records of one fact that drift the first
        time the daemon restarts -- and the failure is the bad direction: the
        queue forgets a file that is still sitting in the FTP root, reachable
        by a target that was told to fetch it.
        """
        root, _partial, meta = self._dirs()[:3]
        out = []
        try:
            names = sorted(os.listdir(root))
        except OSError:
            return out
        for n in names:
            p = os.path.join(root, n)
            if not os.path.isfile(p):
                continue
            rec = {"name": n, "bytes": os.path.getsize(p), "sha256": None,
                   "source": None, "staged_at": None}
            try:
                with open(os.path.join(meta, n + ".json")) as f:
                    rec.update(json.load(f))
            except Exception:
                # A file in the root with no metadata was not put there by
                # this capability. Reported rather than hidden or deleted:
                # it is still something the target can fetch, so pretending
                # it is absent would be the more dangerous tidiness.
                rec["source"] = None
                rec["orphan"] = True
            out.append(rec)
        return out

    def _await_incoming(self, name, since, timeout):
        """Wait for the target to put `name` into incoming/. True if it lands.

        SIZE-STABLE, NOT MERELY PRESENT. FTP creates the file and then fills
        it, so a poll that returns on existence catches it mid-write and hands
        back a short file that will hash differently every time -- the
        collector learned this and its comment says so. Two equal sizes a poll
        apart is the cheapest thing that is actually true.

        `since` guards against a stale file of the same name from an earlier
        attempt being read as this one's, which is the failure that makes a
        retry look like a success.
        """
        _stage, _p, _m, _root, incoming = self._dirs()
        deadline = time.time() + float(timeout)
        last = None

        def _find():
            """The NEWEST file whose name matches, whatever case it arrives in.

            MEASURED, NOT DEFENSIVE. `HELLO.TXT.CHK` was asked for and
            `hello.txt.chk` arrived, so the wait timed out on a transfer that
            had completed -- the server log said `STOR ... completed=1
            bytes=49` while this reported the file never came back. The far
            end is a FAT volume and an FTP client from 1996: case is not a
            property either of them promises, and a verification that hinges
            on it is testing the wrong thing.

            NEWEST, NOT FIRST, AND THAT COST A RUN. This returned the first
            match os.listdir happened to yield and stopped there. On the rig
            2026-08-24, `incoming/` held BOTH `NETPROOF.TXT` from a session
            half an hour earlier and the `netproof.txt` that had just
            arrived -- two spellings of one name, which is exactly what a
            case-unstable far end produces over two runs. listdir handed back
            the stale one, the `since` test below correctly refused it as too
            old, and THE FRESH ONE TWO ENTRIES AWAY WAS NEVER LOOKED AT. The
            job reported `no-net` -- the target is not on the network -- about
            a machine whose transfer had completed and was sitting in the
            server's own log.

            The freshness guard was working. It was being handed the wrong
            candidate to judge, which no amount of care in the guard can fix:
            a filter that can only see one of two matches is not a filter, it
            is a coin toss with a check after it.
            """
            return self._incoming_path(name)

        while time.time() < deadline:
            try:
                p = _find()
                if p is None:
                    raise OSError("not yet")
                st = os.stat(p)
                if st.st_mtime >= since - 1.0:
                    if last is not None and st.st_size == last and st.st_size:
                        return True
                    last = st.st_size
                else:
                    last = None
            except OSError:
                last = None
            time.sleep(0.5)
        return False

    def _incoming_path(self, name):
        """The NEWEST file of that name in incoming/, whatever case it wears.

        Case-insensitive because the far end is a FAT volume and an FTP client
        from 1996, and case is not a property either of them promises. A
        verification that hinges on it is testing the wrong thing --
        `HELLO.TXT.CHK` was asked for, `hello.txt` arrived, and a completed
        transfer was reported as one that never happened.

        NEWEST RATHER THAN FIRST, AND THE SAME BUG WAS IN TWO FUNCTIONS. This
        returned whichever match `os.listdir` yielded first. So did the finder
        inside `_await_incoming`. On the rig 2026-08-24 incoming/ held both
        `NETPROOF.TXT` from an earlier session and the `netproof.txt` that had
        just landed -- two spellings of one name, which is precisely what a
        case-unstable far end produces across two runs -- and the stale one
        was returned. The wait then refused it as too old and never saw the
        fresh one; the run reported `no-net` about a machine whose transfer
        was sitting completed in the server's own log.

        ONE IMPLEMENTATION NOW, and that is the actual repair. Two copies of
        "which file does this name refer to" is how the same defect came to
        exist twice, and fixing one of them would have left the sha
        comparison in `_send_one` reading whichever copy turned up first.
        """
        _stage, _p, _m, _root, incoming = self._dirs()
        best, best_t = None, None
        want = name.lower()
        try:
            for n in os.listdir(incoming):
                if n.lower() != want:
                    continue
                path = os.path.join(incoming, n)
                try:
                    t = os.stat(path).st_mtime
                except OSError:
                    continue
                if best_t is None or t > best_t:
                    best, best_t = path, t
        except OSError:
            pass
        return best

    def _sha_of(self, path):
        """sha256 of a file on this host, or None if it cannot be read."""
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
        except OSError:
            return None
        return h.hexdigest()

    def _incoming_sha(self, name):
        """sha256 of a returned file, or None if it cannot be read."""
        path = self._incoming_path(name)
        return None if path is None else self._sha_of(path)

    def _drop_incoming(self, path):
        """Remove a file the target sent that we are NOT keeping.

        incoming/ is inside the FTP root, so anything left there is both
        served and overwritable by the target. A copy that failed its size
        check is exactly the kind of file that must not sit around looking
        like a result -- and leaving it would also make the next fetch of the
        same name race its own leftovers.
        """
        try:
            if path:
                os.remove(path)
        except OSError:
            pass

    # -- what came back, and what the card says it has ------------------------
    #
    # TWO MORE DIRECTORIES, AND BOTH OUTSIDE THE FTP ROOT:
    #
    #   <root>-pulled/       files fetched off the target and verified
    #   <root>-pulled-meta/  their sha and provenance, and the last listing
    #
    # A VERIFIED PULL IS EVIDENCE AND MUST STOP BEING REACHABLE. incoming/ is
    # inside the root: the target can write it, the next fetch can overwrite
    # it by name, and anything with an FTP login can read it. Promoting out of
    # it is the same move the staging side makes in the other direction, and
    # for the same reason -- the moment a file's bytes are what somebody will
    # rely on, it goes somewhere nothing else writes.
    #
    # AND THEY ARE SIBLINGS OF THE ROOT, which is the atomicity precondition:
    # os.replace() is atomic within one filesystem and raises EXDEV across
    # devices. Deriving both from the configured root guarantees they share
    # one, whatever `root_dir` is set to.

    PULLED_CHUNK_MAX = 4 * 1024 * 1024

    def _pulled_dirs(self):
        """(bytes, metadata). Siblings of the FTP root, never inside it."""
        root = self._dirs()[3]
        return root + "-pulled", root + "-pulled-meta"

    def _ensure_pulled(self):
        dirs = self._pulled_dirs()
        for d in dirs:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as exc:
                raise RuntimeError("cannot create %s: %s" % (d, exc))
        return dirs

    def _promote_pulled(self, path, name, meta):
        """Move a verified file out of incoming/ and record what it is.

        THE 8.3 CONVERSION RUNS AGAIN HERE, on a name that already passed it
        in the listing parser. That is not belt and braces for its own sake:
        this is the line that joins a name FROM THE TARGET onto a path on a
        host running as root, and the rule this project keeps relearning is
        that a containment check belongs at the join, not upstream of it.

        Returns (path, replaced). An earlier copy of the same name is
        overwritten -- it is the same file in the same directory on the same
        card, and refusing would strand the newer bytes -- but the fact is
        returned so the log can say so rather than letting a silent
        replacement look like a first arrival.
        """
        pulled, meta_dir = self._ensure_pulled()
        safe, _notes = dos_filename(name)
        dest = os.path.join(pulled, safe)
        replaced = os.path.exists(dest)
        os.replace(path, dest)
        try:
            with open(os.path.join(meta_dir, safe + ".json"), "w") as f:
                json.dump(meta, f)
        except OSError:
            # The bytes are the thing. Losing the sidecar costs provenance,
            # which _pulled() then reports as unknown rather than inventing.
            pass
        return dest, replaced

    def _pulled(self):
        """What is in the pulled directory, READ FROM DISK.

        One source of truth, the same discipline `_queued()` follows: a
        remembered list beside a directory of files is two records of one fact
        and they diverge on the first restart.
        """
        try:
            pulled, meta_dir = self._ensure_pulled()
        except RuntimeError:
            return []
        out = []
        try:
            names = sorted(os.listdir(pulled))
        except OSError:
            return out
        for n in names:
            path = os.path.join(pulled, n)
            if not os.path.isfile(path):
                continue
            rec = {"name": n, "bytes": os.path.getsize(path), "sha256": None,
                   "verified": None, "source": None, "pulled_at": None,
                   "path": path}
            try:
                with open(os.path.join(meta_dir, n + ".json")) as f:
                    rec.update(json.load(f))
                    rec["path"] = path
            except Exception:
                # Reported as unaccounted rather than hidden or deleted, the
                # same way an orphan in the FTP root is: it is still bytes
                # somebody may want, and this capability cannot say where they
                # came from.
                rec["orphan"] = True
            out.append(rec)
        return out

    # -- the target's own account of its outgoing directory -------------------

    def _listing_file(self):
        return os.path.join(self._pulled_dirs()[1], "listing.json")

    def _listing_raw(self):
        try:
            with open(self._listing_file()) as f:
                return json.load(f)
        except Exception:
            return None

    def _listing_store(self):
        """The whole store: {"listings": {DIR: ...}, "attempts": {DIR: ...}}.

        KEYED BY DIRECTORY BECAUSE THERE IS MORE THAN ONE NOW. The harness
        reads C:\\DOSKUTSU\\LOGS and the KVM picker reads C:\\XFER\\OUT; with a
        single slot, whichever ran last would answer for both -- and the
        picker would show a list of log files under a heading naming the
        transfer directory. A reading belongs to the thing it is a reading OF.

        MIGRATES THE OLD SHAPE RATHER THAN DISCARDING IT. There is a live
        store on the rig written by the single-slot version; dropping it would
        silently turn a real reading into "never read", which is the one
        answer this whole area is careful to keep distinct.
        """
        raw = self._listing_raw() or {}
        if "listings" in raw or "attempts" in raw:
            return {"listings": raw.get("listings") or {},
                    "attempts": raw.get("attempts") or {}}
        out = {"listings": {}, "attempts": {}}
        old_listing, old_attempt = raw.get("listing"), raw.get("attempt")
        for rec, key in ((old_listing, "listings"), (old_attempt, "attempts")):
            if not rec:
                continue
            where = (old_listing or {}).get("dir") or self._out_dir()
            out[key][where] = rec
        return out

    def _save_listing(self, listing, where=None):
        """Keep the last READABLE listing and the last ATTEMPT, separately.

        STALE CLOSES THE QUESTION, AND SO DOES OVERWRITING. A read that came
        back unparseable says something about that read; it does not make the
        previous listing wrong, only older. Collapsing the two would either
        throw away the only account of the directory anybody has, or hide the
        fact that the newest look failed -- and each of those is a way of
        answering a question that was not asked.

        So both are stored, and `file_listing` hands back both. The consumer
        gets to see "here is what was there at 14:02, and the 15:40 attempt
        could not be read", which is the truth and is not expressible in one
        record.
        """
        try:
            self._ensure_pulled()
        except RuntimeError:
            return
        where = where or listing.get("dir") or self._out_dir()
        store = self._listing_store()
        store["attempts"][where] = {"at": listing.get("read_at") or time.time(),
                                    "ok": bool(listing.get("ok")),
                                    "why": listing.get("why"),
                                    "reason": listing.get("reason"),
                                    "raw": listing.get("raw")}
        if listing.get("ok"):
            store["listings"][where] = listing
        try:
            with open(self._listing_file(), "w") as f:
                json.dump(store, f)
        except OSError:
            pass

    def _file_listing(self, req):
        """What the target's outgoing directory held WHEN IT WAS LAST READ.

        THIS TOUCHES NOTHING. Reading that directory means rebooting the
        machine into NET, so the picker is fed from the last reading rather
        than from a live one -- and the age is reported beside it, always, so
        nobody reads a three-hour-old list as a current one.

        NO STALENESS THRESHOLD IS INVENTED HERE. There is no number of minutes
        after which a listing becomes wrong: it goes wrong when somebody
        writes to that directory, which this host cannot see. So the age is a
        fact and the judgement is the operator's, rather than a boolean with a
        constant behind it that nothing measured.
        """
        store = self._listing_store()
        try:
            where = (dos_dir_path(req.get("dir")) if req.get("dir")
                     else self._out_dir())
        except ValueError as exc:
            return {"ok": False, "why": "bad-dir", "error": str(exc)}
        listing = store["listings"].get(where)
        attempt = store["attempts"].get(where)
        out = {"ok": True, "out_dir": where, "listing": listing,
               "attempt": attempt, "age_s": None, "note": None,
               "known": sorted(store["listings"])}
        if listing:
            out["age_s"] = round(time.time() - (listing.get("read_at") or 0), 1)
            out["files"] = listing.get("files") or []
            out["count"] = len(out["files"])
        else:
            out["files"], out["count"] = [], 0
            out["note"] = ("nothing has read %s yet. `vcctrl file-refresh` "
                           "reboots the target into NET and reads it -- an "
                           "unread directory is not an empty one" % where)
        if attempt and not attempt.get("ok"):
            out["attempt_note"] = ("the most recent read failed (%s): %s"
                                   % (attempt.get("why"),
                                      attempt.get("reason")))
        return out

    def _file_pulled(self, req):
        """What has been fetched off the target and is sitting on this host.

            list   what is here, with how it was verified and where it came
                   from
            read   one chunk of one file, base64 -- so the CLI can write the
                   bytes out on the caller's own disk, and the browser has
                   /pulled for the same job without a round trip through JSON
            clear  drop one or all. Refuses to remove anything it cannot
                   account for, the same rule the staging queue follows

        `read` IS CHUNKED FOR THE SAME REASON `file_stage` IS: the size policy
        admits files far larger than one JSON message should carry, and a
        10 MB base64 blob in a single response is a message this socket should
        not be asked to hold.

        Refusals, and the set is NOT CLOSED:

            bad-name         not expressible as a DOS 8.3 filename, which is
                             also the path guard -- see dos_filename
            not-here         nothing of that name has been pulled. Distinct
                             from a read that failed, which reports the OS
                             error instead
            offset-mismatch  a read that starts past the end of the file
        """
        action = (req.get("action") or "list").lower()
        try:
            pulled, meta_dir = self._ensure_pulled()
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        have = self._pulled()

        if action == "list":
            return {"ok": True, "pulled": have, "count": len(have),
                    "dir": pulled,
                    "bytes": sum(r["bytes"] for r in have)}

        if action == "read":
            try:
                name, _notes = dos_filename(req.get("name"))
            except ValueError as exc:
                return {"ok": False, "why": "bad-name", "error": str(exc)}
            path = os.path.join(pulled, name)
            if not os.path.isfile(path):
                return {"ok": False, "why": "not-here",
                        "error": ("%s has not been pulled off the target -- "
                                  "`vcctrl pulled list` says what has" % name)}
            total = os.path.getsize(path)
            try:
                offset = int(req.get("offset") or 0)
                length = int(req.get("length") or self.PULLED_CHUNK_MAX)
            except (TypeError, ValueError):
                return {"ok": False, "error": "offset and length are bytes"}
            length = max(0, min(length, self.PULLED_CHUNK_MAX))
            if offset < 0 or offset > total:
                return {"ok": False, "why": "offset-mismatch",
                        "error": "offset %d is outside %s (%d bytes)"
                                 % (offset, name, total)}
            try:
                with open(path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read(length)
            except OSError as exc:
                return {"ok": False, "error": "cannot read %s: %s"
                                              % (path, exc)}
            return {"ok": True, "name": name, "total": total,
                    "offset": offset, "len": len(chunk),
                    "eof": offset + len(chunk) >= total,
                    "data": base64.b64encode(chunk).decode()}

        if action != "clear":
            return {"ok": False, "error": "unknown pulled action: %r" % action}

        only = req.get("name")
        removed, kept = [], []
        for r in have:
            if only and r["name"] != only:
                continue
            if r.get("orphan"):
                kept.append(r["name"])
                continue
            for path in (os.path.join(pulled, r["name"]),
                         os.path.join(meta_dir, r["name"] + ".json")):
                try:
                    os.remove(path)
                except OSError:
                    pass
            removed.append(r["name"])
        return {"ok": True, "removed": removed, "left_alone": kept,
                "pulled": self._pulled(),
                "note": ("files this capability cannot account for are left in "
                         "place and named in left_alone") if kept else None}

    def _file_queue(self, req):
        """List what is staged, or drop it.

        `clear` removes staged files AND their metadata. It refuses to touch
        anything it did not stage -- an orphan in the FTP root is reported by
        `_queued()` and left alone, because deleting a file this capability
        cannot account for is how a recovery copy somebody put there by hand
        disappears.
        """
        action = (req.get("action") or "list").lower()
        try:
            root, partial, meta = self._ensure_dirs()[:3]
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        q = self._queued()
        if action == "list":
            v = size_verdict([r["bytes"] for r in q]) if q else \
                {"ok": True, "why": None, "reason": None, "total": 0}
            return {"ok": True, "queue": q, "count": len(q), "size": v,
                    "stage_dir": root}
        if action != "clear":
            return {"ok": False, "error": "unknown queue action: %r" % action}
        only = req.get("name")
        removed, kept = [], []
        for r in q:
            if only and r["name"] != only:
                continue
            if r.get("orphan"):
                kept.append(r["name"])
                continue
            for p in (os.path.join(root, r["name"]),
                      os.path.join(meta, r["name"] + ".json")):
                try:
                    os.remove(p)
                except OSError:
                    pass
            removed.append(r["name"])
        return {"ok": True, "removed": removed, "left_alone": kept,
                "queue": self._queued(),
                "note": ("files this capability did not stage are left in "
                         "place and named in left_alone") if kept else None}

    def _file_stage(self, req):
        """Receive bytes and put a COMPLETE, VERIFIED file into the FTP root.

        Chunked, because the size policy admits files far larger than one
        JSON message should carry: `offset` says where this chunk belongs and
        must equal what has already arrived, so a dropped or reordered call
        fails loudly instead of writing a hole.

        THE SHA IS CHECKED HERE, AND THAT IS WHAT MAKES THE LATER ROUND TRIP
        MEAN ANYTHING. The transfer's verification pulls the file back off the
        target and compares it against this staged copy. If the staged copy is
        already wrong, that comparison compares a wrong file against itself
        and passes -- a check that confirms the transport while saying nothing
        about the payload. Verifying the source at the moment it lands is the
        only point where that can be caught, because afterwards there is
        nothing left to compare against.

        The caller does NOT choose a path. It sends a name; this decides where
        it goes. A caller-supplied path into the FTP root is a write-anywhere
        primitive on the daemon host, reachable from a browser.

        REFUSALS, and the set is NOT CLOSED. Five words, split across a line
        the caller has to care about -- the first three are things the CALLER
        must change and the last two are things that went wrong in transit:

            bad-name         not expressible as a DOS 8.3 filename
            too-large        over the ceiling, alone or with the queue
            name-taken       something is already staged under that 8.3 name
            offset-mismatch  a chunk did not start where the last one ended
            short            the final chunk arrived and the file is undersized
            sha-mismatch     the assembled bytes are not the declared ones

        `offset-mismatch` is loud rather than patched. Seeking to the offset
        would leave a hole full of zeroes and the sha would fail at the end
        with nothing saying which chunk was lost -- the failure would be real
        and the diagnosis absent.
        """
        try:
            root, partial, meta = self._ensure_dirs()[:3]
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}

        try:
            name, notes = dos_filename(req.get("name"))
        except ValueError as exc:
            return {"ok": False, "why": "bad-name", "error": str(exc)}

        try:
            total = int(req.get("total"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "total (the whole file's size in "
                                          "bytes) is required"}

        # THE POLICY IS CHECKED AGAINST THE DECLARED TOTAL, BEFORE ANY BYTES
        # ARE WRITTEN, AND AGAINST THE QUEUE IT WOULD JOIN. Checking only at
        # the end would mean refusing a file after spending the time to
        # receive all of it, and checking only this file would let three
        # 30 MB uploads walk past a 64 MB ceiling one at a time.
        others = [r["bytes"] for r in self._queued() if r["name"] != name]
        verdict = size_verdict(others + [total])
        if not verdict["ok"]:
            return {"ok": False, "why": verdict["why"],
                    "error": verdict["reason"], "size": verdict}

        existing = {r["name"] for r in self._queued()}
        if name in existing and not req.get("replace"):
            # TWO PHOTOS OFF A PHONE THAT DIFFER AFTER THE EIGHTH CHARACTER
            # BECOME ONE FILE. Refused rather than resolved: renaming behind
            # the operator's back means the file they look for on the target
            # is not the one that arrived.
            return {"ok": False, "why": "name-taken",
                    "error": ("%s is already staged. Two different files can "
                              "mangle to one 8.3 name -- pass replace to "
                              "overwrite deliberately." % name)}

        try:
            offset = int(req.get("offset") or 0)
        except (TypeError, ValueError):
            return {"ok": False, "error": "offset must be a byte offset"}

        try:
            chunk = base64.b64decode(req.get("data") or "", validate=True)
        except Exception as exc:
            return {"ok": False, "error": "data is not valid base64: %s" % exc}
        if len(chunk) > self.STAGE_CHUNK_MAX:
            return {"ok": False, "error": "chunk is %d bytes; the limit is %d"
                                          % (len(chunk), self.STAGE_CHUNK_MAX)}

        part = os.path.join(partial, name)
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if offset == 0 and have:
            try:
                os.remove(part)
            except OSError:
                pass
            have = 0
        if offset != have:
            # LOUD, NOT PATCHED. Seeking to the offset would happily leave a
            # hole full of zeroes, and the sha would then fail at the end with
            # nothing saying which chunk was lost.
            return {"ok": False, "why": "offset-mismatch",
                    "error": ("this chunk starts at %d and %d bytes have "
                              "arrived" % (offset, have)),
                    "have": have}
        if have + len(chunk) > total:
            return {"ok": False, "error": ("this chunk would take %s past its "
                                           "declared total of %d bytes"
                                           % (name, total))}
        try:
            with open(part, "ab") as f:
                f.write(chunk)
            have += len(chunk)
        except OSError as exc:
            return {"ok": False, "error": "cannot write %s: %s" % (part, exc)}

        if not req.get("final"):
            return {"ok": True, "name": name, "notes": notes,
                    "have": have, "total": total, "complete": False}

        if have != total:
            return {"ok": False, "why": "short",
                    "error": ("final chunk received but %s is %d bytes, not "
                              "the declared %d" % (name, have, total)),
                    "have": have}

        h = hashlib.sha256()
        try:
            with open(part, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
        except OSError as exc:
            return {"ok": False, "error": "cannot read back %s: %s"
                                          % (part, exc)}
        digest = h.hexdigest()
        want = (req.get("sha256") or "").strip().lower()
        if want and want != digest:
            # NOT PROMOTED. The bytes that arrived are not the bytes that were
            # sent, and putting them where the target can fetch them would
            # make the later round-trip compare a wrong file against itself.
            try:
                os.remove(part)
            except OSError:
                pass
            return {"ok": False, "why": "sha-mismatch",
                    "error": ("%s arrived with sha256 %s, not the declared %s "
                              "-- discarded rather than staged"
                              % (name, digest[:16], want[:16]))}

        # PROMOTED BY RENAME, which is atomic within a filesystem. There is no
        # instant at which a half-written file is visible under a name the
        # target could be told to fetch.
        try:
            os.replace(part, os.path.join(root, name))
            with open(os.path.join(meta, name + ".json"), "w") as f:
                json.dump({"name": name, "bytes": total, "sha256": digest,
                           "source": req.get("name"),
                           "staged_at": time.time()}, f)
        except OSError as exc:
            return {"ok": False, "error": "cannot stage %s: %s" % (name, exc)}

        q = self._queued()
        return {"ok": True, "name": name, "notes": notes, "complete": True,
                "bytes": total, "sha256": digest,
                "queue": q, "count": len(q),
                "size": size_verdict([r["bytes"] for r in q])}

    # -- the contract ---------------------------------------------------------

    def snapshot(self):
        """EVERY KEY ALWAYS PRESENT, null where unknown.

            {"available": true,  "why": null, ...}
            {"available": false, "why": "unsupported",    "reason": "..."}
            {"available": false, "why": "unknown",        "reason": "..."}
            {"available": false, "why": "not_configured", "reason": "..."}
            {"available": false, "why": "unreachable",    "reason": "..."}
            {"available": false, "why": "unchecked",      "reason": "..."}

        SIX WORDS, AND THEY ARE NOT INTERCHANGEABLE -- which is the whole
        point of this method. `unsupported` is a Macintosh and there is
        nothing to fix. `unknown` is "which machine is this?". `not_configured`
        is nobody asked for the feature. `unreachable` is a server that is not
        running, and it is the only one an operator fixes in a shell.
        `unchecked` is the probe itself failing, and reporting it as any of the
        others would be the instrument's state wearing the target's name.

        The set is NOT closed. LedsCapability's docstring said CLOSED SET and
        listed three while emitting five; the cost was a session reading this
        code and building against a set that was already wrong.
        """
        srv = self._server()
        out = {"available": False, "why": None, "reason": None,
               "backend": self.backend_name,
               "server": ("%s:%d" % srv) if srv else None,
               "dest": self._dest(), "out_dir": self._out_dir(),
               "warn_bytes": WARN_BYTES, "refuse_bytes": REFUSE_BYTES}

        supported, why = self.support()
        if supported is False:
            out["why"], out["reason"] = "unsupported", why
            return out
        if supported is None:
            out["why"], out["reason"] = "unknown", why
            return out
        if srv is None:
            # UNDERSCORE, matching Registry._refusal. It was written
            # `not-configured` here for two hours: one daemon, one state, two
            # spellings, and a consumer branching on the registry's word would
            # have fallen through to the default for this capability's. Caught
            # by the `why` guard only after that guard was rebuilt to see
            # assignment forms -- the old one could not read this line at all.
            out["why"] = "not_configured"
            out["reason"] = ("no capabilities.files.settings.target_host is "
                             "set. That key is the address baked into the "
                             "card's VCGET.BAT, and without it there is "
                             "nothing to write into the BAT and nothing to "
                             "check")
            return out
        live, why = self._reachable()
        if live is False:
            # THE SERVER'S OWN REASON BEATS THE PROBE'S. "Connection refused"
            # is what a probe sees; "no credentials" is what actually
            # happened, and only one of those tells anybody what to do.
            if FilesCapability._ftpd_error:
                why = "%s (the server on this host did not start: %s)" % (
                    why, FilesCapability._ftpd_error)
            out["why"], out["reason"] = "unreachable", why
            out["server_error"] = FilesCapability._ftpd_error
            return out
        if live is None:
            out["why"], out["reason"] = "unchecked", why
            return out
        out["available"] = True
        return out

    def _files(self, req):
        return {"ok": True, "files": self.snapshot()}

    def _file_name(self, req):
        """Dry-run the rename, so a person sees it before the bytes move."""
        try:
            name, notes = dos_filename(req.get("name"))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "name": name, "notes": notes}

    def _file_check(self, req):
        """The EXPENSIVE check, run before anything is destroyed.

        The cheap probe decides whether a button is grey. This one is what
        stands between the operator and a wasted reboot: a transfer that finds
        the server dead AFTER the machine has rebooted has cost two minutes and
        the target's entire environment, for a fault that a shell command
        fixes. So it runs after the confirmation and before the first
        keystroke -- the expensive check guarding the expensive action.
        """
        snap = self.snapshot()
        if not snap["available"]:
            return {"ok": False, "error": snap["reason"], "why": snap["why"],
                    "files": snap}
        live, why = self._reachable(timeout=float(req.get("timeout", 5.0)))
        if live is not True:
            return {"ok": False, "why": "unreachable" if live is False
                    else "unchecked", "error": why, "files": snap}
        return {"ok": True, "files": snap}


VideoCapability.BACKENDS = {"v4l2-ffmpeg": VideoCapability}
VideoCapability.DEFAULT_BACKEND = VideoCapability
VideoCapability.DEFAULT_BACKEND_NAME = 'v4l2-ffmpeg'
AudioCapability.BACKENDS = {"alsa-ffmpeg": AudioCapability}
AudioCapability.DEFAULT_BACKEND = AudioCapability
AudioCapability.DEFAULT_BACKEND_NAME = 'alsa-ffmpeg'
CameraCapability.BACKENDS = {"v4l2-ffmpeg": CameraCapability}
CameraCapability.DEFAULT_BACKEND = CameraCapability
CameraCapability.DEFAULT_BACKEND_NAME = 'v4l2-ffmpeg'
# SAME SHAPE AS CAMERA ABOVE, same reason -- DEFAULT_BACKEND/_NAME name what
# gets reported if the fallback path is ever taken (an absent `backend:` key
# resolves to `cls` regardless of these two, see Registry._resolve_backend),
# not whether it is safe to leave a profile silent about msd. It is not: a
# profile sharing no USB port with modernpc's gadget (gateway2000) needs an
# EXPLICIT `backend: none`, or an absent key gives it working-looking msd_*
# commands aimed at hardware it has no relationship to. See every
# profile-kind template and vcctrl.yaml/vcctrl-modernpc.yaml for the lines
# that actually do the disabling.
MsdCapability.BACKENDS = {"usb-gadget-msd": MsdCapability}
MsdCapability.DEFAULT_BACKEND = MsdCapability
MsdCapability.DEFAULT_BACKEND_NAME = 'usb-gadget-msd'

FilesCapability.BACKENDS = {"mtcp-ftp": FilesCapability}
FilesCapability.DEFAULT_BACKEND = FilesCapability
FilesCapability.DEFAULT_BACKEND_NAME = 'mtcp-ftp'

# One implementation, like Video/Audio/Files above -- it reads a pulled
# file rather than talking to a device, but the registry's config schema
# treats every capability uniformly, so it still needs a name rather than
# being a special case that only works unconfigured.
SysinfoCapability.BACKENDS = {"dinspect-pulled": SysinfoCapability}
SysinfoCapability.DEFAULT_BACKEND = SysinfoCapability
SysinfoCapability.DEFAULT_BACKEND_NAME = 'dinspect-pulled'


class NoteCapability(Capability):
    """The one-sentence 'what is happening right now', set by whoever is
    driving the rig -- typically a Claude Code session, not a person at a
    keyboard.

    Not a fact about any device, so it holds nothing that Rule 1 would
    object to: it never touches input, power or video. It exists because the
    activity log has command names, not narration, and the public read-only
    page has no other way to tell a viewer WHY the screen is doing what it is
    doing.

    In-memory only, like `lastCmd`/`ledLog` on the page side -- a note that
    survived a daemon restart would describe a session that may not still be
    running, which is a worse failure than an honest "nothing reported yet".

    DESCRIPTIVE, not measured (see the class docstring above): every key is
    always present, null where unset.
    """

    name = "note"

    def __init__(self, devs):
        super(NoteCapability, self).__init__(devs)
        self._text = None
        self._at = None
        self._by = None

    def commands(self):
        return {"note": self._note, "note_set": self._note_set}

    def snapshot(self):
        if self._text is None:
            return {"text": None, "at": None, "by": None,
                    "reason": "nothing reported yet"}
        return {"text": self._text, "at": self._at, "by": self._by,
                "reason": None}

    def _note(self, req):
        return dict({"ok": True}, **self.snapshot())

    def _note_set(self, req):
        text = req.get("text")
        if not text:
            return {"ok": False, "error": "note_set needs 'text'"}
        text = str(text)
        # One sentence, not a log dump -- long enough for the examples this
        # was asked for ("rebooting into NET profile to copy log files for
        # review"), short enough that a runaway caller cannot turn this into
        # a second activity log.
        if len(text) > 200:
            text = text[:199] + "…"
        self._text = text
        self._at = time.time()
        self._by = req.get("as")
        return dict({"ok": True}, **self.snapshot())


NoteCapability.BACKENDS = {"in-memory": NoteCapability}
NoteCapability.DEFAULT_BACKEND = NoteCapability
NoteCapability.DEFAULT_BACKEND_NAME = 'in-memory'


class PublicTelemetryCapability(Capability):
    """Aggregate, non-identifying usage counters from the public read-only
    mirror (daemon/vcweb_public.py) -- visit counts by day, first-frame
    timing distribution, client-side error counts by message.

    READS ONLY, FROM A FILE, same shape as SysinfoCapability above:
    vcweb_public.py writes its own state to
    /var/lib/vcctrl-web-public/telemetry.json on its own schedule (see that
    file's _telemetry_save()) and this capability just reads it back
    whenever asked -- no network call, no IPC, nothing that crosses the
    boundary vcweb_public.py's own module docstring is so careful about.
    The two processes share a filesystem, on the same Pi; they do not share
    a wire. This is why the data can be "server-side, private-daemon-only"
    at all -- vcweb_public.py's own HTTP server never serves this file back
    to a visitor, and this capability's only route in is a local read. The
    file itself is mode 0700, owned by `vcctrl-ro` (the unprivileged user
    vcweb_public.py runs as) -- readable here anyway because this daemon
    runs as root, which was checked, not assumed, before relying on it.

    NEVER PER-VISITOR. vcweb_public.py's own collection is deliberately
    aggregate-only -- no IP, no user-agent, no cookie, no session id ever
    reaches the file this reads. This capability adds no aggregation of its
    own; it reports exactly what is on disk.
    """

    name = "public_telemetry"
    PATH = "/var/lib/vcctrl-web-public/telemetry.json"

    def commands(self):
        return {"public_telemetry": self._telemetry}

    def _telemetry(self, req):
        try:
            with open(self.PATH) as f:
                data = json.load(f)
        except FileNotFoundError:
            return {"ok": True, "available": False, "data": None,
                    "reason": "no telemetry file yet -- vcctrl-web-public "
                              "has not persisted one yet, or has never run"}
        except (OSError, ValueError) as exc:
            return {"ok": False, "available": False, "data": None,
                    "error": "%s: %s" % (type(exc).__name__, exc)}
        return {"ok": True, "available": True, "data": data, "reason": None}


PublicTelemetryCapability.BACKENDS = {"file": PublicTelemetryCapability}
PublicTelemetryCapability.DEFAULT_BACKEND = PublicTelemetryCapability
PublicTelemetryCapability.DEFAULT_BACKEND_NAME = 'file'

CAPABILITIES = [InputCapability, LedsCapability, PowerCapability,
                VideoCapability, AudioCapability, CameraCapability,
                MsdCapability,
                BoardCapability,
                FilesCapability,
                # Reads FilesCapability._pulled(), so it is registered
                # after it -- not that load order enforces this (every
                # capability is fully constructed before any start()
                # runs), only that it is the honest place to put a
                # capability whose data depends on another's.
                SysinfoCapability,
                NoteCapability,
                PublicTelemetryCapability]

# THIS PROFILE'S TLS cert/key paths -- state_dir-derived default, or an
# explicit daemon.web.tls.cert/key override. Returned rather than pushed
# onto vcweb.TLSServer's CLASS attributes the way the single-profile
# _push_tls_paths() used to: a second profile's start_web() call would then
# silently repoint the FIRST profile's already-running TLSServer at the
# wrong certificate the moment its own _context() next checked the file's
# mtime, since a class attribute is shared by every instance of that class
# in the process. Registry.start_web() instead passes these to
# vcweb.WebCapability, which applies them to its OWN TLSServer instance only
# (see vcweb.py's WebCapability.__init__/start -- the one deliberate
# exception to this phase not touching vcweb.py's routing, because this
# specific bug has nothing to do with routing and everything to do with a
# second profile's HTTPS listener presenting the wrong certificate).
def _tls_paths():
    state_dir = CFG.default("daemon.state_dir", "/var/lib/vcctrl")
    cert = os.path.join(state_dir, "tls.crt")
    key = os.path.join(state_dir, "tls.key")
    c = CFG.optional("daemon.web.tls.cert")
    k = CFG.optional("daemon.web.tls.key")
    if c not in (vcconfig.ABSENT, vcconfig.NONE):
        cert = c
    if k not in (vcconfig.ABSENT, vcconfig.NONE):
        key = k
    return cert, key

# Bind address for the web UI: LOOPBACK ONLY.
#
# Nothing listens on the tailnet directly. `tailscale serve` terminates TLS for
# the machine's MagicDNS name and proxies here, so the only way in is HTTPS,
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
WEB_BIND = CFG.default("daemon.web.bind", "127.0.0.1")
WEB_PORT = int(CFG.default("daemon.web.port", 8080))
# HTTPS served by the daemon itself, so browsers get HTTP/1.1 and WebSocket
# works. `tailscale serve --tcp` forwards this port as raw TCP. See TLSServer.
WEB_TLS_PORT = int(CFG.default("daemon.web.tls_port", 8443))


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
        # THE THIRD STATE. A capability whose backend is `none` is NOT failed
        # and NOT working -- it is not configured, and merging it into either
        # of the other two is the collapse this whole plan exists to prevent.
        # Failed means "it should work and does not, go and fix it"; not
        # configured means "nobody asked for it", and they need different
        # responses from a person.
        self.disabled = {}
        self.routes = {}
        for cls in CAPABILITIES:
            chosen, chosen_name, why = self._resolve_backend(cls)
            if chosen is _DISABLED:
                self.disabled[cls.name] = why
                self._register_refusals(cls, why)
                sys.stderr.write("capability %s: %s\n" % (cls.name, why))
                continue
            if chosen is None:
                # An unknown backend NAME. Deliberately a failure and not a
                # fallback to the default: silently substituting would give a
                # rig that reports healthy while running something other than
                # what its config asked for, and the typo would never surface.
                self.failed[cls.name] = why
                sys.stderr.write("capability %s: %s\n" % (cls.name, why))
                continue
            try:
                try:
                    cap = chosen(devs, self.bus)
                except TypeError:
                    cap = chosen(devs)
                # Set unconditionally, so every capability has one whether or
                # not its constructor asked for it.
                cap.bus = self.bus
                # Only the files capability declares it wants this, and it is
                # set unconditionally for the same reason `bus` is: an
                # attribute that exists on some siblings and not others, with
                # nothing saying which, is how LedsCapability ended up without
                # a bus and raised on the first line that touched it.
                cap.registry = self
                # The CONFIGURED name, not the class's. See
                # DEFAULT_BACKEND_NAME above for why those differ.
                cap.backend_name = chosen_name
                cap.settings = _cap_settings(cls.name)
                cap.start()
            except Exception as exc:
                self.failed[cls.name] = "%s: %s" % (type(exc).__name__, exc)
                sys.stderr.write("capability %s failed to start: %s\n" % (
                    cls.name, self.failed[cls.name]))
                continue
            self.caps[cls.name] = cap
            for cmd, fn in cap.commands().items():
                self.routes[cmd] = (cls.name, fn)

    @staticmethod
    def _resolve_backend(cls):
        """(implementation, name, None) | (_DISABLED, None, why)
        | (None, None, why-it-failed).

        The NAME is returned alongside the implementation because they are not
        recoverable from each other: several backend names can map to one
        class.
        """
        want = CFG.optional("capabilities.%s.backend" % cls.name)
        if want is vcconfig.ABSENT:
            return ((cls.DEFAULT_BACKEND or cls),
                    cls.DEFAULT_BACKEND_NAME, None)
        if want is vcconfig.NONE or want == "none":
            return _DISABLED, None, (
                "backend is `none` -- not configured, so it will answer "
                "'not configured' rather than a default")
        if not cls.BACKENDS:
            return None, None, (
                "no backend names are registered for this capability, so "
                "%r cannot be honoured; use `none` to switch it off" % (want,))
        impl = cls.BACKENDS.get(want)
        if impl is None:
            return None, None, ("unknown backend %r -- valid: %s, or `none`"
                                % (want, ", ".join(sorted(cls.BACKENDS))))
        return impl, want, None

    def _register_refusals(self, cls, why):
        """A disabled capability still OWNS its verbs.

        Without this its commands simply do not exist, and the daemon answers
        "unknown command" -- which reads as a broken client or a typo. The
        honest answer is that the verb is real and this rig has not configured
        it.
        """
        try:
            try:
                probe = cls(self.devs, self.bus)
            except TypeError:
                probe = cls(self.devs)
            names = list(probe.commands())
        except Exception:
            # Cannot enumerate without constructing, and constructing failed.
            # Say so rather than guessing a command list.
            self.disabled[cls.name] = (why + " (its verbs could not be "
                                       "enumerated, so they will report as "
                                       "unknown commands)")
            return
        for cmd in names:
            self.routes[cmd] = (cls.name, self._refusal(cls.name, cmd, why))

    @staticmethod
    def _refusal(name, cmd, why):
        def fn(_req):
            return {"ok": False,
                    "error": "%s is not configured on this rig" % name,
                    "why": "not_configured",
                    "detail": why,
                    "hint": "set capabilities.%s.backend in %s"
                            % (name, CFG.source or "vcctrl.yaml")}
        return fn

    def dispatch(self, cmd, req):
        route = self.routes.get(cmd)
        if route is None:
            return None
        _name, fn = route

        # Gating and event publishing are central rather than per-capability,
        # so a new capability cannot forget either. A capability that wants to
        # be gated only has to name its command in GATED_COMMANDS.
        if _gated(cmd, req):
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

    # start_web() used to live here: one call per Registry, one
    # WebCapability, one port -- correct back when at most one Registry
    # existed per process. Phase C moved it to module-level _start_web(),
    # called once from main() after EVERY profile's Registry is built, so
    # one WebCapability can be handed all of them (see _start_web's own
    # docstring). A lone Registry built outside main() (a test, most
    # likely) has no equivalent one-liner anymore; construct
    # vcweb.WebCapability({None: registry}, ...) directly -- that dict
    # form is exactly what a single-profile process builds internally.

    def configured_targets(self):
        """The `targets:` list as the page needs it, or [] when unconfigured.

        [] rather than a built-in table: a page that falls back to this rig's
        machines would name a Gateway 2000 to somebody who has never owned
        one. The consumer says "not configured" instead, which is true.
        """
        t = CFG.optional("targets")
        if t is vcconfig.ABSENT or t is vcconfig.NONE:
            return []
        out = []
        for row in t:
            if not isinstance(row, dict):
                continue
            nat = row.get("native") or {}
            out.append({"board_id": row.get("board_id"),
                        "name": row.get("name"),
                        "width": nat.get("width"),
                        "height": nat.get("height"),
                        "leds": row.get("leds")})
        return out

    def configured_machine(self):
        """The `machine:` block as the page needs it, or {} when absent.

        OPTIONAL, NOT YET UNIVERSAL. Every profile will carry one once WP2
        of internal/KVM-MACHINES-PLAN.md is fully landed; today a profile
        written before that (or a usb4vc-kind profile still describing
        itself through `targets:` alone) simply has none, and {} is that
        profile saying so -- not an error, the same "absent is a real
        answer" shape `configured_targets()` beside this uses for an empty
        list.

        `keyboard` is the one field a consumer needs even for a profile
        with no protocol board to detect one from -- see
        vcweb.py's board-capability fallback, which reaches for exactly
        this when `board` itself is unconfigured (modernpc: `backend:
        none`, so there is no BoardCapability instance to answer at all).
        """
        m = CFG.optional("machine")
        if m is vcconfig.ABSENT or m is vcconfig.NONE or not isinstance(m, dict):
            return {}
        nat = m.get("native") or {}
        return {"label": m.get("label"), "kind": m.get("kind"),
                "os": m.get("os"), "keyboard": m.get("keyboard"),
                "mouse": m.get("mouse"),
                "native": ({"width": nat.get("width"),
                           "height": nat.get("height")} if nat else None)}

    def report(self):
        """Three states, never two.

        `ok: False` alone cannot distinguish "broken" from "not asked for",
        and a reader who sees only the boolean will treat an unconfigured plug
        as a fault to chase. `configured` carries that, and `ok` is False for
        both so nothing that currently checks it starts passing by accident.
        """
        out = {}
        for name, cap in self.caps.items():
            out[name] = {"ok": True, "configured": True,
                         "backend": getattr(cap, "backend_name", None)}
        for name, err in self.failed.items():
            out[name] = {"ok": False, "configured": True, "error": err}
        for name, why in self.disabled.items():
            out[name] = {"ok": False, "configured": False, "error": None,
                         "why": "not_configured", "detail": why}
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
    if cmd in ("mouse_down", "mouse_up"):
        return req.get("button", "left")
    if cmd == "mouse_wheel":
        return str(req.get("dy", 0))
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
        # SAME TWO KEYS regardless of backend (see the comment above this
        # function on why the shape is a compatibility contract) -- the
        # VALUES differ, because there is no uinput device path to report
        # under hid-gadget: the daemon side is /dev/hidg0/1, not an evdev
        # node, and usb4vc_holds_us() answers a question that presupposes
        # USB4VC exists, which it does not for this backend either.
        if devs.hid_mode:
            kbd_path, mouse_path = devs.hid_kbd_device, devs.hid_mouse_device
            usb4vc_status = {}
        else:
            kbd_path, mouse_path = devs.kbd.device.path, devs.mouse.device.path
            usb4vc_status = usb4vc_holds_us()
        return {"ok": True,
                "keyboard": kbd_path,
                "mouse": mouse_path,
                "usb4vc": usb4vc_status,
                "led_paths": devs.led_paths,
                "leds": devs.read_leds()}

    if cmd == "caps":
        return {"ok": True, "capabilities": registry.report()}

    if cmd == "config":
        # ATTEST FROM THE THING THAT RAN IT.
        #
        # This reports what THIS PROCESS resolved at start, not what the file
        # on disk says now. Those differ whenever the file was edited without a
        # restart, which is the exact moment somebody is trying to work out why
        # a setting "is not taking effect" -- and re-reading the file to answer
        # that question tells them what they already believe rather than what
        # is running. A config file is not the configuration.
        #
        # `error` is present and null on a clean load rather than omitted: a
        # missing key would let a reader conclude "no error" from a payload
        # that never carried the field, which is the same shape as a plausible
        # set of retained zeroes.
        #
        # _CFG_CTX.error, NOT the bare CFG_ERROR module global: this handler
        # runs inside the calling connection's own _profile_scope (see
        # _serve_one_for), so on a multi-profile daemon this reports THIS
        # profile's own load error, not always the primary's -- a modernpc
        # whose own vcctrl-modernpc.yaml failed to parse would otherwise never
        # be running at all (discover_profiles() skips it), so in practice
        # this is only ever non-None for the primary today, but the read is
        # correct regardless of how many profiles exist.
        cfg_error = _CFG_CTX.error
        return {"ok": True,
                "config": {
                    "source": CFG.source,
                    "error": cfg_error,
                    "on_defaults": cfg_error is not None or CFG.source is None,
                    "overrides": list(CFG.warnings),
                    "resolved": CFG.as_dict(),
                }}

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


class Instance(object):
    """One profile's fully-constructed runtime state: its own Config,
    Devices, Registry, and the socket path(s) clients reach it on. Built
    once per discovered profile at startup (see discover_profiles()/
    _build_instance()) and kept for the life of the process.

    `name` is None ONLY for a root config that has never set
    daemon.profile_name -- Phase D's whole point is that a profile is
    "gateway2000" or "modernpc", not "the primary" and "a named one" as two
    structurally different kinds of thing. None means "not yet named",
    a backward-compatible degraded state, not a permanent second class of
    profile.

    `is_primary` is True for exactly the ONE instance built from the root
    config file (discover_profiles() always puts it first) -- this is
    what decides whether the legacy daemon.socket alias gets bound
    alongside the name-derived path, not `name is None`. A primary WITH a
    declared name still binds both, so bin/vcctrl-client with no
    --profile flag keeps reaching it (see _build_instance()'s own
    comment on socket_paths).
    """

    def __init__(self, name, cfg, error=None, is_primary=False):
        self.name = name
        self.cfg = cfg
        self.error = error
        self.is_primary = is_primary
        self.devs = None
        self.registry = None
        self.socket_paths = []

    def label(self):
        return self.name or ("(unnamed default)" if self.is_primary
                             else "(unnamed)")


def discover_profiles():
    """The primary profile, plus any sibling vcctrl-<name>.yaml files found
    next to wherever the primary's own config file was loaded from.

    NO SEPARATE REGISTRY FILE: the directory listing IS the registry, so it
    cannot drift from which profiles actually have a config on disk -- the
    same reasoning bin/vcctrl-client's own `profiles` command already
    documents for socket discovery, and pi/install.sh's
    install_modernpc_profile()'s naming (vcctrl-modernpc.yaml,
    /run/vcctrl-modernpc.sock) already commits to. A profile named `modernpc`
    means the same file/socket names everywhere in this codebase; this
    function does not get to invent a different convention just because it
    lives in a different file.

    A SIBLING THAT FAILS TO PARSE IS SKIPPED, with a clear message, rather
    than run on built-in defaults the way a MISSING primary config does --
    those are different situations. A rig with no vcctrl.yaml at all is a
    fresh clone nobody has configured yet, and running it on defaults is
    what makes that clone usable out of the box (see the "READ ONCE, AND
    NEVER FATAL" comment above CFG_ERROR). A vcctrl-modernpc.yaml that
    exists and fails to parse is a SPECIFIC, additional profile somebody
    deliberately configured, and starting it on defaults would silently
    hand it usb4vc-uinput and /dev/video0 -- neither of which a hid-gadget
    target has any business touching. Absent is the honest answer for a
    profile whose own file is broken, not a guess at what it meant.

    Returns [Instance, ...], primary always first and always present (even
    running on built-in defaults, if its own file was missing or broken --
    exactly today's single-profile behavior, unconditionally, for a rig
    with no sibling files at all).

    THE PRIMARY'S OWN NAME COMES FROM daemon.profile_name IN ITS OWN FILE,
    never a literal baked into this function -- "gateway2000" is a fact
    about one rig's config, not a fact about this codebase. Absent (no
    such key, or the root config missing/broken entirely) leaves
    Instance.name at None, the same backward-compatible degraded state
    this function has always returned for the primary.
    """
    primary_name = None
    _n = _PRIMARY_CFG.optional("daemon.profile_name")
    if _n not in (vcconfig.ABSENT, vcconfig.NONE):
        primary_name = _n
    profiles = [Instance(primary_name, _PRIMARY_CFG, CFG_ERROR,
                         is_primary=True)]
    if not _PRIMARY_CFG.source:
        return profiles
    if os.path.basename(_PRIMARY_CFG.source) != "vcctrl.yaml":
        # An explicit VCCTRL_CONFIG/--config pointed somewhere unconventional
        # (a test harness, most likely). Sibling discovery only applies to
        # the normal deployed layout -- an unconventional primary path has
        # no established sibling-naming convention to reuse.
        return profiles
    d = os.path.dirname(os.path.abspath(_PRIMARY_CFG.source))
    for path in sorted(glob.glob(os.path.join(d, "vcctrl-*.yaml"))):
        fname = os.path.basename(path)
        if fname.endswith(".example.yaml"):
            continue
        name = fname[len("vcctrl-"):-len(".yaml")]
        try:
            cfg = vcconfig.load(path)
        except vcconfig.ConfigError as exc:
            sys.stderr.write(
                "config: profile %r (%s) failed to load, skipping this "
                "profile entirely: %s\n" % (name, path, exc))
            continue
        profiles.append(Instance(name, cfg))
    return profiles


def _build_instance(inst):
    """Construct one profile's Devices/Registry, with CFG bound to ITS OWN
    config for the whole call -- every _resolve_backend/_cap_settings/
    Devices.__init__/start_web() read of CFG.xxx along the way happens
    inside this scope, so each one resolves against THIS profile's file,
    not whichever profile happened to build last (see _profile_scope's own
    docstring). Returns False (and builds nothing) if this profile cannot
    start at all -- today, only the hid-gadget device-existence check can
    cause that; a bad capability backend inside a successfully-loaded
    config degrades that ONE capability instead (Rule 2), same as always.
    """
    with _profile_scope(inst.cfg, inst.error):
        if CFG.default("capabilities.input.backend",
                       "usb4vc-uinput") == "hid-gadget":
            kbd = CFG.default("capabilities.input.settings.hid_keyboard_device",
                              HID_KBD_DEVICE)
            mouse = CFG.default("capabilities.input.settings.hid_mouse_device",
                                HID_MOUSE_DEVICE)
            missing = [p for p in (kbd, mouse) if not os.path.exists(p)]
            if missing:
                sys.stderr.write(
                    "vcctrld: profile %s's capabilities.input.backend is "
                    "hid-gadget but %s do(es) not exist -- has "
                    "vcctrl-hid-gadget.service run on this boot? "
                    "(pi/files/vcctrl-hid-gadget-setup.sh builds them; it "
                    "needs dtoverlay=dwc2,dr_mode=peripheral active, which "
                    "needs a reboot after it is first added)\n"
                    % (inst.label(), ", ".join(missing)))
                return False
        inst.devs = Devices()
        if not inst.devs.hid_mode:
            # Give USB4VC's 0.75 s scan time to find us before accepting
            # work, so the first command a client sends is not silently
            # dropped. Skipped entirely under hid-gadget: there is no
            # USB4VC for this instance to be found by, and waiting 1.5s for
            # a scan that will never happen and then warning about it is
            # pure noise, not a diagnostic.
            time.sleep(1.5)
            held = usb4vc_holds_us()
            if not all(held.values()):
                sys.stderr.write(
                    "warning: USB4VC has not opened %s (profile %s)\n"
                    % ([k for k, v in held.items() if not v], inst.label()))
        inst.registry = Registry(inst.devs)
        # NOT inst.registry.start_web() here (Phase B still did, one call
        # per profile, one port per profile) -- Phase C's whole point is
        # ONE web port for every profile, reached via a /p/<name>/ path
        # prefix instead. That needs every instance built first (see
        # _start_web(), called once from main() after this loop finishes),
        # not one per instance as it is constructed.
        #
        # SOCKET PATHS -- Phase D. A named profile's socket is ALWAYS
        # /run/vcctrl-<name>.sock, derived from the name, never from its
        # own daemon.socket -- the same "the naming convention IS the
        # registry" reasoning bin/vcctrl-client's own profile_socket_path()
        # already uses, now applied inside the daemon that has to answer to
        # it. daemon.socket in a NAMED profile's config is vestigial (same
        # treatment as daemon.web.* in _start_web()) and warned about below
        # rather than silently honoured, which would let two differently-
        # configured sockets both claim to be "this profile's socket".
        #
        # THE PRIMARY IS DIFFERENT, ON PURPOSE: it ALWAYS binds
        # daemon.socket (default /run/vcctrl.sock) as a standing alias,
        # named or not -- every existing bin/vcctrl-client call with no
        # --profile flag, and agent/vcctrl_mcp.py's default (unset)
        # `profile` parameter, was built assuming that path reaches "the
        # main target" and must keep doing so with ZERO behavior change.
        # A NAMED primary binds BOTH that alias and its own
        # /run/vcctrl-<name>.sock -- same instance, same Devices, same
        # Registry, same Arbiter, reachable by either path, not two
        # independent things that could drift.
        paths = []
        if inst.is_primary:
            paths.append(CFG.default("daemon.socket", "/run/vcctrl.sock"))
        if inst.name:
            paths.append("/run/vcctrl-%s.sock" % inst.name)
        elif not inst.is_primary:
            # discover_profiles() only ever builds a nameless non-primary
            # Instance if something upstream changes; today it cannot
            # happen (siblings are always named from their own filename),
            # but a socket-less instance would hang forever in
            # _serve_instance() with no diagnostic at all, so this is
            # checked here rather than assumed away.
            sys.stderr.write(
                "vcctrld: a non-primary profile has no name -- refusing to "
                "start it rather than guess a socket path\n")
            return False
        if not inst.is_primary:
            _sock_setting = CFG.optional("daemon.socket")
            if _sock_setting not in (vcconfig.ABSENT, vcconfig.NONE):
                sys.stderr.write(
                    "vcctrld: profile %s sets daemon.socket (%r), but a "
                    "named profile's socket is always /run/vcctrl-%s.sock, "
                    "derived from its name -- remove daemon.socket from "
                    "this profile's config.\n"
                    % (inst.label(), _sock_setting, inst.name))
        inst.socket_paths = paths
    return True


def _start_web(built):
    """ONE vcweb.WebCapability shared by every built instance -- Phase C.

    Bound to the FIRST built instance's own daemon.web.* settings, not
    hard-coded to "the primary": discover_profiles() always puts the
    primary first, but if its own _build_instance() failed (today, only
    the hid-gadget missing-device check can do that) while a named
    profile succeeded, that profile is the only sensible thing left to
    own the one shared port -- there is nothing else running to share it
    with. In the normal case this is simply the primary.

    A NAMED PROFILE'S OWN daemon.web.* SETTINGS ARE NOW VESTIGIAL. Before
    this phase each profile had its own WebCapability on its own port
    (modernpc: 8180); now there is one port total, reached at
    /p/<name>/... instead. Warn rather than silently ignore -- a leftover
    daemon.web.port in an old per-profile config should not look like it
    is still doing something when it no longer is.
    """
    primary = built[0]
    for inst in built[1:]:
        with _profile_scope(inst.cfg, inst.error):
            for key in ("bind", "port", "tls_port"):
                if CFG.optional("daemon.web.%s" % key) is not vcconfig.ABSENT:
                    sys.stderr.write(
                        "vcctrld: profile %s sets daemon.web.%s, but only "
                        "the first-built instance's web settings are used "
                        "now -- reachable at /p/%s/... on that one shared "
                        "port instead. Remove it from this profile's "
                        "config.\n" % (inst.label(), key, inst.label()))

    # `None` (the "no /p/<name>/ prefix" default -- see vcweb.py's
    # Handler._route_profile()) is ALWAYS an alias for `primary` here, even
    # once it has a real declared name -- the exact same dual-key shape
    # _build_instance() already gives the primary's SOCKET (both
    # /run/vcctrl.sock and /run/vcctrl-<name>.sock reach it). Phase D gave
    # the primary a real Instance.name and this dict stopped having a bare
    # `None` entry for it -- every plain /state.json or /cmd request (no
    # prefix at all) was resolving WebCapability.registry to
    # self._registries.get(None), which no longer existed, and 500ing with
    # "'NoneType' object has no attribute 'caps'"/"'execute'" on every
    # single request. Caught live on the real rig after the Phase E
    # cutover -- the unix-socket-based CLI/MCP path never went through this
    # dict at all, so every test done that way looked completely healthy
    # while the web UI was already broken for everyone.
    registries = {inst.name: inst.registry for inst in built}
    registries[None] = primary.registry
    # SAME KEYING, for `CFG` this time -- see vcweb.py's own comment on
    # `_profile_configs`/`_cfg_ctx` for why a web request needs this at
    # all: `self.registry` was already bound per profile, but a `CFG.xxx`
    # read reached from inside a request (configured_targets(),
    # _configured_keyboards(), ...) was not, and reported whichever
    # profile's config a previous request (or none) had left bound on that
    # thread.
    configs = {inst.name: (inst.cfg, inst.error) for inst in built}
    configs[None] = (primary.cfg, primary.error)
    with _profile_scope(primary.cfg, primary.error):
        bind = CFG.default("daemon.web.bind", "127.0.0.1")
        port = int(CFG.default("daemon.web.port", 8080))
        tls_port = int(CFG.default("daemon.web.tls_port", 8443))
        cert, key = _tls_paths()
    try:
        import vcweb
        web = vcweb.WebCapability(registries, bind, port, tls_port=tls_port,
                                  cert=cert, key=key,
                                  profile_configs=configs, cfg_ctx=_CFG_CTX)
        web.start()
    except Exception as exc:
        sys.stderr.write("capability web failed to start: %s: %s\n"
                         % (type(exc).__name__, exc))
        return None
    # Every instance's OWN `caps` report should still list "web" as
    # present, same as when start_web() set this on its own registry --
    # a client checking any one profile's socket still sees it has a web
    # UI, even though the process behind it is now shared.
    for inst in built:
        inst.registry.caps["web"] = web
    sys.stderr.write(
        "web ui on http://%s:%d/  tls=%s  profiles=%s\n"
        % (bind, port, tls_port if web.tls_up else "unavailable",
           sorted(inst.label() for inst in built)))
    return web


def _serve_one_for(conn, inst):
    """_serve_one, with CFG bound to THIS connection's profile for its
    whole life -- a threading.local does not inherit across a
    `threading.Thread(...)` boundary, so each freshly-spawned connection
    thread has to rebind it itself; see _profile_scope's own docstring."""
    with _profile_scope(inst.cfg, inst.error):
        _serve_one(conn, inst.devs, inst.registry)


def _accept_loop(sock_path, inst):
    """Bind one socket path and accept connections for it forever, one
    thread per connection -- exactly as serve() always worked, just now
    one of possibly several such loops (one per PATH, not per instance --
    see _serve_instance()) running in this process. See serve()'s own
    former docstring (still true, unchanged) for why one-thread-per-
    connection is safe: every multi-event Devices operation holds
    self.lock for the whole operation, not per event, so concurrent
    `type` calls serialise into intact strings rather than interleaving.

    Every path bound for the SAME inst shares its devs/registry/arbiter --
    there is exactly one of each per Instance regardless of how many
    socket paths reach it (see _build_instance()'s own comment on
    socket_paths), so a lock acquired via one path is the identical
    Arbiter state seen via the other, not two independent things that
    could drift.
    """
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    os.chmod(sock_path, 0o666)
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        t = threading.Thread(target=_serve_one_for, args=(conn, inst),
                             daemon=True)
        t.start()


def _serve_instance(inst):
    """Bind every socket path this instance answers on (see
    _build_instance()'s socket_paths comment -- a named primary binds
    both its legacy alias and its own name-derived path) and accept
    connections on all of them. All but the last path get their own
    background thread; the last runs its accept loop in THIS thread/
    call, matching main()'s own contract that _serve_instance(built[0])
    blocks the process.
    """
    with _profile_scope(inst.cfg, inst.error):
        if inst.devs.hid_mode:
            kbd_desc = inst.devs.hid_kbd_device
            mouse_desc = inst.devs.hid_mouse_device
        else:
            kbd_desc = inst.devs.kbd.device.path
            mouse_desc = inst.devs.mouse.device.path
    sys.stderr.write(
        "vcctrld ready: profile=%s sockets=%s kbd=%s mouse=%s leds=%s "
        "caps=%s\n"
        % (inst.label(), inst.socket_paths, kbd_desc, mouse_desc,
           sorted(inst.devs.led_paths), sorted(inst.registry.caps)))
    if inst.registry.failed:
        sys.stderr.write("vcctrld degraded (profile %s): %s\n"
                         % (inst.label(), sorted(inst.registry.failed)))
    sys.stderr.flush()
    for path in inst.socket_paths[1:]:
        threading.Thread(target=_accept_loop, args=(path, inst),
                         daemon=True).start()
    _accept_loop(inst.socket_paths[0], inst)


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

    profiles = discover_profiles()
    for inst in profiles:
        if inst.error:
            sys.stderr.write(
                "config (profile %s): RUNNING ON BUILT-IN DEFAULTS -- %s\n"
                % (inst.label(), inst.error))
        else:
            sys.stderr.write("config (profile %s): %s\n" % (
                inst.label(),
                inst.cfg.source or "none found, built-in defaults"))
            for w in inst.cfg.warnings:
                sys.stderr.write("config (profile %s): override %s\n"
                                 % (inst.label(), w))

    # EXCLUSIVE HARDWARE GUARD. usb4vc-uinput assumes exactly ONE physical
    # USB4VC/SPI bridge on this Pi -- true for the primary today, and true
    # for any future board-swap profile (see profile-kinds/
    # rgb2hdmi-usb4vc.yaml) sharing the same input mechanism. Two profiles
    # both building usb4vc-uinput Devices() would each create their own
    # "vcctrl virtual keyboard"/"vcctrl virtual mouse" uinput pair, and
    # USB4VC has no concept of which one to trust -- exactly the "hardware
    # active on more than one profile at once" case the operator ruled out
    # by policy. Enforced here rather than left as an operator mistake
    # waiting to happen: the FIRST such profile (discover_profiles() always
    # puts the primary first) wins the bridge; any other is refused with a
    # clear reason, same shape as a hid-gadget profile refusing on a
    # missing device.
    conflict_free = []
    claimed_uinput = False
    for inst in profiles:
        with _profile_scope(inst.cfg, inst.error):
            backend = CFG.default("capabilities.input.backend",
                                  "usb4vc-uinput")
        if backend == "usb4vc-uinput":
            if claimed_uinput:
                sys.stderr.write(
                    "vcctrld: profile %s also uses "
                    "capabilities.input.backend: usb4vc-uinput, but another "
                    "profile already claimed the one physical USB4VC bridge "
                    "-- refusing to start it rather than create a second "
                    "uinput keyboard/mouse pair USB4VC cannot arbitrate "
                    "between.\n" % inst.label())
                continue
            claimed_uinput = True
        conflict_free.append(inst)

    built = [inst for inst in conflict_free if _build_instance(inst)]
    if not built:
        sys.stderr.write("vcctrld: no profile could be started\n")
        return 1

    _start_web(built)

    # Every profile but the first gets its own background accept-loop
    # thread. The FIRST ONE (always the primary/default profile, unless it
    # alone failed to build -- see discover_profiles()) runs its accept
    # loop on THIS, the main thread, exactly as serve() always did: a
    # daemon thread does not keep the process alive on its own, so
    # something has to block here, and it might as well be the same
    # instance that always did.
    for inst in built[1:]:
        threading.Thread(target=_serve_instance, args=(inst,),
                         daemon=True).start()
    _serve_instance(built[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
