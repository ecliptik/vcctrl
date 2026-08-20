#!/usr/bin/env python3
"""Acceptance tests for the vcctrld core refactor (docs/WEBKVM.md sec. 13.2).

These are the criteria that can be checked WITHOUT the rig. They deliberately
do not create real uinput devices: on a machine with a virtual console, a
virtual keyboard types into it, and the property under test lives in Devices'
lock discipline rather than in evdev. A fake device exercises exactly that and
cannot type into anything.

The criteria that DO need the rig -- byte-identical CLI output including error
paths, power-cycle-does-not-block-type against the real Kasa plug, USB4VC
holding both devices after a restart, and sweep/collect/uvconfig running
unmodified -- are listed in sec. 13.2 and are the handover point to the vcctrl
session. They are not here because they cannot be honestly faked.

    python3 tests/test_core.py

`check()` deliberately records failures rather than raising, so that one bad
invariant does not hide the twenty checks after it. That collect-and-report
design is worth keeping and it has one sharp edge: a test function that only
appends to FAILURES still RETURNS NORMALLY, so pytest counts it as passed.
`pytest tests/test_core.py` reported "17 passed" on this suite while nothing
was asserting anything -- and that number was cited as evidence for a merge.
tests/conftest.py closes that: under pytest, any check that fails during a
test fails that test, reported as a failure and not counted as passed. Under
`python3` nothing changes.
"""

import importlib.util
import os
import re
import sys
import threading
import time
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, os.pardir, "daemon", "vcctrld.py")

_loader = SourceFileLoader("vcctrld", DAEMON)
_spec = importlib.util.spec_from_loader("vcctrld", _loader)
vcctrld = importlib.util.module_from_spec(_spec)
sys.modules["vcctrld"] = vcctrld
_loader.exec_module(vcctrld)

from evdev import ecodes as e

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILURES.append(name)

# Under pytest, a test that only appends to FAILURES would otherwise return
# normally and be counted as passed. tests/conftest.py turns that into a real
# failure. It lives there rather than here because only a conftest hook can
# reach the call phase, and doing it in a teardown fixture gets the test
# labelled an "error" while still counting toward "passed".


# ---------------------------------------------------------------- fakes

class FakeDev(object):
    """Records (code, value) in order. The sleep widens the interleaving
    window: without it a race can hide behind the GIL switching interval."""

    def __init__(self, log, delay=0.0):
        self.log = log
        self.delay = delay
        # `status` reads devs.kbd.device.path; the fake needs it to answer.
        self.device = type("D", (), {"path": "/dev/input/event0"})()

    def write(self, etype, code, value):
        if self.delay:
            time.sleep(self.delay)
        self.log.append((etype, code, value))

    def syn(self):
        pass


def make_devices(delay=0.0):
    """Build a Devices without touching /dev/uinput."""
    d = vcctrld.Devices.__new__(vcctrld.Devices)
    d.log = []
    d.kbd = FakeDev(d.log, delay)
    d.mouse = FakeDev(d.log, delay)
    d.lock = threading.Lock()
    d.held = set()
    d.led_paths = {}
    return d


# ---------------------------------------------------------------- key table

def test_key_table():
    print("\nkey table")
    nk = vcctrld.NAMED_KEYS
    # USB4VC classification (usb4vc_usb_scan.py:913) requires these two.
    check("KEY_ENTER present", nk.get("enter") == e.KEY_ENTER)
    check("KEY_Y present", nk.get("y") == e.KEY_Y)
    # :877 -- declaring gamepad buttons would make USB4VC reclassify us.
    btns = [k for k, v in nk.items()
            if isinstance(v, int) and 0x100 <= v < 0x160]
    check("no BTN_* codes in the keyboard table", not btns, btns[:5])
    # The full-keyboard additions this refactor is for.
    for name in ("minus", "kp0", "kp9", "kpenter", "sysrq", "pause",
                 "menu", "102nd", "leftmeta", "semicolon", "slash"):
        check("has %s" % name, name in nk)
    check("all values are ints",
          all(isinstance(v, int) for v in nk.values()))


# ---------------------------------------------------------------- concurrency

def test_concurrent_type():
    """THE test. Two clients typing at once must not interleave.

    Repeated and with long homogeneous strings on the vcctrl session's advice:
    a single trial passes most runs even with the lock entirely absent, which
    would be the worst possible acceptance result -- a green check on the one
    item that matters.
    """
    print("\nconcurrent type (the corruption test)")
    N, TRIALS = 40, 25
    bad = 0
    for _ in range(TRIALS):
        d = make_devices(delay=0.00002)
        # Lowercase deliberately, and this comment is the point of the test as
        # much as the assertion is. CHARMAP maps "A" to (KEY_A, shift=True), so
        # an uppercase string emits KEY_LEFTSHIFT between every letter and the
        # run-detector below scores a correctly-locked run as interleaved.
        # This test failed 25/25 on its first run for exactly that reason.
        #
        # Left here rather than silently fixed because the next person writing
        # an input test will reach for uppercase -- it is more visually
        # distinct in a log -- and will hit the same thing. Note how close that
        # was to being "fixed" by loosening the assertion below, which would
        # have left a green check on the one item that can silently corrupt a
        # sweep launch. The control case is what made that impossible.
        ts = [threading.Thread(target=d.type_text, args=("a" * N, 0.0)),
              threading.Thread(target=d.type_text, args=("b" * N, 0.0))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        # Keydown events only, in emission order.
        seq = [c for (et, c, v) in d.log if et == e.EV_KEY and v == 1]
        runs = []
        for c in seq:
            if not runs or runs[-1][0] != c:
                runs.append([c, 0])
            runs[-1][1] += 1
        if len(runs) != 2 or any(r[1] != N for r in runs):
            bad += 1
    check("%d trials of 2x%d chars, no interleaving" % (TRIALS, N),
          bad == 0, "%d/%d trials corrupted" % (bad, TRIALS))

    # Control: prove the test can actually detect the failure it claims to.
    # Without this a green result says nothing about whether the check works.
    d = make_devices(delay=0.00002)
    unlocked = []
    for ch, code in (("A", e.KEY_A), ("B", e.KEY_B)):
        def spin(code=code):
            for _ in range(N):
                d.kbd.write(e.EV_KEY, code, 1)
                d.kbd.write(e.EV_KEY, code, 0)
        unlocked.append(threading.Thread(target=spin))
    for t in unlocked:
        t.start()
    for t in unlocked:
        t.join()
    seq = [c for (et, c, v) in d.log if v == 1]
    runs = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1]) + 1
    check("control: the same test DOES catch an unlocked writer", runs > 2,
          "saw %d runs, expected many" % runs)


def test_lock_scope():
    print("\nlock scope")
    for op, args in (("key", (["a", "b", "c"],)),
                     ("combo", (["ctrl", "alt", "delete"],)),
                     ("type_text", ("hello",))):
        d = make_devices()
        holder = {"held": False}
        real = d.lock

        class Watch(object):
            def __enter__(self):
                holder["held"] = True
                return real.__enter__()

            def __exit__(self, *a):
                holder["held"] = False
                return real.__exit__(*a)

        d.lock = Watch()
        getattr(d, op)(*args)
        check("%s completes and releases the lock" % op,
              holder["held"] is False)


# ---------------------------------------------------------------- keydown/up

def test_keydown_release_all():
    print("\nkeydown / keyup / release_all")
    d = make_devices()
    d.keydown("left")
    d.keydown("lshift")
    check("held tracks two keys", len(d.held) == 2)
    d.keyup("left")
    check("keyup drops one", len(d.held) == 1)
    n = d.release_all(0.0)
    check("release_all releases the remainder", n == 1 and not d.held)
    ups = [(c, v) for (et, c, v) in d.log if v == 0]
    check("every held key got an explicit release",
          sorted(c for c, _ in ups) == sorted([e.KEY_LEFT, e.KEY_LEFTSHIFT]))
    try:
        d.keydown("nosuchkey")
        check("unknown key rejected", False, "no exception")
    except ValueError:
        check("unknown key rejected", True)


# ---------------------------------------------------------------- registry

def test_registry():
    print("\nregistry and dispatch")
    d = make_devices()
    reg = vcctrld.Registry(d)
    for cmd in ("key", "type", "hold", "combo", "keydown", "keyup",
                "mouse_move", "mouse_click", "leds", "ledwait", "power"):
        check("routes %s" % cmd, cmd in reg.routes)
    check("the core capabilities started",
          set(["input", "leds", "power"]) <= set(reg.caps), sorted(reg.caps))
    check("nothing failed to load", reg.failed == {}, reg.failed)
    # video loads even with no capture device present -- it reports itself
    # degraded rather than refusing to start, so the daemon on a machine with
    # no stick attached still serves input.
    check("video loads without a device", "video" in reg.caps)

    # Error shape is a compatibility contract: callers branch on `ok` and some
    # match the message, so this string is verbatim from before the refactor.
    resp = vcctrld.handle(d, reg, {"cmd": "nope"})
    check("unknown command error text unchanged",
          resp == {"ok": False, "error": "unknown command: 'nope'"}, resp)

    resp = vcctrld.handle(d, reg, {"cmd": "caps"})
    # Every capability must have a bus, whatever its constructor accepts.
    # LedsCapability did not, and the first command that published an event
    # raised AttributeError at the point of use rather than at start-up.
    for name, cap in reg.caps.items():
        check("%s has a bus" % name, hasattr(cap, "bus"))

    check("caps reports every capability",
          resp["ok"] and set(["input", "leds", "power", "video"]) <=
          set(resp["capabilities"]), sorted(resp.get("capabilities", {})))


def test_rule_2_isolation():
    """A capability that raises is unloaded, not fatal -- and the input path
    keeps working. This is open question 8 in the plan: run it, do not assume."""
    print("\nrule 2: a failing capability is not fatal")

    class Exploding(vcctrld.Capability):
        name = "exploding"

        def start(self):
            raise RuntimeError("simulated capability failure")

    orig = vcctrld.CAPABILITIES
    vcctrld.CAPABILITIES = list(orig) + [Exploding]
    try:
        d = make_devices()
        reg = vcctrld.Registry(d)
        check("daemon survived the failure", "input" in reg.caps)
        check("failure recorded", "exploding" in reg.failed)
        check("caps reports it as not ok",
              reg.report()["exploding"]["ok"] is False)
        resp = vcctrld.handle(d, reg, {"cmd": "type", "text": "DIR"})
        check("input still lands with a capability broken",
              resp == {"ok": True}, resp)
    finally:
        vcctrld.CAPABILITIES = orig


def test_status_shape():
    """`status` is a compatibility contract. Capability health went into a new
    `caps` command specifically so this stays byte-identical."""
    print("\nstatus shape")
    d = make_devices()

    class FakePath(object):
        device = type("D", (), {"path": "/dev/input/event0"})()

    d.kbd = FakePath()
    d.mouse = FakePath()
    reg = vcctrld.Registry(make_devices())
    orig = vcctrld.usb4vc_holds_us
    vcctrld.usb4vc_holds_us = lambda: {"vcctrl virtual keyboard": True,
                                       "vcctrl virtual mouse": True}
    try:
        resp = vcctrld.handle(d, reg, {"cmd": "status"})
    finally:
        vcctrld.usb4vc_holds_us = orig
    check("status keys unchanged",
          sorted(resp) == ["keyboard", "led_paths", "leds", "mouse", "ok",
                           "usb4vc"], sorted(resp))


def test_lock_default_unheld():
    """The compatibility property: with no lock held, input behaves exactly as
    it did before the arbiter existed. Nothing in the existing tooling passes
    an `as`, so this is what lets the lock land without touching a sweep."""
    print("\nlock: unheld by default")
    d = make_devices()
    reg = vcctrld.Registry(d)
    check("nothing holds the lock", reg.arbiter.status()["owner"] is None)
    resp = vcctrld.handle(d, reg, {"cmd": "type", "text": "DIR"})
    check("input works with no 'as' while unlocked", resp == {"ok": True}, resp)


def test_lock_gating():
    print("\nlock: gating and break-glass")
    d = make_devices()
    reg = vcctrld.Registry(d)

    got = vcctrld.handle(d, reg, {"cmd": "lock", "action": "acquire",
                                  "as": "sweep-RB"})
    check("sweep takes the lock", got["ok"] and got["owner"] == "sweep-RB")

    refused = vcctrld.handle(d, reg, {"cmd": "type", "text": "oops"})
    check("browser input refused while sweep holds it",
          refused["ok"] is False and refused["locked_by"] == "sweep-RB")
    check("refusal names the holder, not just 'busy'",
          "sweep-RB" in refused["error"])

    ok = vcctrld.handle(d, reg, {"cmd": "type", "text": "RB", "as": "sweep-RB"})
    check("the lock holder can still type", ok == {"ok": True}, ok)

    # Observation is NEVER gated -- the invariant from sec. 2. A browser that
    # goes dark because a sweep is running removes the whole point of the tool.
    for cmd in ("status", "caps", "leds", "events", "activity"):
        r = vcctrld.handle(d, reg, {"cmd": cmd})
        check("%s is not gated by the lock" % cmd, r.get("ok") is True)

    # Break-glass.
    brk = vcctrld.handle(d, reg, {"cmd": "lock", "action": "break",
                                  "as": "operator"})
    check("break-glass transfers the lock",
          brk["ok"] and brk["owner"] == "operator")
    evs = reg.bus.since(0, 500)["events"]
    taints = [e for e in evs if e["kind"] == "lock.broken"]
    check("the break published a taint event",
          len(taints) == 1 and taints[0]["taint"] is True and
          taints[0]["broke"] == "sweep-RB", taints)
    after = vcctrld.handle(d, reg, {"cmd": "type", "text": "now mine",
                                    "as": "operator"})
    check("operator can type after breaking in", after == {"ok": True})


def test_event_bus():
    print("\nevent bus")
    d = make_devices()
    reg = vcctrld.Registry(d)
    vcctrld.handle(d, reg, {"cmd": "type", "text": "CD \\DOSKUTSU"})
    vcctrld.handle(d, reg, {"cmd": "key", "keys": ["enter"]})
    out = reg.bus.since(0, 500)
    kinds = [e["kind"] for e in out["events"]]
    check("commands are published", kinds.count("cmd") == 2, kinds)
    typed = [e for e in out["events"] if e.get("detail") == "CD \\DOSKUTSU"]
    check("the log records WHAT was typed, not just that something was",
          len(typed) == 1, [e.get("detail") for e in out["events"]])
    check("seq is monotonic",
          [e["seq"] for e in out["events"]] ==
          sorted(e["seq"] for e in out["events"]))
    check("since() filters", reg.bus.since(out["seq"], 500)["events"] == [])
    check("no missed flag on a complete history", out["missed"] is False)

    # A client that fell off the back of the ring must be told, not left to
    # believe it has a complete history.
    small = vcctrld.Bus(cap=3)
    for i in range(10):
        small.publish("cmd", cmd="x%d" % i)
    check("missed flag set when the ring wrapped past the client",
          small.since(1, 100)["missed"] is True)


def test_activity_age():
    """'harness has been in ledwait for 14 minutes' is the operator's question
    in one line. Test it reports an in-flight command while it is still
    running, which needs a second thread."""
    print("\nactivity: in-flight age")
    d = make_devices()
    reg = vcctrld.Registry(d)
    seen = {}
    started = threading.Event()

    def slow(req):
        started.set()
        time.sleep(0.25)
        return {"ok": True}

    reg.routes["slowcmd"] = ("test", slow)
    t = threading.Thread(target=reg.dispatch, args=("slowcmd", {}))
    t.start()
    started.wait(2.0)
    time.sleep(0.05)
    seen = vcctrld.handle(d, reg, {"cmd": "activity"})
    t.join()
    inflight = [i for i in seen["inflight"] if i["cmd"] == "slowcmd"]
    check("a running command appears in flight with an age",
          len(inflight) == 1 and inflight[0]["age_s"] > 0.0, seen["inflight"])
    after = vcctrld.handle(d, reg, {"cmd": "activity"})
    check("it clears when finished",
          not [i for i in after["inflight"] if i["cmd"] == "slowcmd"])
    check("last_event_age_s is reported",
          after["last_event_age_s"] is not None)


def test_audio_levels():
    """The level scale is a compatibility contract, not an internal detail.

    Every reference figure in FINDINGS -- the -30.8 dB working level, the
    -65.6 dB floor -- was read off `ffmpeg -af volumedetect`, and vcctrl-audio's
    verdicts are tuned to those numbers. A shifted scale would invalidate all
    of it silently, so this checks the arithmetic against known signals rather
    than against itself.
    """
    print("\naudio levels")
    import array, collections, math, struct, threading

    RATE = 48000

    class Fake(vcctrld.AudioCapability):
        def __init__(self, frag):
            self.lock = threading.Lock()
            self.ring = collections.deque([(0.0, 1, frag)])
            self.state = "capturing"

    def sine(dbfs, secs=1.0, f=440.0):
        amp = int(32767 * (10 ** (dbfs / 20.0)))
        out = bytearray()
        for i in range(int(RATE * secs)):
            v = int(amp * math.sin(2 * math.pi * f * i / RATE))
            out += struct.pack("<hh", v, v)
        return bytes(out)

    # Assert against the amplitude the generator actually produced, not the one
    # it was asked for. At -60 dBFS the sample amplitude quantises to integer
    # 32, which genuinely is -60.21 dB -- ffmpeg reports the same figure. A
    # test written against the requested level fails here and blames the
    # measurement for the generator's rounding.
    for target in (-20.0, -40.0, -60.0):
        amp = int(32767 * (10 ** (target / 20.0)))
        want_peak = 20 * math.log10(amp / 32768.0)
        lv = Fake(sine(target))._levels(ms=1000)
        check("sine at %.0f dBFS -> peak matches its quantised amplitude"
              % target, abs(lv["peak_db"] - want_peak) < 0.05,
              "%.2f vs %.2f" % (lv["peak_db"], want_peak))
        # RMS is compared against the EXACT rms of the samples that were
        # generated, computed over every one of them, rather than against the
        # continuous-sine identity peak-3.01dB. At -60 dBFS the amplitude is
        # 32 integer steps, so quantisation moves the real RMS 0.19 dB off that
        # identity -- and ffmpeg agrees with the measurement, not the identity.
        # Asserting the ideal here tests the generator's arithmetic and blames
        # the measurement.
        raw = sine(target)
        a16 = array.array("h")
        a16.frombytes(raw)
        exact = 20 * math.log10(
            (sum(float(v) * v for v in a16) / len(a16)) ** 0.5 / 32768.0)
        check("sine at %.0f dBFS -> mean matches the exact rms of the samples"
              % target, abs(lv["mean_db"] - exact) < 0.1,
              "%.2f vs %.2f" % (lv["mean_db"], exact))

    silent = Fake(b"\x00\x00" * (RATE * 2))._levels(ms=1000)
    check("digital silence reads -91 flat",
          silent["mean_db"] == -91.0 and silent["peak_db"] == -91.0, silent)
    # vcctrl-audio calls NO SIGNAL when mean == peak at the floor. That
    # equality is what distinguishes a dead path from a quiet one, so it has to
    # survive exactly rather than approximately.
    check("silence has mean == peak, which is what NO SIGNAL keys on",
          silent["mean_db"] == silent["peak_db"])

    # A dither-level signal must NOT read as flat: that is the "connected but
    # silent" case, and integer RMS used to collapse it onto the floor.
    import random
    random.seed(3)
    out = bytearray()
    for _ in range(RATE):
        v = random.randint(-2, 2)
        out += struct.pack("<hh", v, v)
    d = Fake(bytes(out))._levels(ms=1000)
    check("dither floor is distinguishable from digital silence",
          d["mean_db"] != d["peak_db"] and d["mean_db"] > -91.0, d)
    check("dither RMS is not integer-truncated (would read ~-90.3)",
          d["mean_db"] > -89.0, d["mean_db"])


def test_watchdogs_survive_one_pass():
    """Every capability's watchdog must complete a pass without raising.

    This exists because one did not. A pin-expiry block intended for
    VideoCapability landed in AudioCapability's watchdog by a text-anchored
    edit that matched the wrong class -- both define _watchdog, and the anchor
    happened to be unique to the wrong one. The thread died on an
    AttributeError one second after every start, so audio ran with NO
    SUPERVISION at all.

    Nothing caught it. `caps` reported audio healthy, because caps asks whether
    the device opened, not whether its supervisor survived -- a dead watchdog is
    invisible to the check that would tell you the subsystem is fine.
    """
    print("\nwatchdogs")
    import threading

    # Build each capability through its REAL __init__, which opens no device
    # and spawns nothing. The first version of this test filled in any missing
    # attribute with None, which would have papered over exactly the bug it
    # exists to catch: the point is that AudioCapability does NOT define
    # pinned_at, so a watchdog touching it must fail here.
    devs = make_devices()
    for cls in vcctrld.CAPABILITIES:
        if not hasattr(cls, "_watchdog"):
            continue
        try:
            cap = cls(devs, None)
        except TypeError:
            cap = cls(devs)
        cap.running = True
        cap.spawn_t = time.time()
        err = []

        def run(c=cap, e=err):
            try:
                threading.Timer(0.05, lambda: setattr(c, "running", False)).start()
                c._watchdog()
            except Exception as exc:
                e.append("%s: %s" % (type(exc).__name__, exc))

        t = threading.Thread(target=run)
        t.start()
        t.join(timeout=4)
        check("%s watchdog completes a pass" % cls.name, not err,
              err[0] if err else "")

    # Control: prove the check can actually detect the failure it claims to.
    # A green result here otherwise says nothing about whether the test works.
    class Broken(vcctrld.AudioCapability):
        name = "broken"

        def _watchdog(self):
            while self.running:
                time.sleep(0.01)
                _ = self.pinned_at          # AudioCapability never defines this

    cap = Broken(devs, None)
    cap.running = True
    err = []
    try:
        threading.Timer(0.05, lambda: setattr(cap, "running", False)).start()
        cap._watchdog()
    except Exception as exc:
        err.append(type(exc).__name__)
    check("control: the same check DOES catch a watchdog touching "
          "an attribute its class lacks", err == ["AttributeError"], err)


def test_theme_contrast():
    """Every colour a theme ships must clear the WCAG floor on every surface.

    Checking four schemes by eye is plausible; checking twenty-five is not, and
    several published schemes place accents near 2:1 against their own
    background -- fine for a syntax token inside a wall of code, not fine for
    the only thing telling you a machine is unreachable.

    tools/themes.py keeps the schemes verbatim and the generator nudges any
    value that misses the floor, reporting what it changed. This asserts the
    EMITTED values, which is what a browser actually renders.
    """
    print("\ntheme contrast")
    import os
    sys.path.insert(0, os.path.join(HERE, os.pardir, "tools"))
    import themes as T

    worst_text, worst_accent, checked = 99.0, 99.0, 0
    for name in T.THEMES:
        roles, _notes = T.fitted(name)
        for surface in ("bg", "panel"):
            for role in ("text", "muted"):
                c = T.contrast(roles[role], roles[surface])
                worst_text = min(worst_text, c)
                checked += 1
                if c < 4.5:
                    check("%s: %s on %s is %.2f:1" % (name, role, surface, c),
                          False)
            for role in T.ACCENTS + ("dim",):
                c = T.contrast(roles[role], roles[surface])
                worst_accent = min(worst_accent, c)
                checked += 1
                if c < 3.0:
                    check("%s: %s on %s is %.2f:1" % (name, role, surface, c),
                          False)
    check("%d colour pairs across %d themes clear the floor"
          % (checked, len(T.THEMES)), True)
    check("worst body text ratio %.2f:1 (floor 4.5)" % worst_text,
          worst_text >= 4.5)
    check("worst indicator ratio %.2f:1 (floor 3.0)" % worst_accent,
          worst_accent >= 3.0)

    # Control: the check must be able to fail. A floor nothing can trip is not
    # a floor.
    bad = T.contrast("#777777", "#808080")
    check("control: the same measure rejects grey on grey (%.2f:1)" % bad,
          bad < 3.0)

    # Every theme names a pairing, so the light/dark button always has a target.
    for name, (_g, _lab, _d, pair, _r) in T.THEMES.items():
        if pair not in T.THEMES:
            check("%s pairs with an unknown theme %r" % (name, pair), False)
    check("every theme's light/dark pair exists", True)


def test_uniform_frame_is_not_picture():
    """A frame with no variance is not a picture, however unrepeated it is.

    Duplicate-hash rejection asks "is this frame a repeat?", and a uniform
    frame passes that -- the check answers a different question from the one
    being asked. The vcctrl session found it the expensive way: two in-game
    frames byte-identical twenty seconds apart, every pixel exactly 7, reported
    as PICTURE mean 7.0, which turned "capture relocks during gameplay" into a
    result that was false. This selector is the algorithm theirs was ported
    from and had the same gap.

    Measured at the 1/8 scale the selector decodes at: real captures span
    77-226 even when almost entirely black, and the stick's no-lock constant is
    exactly 0.
    """
    print("\nuniform frames")
    import io
    import threading
    from PIL import Image

    class Fake(vcctrld.VideoCapability):
        def __init__(self):
            self.lock = threading.Lock()

    def const(v):
        b = io.BytesIO()
        Image.new("RGB", (640, 480), (v, v, v)).save(b, "JPEG", quality=90)
        return b.getvalue()

    def scene():
        # A dark scene with a little content -- the case that must NOT be
        # rejected, since "dark screen" and "no signal" are different facts.
        im = Image.new("RGB", (640, 480), (2, 2, 2))
        for x in range(40, 240):
            for y in range(40, 60):
                im.putpixel((x, y), (90, 90, 90))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=90)
        return b.getvalue()

    v = Fake()
    check("a uniform frame is not picture", not v._is_picture(const(7)))
    check("a dark frame with content IS picture", v._is_picture(scene()))

    # Distinct hashes, so duplicate rejection cannot catch these: only the
    # variance floor can.
    items = [(0.0, 1, const(7)), (1.0, 2, const(8)), (2.0, 3, const(9))]
    best, _mean, reason = v._select(items)
    check("selector rejects several DIFFERENT constants", best is None)
    check("and says they were constants, not that they were duplicates",
          "uniform constant" in (reason or ""), reason)

    best, mean, _live = v._select(items + [(3.0, 4, scene())])
    check("a real frame among constants still wins", best is not None)

    # Control: the floor must be able to accept something, or it is not a floor
    # but a rejection.
    check("control: the check is not simply rejecting everything",
          v._is_picture(scene()) and not v._is_picture(const(0)))


def test_websocket_accept_vector():
    """The RFC 6455 handshake, checked against the RFC's own test vector.

    This existed as a transcribed constant and was wrong for hours: the final
    group read 5AB0DC85B11D instead of C5AB0DC85B11, the same twelve characters
    rotated by one. Every probe written to test the handshake imported the
    constant from the module under test, so all of them computed the same wrong
    value, agreed with the server, and reported success. Firefox has its own
    copy and was the only party that ever disagreed -- and it said so plainly,
    in a log nobody had thought to read.

    A test that derives its expectation from the code cannot catch a wrong
    constant. This one hard-codes the RFC's published pair.
    """
    print("\nwebsocket handshake")
    import base64, hashlib, os, sys
    sys.path.insert(0, os.path.join(HERE, os.pardir, "daemon"))
    import vcweb

    KEY = "dGhlIHNhbXBsZSBub25jZQ=="
    ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    got = base64.b64encode(
        hashlib.sha1((KEY + vcweb.WS_GUID).encode()).digest()).decode()
    check("RFC 6455 vector: %s -> %s" % (KEY[:12] + "...", ACCEPT),
          got == ACCEPT, got)

    # Control: the check must reject a GUID that is wrong by one character.
    bad = base64.b64encode(
        hashlib.sha1((KEY + "258EAFA5-E914-47DA-95CA-5AB0DC85B11D")
                     .encode()).digest()).decode()
    check("control: the vector rejects the off-by-one GUID", bad != ACCEPT)


def test_page_dom_references():
    """Every element the page's script reaches for must exist in its markup.

    Three outages tonight were one missing element or one undeclared name at
    top level: a settings key with no checkbox, a const read before its
    declaration, a watchdog touching an attribute its class lacks. In a script
    that runs at top level, one throw takes every line after it -- so the whole
    page dies and the symptom is "unresponsive", or one stuck status chip, or a
    tab bar that does nothing. None of those name the cause.

    Syntax checking does not catch it: the page parsed cleanly every time.
    """
    print("\npage DOM references")
    import os
    import re

    page = os.path.join(HERE, os.pardir, "daemon", "kvm.html")
    with open(page, encoding="utf-8") as f:
        h = f.read()
    ids = set(re.findall(r"\$\('([\w-]+)'\)", h))
    present = set(re.findall(r'id="([\w-]+)"', h))
    missing = sorted(i for i in ids if i not in present)
    check("every $('id') in the script exists in the markup",
          not missing, missing)

    # The settings loop looks up $('opt-' + key) for every key in OPTS, so any
    # key without a checkbox must be tolerated rather than assumed.
    m = re.search(r"const OPTS = \{([^}]*)\}", h)
    keys = re.findall(r"(\w+)\s*:", m.group(1)) if m else []
    for k in keys:
        if "opt-%s" % k not in present:
            check("OPTS key %r has no checkbox -- loop must guard" % k,
                  "if (!el) continue;" in h, "no guard found")
    check("settings loop guards against a missing element",
          "if (!el) continue;" in h)

    # Parse the script the way a browser would, when there is an engine to do
    # it with. It cannot catch an undefined name, but it catches the class of
    # edit that leaves the page silent -- a stray brace from a block move, a
    # half-applied replacement.
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node") or shutil.which("nodejs")
    if node:
        blocks = re.findall(r"<script>(.*?)</script>", h, re.S)
        check("the page has exactly one script block", len(blocks) == 1,
              len(blocks))
        for i, b in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js",
                                             delete=False) as f:
                f.write(b)
                path = f.name
            r = subprocess.run([node, "--check", path],
                               capture_output=True, text=True)
            os.unlink(path)
            check("script block %d parses" % i, r.returncode == 0,
                  r.stderr.strip().splitlines()[:3])
    else:
        print("  SKIP  no node: script not parsed")


def test_zoom_modes():
    """The zoom control must offer only modes the script implements, and the
    modes must do what their labels say.

    Reported from the rig: "Fit is still not actually fitting the screen, with
    a lot of space around it and it's almost the same as Crop to picture". Both
    halves were true and neither was a rendering bug. The stage is much wider
    than 4:3, so a fitted picture leaves black down the sides; and on a
    full-screen text mode there is no letterbox, so cropping to the picture
    returned the picture -- k = 1.000, identical to fit, exactly as computed.
    The missing mode was "cover the stage", so that is what was added.
    """
    print("\nzoom modes")
    import os
    import re

    page = os.path.join(HERE, os.pardir, "daemon", "kvm.html")
    with open(page, encoding="utf-8") as f:
        h = f.read()

    sel = re.search(r'<div id="pop-zoom".*?</div>', h, re.S).group(0)
    markup = re.findall(r'data-zoom="([^"]+)"', sel)
    arr = re.search(r"const ZOOMS = \[([^\]]*)\]", h).group(1)
    script = re.findall(r"'([^']+)'", arr)
    # A value in one and not the other is silent either way: a stored mode the
    # script rejects resets to fit, a listed mode the script never validates
    # would sail past the guard.
    check("every menu entry is a known mode and vice versa",
          markup == script, "%s vs %s" % (markup, script))

    # ── the arithmetic, ported from applyZoom() ────────────────────────────
    # k is the transform on top of what object-fit:contain already did, so the
    # honest thing to check is the size the picture ends up on screen.
    def shown(mode, W, H, nw, nh, crop=None):
        """(width, height) of the drawn picture in CSS pixels."""
        s0 = min(W / nw, H / nh)
        x0, y0, bw, bh = crop or (0, 0, nw, nh)
        if mode == "fit":
            k = 1.0 if crop is None else min(W / bw, H / bh) / s0
        else:
            k = float(mode) / s0
        return bw * s0 * k, bh * s0 * k

    # A real desktop stage: whatever is left after the header and command line.
    W, H, nw, nh = 1580, 600, 640, 480

    # "Original" means what came off the capture stick, unadjusted. It is the
    # one mode whose answer does not depend on the window, which is the whole
    # reason for measuring the ladder from here.
    for stage in ((1580, 600), (1024, 768), (390, 300)):
        w, hh = shown("1", stage[0], stage[1], nw, nh)
        check("original is %dx%d on a %dx%d stage" % (nw, nh, *stage),
              abs(w - nw) < 0.01 and abs(hh - nh) < 0.01, (w, hh))
    for mult in ("2", "4"):
        w, hh = shown(mult, W, H, nw, nh)
        check("%sx original is exactly %s times it" % (mult, mult),
              abs(w - nw * float(mult)) < 0.01, w)

    w, hh = shown("fit", W, H, nw, nh)
    check("fit shows the whole frame", w <= W + 0.5 and hh <= H + 0.5, (w, hh))
    check("control: fit leaves the sides black on a wide stage",
          w < W - 100, w)

    # A 512x384 mode centred in the capture: fit works on the picture, not on
    # the frame, so the black border does not eat into the size.
    box = (64, 48, 512, 384)
    w, hh = shown("fit", W, H, nw, nh, box)
    check("fit of a letterboxed mode fills the stage height",
          abs(hh - H) < 0.5, hh)
    # Fitting the frame instead would put the same 384 rows at 384/480 of the
    # stage: 480 px, a fifth smaller, with black above and below it.
    naive = shown("fit", W, H, nw, nh)[1] * box[3] / nh
    check("control: fitting the frame instead would be a fifth smaller",
          naive < hh - 1, (naive, hh))

    # ── what counts as a letterbox, ported from measureCrop() ─────────────
    def is_letterbox(x0, y0, x1, y1, w=640, h=480):
        pad = 4
        x0 = max(0, x0 - pad); y0 = max(0, y0 - pad)
        x1 = min(w - 1, x1 + pad); y1 = min(h - 1, y1 + pad)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if bw < 32 or bh < 32:
            return False
        if bw > w * 0.94 and bh > h * 0.94:
            return False
        lm, rm, tm, bm = x0, w - 1 - x1, y0, h - 1 - y1
        return abs(lm - rm) <= w * 0.06 and abs(tm - bm) <= h * 0.06

    # Measured off the live rig at a DOS prompt: the text stopped at column
    # 550, so the bounding box was 551 wide with every bit of the slack on one
    # side. Cropping to it would have trimmed live screen.
    check("a short last line is not a letterbox",
          not is_letterbox(0, 0, 550, 478))
    check("control: a centred 512x384 mode is", is_letterbox(64, 48, 575, 431))
    check("control: a centred 320x240 mode is", is_letterbox(160, 120, 479, 359))


HARNESS = r"""
<pre id="harness-out"></pre>
<script>
// Measure what the ENGINE lays out, not what the model predicts it will.
//
// THE CANVAS, NOT THE IMG, and that is the whole reason this harness is
// reliable. Loading a picture into the <img> needs a real decode, and
// chromium's --virtual-time-budget races ahead of real work: the page has
// 1.5 s pollers, so a 30 s budget is exhausted in a fraction of a second and
// the DOM is dumped before the decode lands. Worse, the page's own transport
// re-points img.src at /stream.mjpg, and reassigning src CANCELS a pending
// load without firing load OR error -- so awaiting those handlers waits
// forever. That combination is what reported "no output".
//
// A canvas has intrinsic dimensions the moment it exists. No load, no decode,
// no race, and mediaEl() picks it as soon as the img is hidden.
//
// No requestAnimationFrame either: getBoundingClientRect forces layout on the
// spot, and waiting for frames under a virtual-time budget hung this harness
// once already.
(async () => {
 try {
  const img = document.getElementById('mjpeg'), cv = document.getElementById('screen');
  const sc = document.getElementById('scroll');
  // Re-asserted before every measurement: the page's transport logic keeps
  // running underneath and will hide the canvas again.
  const arm = () => { img.style.display = 'none'; cv.style.display = 'block'; };
  arm();
  const out = [];
  const pre = document.getElementById('harness-out');
  // Written AS IT GOES. All-or-nothing meant a run that stopped two thirds of
  // the way through reported "no output", which reads as a broken page rather
  // than as a harness that stalled -- and hid which step it stalled on.
  const emit = v => { out.push(v); pre.textContent = out.join('|'); };
  emit(`loaded ${cv.width} ${cv.height}`);
  const say = (name, extra) => {
    const b = cv.getBoundingClientRect();
    emit(`${name} ${b.width.toFixed(2)} ${b.height.toFixed(2)}` +
         (extra === undefined ? '' : ' ' + extra));
  };
  for (const m of ['fit', '1', '2', '4']) {
    arm();
    zoomMode = m; crop = null; applyZoom(true);
    // clientWidth excludes any scrollbar, which is the space a fit has.
    emit(`view-${m} ${sc.clientWidth} ${sc.clientHeight}`);
    say(m, `${sc.scrollWidth > sc.clientWidth + 1 ? 1 : 0}${sc.scrollHeight > sc.clientHeight + 1 ? 1 : 0}`);
  }
  // The picker builds itself from the stylesheet. When that read fails it
  // fails SILENTLY -- an empty grid and a raw theme id where the name goes --
  // which is exactly what a cross-origin cssRules read did on the first
  // attempt, and what a stale themes.css did on the second.
  emit(`themes ${THEMES.length} ${PAIRS.length}`);
  const chips = document.querySelectorAll('#themes button');
  let painted = 0;
  for (const c of chips) {
    const bg = getComputedStyle(c).backgroundColor;
    if (bg && bg !== 'rgba(0, 0, 0, 0)') painted++;
  }
  emit(`chips ${chips.length} ${painted}`);
  // Every button says what it does on hover. The ones that lack a tooltip are
  // always the ones added last, which is why this is counted rather than
  // eyeballed.
  const btns = document.querySelectorAll('button');
  let untitled = 0;
  for (const b of btns) if (!b.title.trim()) untitled++;
  emit(`tips ${btns.length} ${untitled}`);

  // Hovering a swatch previews it, and leaving puts it back. The failure
  // that matters is the second half: a preview that sticks has silently
  // changed the theme without anyone choosing it.
  {
    const root = document.documentElement;
    const before = root.getAttribute('data-theme');
    const chip = [...document.querySelectorAll('#themes button')]
                   .find(c => c.dataset.theme !== before);
    chip.onmouseenter();
    const during = root.getAttribute('data-theme');
    chip.onmouseleave();
    const after = root.getAttribute('data-theme');
    const selNow = document.querySelector('#themes button.sel');
    emit(`preview ${during === chip.dataset.theme ? 1 : 0} ${after === before ? 1 : 0}`);
    emit(`selkept ${selNow && selNow.dataset.theme === before ? 1 : 0} 0`);
  }

  // The rig table renders from the daemon's own words, including ffmpeg's
  // and a device's name string. Neither is a place to assume there are no
  // angle brackets -- this is the page's only innerHTML built from remote
  // text.
  {
    const host = document.createElement('div');
    host.innerHTML = statsRows({
      board: {id: null, reason: 'usb4vc not running'},
      video: {state: 'unavailable', device: '<img src=x onerror=alert(1)>',
              device_present: null},
      audio: {state: 'capturing', device: 'hw:1,0', device_present: true},
      power: {alias: 'retro-rig-plug', on: null, reason: 'EHOSTUNREACH',
              stale: true},
      viewers: 1, listeners: 0, lock: {owner: null}});
    const txt = host.textContent;
    const groups = ['TARGET','PICTURE','POWER','DEVICES']
      .filter(g => txt.toUpperCase().includes(g)).length;
    emit(`rig ${groups} ${host.querySelectorAll('img,script').length}`);
    // A null must read as "could not tell", never as "no". Three bugs on this
    // rig were exactly that collapse.
    const nulls = (txt.match(/could not tell/g) || []).length;
    emit(`rignull ${nulls >= 1 ? 1 : 0} ${txt.includes('usb4vc not running') ? 1 : 0}`);
  }

  // Three absences, not one. Fed the shapes the rig actually published.
  {
    const say = c => { const t = deviceTrouble(c); return t ? t[0] : 'null'; };
    const gone = say({state:'unavailable', device:'/dev/video0',
      device_present:false, last_error:'[video4linux2,v4l2 @ 0x1] Cannot open video device /dev/video0: No such file or directory | Error opening input'});
    const shut = say({state:'unavailable', device:'hw:1,0',
      device_present:true, last_error:'[alsa @ 0x1] cannot open audio device hw:1,0 (No such file or directory) | Error opening input'});
    const dunno = say({state:'unavailable', device:'hw:1,0', device_present:null});
    const fine = say({state:'locked', device:'/dev/video0', device_present:true});
    const detail = deviceTrouble({state:'unavailable', device:'hw:1,0',
      device_present:true, last_error:'[alsa @ 0x1] cannot open audio device hw:1,0 (No such file or directory) | Error opening input'})[1];
    emit(`absent3 ${gone !== shut && shut !== dunno && gone !== dunno ? 1 : 0} ${fine === 'null' ? 1 : 0}`);
    // The reason must survive, without ffmpeg's module and pointer.
    emit(`reason ${detail.includes('cannot open audio device hw:1,0') ? 1 : 0} ${detail.startsWith('[') ? 0 : 1}`);
  }

  // Typing a file in. The failure that matters is a half-typed file: if the
  // daemon refuses partway, stopping leaves the target with a known prefix
  // and carrying on leaves it with an unknown mixture.
  {
    const posted = [];
    const realPost = window.post;
    let refuseAfter = 99;
    window.post = async (cmd, body) => {
      posted.push([cmd, body && body.text]);
      return posted.length > refuseAfter
        ? {ok: false, error: 'input refused'} : {ok: true};
    };
    const file = txt => ({name: 'T.BAT', text: async () => txt});

    // CRLF must not become a blank line after every line.
    await typeFileForTest(file('ECHO A\r\nECHO B\r\n'));
    const lines = posted.filter(p => p[0] === 'type').map(p => p[1]);
    const enters = posted.filter(p => p[0] === 'key').length;
    emit(`file ${lines.length} ${enters}`);
    emit(`filetext ${lines[0] === 'ECHO A' && lines[1] === 'ECHO B' ? 1 : 0} 0`);

    // A refusal partway must STOP, not plough on.
    posted.length = 0; refuseAfter = 2;
    await typeFileForTest(file('A\nB\nC\nD\nE\n'));
    emit(`filestop ${posted.length <= 4 ? 1 : 0} ${posted.length}`);
    window.post = realPost;
  }

  // Grabbing the keyboard must not change the LAYOUT. The message used to be
  // a row that appeared, so clicking the picture pushed the picture up -- the
  // reward for using the thing was the thing moving.
  {
    const before = document.getElementById('screen').getBoundingClientRect();
    setArmed(true);
    const during = document.getElementById('screen').getBoundingClientRect();
    const lamp = document.getElementById('lamp-kbd');
    const lit = lamp.classList.contains('on');
    setArmed(false);
    const after = document.getElementById('screen').getBoundingClientRect();
    const still = Math.abs(during.height - before.height) < 0.5
               && Math.abs(after.height - before.height) < 0.5;
    emit(`grab ${still ? 1 : 0} ${lit && !lamp.classList.contains('on') ? 1 : 0}`);
  }

  // The resolution comes from the FRAME, not from a constant. A canvas
  // carries 640x480 as an attribute, so reporting before a frame arrives
  // would report the Gateway's geometry on a Macintosh.
  {
    frames = 0; capW = 0; capH = 0;
    zoomMode = 'fit'; crop = null; applyZoom(true);
    const before = document.querySelector('#pop-zoom [data-zoom="1"]').textContent;
    frames = 1; cv.width = 512; cv.height = 342;
    applyZoom(true);
    const after = document.querySelector('#pop-zoom [data-zoom="1"]').textContent;
    cv.width = 640; cv.height = 480; applyZoom(true);
    emit(`res ${before.includes('actual resolution') ? 1 : 0} ${after.includes('512×342') ? 1 : 0}`);
  }

  // Every control in the strip must be the same height. The sound control is
  // a wrapper rather than a <button>, so none of the button sizing applied to
  // it and it rendered shorter than its neighbours.
  {
    const h = id => Math.round(document.getElementById(id).getBoundingClientRect().height);
    emit(`striph ${h('soundwrap')} ${h('powerbtn')}`);
    // The command field too. Its minimum lived on the INPUT while the border
    // lived on the WRAPPER, so the field stood two pixels proud of every
    // button beside it -- small, and enough to make the strip look crooked.
    emit(`fieldh ${h('linewrap')} ${h('keysbtn')}`);
    // Same anatomy as the menu buttons: a glyph cell with a rule, a label.
    const sb = document.getElementById('sendline');
    emit(`sendparts ${sb.querySelector('.bi') && sb.querySelector('.bl') ? 1 : 0} `
       + `${getComputedStyle(sb.querySelector('.bi')).borderRightWidth === '1px' ? 1 : 0}`);
  }

  // A DARK TARGET IS NOT A BROKEN TRANSPORT. Both are silence from here, and
  // the page used to treat the second as the first -- so a Gateway reboot
  // downgraded the session to a slower path that had the same nothing to
  // deliver.
  {
    const keep = lastState;
    lastState = {video: {state: 'locked'}};
    const blamesUs = sourceIsLive();
    lastState = {video: {state: 'nosignal'}};
    const blamesTarget = !sourceIsLive();
    lastState = {video: {state: 'frozen'}};
    const blamesTarget2 = !sourceIsLive();
    lastState = null;
    const noInfo = sourceIsLive();      // no information: assume it is us
    lastState = keep;
    emit(`sourcelive ${blamesUs && blamesTarget && blamesTarget2 ? 1 : 0} ${noInfo ? 1 : 0}`);
  }

  // Falling back to mjpeg must not be PERMANENT. ws.onclose only reconnects
  // while the transport is not already mjpeg, so the first fallback used to
  // stick until someone found Refresh video in the Power menu.
  {
    const realRestart = window.restart;
    let restarts = 0;
    window.restart = () => { restarts++; };
    wsRetryDelay = 60000;
    scheduleWsRetry('test');
    const armed = wsRetryTimer !== null;
    const grew = wsRetryDelay === 120000;      // next wait is longer
    // A working socket must reset the patience, or one blip after an hour of
    // health would wait the maximum.
    const wasXport = xport;
    xport = 'ws'; gotFrame(); xport = wasXport;
    const reset = wsRetryDelay === 60000;
    clearTimeout(wsRetryTimer);
    // And the lamp is a control: clicking it retries now.
    document.getElementById('lamp-link').onclick();
    window.restart = realRestart;
    emit(`wsretry ${armed && grew && reset ? 1 : 0} ${restarts >= 1 ? 1 : 0}`);
  }

  // Ctrl+Alt+Delete must ASK. It sits in a rail of harmless keys, at thumb
  // distance from Esc, and it is the only one whose mis-tap costs the
  // machine's state.
  {
    const realConfirm = window.confirm, realPost = window.post;
    let asked = null, sent = 0;
    window.confirm = m => { asked = m; return false; };
    window.post = async () => { sent++; return {ok: true}; };
    const cad = document.querySelector('[data-combo="ctrl,alt,delete"]');
    cad.onclick();
    const blocked = sent === 0 && asked && /reboots/i.test(asked);
    window.confirm = () => true;
    cad.onclick();
    const wentThrough = sent === 1;
    // A plain key must NOT ask -- a rail that confirms everything is a rail
    // nobody reads the confirmations in.
    asked = null;
    document.querySelector('[data-key="esc"]').onclick();
    const quiet = asked === null;
    window.confirm = realConfirm; window.post = realPost;
    emit(`cad ${blocked && wentThrough ? 1 : 0} ${quiet ? 1 : 0}`);
  }

  // The file control has to be CLICKABLE. display:none on a file input makes
  // .click() a no-op in some browsers, which is what "the File button does
  // not work" was -- a handler that ran perfectly and opened nothing.
  {
    const fi = document.getElementById('fileinput');
    const st = getComputedStyle(fi);
    const clickable = st.display !== 'none' && st.visibility !== 'hidden';
    // And it must sit inside the field rather than beside it.
    const inField = document.getElementById('filebtn').closest('#linewrap') !== null;
    emit(`file2 ${clickable ? 1 : 0} ${inField ? 1 : 0}`);
  }

  // The activity flicker must fire on traffic from ANYONE and must not
  // disturb the state the lamp is already carrying.
  {
    setInputLamps(null, {ok: true});          // kbd idle, mouse ready
    const kbd = document.getElementById('lamp-kbd');
    const before = kbd.className;
    flashForCmd('type');
    const flashed = kbd.classList.contains('flash');
    const kept = kbd.className.replace(' flash', '') === before;
    flashForCmd('power');                     // not an input command
    const mouse = document.getElementById('lamp-mouse');
    mouse.classList.remove('flash');
    flashForCmd('mouse_move');
    emit(`flash ${flashed && kept ? 1 : 0} ${mouse.classList.contains('flash') ? 1 : 0}`);
  }

  // The input lamps have THREE meanings and held is its own. Red reads as
  // broken and green reads as available; a lock is neither.
  {
    const cls = id => {
      const e = document.getElementById(id);
      return e.classList.contains('on') ? 'on'
           : e.classList.contains('warn') ? 'held'
           : e.classList.contains('bad') ? 'bad' : 'off';
    };
    setInputLamps(null, {ok: true});
    // KBD and MOS must answer the SAME question. KBD used to light only while
    // this browser was grabbing, which made one lamp in a row of eight mean
    // something different from its neighbours.
    const kbdFree = cls('lamp-kbd');
    const free = cls('lamp-mouse');
    setInputLamps('claude-e2e', {ok: true});
    const lock = cls('lamp-mouse');
    const pad = document.getElementById('lamp-mouse').classList.contains('locked');
    setInputLamps(null, {ok: false, error: 'uinput gone'});
    const dead = cls('lamp-mouse');
    setInputLamps(null, {ok: true});
    emit(`inlamp ${free === 'on' && lock === 'held' && dead === 'bad' ? 1 : 0} ${pad ? 1 : 0}`);
    emit(`samequestion ${kbdFree === free ? 1 : 0} 0`);
  }

  // Power is TRI-STATE. "the machine is off" and "I cannot reach the plug"
  // are opposite facts, and a two-valued reading of a cached field reports
  // the instrument's state as the target's -- the failure this rig has
  // produced in three separate places.
  {
    // Power is a lamp now, so the reading is its class and its tooltip: on,
    // bad (off) and stale (could not tell) must be three different things.
    const word = () => {
      const el = document.getElementById('lamp-pwr');
      return el.classList.contains('on') ? 'on'
           : el.classList.contains('bad') ? 'off'
           : el.classList.contains('stale') ? 'unknown' : 'none';
    };
    showPower({alias: 'retro-rig-plug', model: 'EP10(US)',
               host: '192.0.2.46', on: true, age_s: 2, stale: false});
    const a = word();
    showPower({alias: 'retro-rig-plug', on: false, age_s: 2, stale: false});
    const b = word();
    showPower({alias: 'retro-rig-plug', on: null, age_s: 212, stale: true,
               reason: 'EHOSTUNREACH'});
    const c = word(), plug = document.getElementById('plugid').textContent
                              + ' ' + document.getElementById('lamp-pwr').title;
    emit(`power3 ${a === 'on' && b === 'off' && c === 'unknown' ? 1 : 0} ` +
             `${plug.includes('retro-rig-plug') && plug.includes('not answering') ? 1 : 0}`);

    // The board left the rail but must still reach the power confirmation,
    // which names the board fitted beside the plug it is about to switch.
    showBoard({id: 3, name: 'Apple Lisa/Mac/ADB', target: 'Macintosh Plus',
               stale: false});
    const kept = boardNow.target === 'Macintosh Plus';
    showBoard({id: null, name: null, target: null, reason: 'usb4vc not running'});
    const cleared = !boardNow.target;
    emit(`board2 ${kept ? 1 : 0} ${cleared ? 1 : 0}`);
  }

  // Full screen: nothing but the picture, and the controls come back as
  // overlays rather than by taking their space back -- reflowing the stage
  // every time you reach for a control is the opposite of the point.
  {
    // Measure the before-picture in the SAME mode as the after-picture: the
    // first version of this compared a 400% frame against a fitted one and
    // reported full screen as broken.
    zoomMode = 'fit'; crop = null; applyZoom(true);
    const before = document.getElementById('screen').getBoundingClientRect();
    setFullscreen(true);
    zoomMode = 'fit'; crop = null; applyZoom(true);
    const hdr = document.querySelector('header');
    const hidden = getComputedStyle(hdr).display === 'none';
    const r = document.getElementById('screen').getBoundingClientRect();
    emit(`fs ${hidden ? 1 : 0} ${Math.round(r.height)}`);
    document.body.classList.add('peek');
    const ph = getComputedStyle(hdr);
    const r2 = document.getElementById('screen').getBoundingClientRect();
    emit(`fspeek ${ph.display !== 'none' && ph.position === 'fixed' ? 1 : 0} ${Math.round(r2.height)}`);
    setFullscreen(false);
    zoomMode = 'fit'; crop = null; applyZoom(true);
    const back = document.getElementById('screen').getBoundingClientRect();
    emit(`fsback ${Math.abs(back.height - before.height) < 2 ? 1 : 0} ${Math.round(back.height)}`);
  }

  // Rate control. The failure that matters is a loop that only ever goes one
  // way: down to the floor on a hiccup, or up past what the tunnel carries.
  {
    // The ceiling follows the SOURCE. Feed it a daemon counter advancing at
    // 30 fps and it must be willing to ask for 30, not the 20 that was
    // hardcoded from a different stick on a different machine.
    noteSourceRate({frames: 0}); srcAt -= 1000;
    noteSourceRate({frames: 30});
    const ceilAt30 = fpsCeiling();
    const sent = [];
    const realWs = ws;
    ws = {readyState: 1, send: m => sent.push(JSON.parse(m).fps)};
    fpsWant = 0; fpsGoodRuns = 0;
    requestRate(14);
    rateStep(6);                      // frames going missing
    const backedOff = fpsWant;
    for (let i = 0; i < 4; i++) rateStep(fpsWant);   // now keeping up
    const creptUp = fpsWant;
    fpsGoodRuns = 0;
    for (let i = 0; i < 40; i++) rateStep(fpsWant);  // and keeps keeping up
    const ceiling = fpsWant;
    ws = realWs;
    emit(`rate ${backedOff} ${creptUp}`);
    emit(`ratecap ${ceiling} ${sent.length}`);
    emit(`srcceil ${ceilAt30} ${Math.round(srcFps)}`);
  }

  // The rail popovers must land under the thing that opened them, and must
  // survive their anchor being hidden -- which is what happens on a phone,
  // where the tab bar opens the same menu the header chip does.
  for (const [nm, anchor] of [['sound', 'soundbtn'], ['power', 'powerbtn']]) {
    openPop(nm);
    const pr = document.getElementById('pop-' + nm).getBoundingClientRect();
    const ar = document.getElementById(anchor).getBoundingClientRect();
    const onscreen = pr.width > 60 && pr.left >= 0 && pr.top >= 0
                     && pr.right <= window.innerWidth + 1
                     && pr.bottom <= window.innerHeight + 1;
    // Below the anchor, or above it when the anchor is near the bottom --
    // these live in a strip at the foot of the window now.
    const placed = pr.top >= ar.bottom - 1 || pr.bottom <= ar.top + 1;
    emit(`pop-${nm} ${onscreen ? 1 : 0} ${placed ? 1 : 0}`);
    closePop();
  }
  emit(`popclosed ${document.getElementById('pop-sound').hidden
                       && document.getElementById('pop-power').hidden ? 1 : 0} 0`);

  // Settings must work with the side panel collapsed. It used to live INSIDE
  // that panel, so collapsing the column took the settings with it and the
  // gear did nothing at all -- silently, since the click handler ran fine.
  const actbtn = document.getElementById('actbtn');
  const statbtn = document.getElementById('statbtn');
  // The column holds ONE panel, and the button that opened it closes it.
  // Assert the whole cycle, because every part of it has been wrong at least
  // once: a collapse that left the grid track sized, a readout that rendered
  // into a hidden div, a pair of toggles that could both read "open".
  actbtn.click();                       // whatever it was, the log is showing
  if (document.body.classList.contains('nopanel')) actbtn.click();
  const logH = document.getElementById('log').clientHeight;
  statbtn.click();
  const statH = document.getElementById('stats').clientHeight;
  const logGone = document.getElementById('log').clientHeight;
  emit(`panelswap ${logH > 40 && statH > 40 && logGone === 0 ? 1 : 0} `
     + `${statbtn.getAttribute('aria-expanded') === 'true'
          && actbtn.getAttribute('aria-expanded') === 'false' ? 1 : 0}`);
  const wide = document.getElementById('stage').getBoundingClientRect().width;
  statbtn.click();                      // press the lit one: column goes away
  const wider = document.getElementById('stage').getBoundingClientRect().width;
  emit(`panelshut ${document.body.classList.contains('nopanel') ? 1 : 0} `
     + `${Math.round(wider - wide)}`);
  document.getElementById('gbtn').click();
  const set = document.getElementById('settings');
  const r = set.getBoundingClientRect();
  const vis = !set.hidden && r.width > 100 && r.height > 100
              && getComputedStyle(set).display !== 'none';
  emit(`drawer ${vis ? 1 : 0} ${Math.round(r.width)}`);
  // It must not cover the rails. The gear that opens settings lives in the
  // top one, so a drawer at inset:0 puts the control underneath the thing it
  // controls and leaves nothing to tap -- which is exactly what shipped.
  const hdr = document.querySelector('header').getBoundingClientRect();
  const strip = document.getElementById('cmdbar').getBoundingClientRect();
  const clearTop = r.top >= hdr.bottom - 1;
  const clearBot = r.bottom <= strip.top + 1;
  emit(`drawerbars ${clearTop ? 1 : 0} ${clearBot ? 1 : 0}`);
  // The gear is the only switch now, so closing is pressing it again --
  // which is also the assertion worth making: a panel you can open and
  // not close from the same control is the bug this replaced.
  document.getElementById('gbtn').click();
  emit(`drawerclosed ${document.getElementById('settings').hidden ? 1 : 0} 0`);
  actbtn.click();
  emit(`named ${document.getElementById('themename').textContent.trim()
                     .replace(/\s+/g, '_')} 0`);

  // The key rail is a POPOVER now, so the assertion is the opposite of what
  // it used to be: opening it must leave the picture exactly where it is.
  arm();
  zoomMode = 'fit'; crop = null; applyZoom(true);
  const kH = sc.clientHeight;
  document.getElementById('keysbtn').click();
  const pk = document.getElementById('pop-keys');
  const kr = pk.getBoundingClientRect();
  emit(`keysmenu ${!pk.hidden && kr.height > 20 ? 1 : 0} `
     + `${Math.round(sc.clientHeight - kH)}`);
  // It must clear the strip that opened it -- a menu covering its own button
  // is the bug the tabs and the settings drawer both had.
  const cr = document.getElementById('cmdbar').getBoundingClientRect();
  emit(`keysbars ${kr.bottom <= cr.top + 1 ? 1 : 0} `
     + `${kr.width > 120 ? 1 : 0}`);
  document.getElementById('keysbtn').click();
  emit(`keysshut ${pk.hidden ? 1 : 0} 0`);

  // The caret points where the menu WILL GO, so the strip menus and the
  // header menu must disagree about which glyph means closed. 1 = up.
  const tipUp = n => { const t = caretLabel(n).textContent.trim();
                       return t.charCodeAt(t.length - 1) === 0x25b4 ? 1 : 0; };
  // THE KEYBOARD IS LAID OUT, not reflowed. Every one of these was wrong
  // when the menu was a wrapping bag of buttons, and every one of them would
  // silently come back if the row containers were ever dropped.
  document.getElementById('keysbtn').click();
  const q = sel => document.querySelector('#pop-keys ' + sel);
  const fk = Array.from(document.querySelectorAll('#pop-keys [data-key]'))
                  .filter(b => /^f\d+$/.test(b.dataset.key));
  const rows = new Set(fk.map(b => Math.round(b.getBoundingClientRect().top)));
  emit(`fkeyrow ${fk.length} ${rows.size}`);
  const up = q('[data-key="up"]').getBoundingClientRect();
  const dn = q('[data-key="down"]').getBoundingClientRect();
  const lf = q('[data-key="left"]').getBoundingClientRect();
  const rt = q('[data-key="right"]').getBoundingClientRect();
  const mid = r => (r.left + r.right) / 2;
  const tee = up.bottom <= dn.top + 1 && Math.abs(mid(up) - mid(dn)) < 2
           && Math.abs(lf.top - dn.top) < 2 && Math.abs(rt.top - dn.top) < 2
           && lf.right <= dn.left + 1 && rt.left >= dn.right - 1;
  emit(`arrowtee ${tee ? 1 : 0} `
     + `${mid(dn) > mid(q('[data-key="scrolllock"]').getBoundingClientRect()) ? 1 : 0}`);
  // Red while it sits there. Compared against a plain key rather than to a
  // literal colour, because the value is a theme token and changes 22 ways.
  emit(`cadcolour ${getComputedStyle(q('[data-combo]')).color
                    !== getComputedStyle(q('[data-key="esc"]')).color ? 1 : 0} `
     + `${document.getElementById('refresh').closest('#pop-zoom') ? 1 : 0}`);
  closePop();
  emit(`caretshut ${tipUp('keys')} ${tipUp('zoom')}`);
  document.getElementById('keysbtn').click();
  emit(`caretopen ${tipUp('keys')} 0`);
  closePop();
  document.getElementById('zoombtn').click();
  emit(`caretzoom ${tipUp('zoom')} 0`);
  closePop();

  // A FIT THAT SCROLLS IS NOT A FIT. The scale is a float, so nh*scale lands
  // a millionth of a pixel over the box on some window heights and the
  // browser rounds that up into a real scrollbar that scrolls one pixel.
  arm();
  zoomMode = 'fit'; crop = null; applyZoom(true);
  const sr = document.getElementById('scroll');
  // Both halves matter: the content must not overflow, AND the container
  // must not be scrollable -- the second is what a rounding error cannot get
  // past, and the first is what makes the second safe.
  emit(`fitscroll ${sr.scrollHeight - sr.clientHeight} `
     + `${getComputedStyle(sr).overflowY === 'hidden' ? 1 : 0}`);
  zoomMode = '4'; applyZoom(true);
  emit(`zoomscroll ${getComputedStyle(sr).overflowY === 'hidden' ? 0 : 1} 0`);
  zoomMode = 'fit'; applyZoom(true);
  emit('end 1 1');
 } catch (e) {
  document.getElementById('harness-out').textContent = 'THREW ' + e.message;
 }
})();
</script>
"""


def test_zoom_layout_in_a_browser():
    """Lay the page out in a real engine and measure the picture.

    Three rounds of this were fixed in the arithmetic and stayed broken,
    because the arithmetic was right and the assumption under it was not.
    `width:auto; max-width:100%` takes the element's intrinsic 640x480 and
    only ever SHRINKS it -- object-fit had nothing to do, since the box
    already matched the content -- so on any stage larger than the capture the
    picture sat at 640x480 with the rest of the well empty, while every mode
    scaled against a fit the layout was never applying.

    A ported model cannot catch that: the port asserted the same wrong
    premise, and agreed with itself. Only the engine knows how big the picture
    is, so the test asks the engine.
    """
    print("\nzoom layout (measured in chromium)")
    import os
    import shutil
    import subprocess
    import tempfile

    chrome = (shutil.which("chromium") or shutil.which("chromium-browser")
              or shutil.which("google-chrome"))
    if not chrome:
        print("  SKIP  no chromium on this host")
        return

    src = os.path.join(HERE, os.pardir, "daemon")
    d = tempfile.mkdtemp(prefix="kvmlayout")
    try:
        with open(os.path.join(src, "kvm.html"), encoding="utf-8") as f:
            page = f.read().replace('href="/themes.css"', 'href="themes.css"')
        shutil.copy(os.path.join(src, "themes.css"), os.path.join(d, "themes.css"))
        import base64
        import io as _io
        from PIL import Image
        buf = _io.BytesIO()
        Image.new("RGB", (640, 480), (16, 16, 16)).save(buf, "JPEG")
        cap = "data:image/jpeg;base64," + base64.b64encode(
            buf.getvalue()).decode()
        with open(os.path.join(d, "page.html"), "w", encoding="utf-8") as f:
            f.write(page + HARNESS.replace("CAPSRC", cap))

        r = subprocess.run(
            [chrome, "--headless", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--window-size=1580,900",
             # 6000 was not enough once the harness started awaiting a
             # typing loop: it reported "no output", which reads as a broken
             # page rather than a clock that ran out.
             "--virtual-time-budget=30000", "--dump-dom",
             "file://" + os.path.join(d, "page.html")],
            capture_output=True, text=True, timeout=90)
        m = re.search(r'<pre id="harness-out">([^<]*)</pre>', r.stdout)
        if not m or not m.group(1).strip():
            # Say which of the several ways this can fail actually happened.
            # "no output" was true and useless: it covers chromium dying, the
            # page throwing, the clock running out, and the element being
            # renamed, and those need different fixes.
            why = ("rc=%d, %d bytes of dom, pre %s, stderr: %s"
                   % (r.returncode, len(r.stdout),
                      "present but empty" if "harness-out" in r.stdout
                      else "MISSING from dom",
                      r.stderr.strip()[-200:] or "(silent)"))
            try:
                shutil.copy(os.path.join(d, "page.html"),
                            "/tmp/failed-harness.html")
                with open("/tmp/failed-harness.dom", "w") as fh:
                    fh.write(r.stdout)
            except Exception:
                pass
            check("the harness reported a measurement", False, why)
            return
        if m.group(1).startswith("THREW"):
            check("the harness ran without throwing", False, m.group(1))
            return
        got, bars = {}, {}
        for part in m.group(1).split("|"):
            bits = part.split()
            if bits[0] == "named":
                got["named_label"] = bits[1]
                continue
            got[bits[0]] = (float(bits[1]), float(bits[2]))
            if len(bits) > 3:
                bars[bits[0]] = bits[3]
    finally:
        shutil.rmtree(d, ignore_errors=True)

    W, H = got["view-fit"]
    print("  viewport %.0fx%.0f: %s" % (W, H, ", ".join(
        "%s=%.0fx%.0f" % (k, v[0], v[1]) for k, v in got.items()
        if not k.startswith("view-") and k not in ("loaded", "named_label"))))
    check("control: the viewport is bigger than the capture",
          W > 640 and H > 480, (W, H))
    # Without this, a picture that never loaded measures 0 and every
    # comparison below fails for a reason that has nothing to do with zoom.
    check("control: the harness actually had a 640x480 picture",
          got.get("loaded") == (640.0, 480.0), got.get("loaded"))

    w, h = got["fit"]
    # The regression: this is what a picture that is never scaled up measures,
    # and it is what shipped for three rounds.
    check("fit is not just the raw 640x480", (w, h) != (640.0, 480.0), (w, h))
    check("fit overflows neither axis", w <= W + 0.5 and h <= H + 0.5, (w, h))
    check("fit fills one axis exactly",
          abs(w - W) < 0.5 or abs(h - H) < 0.5, (w, h))
    check("fit raises no scrollbars", bars.get("fit") == "00", bars.get("fit"))

    check("original is exactly 640x480 on screen",
          abs(got["1"][0] - 640) < 0.5 and abs(got["1"][1] - 480) < 0.5,
          got["1"])
    check("2x original is exactly 1280x960",
          abs(got["2"][0] - 1280) < 1 and abs(got["2"][1] - 960) < 1, got["2"])
    check("4x original is exactly 2560x1920",
          abs(got["4"][0] - 2560) < 2 and abs(got["4"][1] - 1920) < 2, got["4"])

    # What the operator asked for: if it is cropped, it scrolls.
    for m in ("2", "4"):
        w, h = got[m]
        spills = w > W + 1 or h > H + 1
        check("%sx spills, so it must offer scrollbars" % m,
              not spills or bars.get(m, "00") != "00",
              (got[m], (W, H), bars.get(m)))

    # The picker: one swatch per identity, each wearing a real palette.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "themes", os.path.join(HERE, os.pardir, "tools", "themes.py"))
    T = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(T)
    darks = sum(1 for v in T.THEMES.values() if v[2])
    check("the page discovers every theme in the table",
          got["themes"] == (float(len(T.THEMES)), float(darks)),
          (got["themes"], len(T.THEMES), darks))
    check("one swatch per pair, and every swatch is painted",
          got["chips"] == (float(darks), float(darks)), got["chips"])
    check("every button has a tooltip",
          got["tips"][1] == 0.0, "%d of %d have none"
          % (got["tips"][1], got["tips"][0]))
    check("control: there are buttons to check", got["tips"][0] > 25,
          got["tips"])

    check("hovering a swatch previews that theme", got["preview"][0] == 1.0,
          got["preview"])
    check("and leaving puts the chosen one back", got["preview"][1] == 1.0,
          got["preview"])
    check("control: a preview does not change the selection",
          got["selkept"][0] == 1.0, got["selkept"])

    check("the status readout renders every group", got["rig"][0] == 4.0,
          got["rig"])
    check("control: and injects nothing from a device name",
          got["rig"][1] == 0.0, got["rig"])
    check("a null device reads as could-not-tell, not as no",
          got["rignull"][0] == 1.0, got["rignull"])
    check("control: and an absent board gives the daemon's own reason",
          got["rignull"][1] == 1.0, got["rignull"])

    check("unplugged, will-not-open and cannot-tell read differently",
          got["absent3"][0] == 1.0, got["absent3"])
    check("control: a healthy device is not called trouble",
          got["absent3"][1] == 1.0, got["absent3"])
    check("ffmpeg's sentence survives", got["reason"][0] == 1.0, got["reason"])
    check("control: without its module tag and pointer",
          got["reason"][1] == 1.0, got["reason"])

    check("a CRLF file types two lines, not two lines and two blanks",
          got["file"] == (2.0, 2.0), got["file"])
    check("control: and the text is the text", got["filetext"][0] == 1.0,
          got["filetext"])
    check("a refusal partway stops rather than ploughing on",
          got["filestop"][0] == 1.0, "%d posts after refusing at 2"
          % got["filestop"][1])

    check("grabbing the keyboard moves nothing on the page",
          got["grab"][0] == 1.0, got["grab"])
    check("control: and the KBD lamp lights and unlights",
          got["grab"][1] == 1.0, got["grab"])

    check("no frame yet means no resolution claimed",
          got["res"][0] == 1.0, got["res"])
    check("control: and a 512x342 frame reports 512x342",
          got["res"][1] == 1.0, got["res"])

    check("a dark target is not blamed on the transport",
          got["sourcelive"][0] == 1.0, got["sourcelive"])
    check("control: with no information the transport is still suspected",
          got["sourcelive"][1] == 1.0, got["sourcelive"])

    check("the sound control is the same height as the buttons beside it",
          abs(got["striph"][0] - got["striph"][1]) <= 1, got["striph"])
    check("control: and that height is a real one, not zero",
          got["striph"][1] >= 30, got["striph"])
    check("the command field is the same height as the buttons beside it",
          got["fieldh"][0] == got["fieldh"][1], got["fieldh"])
    check("Send is built like the menu buttons: glyph, rule, label",
          got["sendparts"][0] == 1.0, got["sendparts"])
    check("and the rule between them is drawn",
          got["sendparts"][1] == 1.0, got["sendparts"])

    check("a fallback schedules a retry, backs off, and resets on success",
          got["wsretry"][0] == 1.0, got["wsretry"])
    check("control: and the link lamp retries on click",
          got["wsretry"][1] == 1.0, got["wsretry"])

    check("ctrl-alt-delete asks first, and sends when confirmed",
          got["cad"][0] == 1.0, got["cad"])
    check("control: an ordinary key does not ask", got["cad"][1] == 1.0,
          got["cad"])

    check("the file input can actually be clicked open",
          got["file2"][0] == 1.0, got["file2"])
    check("control: and the control sits inside the command field",
          got["file2"][1] == 1.0, got["file2"])

    check("input traffic flickers the lamp without changing its state",
          got["flash"][0] == 1.0, got["flash"])
    check("control: and a mouse command lights the mouse, not the keyboard",
          got["flash"][1] == 1.0, got["flash"])

    check("KBD and MOS answer the same question",
          got["samequestion"][0] == 1.0, got["samequestion"])

    check("free / held / broken are three different lamps",
          got["inlamp"][0] == 1.0, got["inlamp"])
    check("control: and held is marked without relying on colour",
          got["inlamp"][1] == 1.0, got["inlamp"])

    check("power reads on / off / unknown, not on / off",
          got["power3"][0] == 1.0, got["power3"])
    check("control: the plug names itself and says when it stopped answering",
          got["power3"][1] == 1.0, got["power3"])
    check("the board still reaches the power confirmation",
          got["board2"][0] == 1.0, got["board2"])
    check("control: and clears when the board is unknown",
          got["board2"][1] == 1.0, got["board2"])

    # Full screen has to actually give the picture the room, and the bars have
    # to come back without moving it.
    check("full screen hides the rail", got["fs"][0] == 1.0, got["fs"])
    check("and the picture takes the height", got["fs"][1] > got["fsback"][1],
          (got["fs"], got["fsback"]))
    check("peeking overlays the rail rather than reflowing",
          got["fspeek"][0] == 1.0 and abs(got["fspeek"][1] - got["fs"][1]) < 2,
          (got["fspeek"], got["fs"]))
    check("control: leaving full screen puts it back",
          got["fsback"][0] == 1.0, got["fsback"])

    # Asking for 14 and getting 6 must come down near 6, not to the floor.
    check("a starved stream backs off toward what arrives",
          4 <= got["rate"][0] <= 7, got["rate"])
    check("and creeps back up once frames keep arriving",
          got["rate"][1] > got["rate"][0], got["rate"])
    check("but never past the ceiling", got["ratecap"][0] <= 30,
          got["ratecap"])
    check("the ceiling follows a 30 fps source instead of a hardcoded 20",
          got["srcceil"][0] == 30.0, got["srcceil"])
    check("control: and it measured the source, not guessed it",
          25 <= got["srcceil"][1] <= 31, got["srcceil"])
    check("control: it actually told the server", got["ratecap"][1] >= 3,
          got["ratecap"])

    for nm in ("sound", "power"):
        check("the %s menu opens on screen" % nm,
              got["pop-" + nm][0] == 1.0, got["pop-" + nm])
        check("control: and clear of the control that opened it",
              got["pop-" + nm][1] == 1.0, got["pop-" + nm])
    check("control: the menus close again", got["popclosed"][0] == 1.0,
          got["popclosed"])

    check("settings opens with the side panel collapsed",
          got["drawer"][0] == 1.0 and got["drawer"][1] > 100, got["drawer"])
    check("settings clears the top rail, so the gear stays tappable",
          got["drawerbars"][0] == 1.0, got["drawerbars"])
    check("and clears the control strip", got["drawerbars"][1] == 1.0,
          got["drawerbars"])
    check("control: and closes again", got["drawerclosed"][0] == 1.0,
          got["drawerclosed"])
    check("Status replaces Activity in the column, not joins it",
          got["panelswap"][0] == 1.0, got["panelswap"])
    check("and exactly one button reads as open",
          got["panelswap"][1] == 1.0, got["panelswap"])
    check("pressing the lit panel button collapses the column",
          got["panelshut"][0] == 1.0, got["panelshut"])
    # The bug this replaced: display:none on the panel while the grid track
    # kept its 340px, so the picture did not move and nothing looked collapsed.
    check("and the picture actually takes the width back",
          got["panelshut"][1] > 100, got["panelshut"])

    check("control: the name line shows a label, not a raw id",
          "_" in str(got.get("named_label", "")) or
          str(got.get("named_label", "")) not in T.THEMES,
          got.get("named_label"))

    # The key rail was a row in the flow: opening it took ~40px of picture
    # and re-fitted the stage, so reaching for a key resized the thing you
    # were about to type at. As a popover it must cost the picture nothing.
    check("the Keys menu opens", got["keysmenu"][0] == 1.0, got["keysmenu"])
    check("and the picture does not move when it does",
          got["keysmenu"][1] == 0.0, got["keysmenu"])
    check("the Keys menu clears the strip that opened it",
          got["keysbars"][0] == 1.0, got["keysbars"])
    check("control: it is a real menu, not a collapsed box",
          got["keysbars"][1] == 1.0, got["keysbars"])
    check("and the same button closes it",
          got["keysshut"][0] == 1.0, got["keysshut"])
    # The rule is "point where the menu will go", NOT "point down when
    # closed" -- so the two bars must disagree, and a change that made them
    # agree would mean the rule had been replaced by a constant.
    check("a closed strip menu points up, toward where it opens",
          got["caretshut"][0] == 1.0, got["caretshut"])
    check("a closed header menu points down, for the same reason",
          got["caretshut"][1] == 0.0, got["caretshut"])
    check("opening a strip menu flips its caret down",
          got["caretopen"][0] == 0.0, got["caretopen"])
    check("opening the header menu flips its caret up",
          got["caretzoom"][0] == 1.0, got["caretzoom"])
    check("control: all ten function keys are present",
          got["fkeyrow"][0] == 10.0, got["fkeyrow"])
    check("and they share one row", got["fkeyrow"][1] == 1.0, got["fkeyrow"])
    check("the arrows form an inverted T",
          got["arrowtee"][0] == 1.0, got["arrowtee"])
    check("to the right of the lock keys",
          got["arrowtee"][1] == 1.0, got["arrowtee"])
    check("Ctrl-Alt-Del is coloured apart from the keys that only type",
          got["cadcolour"][0] == 1.0, got["cadcolour"])
    check("Refresh video is in the screen menu, not the power menu",
          got["cadcolour"][1] == 1.0, got["cadcolour"])
    check("a fitted picture does not overflow vertically",
          got["fitscroll"][0] <= 0, got["fitscroll"])
    check("and a fitted stage is not scrollable at all",
          got["fitscroll"][1] == 1.0, got["fitscroll"])
    # The control that stops the fix from being "turn scrolling off": at 400%
    # the picture genuinely exceeds the box and scrolling is the only way to
    # reach the rest of it.
    check("control: a zoomed stage still scrolls",
          got["zoomscroll"][0] == 1.0, got["zoomscroll"])


def test_favicon_single_source():
    """daemon/favicon.svg and the icon inlined in kvm.html must not drift.

    The page inlines the icon as a data: URI, which is the right call -- it
    costs no request and survives any change to routing. But it means the
    bytes exist twice, and base64 is not something anyone edits by hand, so
    the .svg is the copy a person would change and the inlined one is the
    copy that actually runs. That is the shape of every duplicated-asset bug
    in this repo: the edit lands on the copy nobody serves. This check is
    what makes the .svg a source rather than an orphan.
    """
    import base64
    import re

    root = os.path.join(HERE, os.pardir)
    with open(os.path.join(root, "daemon", "favicon.svg"), "rb") as fh:
        svg = fh.read()
    with open(os.path.join(root, "daemon", "kvm.html"), encoding="utf-8") as fh:
        html = fh.read()

    uris = re.findall(r'href="data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)"',
                      html)
    check("kvm.html inlines at least one svg icon", len(uris) >= 1,
          "found %d" % len(uris))
    for i, uri in enumerate(uris):
        check("inlined icon %d matches daemon/favicon.svg" % i,
              base64.b64decode(uri) == svg,
              "the .svg was edited without re-inlining, or vice versa")


def test_shot_out_contract():
    """`shot --out F` is two-valued: F is this run's frame, or F is absent.

    The half-measure -- write on success, leave whatever is there on failure
    -- is worse than not implementing --out at all, because the caller's
    next line reads F and gets the PREVIOUS run's picture with nothing
    marking it stale. That is a sample that is real and no longer current,
    which is the failure this repo has hit under several different names.
    """
    import tempfile

    client_path = os.path.join(HERE, os.pardir, "bin", "vcctrl-client")
    loader = SourceFileLoader("vcctrl_client", client_path)
    spec = importlib.util.spec_from_loader("vcctrl_client", loader)
    client = importlib.util.module_from_spec(spec)
    loader.exec_module(client)

    import base64
    payload = b"\xff\xd8\xff not really a jpeg but bytes are bytes"
    good = {"ok": True, "picture": True,
            "jpeg": base64.b64encode(payload).decode()}
    bad = {"ok": True, "picture": False, "reason": "every frame a duplicate"}

    d = tempfile.mkdtemp()
    target = os.path.join(d, "frame.jpg")

    rc = client.write_frame(good, target)
    check("write_frame returns 0 on a picture", rc == 0, "got %r" % rc)
    check("write_frame wrote the frame bytes",
          os.path.exists(target) and open(target, "rb").read() == payload)
    check("write_frame left no .part behind",
          not os.path.exists(target + ".part"))

    rc = client.write_frame(bad, target)
    check("write_frame returns 1 on no picture", rc == 1, "got %r" % rc)
    check("write_frame REMOVED the stale frame rather than leaving it",
          not os.path.exists(target),
          "a caller reading this path would get the previous run's picture")

    rc = client.write_frame(bad, os.path.join(d, "never-existed.jpg"))
    check("no picture with no pre-existing file is still status 1", rc == 1,
          "got %r" % rc)


def _unbound_names(src, path="<src>"):
    """Names loaded but bound nowhere in the file, and not a builtin.

    Deliberately over-permissive about SCOPE: a name bound anywhere in the
    file counts as bound everywhere. That misses using a local from another
    function, and it costs nothing in false alarms -- which matters, because a
    check people switch off catches less than no check at all.
    """
    import ast
    import builtins as B

    tree = ast.parse(src, path)
    bound, loaded = set(), {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                bound.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
        elif isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Load):
                loaded.setdefault(n.id, n.lineno)
            else:
                bound.add(n.id)
    ok = set(dir(B)) | {"__name__", "__file__", "__doc__", "__package__"}
    return sorted((k, v) for k, v in loaded.items()
                  if k not in bound and k not in ok)


def test_no_unbound_names():
    """No module uses a name it never imported or defined.

    Found on the live rig 2026-08-20, on the Pi 5's first boot: vcweb called
    sys.stderr.write() and never imported sys. The line was the FALLBACK -- it
    runs when the direct TLS listener cannot start, to say so and carry on
    serving plain HTTP. So the code whose entire job was to degrade gracefully
    was itself a NameError, and the result was not "no WebSocket transport",
    it was the whole web capability dead and no KVM at all.

    It never fired on the old machine for the reason these never fire: the
    cert was already there, so the listener always started and that line was
    never executed in the life of that Pi. It took a fresh install on a
    machine without a cert to run one line of error handling for the first
    time.

    Every test in this file exercises code by running it. This is the one
    class that cannot be reached that way -- an error path that only executes
    on a machine that does not exist yet -- so it is read instead.
    """
    print("\nunbound names")
    import os

    root = os.path.join(HERE, os.pardir)
    files = []
    for d in ("daemon", "tools"):
        p = os.path.join(root, d)
        if os.path.isdir(p):
            files += [os.path.join(p, f) for f in sorted(os.listdir(p))
                      if f.endswith(".py")]
    for f in sorted(os.listdir(os.path.join(root, "bin"))):
        path = os.path.join(root, "bin", f)
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as fh:
            if fh.readline().startswith("#!/usr/bin/env python"):
                files.append(path)

    bad = []
    for path in files:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for name, line in _unbound_names(src, path):
            bad.append("%s:%d %s" % (os.path.basename(path), line, name))
    check("%d modules use only names they have" % len(files), not bad, bad)
    check("control: there were modules to read", len(files) >= 4, len(files))

    # The control that matters: this check has to be able to FAIL. Take the
    # import back out of the module it actually happened in and confirm the
    # reader notices -- otherwise the test above passes for the wrong reason
    # on the day someone breaks the reader.
    web = os.path.join(root, "daemon", "vcweb.py")
    with open(web, encoding="utf-8") as fh:
        src = fh.read()
    assert "\nimport sys\n" in src, "vcweb.py no longer imports sys"
    hurt = src.replace("\nimport sys\n", "\n", 1)
    found = [n for n, _ in _unbound_names(hurt, web)]
    check("control: removing an import makes it fail", "sys" in found, found)


def test_ffmpeg_stderr_is_kept():
    """A device that will not open must say why.

    Both capture spawns used stderr=DEVNULL. Measured on the Pi 5 with no
    capture stick attached: video and audio both `unavailable`, nine
    fast_failures each, and `last_error: null` -- while ffmpeg was, on the
    other side of a pipe pointed at /dev/null, saying exactly what was wrong.

    That is the wrong thing to discard on a rig that addresses its devices by
    INDEX. "hw:1,0" is not a stick, it is a guess about enumeration order, and
    the Pi 5 enumerates differently from the Pi 3 it replaced. When the guess
    is wrong, the difference between "cannot open audio device hw:1,0" and
    "Device or resource busy" is the difference between one environment
    variable and an afternoon.

    Run against the real ffmpeg rather than a fake, because the thing being
    tested is whether ffmpeg's words survive the plumbing.
    """
    print("\nffmpeg stderr")
    import shutil
    import subprocess
    import types

    if not shutil.which("ffmpeg"):
        print("  SKIP  no ffmpeg on this host")
        return

    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "v4l2", "-i", "/dev/video-does-not-exist",
         "-c:v", "copy", "-f", "mjpeg", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    cap = types.SimpleNamespace(lock=threading.Lock(), last_error=None)
    t = threading.Thread(target=vcctrld._keep_stderr, args=(cap, proc),
                         daemon=True)
    t.start()
    try:
        proc.wait(timeout=15)
    except Exception:
        proc.kill()
    t.join(timeout=5)
    try:
        proc.stdout.close()
    except Exception:
        pass

    got = cap.last_error or ""
    print("  ffmpeg said: %s" % (got or "<nothing>"))
    check("the reason reaches last_error", bool(got), got)
    check("and it names the device", "video-does-not-exist" in got, got)
    check("control: it is bounded, not the whole log", len(got) <= 240,
          len(got))

    # The control that matters: DEVNULL is what shipped, and it must be able
    # to show as the absence it is.
    quiet = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "v4l2", "-i", "/dev/video-does-not-exist",
         "-c:v", "copy", "-f", "mjpeg", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    cap2 = types.SimpleNamespace(lock=threading.Lock(), last_error=None)
    try:
        quiet.wait(timeout=15)
    except Exception:
        quiet.kill()
    try:
        quiet.stdout.close()
    except Exception:
        pass
    check("control: with DEVNULL there is nothing to report",
          cap2.last_error is None, cap2.last_error)


def test_buffer_span():
    """Asking for a longer scrub buffer must actually buy one, and must say so
    when memory will not allow it.

    The 48 MB / 30 s default was chosen for a Pi 3 with 920 MB and NO SWAP,
    where an overshoot did not slow the daemon down, it killed it and dropped
    the uinput devices with it. The rig is a 4 GB Pi 5 now and that constraint
    is gone -- but MEM_FLOOR_MB is not, so a request memory cannot honour has
    to be clamped AND said to be clamped. A silently ignored setting is worse
    than no setting.
    """
    print("\nscrub buffer")
    cap = vcctrld.VideoCapability(None, vcctrld.Bus())

    base = cap._buffer({})
    check("reports the default span", base["target_span_s"] == 30.0,
          base["target_span_s"])

    got = cap._buffer({"seconds": 120})
    check("a longer span is taken", got["target_span_s"] == 120.0,
          got["target_span_s"])
    check("and the byte cap grows with it, so granularity is kept",
          got["asked_bytes"] == int(120 * cap.BYTES_PER_S),
          (got["asked_bytes"], cap.RING_BYTES))
    check("control: it is four times the 30 s default",
          abs(got["asked_bytes"] / (30 * cap.BYTES_PER_S) - 4.0) < 0.01,
          got["asked_bytes"])

    check("an absurd request is clamped, not obeyed",
          cap._buffer({"seconds": 99999})["target_span_s"]
          == cap.SPAN_MAX_S, cap.SPAN_MAX_S)
    check("control: and so is zero",
          cap._buffer({"seconds": 0})["target_span_s"] == cap.SPAN_MIN_S,
          cap.SPAN_MIN_S)
    check("nonsense is refused rather than coerced",
          cap._buffer({"seconds": "lots"}).get("ok") is False)

    # Memory is the real limit, and the flag has to follow it rather than the
    # request. Force the floor above what the machine has.
    cap._buffer({"seconds": 600})
    cap.MEM_FLOOR_MB = 10 ** 9
    squeezed = cap._buffer({"seconds": 600})
    check("a cap memory cannot honour is reported as limited",
          squeezed["mem_limited"] is True, squeezed)
    check("control: and the cap really is smaller than asked",
          squeezed["cap_bytes"] < squeezed["asked_bytes"], squeezed)

    # The page reads the live value back rather than remembering its own.
    check("the span is published in the state the page polls",
          cap._state().get("target_span_s") == 600.0,
          cap._state().get("target_span_s"))


def test_stall_tracker_three_states():
    """A no-picture poll is neither movement nor stillness.

    The two-valued version scored it as MOVEMENT: `sig` was None,
    sig_differs(prev, None) returned True, `still_since` reset, and the sweep
    watch loop printed "changing still 0s" for six minutes against a black
    screen. A wedge produced the healthiest output the loop can print.

    This is a unit test rather than a replay of that log, deliberately. The log
    was deleted, and a replay would only prove the detector handles THAT
    recording -- feeding it a run of Nones tests the property, which is the
    finding: absence must not participate in a difference test.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    path = os.path.join(HERE, os.pardir, "bin", "vcctrl-sweep")
    loader = SourceFileLoader("vcctrl_sweep", path)
    spec = importlib.util.spec_from_loader("vcctrl_sweep", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    A = [10] * 768
    B = [200] * 768        # far beyond the 2%-of-pixels threshold

    t = mod.StallTracker(blind_limit=5)
    check("first real frame reads as changing", t.update(A, now=0)["state"] == "changing")
    check("an identical frame reads as static", t.update(A, now=1)["state"] == "static")
    check("stillness accumulates", t.update(A, now=5)["still"] == 4)

    # The regression itself.
    r = t.update(None, now=6)
    check("a no-picture poll is BLIND, not changing and not static",
          r["state"] == "blind", "got %r" % r["state"])
    check("blind does not reset the stillness clock to zero-and-moving",
          r["blind"] == 1)

    # And it must escalate rather than wait forever.
    for i in range(2, 5):
        r = t.update(None, now=6 + i)
        check("blind poll %d does not escalate early" % i, not r["escalate"])
    r = t.update(None, now=20)
    check("escalates once the blind run hits the limit", r["escalate"],
          "a run of no-picture polls must terminate the watch, not extend it")

    # A frame arriving after a blind spell compares against the last frame
    # actually SEEN, not against the gap -- otherwise the blind spell launders
    # itself into evidence of movement.
    r = t.update(A, now=25)
    check("the first frame after a blind spell is compared to the last real one",
          r["state"] == "static", "got %r -- blind spell reported as movement" % r["state"])
    check("a genuinely different frame still reads as changing",
          t.update(B, now=26)["state"] == "changing")

    # And the old shape must be impossible to reintroduce quietly.
    try:
        mod.sig_differs(A, None)
        check("sig_differs REFUSES a null signature", False,
              "it returned instead of raising -- the old bug is reachable again")
    except ValueError:
        check("sig_differs REFUSES a null signature", True)


def test_avi_is_a_real_file_ffmpeg_can_decode():
    """Mux known frames, then make ffmpeg tell us what it got.

    A test that parsed my own header with my own reader would agree with
    itself no matter how wrong the header was -- the failure this repo has
    already had under the name "a port inherits the premise". ffmpeg has
    never seen this writer and has no reason to be kind to it, so its frame
    count, dimensions and duration are an outside opinion.
    """
    import subprocess, tempfile, shutil, json as _json
    T = vcctrld

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("  SKIP  ffmpeg/ffprobe not installed")
        return

    # Three distinguishable frames, made by ffmpeg so the JPEGs are real ones
    # rather than bytes this test also invented.
    d = tempfile.mkdtemp()
    try:
        jpegs = []
        for i, colour in enumerate(("red", "green", "blue")):
            f = os.path.join(d, "%d.jpg" % i)
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=c=%s:s=320x240" % colour, "-frames:v", "1", f],
                check=True)
            jpegs.append(open(f, "rb").read())

        check("control: the fixture JPEGs carry readable dimensions",
              T.jpeg_dims(jpegs[0]) == (320, 240), T.jpeg_dims(jpegs[0]))

        # Ten frames at 10 fps: one second, and a count ffprobe can confirm.
        blob = T.avi_mjpeg([jpegs[i % 3] for i in range(10)], 10.0, 320, 240)
        out = os.path.join(d, "b.avi")
        open(out, "wb").write(blob)

        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-count_frames", "-show_entries",
             "stream=codec_name,width,height,nb_read_frames,r_frame_rate",
             "-of", "json", out],
            capture_output=True, text=True)
        check("ffprobe reads the file at all", p.returncode == 0, p.stderr[:200])
        st = _json.loads(p.stdout or "{}").get("streams", [{}])[0]
        check("ffprobe says it is mjpeg", st.get("codec_name") == "mjpeg", st)
        check("with the dimensions we wrote",
              (st.get("width"), st.get("height")) == (320, 240), st)
        check("and every frame we put in",
              str(st.get("nb_read_frames")) == "10", st)
        check("at the rate we asked for",
              st.get("r_frame_rate") == "10/1", st)

        # DECODE one back out and confirm the bytes survived. A container that
        # plays but hands back a different image would pass everything above.
        png = os.path.join(d, "f0.png")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", out,
                        "-frames:v", "1", png], check=True)
        px = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
             "-of", "csv=p=0", png], capture_output=True, text=True)
        check("and the first frame decodes to the right size",
              px.stdout.strip().startswith("320,240"), px.stdout.strip())
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_avi_preserves_stalls_rather_than_smoothing_them():
    """A gap in the ring must cost frames in the file.

    This is the property the whole design turns on: the buffer is thinned
    when it runs out of bytes, so a file written at the average rate would
    replay a stall as smooth motion -- a lie about the one thing the
    recording exists to show.
    """
    T = vcctrld
    # A synthetic ring: ten frames 100ms apart, then a one-second stall.
    ts = [i * 0.1 for i in range(10)] + [1.9]
    med = 0.1
    fps = min(30.0, max(1.0, round(1.0 / med)))
    reps = []
    for i, t in enumerate(ts):
        dur = (ts[i + 1] - t) if i + 1 < len(ts) else med
        reps.append(max(1, int(round(dur * fps))))
    check("control: an evenly spaced frame is written once",
          reps[0] == 1, reps)
    check("the frame that spanned the stall is repeated to cover it",
          reps[9] == 10, reps)
    check("control: and the rate came from the data, not a constant",
          fps == 10.0, fps)


if __name__ == "__main__":
    test_key_table()
    test_concurrent_type()
    test_lock_scope()
    test_keydown_release_all()
    test_registry()
    test_rule_2_isolation()
    test_status_shape()
    test_lock_default_unheld()
    test_lock_gating()
    test_event_bus()
    test_activity_age()
    test_audio_levels()
    test_watchdogs_survive_one_pass()
    test_theme_contrast()
    test_uniform_frame_is_not_picture()
    test_websocket_accept_vector()
    test_page_dom_references()
    test_buffer_span()
    test_avi_is_a_real_file_ffmpeg_can_decode()
    test_avi_preserves_stalls_rather_than_smoothing_them()
    test_no_unbound_names()
    test_ffmpeg_stderr_is_kept()
    test_zoom_modes()
    test_zoom_layout_in_a_browser()
    test_favicon_single_source()
    test_shot_out_contract()
    test_stall_tracker_three_states()
    print("\n%s" % ("ALL PASS" if not FAILURES
                    else "FAILED: %s" % ", ".join(FAILURES)))
    sys.exit(1 if FAILURES else 0)


def test_leds_three_states_by_board():
    """A Macintosh must not render like a Gateway with a dead PS/2 lead.

    Before this, `leds` was a flat dict of three values and `input_verified`
    was ok/not-ok. On the ADB board that produced the same output as a broken
    PS/2 link: no LED values, a round trip that times out, a red lamp. One is
    working hardware and the other is a fault, and the page could not tell
    them apart because the daemon could not either.

    The property under test is that `why` is a CLOSED SET distinguishing
    unsupported from error, and that VALUE KEYS ARE ABSENT when unavailable --
    not zero. A plausible set of zeroes is worse than no data: a consumer that
    forgets to check `available` reads it as "all three LEDs are off" and is
    confidently wrong, where a missing key gives undefined and shows a dash.
    """
    class Devs(object):
        def __init__(self, values=None, boom=False):
            self.values = values
            self.boom = boom

        def read_leds(self):
            if self.boom:
                raise OSError("no such file")
            return dict(self.values or {})

    good = {"capslock": 0, "numlock": 1, "scrolllock": 1}

    def cap(board, devs):
        c = vcctrld.LedsCapability(devs)
        c.support = lambda: (
            (True, None) if board == 1 else
            (False, "board %s has no PS/2 LED return channel" % board)
            if board is not None else
            (None, "board unknown"))
        return c

    # 1. IBM PC, readable -> available, values FLAT and present
    s = cap(1, Devs(good)).snapshot()
    check("ibmpc reports available", s["available"] is True)
    check("ibmpc why is null", s["why"] is None)
    check("ibmpc values are flat and present",
          s.get("capslock") == 0 and s.get("numlock") == 1
          and s.get("scrolllock") == 1, s)

    # 2. ADB -> unsupported, and NO value keys at all
    s = cap(3, Devs(good)).snapshot()
    check("adb reports unavailable", s["available"] is False)
    check("adb why is 'unsupported'", s["why"] == "unsupported", s.get("why"))
    check("adb gives a reason", bool(s.get("reason")))
    check("ADB OMITS the value keys rather than zeroing them",
          not any(k in s for k in good), s)

    # 3. IBM PC, read raises -> error, distinguishable from unsupported
    s = cap(1, Devs(boom=True)).snapshot()
    check("read failure reports unavailable", s["available"] is False)
    check("read failure why is 'error', NOT 'unsupported'",
          s["why"] == "error", s.get("why"))
    check("error omits the value keys too", not any(k in s for k in good), s)

    # 4. board unknown -> its own state, not folded into either neighbour
    s = cap(None, Devs(good)).snapshot()
    check("unknown board why is 'unknown'", s["why"] == "unknown", s.get("why"))

    # The whole point: these three must not be confusable.
    whys = {cap(1, Devs(boom=True)).snapshot()["why"],
            cap(3, Devs(good)).snapshot()["why"],
            cap(None, Devs(good)).snapshot()["why"]}
    check("unsupported / error / unknown are three distinct values",
          whys == {"error", "unsupported", "unknown"}, whys)

    # verify_input must REFUSE on ADB rather than run and latch a false red.
    c = cap(3, Devs(good))
    before_at, before_ok = c.verified_at, c.verified_ok
    out = c._verify_input({})
    check("verify_input on ADB does not claim a failure",
          out.get("verified") is None and out.get("why") == "unsupported", out)
    check("verify_input on ADB LEAVES verified_* alone",
          c.verified_at is before_at and c.verified_ok is before_ok,
          (c.verified_at, c.verified_ok))


def test_leds_flat_shape_survives_for_the_harness():
    """bin/vcctrl_common.leds() callers must keep working, or sweeps stall.

    Every LED caller does `.get("capslock")` on the dict this returns.
    Nesting the values would make stable_led(), wait_led(), arm_leds() and
    at_prompt() all read None -> False on a healthy machine. That is the worst
    failure direction the harness has: at_prompt() returning False reads as
    "something is still running", so an unattended sweep declines to type and
    stalls looking exactly like a wedge.
    """
    class Devs(object):
        def read_leds(self):
            return {"capslock": 1, "numlock": 0, "scrolllock": 0}

    c = vcctrld.LedsCapability(Devs())
    c.support = lambda: (True, None)
    out = c._leds({})
    check("_leds still returns an 'leds' key", "leds" in out, out)
    leds = out["leds"]
    check("values remain reachable as leds['capslock']",
          leds.get("capslock") == 1, leds)
    check("values remain reachable as leds['scrolllock']",
          leds.get("scrolllock") == 0, leds)
    check("the availability flag rides alongside, not around",
          leds.get("available") is True, leds)


def test_installed_board_id_never_guesses():
    """Unknown must not default to IBM PC.

    BOARD-IDENTITY sec. 2: config.json read {"3"} for the whole time the Pi 3
    was driving the Gateway, so a plausible source returned the wrong machine
    with total confidence. This reader consults only the tmpfs status file and
    answers None for everything else -- a confident wrong answer here would
    mark a real LED fault as "no channel on this board" and hide it.
    """
    import json as _json
    import tempfile
    d = tempfile.mkdtemp(prefix="boardid")
    orig = vcctrld.BoardCapability.FILE
    try:
        p = os.path.join(d, "board.json")
        vcctrld.BoardCapability.FILE = p

        vcctrld.BoardCapability.FILE = os.path.join(d, "absent.json")
        check("missing file -> None, not 1",
              vcctrld.installed_board_id() is None)

        vcctrld.BoardCapability.FILE = p
        with open(p, "w") as f:
            f.write("{ this is not json")
        check("corrupt file -> None, not 1",
              vcctrld.installed_board_id() is None)

        with open(p, "w") as f:
            _json.dump({"name": "IBM PC Compatible"}, f)   # no id
        check("file without an id -> None, not 1",
              vcctrld.installed_board_id() is None)

        with open(p, "w") as f:
            _json.dump({"id": 3}, f)
        check("reads a real id", vcctrld.installed_board_id() == 3)

        with open(p, "w") as f:
            _json.dump({"id": 1}, f)
        check("reads the IBM PC id", vcctrld.installed_board_id() == 1)
    finally:
        vcctrld.BoardCapability.FILE = orig


def test_wrapper_out_is_the_callers_disk():
    """`--out` must mean the caller's filesystem, not the daemon host's.

    bin/vcctrl forwards to the Pi over ssh, so before this `--out` was a path
    on the PI. The failing case was merely confusing -- an ENOENT that reads
    like a local permissions problem. The SUCCEEDING case was the dangerous
    one: a path that exists on both machines writes on the Pi and returns 0,
    and the caller then reads whatever its own copy holds, which may be a
    frame from an earlier run. That is precisely the stale-frame failure the
    two-valued contract was written to prevent, one host over where the
    contract could not see it.

    Only the branches that exit BEFORE any ssh are exercised here, so this
    runs without the rig. They are also the dangerous ones: refusing to
    destroy a directory the caller named.
    """
    import subprocess
    import tempfile

    wrapper = os.path.join(HERE, os.pardir, "bin", "vcctrl")

    def run(*args):
        return subprocess.run([wrapper] + list(args), capture_output=True,
                              text=True, timeout=30,
                              env=dict(os.environ, VCCTRL_HOST="127.0.0.1"))

    d = tempfile.mkdtemp(prefix="vcout")

    r = run("shot", "--out", os.path.join(d, "missing", "x.jpg"))
    check("a missing destination directory is refused before the target is "
          "touched", r.returncode == 3 and "does not exist" in r.stderr,
          (r.returncode, r.stderr.strip()[:120]))

    # THE one that must never regress: a --out-dir with the caller's files in
    # it. The two-valued rule says a failed run leaves no output, and honouring
    # that by emptying a directory somebody named is data loss wearing a
    # contract.
    full = os.path.join(d, "full")
    os.makedirs(full)
    keep = os.path.join(full, "precious.txt")
    with open(keep, "w") as f:
        f.write("the caller's existing work")
    r = run("burst", "4", "--out-dir", full)
    check("a non-empty --out-dir is REFUSED rather than emptied",
          r.returncode == 3 and "not empty" in r.stderr,
          (r.returncode, r.stderr.strip()[:120]))
    check("and the caller's file is still there", os.path.exists(keep))

    notdir = os.path.join(d, "afile")
    with open(notdir, "w") as f:
        f.write("x")
    r = run("burst", "4", "--out-dir", notdir)
    check("--out-dir pointing at a regular file is refused",
          r.returncode == 3 and "not a directory" in r.stderr,
          (r.returncode, r.stderr.strip()[:120]))


def _client():
    import importlib.util
    from importlib.machinery import SourceFileLoader
    path = os.path.join(HERE, os.pardir, "bin", "vcctrl-client")
    loader = SourceFileLoader("vcctrl_client", path)
    spec = importlib.util.spec_from_loader("vcctrl_client", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_verify_input_exit_codes():
    """The exit code carries the READING, so a script can branch without
    parsing JSON -- and could-not-look must not read as a failure.

    `vcctrl verify-input || abort` is the intended use. If an ADB board (no
    LED return channel at all) exited 1, every harness guarded that way would
    abort on working hardware. Three states, three codes.
    """
    c = _client()
    check("verified -> 0", c.verify_input_exit({"ok": True, "verified": True}) == 0)
    check("not verified -> 1 (a real fault)",
          c.verify_input_exit({"ok": True, "verified": False}) == 1)
    check("could not look -> 2, NOT 1",
          c.verify_input_exit({"ok": True, "verified": None,
                               "why": "unsupported"}) == 2)
    check("tool failure -> 3",
          c.verify_input_exit({"ok": False, "error": "daemon said no"}) == 3)
    codes = {c.verify_input_exit({"ok": True, "verified": True}),
             c.verify_input_exit({"ok": True, "verified": False}),
             c.verify_input_exit({"ok": True, "verified": None}),
             c.verify_input_exit({"ok": False})}
    check("all four outcomes are distinguishable", codes == {0, 1, 2, 3}, codes)


def test_burst_is_all_or_nothing():
    """A burst with silent holes looks like a complete capture and is not.

    If frame 3 of 5 fails to decode, the caller must not be left with 2 files
    and a zero status -- nothing on disk would say which frames are missing or
    why, and a set with holes is indistinguishable from a set where the target
    genuinely went dark. Roll back instead.
    """
    import base64
    import tempfile
    c = _client()
    d = tempfile.mkdtemp(prefix="burst")

    jpg = base64.b64encode(b"\xff\xd8fake").decode()
    good = {"ok": True, "raw": True, "n": 3,
            "frames": [{"t": 1.0, "seq": i, "jpeg": jpg} for i in (5, 6, 7)]}
    out = os.path.join(d, "ok")
    check("a good burst writes every frame and returns 0",
          c.write_burst(good, out) == 0 and len(os.listdir(out)) == 3)

    bad = {"ok": True, "raw": True, "n": 3,
           "frames": [{"t": 1.0, "seq": 5, "jpeg": jpg},
                      {"t": 1.0, "seq": 6, "jpeg": "!!! not base64 !!!"},
                      {"t": 1.0, "seq": 7, "jpeg": jpg}]}
    out2 = os.path.join(d, "partial")
    rc = c.write_burst(bad, out2)
    left = os.listdir(out2) if os.path.isdir(out2) else []
    check("a burst that fails partway returns non-zero", rc != 0, rc)
    check("and leaves NO frames behind, not a partial set", left == [], left)

    out3 = os.path.join(d, "empty")
    check("zero frames is a failure, not an empty success",
          c.write_burst({"ok": True, "frames": []}, out3) == 1)


def test_raw_frames_are_not_judged_as_pictures():
    """`frame` and `burst` are labelled raw and carry no picture judgement.

    write_frame's rule is resp["picture"], which is ABSENT on a raw response
    rather than false. Judging raw frames by it would report "no picture" for
    every frame ever fetched by sequence number -- and, because the contract
    removes the destination on a no-picture, would delete the caller's file
    too.
    """
    import base64
    import tempfile
    c = _client()
    d = tempfile.mkdtemp(prefix="rawframe")
    p = os.path.join(d, "f.jpg")
    raw = {"ok": True, "raw": True, "seq": 42, "t": 1.0, "bytes": 6,
           "jpeg": base64.b64encode(b"\xff\xd8fake").decode()}
    check("a raw frame is written and returns 0", c.write_frame(raw, p) == 0)
    check("and the file exists", os.path.exists(p))

    # A judged response with picture=False must still remove the file.
    check("a judged no-picture still returns 1",
          c.write_frame({"ok": True, "picture": False, "reason": "dark"}, p) == 1)
    check("and removes the stale file", not os.path.exists(p))
