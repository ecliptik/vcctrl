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
    for name, (_g, _d, pair, _r) in T.THEMES.items():
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
    test_favicon_single_source()
    test_shot_out_contract()
    print("\n%s" % ("ALL PASS" if not FAILURES
                    else "FAILED: %s" % ", ".join(FAILURES)))
    sys.exit(1 if FAILURES else 0)
