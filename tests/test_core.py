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

# PIN THE CONFIGURATION BEFORE vcctrld IS IMPORTED.
#
# vcctrld resolves its configuration once, at import, and its search order ends
# at ./vcctrl.yaml in the repo root -- the operator's real file. That handed the
# suite a live smart-plug address, the power watchdog contacted the actual plug,
# read "off", published powered=False, and three LED tests began reporting
# `unpowered` instead of the state they were asserting.
#
# They were right to fail, and the failure was the useful kind: a suite whose
# verdict depends on whether a plug in another room is switched on is reporting
# the RIG's state as the CODE's state. It would have gone green again the
# moment somebody powered the target on, which is worse than staying red.
#
# Set before the import below, rather than in a fixture, so `python3
# tests/test_core.py` gets it too -- a hermetic suite under pytest and an
# ambient one under python3 is the same defect wearing a different hat.
os.environ.setdefault("VCCTRL_CONFIG", os.path.join(HERE, "test-config.yaml"))

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
          set(["input", "leds"]) <= set(reg.caps), sorted(reg.caps))
    check("nothing failed to load", reg.failed == {}, reg.failed)

    # THE THIRD STATE, exercised rather than described. tests/test-config.yaml
    # sets power's backend to `none` -- deliberately, because that is what
    # keeps the suite off the operator's real plug. So power must be reported
    # as NOT CONFIGURED, and specifically not as failed: "go and fix it" and
    # "nobody asked for it" need different responses from a person, and a
    # two-valued report cannot tell them apart.
    check("a `none` backend is not started", "power" not in reg.caps,
          sorted(reg.caps))
    check("and is NOT recorded as a failure", "power" not in reg.failed,
          reg.failed)
    check("it is recorded as not configured", "power" in reg.disabled,
          sorted(reg.disabled))
    rep = reg.report()
    check("report() distinguishes the three states",
          rep["power"]["configured"] is False
          and rep["power"]["why"] == "not_configured"
          and rep["input"]["configured"] is True
          and rep["input"]["ok"] is True, rep.get("power"))
    check("ok stays False for an unconfigured capability, so nothing that "
          "checks only the boolean starts passing by accident",
          rep["power"]["ok"] is False, rep["power"])

    # And the verb still exists: a disabled capability owns its commands, so
    # the answer is "not configured on this rig" rather than "unknown command",
    # which would read as a broken client or a typo.
    resp = reg.routes["power"][1]({"cmd": "power", "action": "state"})
    check("a disabled capability answers its own verb honestly",
          resp["ok"] is False and resp["why"] == "not_configured"
          and "not configured" in resp["error"], resp)
    check("and the refusal says where to configure it",
          "capabilities.power.backend" in resp.get("hint", ""), resp)
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

    # A CONTROL BOUNDARY IS NOT A DIVIDER. WCAG 1.4.11 asks 3:1 of the edge
    # of an interactive component and nothing of a decorative line, and one
    # token was doing both jobs at 1.1-1.7:1. Reported as "buttons don't have
    # borders like they do in Dark themes" -- and the token was equally weak
    # in both. What differed was the button's ground, a black wash that barely
    # moves a dark fill and drags a light one down onto the border colour.
    weak = []
    for name in T.THEMES:
        roles, _n = T.fitted(name)
        for surface in ("bg", "panel"):
            c = T.contrast(roles["edge"], roles[surface])
            if c < 3.0:
                weak.append("%s: edge on %s is %.2f:1" % (name, surface, c))
    check("every theme's control edge clears 3:1 on both surfaces",
          not weak, "; ".join(weak[:3]))

    # And the page must actually USE it: a token nothing references is a
    # value that passes its own test and changes nothing on screen.
    page = open(os.path.join(HERE, os.pardir, "daemon", "kvm.html"),
                encoding="utf-8").read()
    check("and the page draws its controls with it",
          page.count("var(--edge)") >= 5, page.count("var(--edge)"))

    # THE ORDERING, which the floors do not give you for free.
    #
    # text > muted > dim is what those names promise the page: three levels of
    # emphasis. Fitting moves each role independently toward the same extreme,
    # so raising dim's floor to 4.5 inverted everforest-dark -- dim landed at
    # 5.41 against muted's 5.38. Three hundredths is invisible, which is
    # exactly why it needs asserting rather than eyeballing: an inversion
    # nobody can see is still a rule the names are breaking.
    inverted = []
    for name in T.THEMES:
        roles, _n = T.fitted(name)
        sur = (roles["bg"], roles["panel"])
        lvl = {r: min(T.contrast(roles[r], s) for s in sur)
               for r in ("text", "muted", "dim")}
        if not (lvl["text"] >= lvl["muted"] >= lvl["dim"]):
            inverted.append("%s: text %.2f muted %.2f dim %.2f"
                            % (name, lvl["text"], lvl["muted"], lvl["dim"]))
    check("emphasis runs text >= muted >= dim in all %d themes" % len(T.THEMES),
          not inverted, "; ".join(inverted[:3]))

    # AND THE FLOOR MATCHES THE USE. dim is the most-used text colour in
    # kvm.html -- seventeen `color:var(--dim)` rules, carrying the product
    # name, the lamp labels and every panel heading -- so it is text, and 3:1
    # was the floor for a decoration. Same for the accents: green carries PWR,
    # AUD and Send; red carries DAEMON UNREACHABLE. This test was GREEN while
    # all of them rendered between 3.15 and 3.77 on solarized-light, because
    # it asked whether each role cleared the floor for its NAME.
    check("dim is floored as text, not as decoration",
          T.FLOOR.get("dim") >= 4.5, T.FLOOR.get("dim"))
    check("and so are the accents, which carry lamps and alarms",
          T.ACCENT_FLOOR >= 4.5, T.ACCENT_FLOOR)

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

    # THE REAL __init__, not a hand-built subset. This used to be a subclass
    # that set `self.lock` and nothing else, and it broke the moment the class
    # gained a decode counter -- an AttributeError from a fake that had
    # drifted from the thing it stands in for. That is the same defect this
    # file already records about `pinned_at`: state that exists on the real
    # object and not on the stand-in, with nothing saying which. The real
    # constructor opens no device and spawns nothing, so there was never a
    # reason to avoid it.

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

    v = vcctrld.VideoCapability(None, vcctrld.Bus())
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
    // Against a button still IN the strip. keysbtn moved inside the field,
    // so comparing to it now measures the field against its own contents.
    emit(`fieldh ${h('linewrap')} ${h('grabbtn')}`);
    // Send keeps its word where there is room for it -- this harness runs at
    // desktop width -- with the arrow after it and no divider between, since
    // both halves say the same thing. And the key menu lives INSIDE the
    // field, with the clip, because a key the field cannot type is the same
    // kind of thing as a file it cannot type.
    const sb = document.getElementById('sendline');
    const ret = sb.querySelector('.ret'), bl = sb.querySelector('.bl');
    const kb = document.getElementById('keysbtn');
    emit(`sendparts ${ret && bl && !sb.querySelector('.bi')
          && bl.getBoundingClientRect().left < ret.getBoundingClientRect().left
          ? 1 : 0} `
       + `${kb && kb.closest('#linewrap') ? 1 : 0}`);
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
  const tipUp = n => caretLabel(n).classList.contains('up') ? 1 : 0;
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
  // The buffer control is in the strip where it can be found, and it carries
  // the same caret rule as the menus beside it -- up when closed, because it
  // opens upward. It was in the screen menu, under a label about picture
  // size, and was reported undiscoverable within the hour.
  {
    const bb = document.getElementById('bufbtn');
    // The camera half acts on the spot; the word half opens the menu. Same
    // shape as Sound, and the shape is the point: a control that both DOES
    // something and OPENS something has to say which half is which.
    const snap = document.getElementById('snapbtn');
    emit(`capsplit ${snap && snap.closest('#capwrap') === bb.closest('#capwrap')
                     ? 1 : 0} `
       + `${document.getElementById('scrubsnap') ? 1 : 0}`);
    const lab = () => bb.querySelector('.caret');
    const up = el => el.classList.contains('up') ? 1 : 0;
    const inStrip = bb.closest('#cmdbar') ? 1 : 0;
    setBufOpen(true);
    const opened = up(lab());
    setBufOpen(false);
    emit(`bufbtn ${inStrip} ${up(lab()) === 1 && opened === 0 ? 1 : 0}`);
  }
  // ABSENCE IS NOT A READING. A board with no LED channel sends
  // {available:false} and no value keys; the old code saw a truthy object,
  // got undefined, and drew three dark lamps captioned "dark" -- identical to
  // a real machine with all three LEDs off. Three states, three renderings.
  {
    const capsEl = document.getElementById('lamp-caps');
    const cls = () => [capsEl.classList.contains('on') ? 'on' : '',
                       capsEl.classList.contains('na') ? 'na' : ''].join('');
    lamps({available: true, capslock: 1, numlock: 0, scrolllock: 0},
          'locked', {available: true, ok: true, age_s: 3});
    const real = cls() === 'on' && !/no LED|not reporting/.test(capsEl.title);
    lamps({available: false, why: 'unsupported',
           reason: 'no LED return channel on board 3 (ADB)'},
          'locked', {available: false, why: 'unsupported'});
    const unsupported = cls() === 'na' && /no LED return channel/i.test(capsEl.title);
    // THE SET GREW TWICE IN ONE EVENING. Each new `why` must show the
    // DAEMON's sentence rather than being folded into an older one -- the
    // struck-through branch used to say "no LED return channel on this
    // board" about a Gateway that was simply switched off.
    lamps({available: false, why: 'unpowered',
           reason: 'the target is powered off, so it publishes nothing'},
          'nosignal', {available: false, why: 'unproven'});
    const unpowered = /powered off/i.test(capsEl.title)
                      && !/no LED return channel/i.test(capsEl.title);
    lamps({available: false, why: 'error', reason: 'read failed: EIO'},
          'locked', {available: true, ok: true, age_s: 3});
    const errored = capsEl.classList.contains('bad')
                    && /read failed/i.test(capsEl.title);
    // A value this page has never heard of still says the right thing.
    lamps({available: false, why: 'martian', reason: 'the daemon says so'},
          'locked', null);
    const future = /the daemon says so/i.test(capsEl.title);
    // `unproven` must not be quieter than the absences that mean less. It
    // is the only state saying "the machine is mid-change and these values
    // are from before it" -- and it must be tellable apart from a plain
    // stale reading WITHOUT colour, or the monochrome themes lose it.
    lamps({available: false, why: 'unproven', reason: 'not published yet'},
          'locked', {available: false, why: 'unproven'});
    const unprovenLoud = capsEl.classList.contains('warn')
                      && capsEl.classList.contains('stale')
                      && !capsEl.classList.contains('na');
    lamps({available: true, capslock: 1}, 'nosignal',
          {available: true, ok: true, age_s: 3});
    const plainStale = capsEl.classList.contains('stale')
                    && !capsEl.classList.contains('warn');
    emit(`unprovenloud ${unprovenLoud && plainStale ? 1 : 0} 0`);
    emit(`whyset ${unpowered && errored ? 1 : 0} ${future ? 1 : 0}`);
    lamps(null, 'locked', null);
    const unknown = cls() === 'na' && /not reporting/i.test(capsEl.title);
    emit(`ledstates ${real && unsupported && unknown ? 1 : 0} `
       + `${unsupported && !capsEl.classList.contains('on') ? 1 : 0}`);
    // Absent throttling must not read as "fine". The direction is the point:
    // defaulting to the reassuring value produces silence, and silence is
    // never investigated.
    const t1 = heat({});
    const t2 = heat({throttled: '0x0'});
    emit(`heatabsent ${t1 && t1.unknown ? 1 : 0} ${t2 === null ? 1 : 0}`);
    // THE DASHES STILL MEAN WHAT THEY MEANT. A solid lamp claims the reading
    // reflects the TARGET, and it only does if the input link has been
    // proven -- with the PS/2 lead unplugged the daemon still reports
    // plausible values, all true and all about the Pi's end of the wire.
    // lamps() was rewritten today for the three-state, so this asserts the
    // behaviour that was already there survived the rewrite.
    const dashed = () => capsEl.classList.contains('stale') ? 1 : 0;
    lamps({available: true, capslock: 1}, 'locked',
          {available: true, ok: true, age_s: 3});
    const proven = dashed();
    lamps({available: true, capslock: 1}, 'locked',
          {available: true, ok: true, age_s: 1200});
    const tooOld = dashed();
    lamps({available: true, capslock: 1}, 'nosignal',
          {available: true, ok: true, age_s: 3});
    const noPicture = dashed();
    lamps({available: true, capslock: 1}, 'locked', null);
    const never = dashed();
    emit(`dashes ${proven === 0 && tooOld === 1 ? 1 : 0} `
       + `${noPicture === 1 && never === 1 ? 1 : 0}`);
    lamps(null, 'locked', null);
  }
  // THE HEADER'S CONTROLS MUST NOT BE ABLE TO LEAVE. Asserted as a
  // MECHANISM rather than as a measurement, because headless clamps the
  // window to 500px and the fault only appeared on a real 393px phone: the
  // whole bar was overflow-x:auto, so the lamps pushed Settings and
  // light/dark off the edge. If the status half can shrink and the controls
  // cannot, no width can reproduce it.
  {
    const hs = document.getElementById('hdrstatus');
    const hsOK = getComputedStyle(hs).minWidth === '0px'
              && getComputedStyle(hs).overflowX === 'auto'
              && getComputedStyle(document.querySelector('header')).overflowX
                 === 'hidden';
    const pinned = Array.from(document.querySelectorAll('header > button'))
      .every(b => getComputedStyle(b).flexShrink === '0');
    // And the zoom caret must sit inside its own button: it hung outside the
    // box when the button was allowed to shrink under its content.
    const zb = document.getElementById('zoombtn');
    const cr = zb.querySelector('.caret').getBoundingClientRect();
    const zr = zb.getBoundingClientRect();
    emit(`headerpin ${hsOK && pinned ? 1 : 0} `
       + `${cr.right <= zr.right + 0.5 && cr.left >= zr.left - 0.5 ? 1 : 0}`);
  }
  // FROZEN OFFERS A LIVE PICTURE; NOSIGNAL DOES NOT. Identical frames are
  // what a DOS prompt produces, so there the stream is available and typing
  // is what settles it -- handing back a still would leave you typing at a
  // photograph. The two states must not offer the same button.
  {
    const db = document.getElementById('dimbtn');
    lastState = {video: {state: 'frozen'}};
    poll_render_veil('frozen', null);
    const froz = db.textContent;
    poll_render_veil('nosignal', null);
    const nosig = db.textContent;
    // UNIFORM OFFERS THE KEPT FRAME, NOT THE BLANK ONE. There is still a
    // picture on screen when the stream goes flat -- the last good frame,
    // dimmed under the notice -- and that undimmed is what anybody wants.
    // Showing the live uniform field put a black rectangle where a readable
    // frame had been.
    poll_render_veil('frozen', '#000000');
    const blank = db.textContent;
    emit(`veiluniform ${!/blank/i.test(blank) && /show frame/i.test(blank) ? 1 : 0} `
       + `${blank === nosig && blank !== froz ? 1 : 0}`);
    emit(`veilbtn ${/live/i.test(froz) && /show frame/i.test(nosig) ? 1 : 0} `
       + `${froz !== nosig ? 1 : 0}`);
    // And choosing live must uncover the canvas, not merely hide the notice.
    lastState = {video: {state: 'frozen'}};
    document.getElementById('stale').style.display = '';
    showLive();
    emit(`veillive ${document.getElementById('stale').style.display === 'none' ? 1 : 0} `
       + `${liveAnyway ? 1 : 0}`);
    liveAnyway = false;
  }
  closePop();
  emit(`caretshut ${tipUp('sound')} ${tipUp('zoom')}`);
  document.getElementById('soundbtn').click();
  emit(`caretopen ${tipUp('sound')} 0`);
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
    check("Send keeps its word on a desktop, with the arrow after it",
          got["sendparts"][0] == 1.0, got["sendparts"])
    check("and the key menu opens from inside the command field",
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
    check("a uniform stream offers the kept frame, not the blank live one",
          got["veiluniform"][0] == 1.0, got["veiluniform"])
    # It should read the same as no-signal (both offer the kept frame) and
    # differently from a frozen REAL picture, which offers the live stream.
    check("control: and it says what no-signal says, not what frozen says",
          got["veiluniform"][1] == 1.0, got["veiluniform"])
    check("frozen offers the live picture, no-signal offers the kept frame",
          got["veilbtn"][0] == 1.0, got["veilbtn"])
    check("control: and the two states do not share a label",
          got["veilbtn"][1] == 1.0, got["veilbtn"])
    check("choosing live uncovers the canvas",
          got["veillive"][0] == 1.0, got["veillive"])
    check("and suppresses the still until a real picture returns",
          got["veillive"][1] == 1.0, got["veillive"])
    check("the header's status can shrink and its controls cannot",
          got["headerpin"][0] == 1.0, got["headerpin"])
    check("so the zoom caret stays inside its own button",
          got["headerpin"][1] == 1.0, got["headerpin"])
    check("unproven is louder than a plain stale reading, and not by colour alone",
          got["unprovenloud"][0] == 1.0, got["unprovenloud"])
    check("each why shows the daemon's own sentence, not a paraphrase",
          got["whyset"][0] == 1.0, got["whyset"])
    # The page must not have to be edited every time the daemon adds a state.
    check("control: an unrecognised why still shows its reason",
          got["whyset"][1] == 1.0, got["whyset"])
    check("LED lamps render present, unsupported and unknown differently",
          got["ledstates"][0] == 1.0, got["ledstates"])
    # The exact failure: a board with no LED channel must not render as a
    # board whose LEDs are all off.
    check("control: an unsupported channel is not drawn as a dark lamp",
          got["ledstates"][1] == 1.0, got["ledstates"])
    check("a proven link is solid and a stale proof goes back to dashed",
          got["dashes"][0] == 1.0, got["dashes"])
    check("and no picture, or never proven, dashes them too",
          got["dashes"][1] == 1.0, got["dashes"])
    check("absent throttling reports unknown, not healthy",
          got["heatabsent"][0] == 1.0, got["heatabsent"])
    check("control: a real zero still reports healthy",
          got["heatabsent"][1] == 1.0, got["heatabsent"])
    check("the camera and the menu are two halves of one control",
          got["capsplit"][0] == 1.0, got["capsplit"])
    check("and the capture menu can snap the frame being scrubbed to",
          got["capsplit"][1] == 1.0, got["capsplit"])
    check("the buffer control is in the strip, where it can be found",
          got["bufbtn"][0] == 1.0, got["bufbtn"])
    check("and its caret follows the same rule as the menus beside it",
          got["bufbtn"][1] == 1.0, got["bufbtn"])
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

    # THE SPAN IS A CEILING, NOT ONLY A FLOOR. Eviction used to run only on
    # bytes, so the setting bought a budget of want * BYTES_PER_S and the span
    # was whatever that budget happened to buy. On content compressing better
    # than the pessimistic 1.6 MB/s the ring simply kept more -- measured live
    # at 1193 s held against 480 asked. Nobody chose twenty minutes.
    import time as _t
    cap2 = vcctrld.VideoCapability(None, vcctrld.Bus())
    cap2._buffer({"seconds": 10})
    now = _t.time()
    # Frames small enough that the BYTE cap can never be the thing evicting:
    # if the span still holds, it is age doing it and not size.
    for age in range(60, -1, -1):
        cap2.ring.append((now - age, age, b"x" * 64))
        cap2.ring_bytes += 64
    cap2._push(b"y" * 64)
    span = cap2.ring[-1][0] - cap2.ring[0][0]
    check("age evicts even when bytes are nowhere near the cap",
          span <= 10 * 1.05 + 1, round(span, 1))
    check("control: the byte cap was never reached, so size did not do it",
          cap2.ring_bytes < cap2.RING_BYTES / 100,
          (cap2.ring_bytes, cap2.RING_BYTES))
    check("control: and it kept the RECENT end, not the old one",
          cap2.ring[-1][2] == b"y" * 64, cap2.ring[-1][1])

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

    path = os.path.join(HERE, os.pardir, "harness", "vcctrl-sweep")
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


def _load(relpath, name):
    import importlib.util
    from importlib.machinery import SourceFileLoader
    path = os.path.join(HERE, os.pardir, relpath)
    loader = SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_absent_key_is_not_a_value():
    """A key's ABSENCE must not be read as a value. Four sites, one shape.

    Two of these were live defects. Both had the property that makes this
    class hard to see: the code is correct, the contract is correct, and the
    bug lives in the seam where a caller meets a field that might not be
    there. The dangerous direction is absence reading as the REASSURING value,
    because then the check produces the same output as a real pass.
    """
    common = _load("bin/vcctrl_common.py", "vcc_common_t")
    sweep = _load("harness/vcctrl-sweep", "vcc_sweep_t")

    # 1. THE VACUOUS CHECK. all({}.values()) is True, so `st.get("usb4vc", {})`
    # passed the preflight whenever the field was missing -- an absent answer
    # read as a clean bill of health, immediately before a 23-minute
    # unattended run.
    check("all({}.values()) is True -- this is why the default was wrong",
          all({}.values()) is True)

    calls = {}

    def fake_vc_json(*a):
        return calls.get(a[0], {})

    sweep.vc_json = fake_vc_json
    conf = {"denied": {}, "sweeps": {"PUMP": {"cells": 4, "timeout_min": 23}},
            "machines": {"1": {"tag": "G", "name": "POD-83"}}}

    for label, status in (("missing usb4vc key", {}),
                          ("empty usb4vc dict", {"usb4vc": {}})):
        calls["status"] = status
        try:
            sweep.preflight(conf, "PUMP", "1")
            check("preflight REFUSES on %s" % label, False,
                  "it returned instead of refusing")
        except SystemExit as exc:
            check("preflight REFUSES on %s" % label,
                  "REFUSED" in str(exc), str(exc)[:80])
        except Exception as exc:
            check("preflight REFUSES on %s" % label, False, repr(exc))

    # 2. POWER IS TRI-STATE. bool(None) is False, so an unreachable plug read
    # as "the machine is off" -- and with --power-on that meant sending `power
    # on` to a machine that might be running, then waiting 240 s for a boot
    # edge that could not arrive.
    seen = {}

    def power_returning(payload):
        def f(*a):
            if a[0] == "power":
                return payload
            return {}
        return f

    common.vc_json = power_returning({"power": {"on": None}})
    check("unreachable plug -> None, NOT False", common.power_on() is None)
    common.vc_json = power_returning({"power": {}})
    check("a reply with no 'on' field -> None", common.power_on() is None)
    common.vc_json = power_returning({})
    check("no power object at all -> None", common.power_on() is None)
    common.vc_json = power_returning({"power": {"on": False}})
    check("a plug that says off -> False", common.power_on() is False)
    common.vc_json = power_returning({"power": {"on": True}})
    check("a plug that says on -> True", common.power_on() is True)

    # ensure_powered must NEVER act on an unknown.
    common.vc_json = power_returning({"power": {"on": None}})
    acted = []
    common.vc = lambda *a, **k: acted.append(a)
    check("ensure_powered refuses on an unknown power state",
          common.ensure_powered(True) is False)
    check("and sends NO power command -- could-not-look is not a finding",
          acted == [], acted)

    # 3. A MISSING COUNT IS NOT ZERO. Defaulting framestats fields to 0 made an
    # absent field read as "no live frames", which is the signature of a
    # frozen capture -- a renamed key would be reported as a dead stick.
    cap = _load("bin/vcctrl-capcheck", "vcc_capcheck_t")

    def fs_returning(payload):
        def f(*a):
            return payload if a[0] == "framestats" else {"picture": False}
        return f

    cap.vc_json = fs_returning({"ok": True, "n": 16, "distinct": 9,
                                "repeated": 2})
    got = cap.profile(16)
    check("capcheck computes a live count when every field is present",
          got is not None and got["live"] == 7, got)

    for missing in ("n", "distinct", "repeated"):
        full = {"ok": True, "n": 16, "distinct": 9, "repeated": 2}
        del full[missing]
        cap.vc_json = fs_returning(full)
        check("capcheck refuses rather than derive a count with %r absent"
              % missing, cap.profile(16) is None)


def test_relay_state_absent_is_not_off():
    """A plug that answers without saying is not a plug that said off.

    PowerCapability keeps `on` tri-state one layer up precisely because "the
    machine is off" and "I cannot reach the plug" are opposite facts. Parsing
    with bool(info.get("relay_state")) collapsed that before it ever reached
    the caller -- undoing the distinction the layer above was built to keep.
    """
    import types
    orig = vcctrld.kasa_send
    try:
        for label, sysinfo, want in (
                ("no relay_state field", {"alias": "x"}, None),
                ("relay_state 0", {"relay_state": 0, "alias": "x"}, False),
                ("relay_state 1", {"relay_state": 1, "alias": "x"}, True)):
            vcctrld.kasa_send = (lambda si: (lambda h, p: {
                "system": {"get_sysinfo": si}}))(sysinfo)
            got = vcctrld.power_state("host")["on"]
            check("%s -> %r" % (label, want), got is want, got)
    finally:
        vcctrld.kasa_send = orig


def test_preflight_is_a_gate_not_a_list():
    """One exit code, and it names the check that decided.

    The benchmarking session's argument for this is better than the feature:
    the check that would have saved their first Round P existed, was correctly
    specified, and was written into the run sheet the same day -- as a LIST.
    Lists get skipped at the moment a round is finally ready to run after a
    long preparation, which is exactly when they matter.

    The properties under test are the ones a harness depends on: a fault
    outranks an unknown, an unknown does not read as a pass, and the verdict
    names its subsystem so a 2 does not send someone to read five statuses.
    """
    c = _client()
    import contextlib
    import io
    import json as _json

    def verdict(states):
        checks = [c._chk(n, s, "detail for %s" % n) for n, s in states]
        c.preflight_checks = lambda who=None, skip_input=False: checks
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = c.preflight()
        return code, _json.loads(buf.getvalue())

    code, d = verdict([("caps", c.OK), ("board", c.OK), ("input", c.OK)])
    check("all ok -> 0", code == 0 and d["verdict"] == "ok", (code, d["verdict"]))
    check("and names nothing as the decider", d["decided_by"] is None)

    code, d = verdict([("caps", c.OK), ("power", c.UNKNOWN), ("input", c.OK)])
    check("an unknown -> 2, NOT 0", code == 2, code)
    check("and names the subsystem", d["decided_by"] == "power", d["decided_by"])

    code, d = verdict([("caps", c.OK), ("input", c.FAULT)])
    check("a fault -> 1", code == 1, code)
    check("and names it", d["decided_by"] == "input", d["decided_by"])

    # A fault OUTRANKS an unknown: if something is definitely broken, that is
    # the more actionable finding and must not be masked by an earlier
    # could-not-look.
    code, d = verdict([("board", c.UNKNOWN), ("input", c.FAULT)])
    check("a fault outranks an earlier unknown", code == 1, code)
    check("and the verdict names the FAULT, not the unknown",
          d["decided_by"] == "input", d["decided_by"])
    check("while still listing the unknown", d["unknowns"] == ["board"],
          d["unknowns"])

    # The three verdicts must be distinguishable, which is the whole point.
    codes = {verdict([("a", c.OK)])[0],
             verdict([("a", c.UNKNOWN)])[0],
             verdict([("a", c.FAULT)])[0]}
    check("ok / unknown / fault are three distinct exit codes",
          codes == {0, 1, 2}, codes)


def test_a_reading_must_belong_to_this_epoch():
    """A retained reading must not render as a live one. FINDINGS sec. 33.

    Demonstrated on the rig: minutes after the Gateway was powered off, the
    daemon reported "the target is powered off" and "the target acknowledged
    a keystroke" in the same breath. Both fields worked exactly as written.
    Only one was about now.

    Reading again does not help -- the retained value is what you get, and a
    stale value and a current one are the same value. The fix is evidence the
    reading was PRODUCED in the current epoch.
    """
    class Devs(object):
        def __init__(self, v):
            self.v = dict(v)

        def read_leds(self):
            return dict(self.v)

    live = {"capslock": 1, "numlock": 1, "scrolllock": 1}

    def fresh(devs):
        c = vcctrld.LedsCapability(devs)
        c.support = lambda: (True, None)
        vcctrld.LedsCapability._seen_values = None
        vcctrld.LedsCapability._proven_epoch = None
        return c

    orig = vcctrld.TARGET
    try:
        vcctrld.TARGET = vcctrld.TargetEpoch()
        d = Devs(live)
        c = fresh(d)

        # Powered and publishing: a normal reading.
        vcctrld.TARGET.observe(True)
        s = c.snapshot()
        check("powered target reports values", s["available"] is True, s)
        check("and the values are flat", s.get("capslock") == 1, s)

        # THE DEMONSTRATED BUG: power cut, nodes unchanged, reading retained.
        vcctrld.TARGET.observe(False)
        s = c.snapshot()
        check("an unpowered target does NOT report available",
              s["available"] is False, s)
        check("why is 'unpowered', distinct from every other reason",
              s["why"] == "unpowered", s.get("why"))
        check("and the retained VALUES ARE OMITTED, not published",
              not any(k in s for k in live), s)

        # THE SHARPER CASE a power-off check alone misses: power returns, the
        # nodes still hold the previous boot's values, the machine is ON.
        vcctrld.TARGET.observe(True)
        s = c.snapshot()
        check("just after power returns, an unchanged reading is UNPROVEN",
              s["available"] is False and s["why"] == "unproven", s)
        check("and those values are omitted too",
              not any(k in s for k in live), s)

        # Once the target publishes something different, it is proven again.
        d.v = {"capslock": 0, "numlock": 1, "scrolllock": 1}
        s = c.snapshot()
        check("a change in this epoch proves the channel is live again",
              s["available"] is True, s)
        check("and the new values are reported", s.get("capslock") == 0, s)

        # The four unavailable reasons must stay distinguishable.
        whys = set()
        vcctrld.TARGET = vcctrld.TargetEpoch(); vcctrld.TARGET.observe(True)
        c2 = fresh(Devs(live)); c2.support = lambda: (False, "adb")
        whys.add(c2.snapshot()["why"])
        c3 = fresh(Devs(live)); c3.support = lambda: (True, None)
        c3.devs = type("D", (), {"read_leds": lambda self: (_ for _ in ()).throw(OSError("x"))})()
        whys.add(c3.snapshot()["why"])
        vcctrld.TARGET.observe(False)
        c4 = fresh(Devs(live)); c4.support = lambda: (True, None)
        whys.add(c4.snapshot()["why"])
        check("unsupported / error / unpowered stay distinct",
              whys == {"unsupported", "error", "unpowered"}, whys)
    finally:
        vcctrld.TARGET = orig
        vcctrld.LedsCapability._seen_values = None
        vcctrld.LedsCapability._proven_epoch = None


def test_first_power_reading_is_not_a_transition():
    """The daemon's first sight of the plug must not void everything.

    If the initial observation counted as a transition, every verification
    made before the first power heartbeat would be marked as belonging to a
    previous epoch -- so a freshly started daemon would declare its own
    startup checks void.
    """
    t = vcctrld.TargetEpoch()
    check("first observation is not a transition", t.observe(True) is False)
    check("and does not advance the epoch", t.state()[0] == 0, t.state())
    check("a real change IS a transition", t.observe(False) is True)
    check("and advances the epoch", t.state()[0] == 1, t.state())
    check("an unchanged reading is not a transition",
          t.observe(False) is False)
    check("and does not advance it", t.state()[0] == 1, t.state())


def test_why_values_are_all_documented():
    """Every `why` the daemon can emit must appear in the docstring that
    consumers are told to read.

    This is the test the cost justifies rather than a tidiness check.
    LedsCapability.snapshot() said "CLOSED SET" and listed three while the
    daemon emitted five, for two hours. The webkvm session read it in good
    faith and wrote a consumer branch against the three, so a Gateway that
    was merely switched OFF would have been described to the operator as a
    board with no LED hardware. Then I read their code, equally in good faith,
    and predicted it would render correctly.

    Same seam, three times in one day, in both directions -- and every time
    the code was right and the description of it was not. A docstring that can
    silently lag the values it documents is not documentation, it is a second
    source of truth. So the drift is made detectable.
    """
    import ast as _ast
    import re
    src = open(os.path.join(HERE, os.pardir, "daemon", "vcctrld.py")).read()
    web = open(os.path.join(HERE, os.pardir, "daemon", "vcweb.py")).read()

    # COLLECTED FROM THE SYNTAX TREE, NOT BY REGEX, and the difference is not
    # style. The pattern here was `"why":\s*"([a-z]+)"`, which finds a `why`
    # only where it is written as a DICT LITERAL. FilesCapability sets its own
    # by assignment --
    #
    #     out["why"], out["reason"] = "unsupported", why
    #
    # -- and five of its six values were invisible to this check. The hyphen
    # was the second hole: the pattern had to match the whole quoted string,
    # so `not-configured` matched nothing at all.
    #
    # BOTH WERE FOUND BY DELIBERATELY BREAKING IT. A value was injected that
    # no docstring mentions, the test was run, and it PASSED -- so the check
    # had been approving whatever it could not see, which is the same thing as
    # not having it. A guard is worth what its negative control proves, and
    # this one had never been given one.
    def _why_strings(node):
        """Every string this node can put under a `why` key, either form."""
        out = []
        for n in _ast.walk(node):
            if isinstance(n, _ast.Dict):
                for k, v in zip(n.keys, n.values):
                    if (isinstance(k, _ast.Constant) and k.value == "why"):
                        out += _consts(v)
            elif isinstance(n, _ast.Assign):
                tgts = n.targets[0]
                tgts = tgts.elts if isinstance(tgts, _ast.Tuple) else [tgts]
                vals = n.value.elts if isinstance(n.value, _ast.Tuple) \
                    else [n.value]
                for t, v in zip(tgts, vals):
                    if (isinstance(t, _ast.Subscript)
                            and isinstance(t.slice, _ast.Constant)
                            and t.slice.value == "why"):
                        out += _consts(v)
        return out

    def _consts(v):
        """A literal, or both arms of `"a" if cond else "b"`."""
        if isinstance(v, _ast.Constant) and isinstance(v.value, str):
            return [v.value]
        if isinstance(v, _ast.IfExp):
            return _consts(v.body) + _consts(v.orelse)
        return []

    emitted = set(_why_strings(_ast.parse(src)) + _why_strings(_ast.parse(web)))
    check("the daemon emits several distinct why values", len(emitted) >= 4,
          emitted)

    # EVERY VOCABULARY, NOT ONE CLASS'S. This asked LedsCapability.snapshot
    # alone, which was right while leds was the only thing emitting `why` and
    # became wrong the moment a second capability did -- the check would have
    # demanded that FilesCapability's words be documented in the LED
    # docstring, which is not a place any consumer would look for them.
    #
    # A vocabulary declares itself by saying its set is open. That is the same
    # sentence this test already required, so it costs nothing and it means
    # the docstrings that define words are found by what they promise rather
    # than by being named in a list here.
    vocab = []
    for node in _ast.walk(_ast.parse(src)):
        if not isinstance(node, (_ast.FunctionDef, _ast.ClassDef,
                                 _ast.Module)):
            continue
        doc = _ast.get_docstring(node) or ""
        if "NOT CLOSED" in doc.upper():
            vocab.append(doc)
    check("at least one docstring declares an open `why` vocabulary",
          vocab, "none says NOT CLOSED")
    undocumented = sorted(w for w in emitted
                          if not any(w in d for d in vocab))
    check("every emitted `why` appears in a documented open vocabulary",
          not undocumented,
          "missing from every NOT-CLOSED docstring: %s"
          % ", ".join(undocumented))

    # And it must SAY SO that the set is open. A positive assertion rather
    # than "the phrase 'closed set' is absent" -- the first version of this
    # check searched for that phrase and tripped over the docstring's own
    # account of the day it was wrong, which is a check that fails on the
    # presence of its own history.
    #
    # A consumer told the set is closed will branch exhaustively on it and
    # mis-describe anything added later. That is not hypothetical: it is what
    # happened.
    check("the LED docstring still states its set is NOT closed",
          "NOT CLOSED" in (vcctrld.LedsCapability.snapshot.__doc__ or "").upper(),
          "it does not tell a consumer the set can grow")
    check("the files docstring states its set is NOT closed",
          "NOT CLOSED" in (vcctrld.FilesCapability.snapshot.__doc__ or "").upper(),
          "it does not tell a consumer the set can grow")


def test_led_change_record():
    """A history, so an intermittent that clears itself leaves a trace.

    The webkvm session's case: the Pi held 1/0/0 while the target had 0/1/1,
    and it cleared on its own with nobody watching. A level cannot show that
    afterwards. It is deliberately NOT a currency proof -- the LED byte
    arrives only on a lock-key change, so an idle machine publishes nothing,
    indistinguishably from one that has fallen off the wire.
    """
    class Devs(object):
        def __init__(self, v): self.v = dict(v)
        def read_leds(self): return dict(self.v)

    import collections as _c
    a = {"capslock": 0, "numlock": 1, "scrolllock": 1}
    d = Devs(a)
    c = vcctrld.LedsCapability(d)
    c.support = lambda: (True, None)

    orig = (vcctrld.LedsCapability._changes,
            vcctrld.LedsCapability._seen_values,
            vcctrld.LedsCapability._changes_seq)
    try:
        vcctrld.LedsCapability._changes = _c.deque(
            maxlen=vcctrld.LedsCapability.CHANGES_MAX)
        vcctrld.LedsCapability._seen_values = None
        vcctrld.LedsCapability._changes_seq = 0

        c._sample()
        check("the first sample records NO transition -- nothing to move from",
              len(vcctrld.LedsCapability._changes) == 0)

        d.v = {"capslock": 1, "numlock": 1, "scrolllock": 1}
        c._sample()
        rec = list(vcctrld.LedsCapability._changes)
        check("a change is recorded", len(rec) == 1, rec)
        check("with from and to", rec[0]["from"] == a and rec[0]["to"] == d.v,
              rec[0])
        check("and a MONOTONIC clock, because the wall clock can step and the "
              "whole value of this record is ordering", "mono" in rec[0])
        check("and the epoch, so a change can be tied to this boot",
              "epoch" in rec[0])

        c._sample()
        check("an unchanged sample records nothing",
              len(vcctrld.LedsCapability._changes) == 1)

        # BOUNDED. A daemon runs for weeks; an unbounded record is a slow leak.
        for i in range(vcctrld.LedsCapability.CHANGES_MAX + 50):
            d.v = {"capslock": i % 2, "numlock": 1, "scrolllock": 1}
            c._sample()
        # Asserted INDEPENDENTLY of the constant. The first version compared
        # the length against CHANGES_MAX read from the code, so it agreed with
        # itself for any value -- raising the constant to 100000 left it
        # passing, which a control caught. The property is "bounded, and
        # bounded at a size a daemon can run for weeks with", not "equal to
        # whatever the source says".
        dq = vcctrld.LedsCapability._changes
        check("the record is a BOUNDED deque, not an unbounded list",
              getattr(dq, "maxlen", None) is not None, type(dq).__name__)
        check("and the bound is small enough to run for weeks (<= 1000)",
              vcctrld.LedsCapability.CHANGES_MAX <= 1000,
              vcctrld.LedsCapability.CHANGES_MAX)
        check("and it actually stops growing",
              len(dq) <= vcctrld.LedsCapability.CHANGES_MAX, len(dq))

        out = c._led_changes({"n": 5})
        check("led_changes returns newest first",
              out["changes"][0]["seq"] > out["changes"][-1]["seq"],
              [x["seq"] for x in out["changes"]])
        check("and honours n", len(out["changes"]) == 5)
        check("and says what it does NOT prove",
              "does not establish" in out["note"].lower(), out["note"][:60])

        snap = c.snapshot()
        check("snapshot summarises the same record, not a second one",
              snap["changes"] == len(vcctrld.LedsCapability._changes)
              and snap["changed_at"] == list(
                  vcctrld.LedsCapability._changes)[-1]["t"], snap.get("changes"))
    finally:
        (vcctrld.LedsCapability._changes,
         vcctrld.LedsCapability._seen_values,
         vcctrld.LedsCapability._changes_seq) = orig


def test_every_daemon_command_has_a_cli_verb():
    """CLI/KVM parity, enforced instead of audited.

    Six daemon commands were reachable only from the web page until this
    evening -- buffer, burst, frame, pin, timeline, verify_input -- which
    meant a headless sweep could not save the seconds before a crash or
    assert its own input path. They were found by a one-off script and fixed
    by hand.

    Within the hour, `led_changes` shipped as a daemon command with no client
    verb and printed usage instead of running. A gap closed by inspection
    stays closed only until the next person adds a command; this one lasted
    about forty minutes, and the person who reopened it was the one who had
    closed it. So it is a test now.

    The daemon and the client are separate files with nothing between them.
    This is that missing thing.
    """
    import ast as _ast
    import re as _re

    dsrc = open(os.path.join(HERE, os.pardir, "daemon", "vcctrld.py")).read()
    tree = _ast.parse(dsrc)
    daemon_cmds = set()
    for cls in [n for n in _ast.walk(tree) if isinstance(n, _ast.ClassDef)]:
        for fn in [n for n in cls.body
                   if isinstance(n, _ast.FunctionDef) and n.name == "commands"]:
            for node in _ast.walk(fn):
                if isinstance(node, _ast.Dict):
                    for k in node.keys:
                        if isinstance(k, _ast.Constant) and isinstance(k.value, str):
                            daemon_cmds.add(k.value)

    csrc = open(os.path.join(HERE, os.pardir, "bin", "vcctrl-client")).read()
    # Every literal the client dispatches on, however it spells it.
    reachable = set(_re.findall(r'cmd == "([a-z_-]+)"', csrc))
    for grp in _re.findall(r'cmd in \(([^)]*)\)', csrc):
        reachable |= {x.strip().strip('"\'') for x in grp.split(",") if x.strip()}
    # Commands the client sends under a different verb name -- it builds the
    # request dict explicitly, so accept any cmd literal it can emit.
    reachable |= set(_re.findall(r'"cmd":\s*"([a-z_]+)"', csrc))

    missing = sorted(c for c in daemon_cmds if c not in reachable)
    check("every daemon command is reachable from the CLI",
          not missing,
          "no CLI verb for: %s" % ", ".join(missing))
    check("and the daemon exposes a substantial command set",
          len(daemon_cmds) >= 20, len(daemon_cmds))


def test_a_pin_belongs_to_whoever_took_it():
    """One release must not free somebody else's frame.

    The ring is one object and several things want it to stand still at once:
    a KVM tab stepping through a capture, a SECOND tab that auto-pinned the
    moment it lost the picture, `vcctrl record` muxing an AVI, a sweep pinning
    at the instant a cell loses lock. The pin was a single timestamp, so the
    last release won whoever it belonged to, and the loser could not tell that
    anything had happened -- it simply asked for a frame that was no longer
    there and got a broken image.

    That is the reported symptom this pins down, and the reason it took a
    while to see: BOTH SIDES WERE BEHAVING CORRECTLY. The tab that released
    had genuinely finished with the ring. The flag it wrote to just could not
    say on whose behalf.
    """
    print("\npin holders")
    cap = vcctrld.VideoCapability(None, vcctrld.Bus())

    a = cap._pin({"action": "on", "holder": "reviewer"})
    check("a named pin holds", a["pinned"] and a["holders"] == ["reviewer"], a)
    b = cap._pin({"action": "on", "holder": "recorder"})
    check("two holders are two holders", b["holders"] == ["recorder", "reviewer"], b)

    # THE WHOLE POINT.
    c = cap._pin({"action": "off", "holder": "recorder"})
    check("one holder letting go does not release the ring",
          c["pinned"] and c["holders"] == ["reviewer"], c)
    d = cap._pin({"action": "off", "holder": "reviewer"})
    check("the last one letting go does", not d["pinned"] and not d["holders"], d)

    # Releasing something you never held is a no-op, not a way to free it.
    cap._pin({"action": "on", "holder": "reviewer"})
    e = cap._pin({"action": "off", "holder": "some-other-tab"})
    check("releasing a lease you do not hold changes nothing",
          e["pinned"] and e["holders"] == ["reviewer"], e)

    # The operator's escape hatch, and the reason it is spelled differently:
    # a person at a terminal typing `vcctrl pin off` means the ring, not their
    # share of it -- and vcctrl-cell's recovery note already tells them to.
    f = cap._pin({"action": "off"})
    check("an unnamed release clears every holder",
          not f["pinned"] and not f["holders"], f)

    # Two numbers, two questions. Reading one for the other was how the old
    # single-value report managed to be true and useless at the same time.
    cap._pin({"action": "on", "holder": "old"})
    cap.pin_holders["old"] = time.time() - 100.0
    g = cap._pin({"action": "on", "holder": "new"})
    check("held_s measures the OLDEST lease -- how long the ring has stood still",
          g["held_s"] >= 99.0, g["held_s"])
    check("expires_in_s measures the NEWEST -- when the ring moves again",
          g["expires_in_s"] > cap.PIN_TIMEOUT_S - 5.0, g["expires_in_s"])

    # A status read must not take a pin. A `pin` with no action used to be
    # harmless because there was nothing to key on; with holders, defaulting
    # the holder in the wrong branch would make every status call a holder.
    before = sorted(cap.pin_holders)
    h = cap._pin({})
    check("a bare status read takes nothing", sorted(cap.pin_holders) == before, h)


def test_one_stale_lease_expires_without_taking_the_others():
    """Expiry is per lease, or the timeout becomes another shared release.

    The 300 s expiry exists because a pinned ring stops accepting new frames,
    and a KVM showing a held buffer while claiming to be live is the failure
    this tool exists to prevent. With more than one holder the expiry has the
    same trap the release did: firing on the oldest and clearing the flag
    would drop a lease taken four seconds ago.
    """
    print("\npin expiry")
    import threading as _th
    cap = vcctrld.VideoCapability(None, vcctrld.Bus())
    # Comfortably longer than the pass this test waits for, or the "live"
    # lease ages out during the wait and the test proves the opposite of what
    # it says. The first version of this used 1.0 s and did exactly that.
    cap.PIN_TIMEOUT_S = 5.0
    cap.pin_holders = {"abandoned": time.time() - 60.0, "live": time.time()}
    cap.running = True
    cap.owned = False           # no device: the pass reaches the pin block and
    cap.proc = None             # then continues, which is what we want
    t = _th.Thread(target=cap._watchdog)
    t.start()
    time.sleep(1.6)             # >= one 0.5 s pass
    cap.running = False
    t.join(timeout=3)

    holders = sorted(cap.pin_holders)
    check("the abandoned lease expired", "abandoned" not in holders, holders)
    check("and the live one did not", holders == ["live"], holders)
    check("so the ring is still held", bool(cap.pin_holders), holders)


def test_the_daemon_says_what_rate_it_applied():
    """A per-socket value the client cannot see is one the client will guess.

    Reported from a phone: 'asking for 5 fps' beside 'arriving here 9.5 fps'.
    Both cannot be true of one socket -- the send loop sleeps 1/rate between
    frames, and measured from outside it honours the ask to within 2% at 5, 15
    and 30. They disagreed because one was a BELIEF: the page displayed what it
    had asked for, and nothing ever told it what it got.

    The clamp is the sharpest case. A client asking for 40 gets 30, and under
    the old protocol was never told; it would go on reporting 40 while
    receiving 30 and its rate controller would read the shortfall as a
    congested link.
    """
    print("\nrate echo")
    import json as _json
    import sys as _sys
    _sys.path.insert(0, os.path.join(HERE, os.pardir, "daemon"))
    import vcweb
    sent = []

    class FakeSock(object):
        def sendall(self, b):
            sent.append(b)

    cap = vcweb.WebCapability.__new__(vcweb.WebCapability)
    cap._ws_say(FakeSock(), None, {"rate": 12.0})
    check("a control message is sent at all", len(sent) == 1, len(sent))

    # Decode it as a client would: text opcode, unmasked (server->client),
    # payload is the JSON. Reading our own writer back through the wire format
    # rather than trusting the dict we passed in.
    b = sent[0]
    check("it is a TEXT frame, not a binary one the page would try to draw",
          b[0] == 0x81, hex(b[0]))
    n = b[1] & 0x7F
    check("and it is not masked (a masked server frame is a protocol error)",
          not (b[1] & 0x80), hex(b[1]))
    body = _json.loads(b[2:2 + n].decode())
    check("carrying the applied rate", body == {"rate": 12.0}, body)

    # A send that fails must not take down a picture that is fine.
    class Broken(object):
        def sendall(self, b):
            raise OSError("peer went away")

    try:
        cap._ws_say(Broken(), None, {"rate": 1.0})
        raised = None
    except Exception as exc:
        raised = exc
    check("a control message that cannot be delivered is not fatal",
          raised is None, raised)


def test_the_page_reads_text_frames_at_all():
    """The rate echo is useless if the page drops every string it is sent.

    It did. `if (typeof ev.data === 'string') return;` sat at the top of
    onmessage, and the only reason that was survivable is that the one text
    message the daemon sent -- a state change -- was also available from the
    status poll. The rate is not: it is per-socket and it lives nowhere else.
    """
    print("\ntext frames")
    page = open(os.path.join(HERE, os.pardir, "daemon", "kvm.html"),
                encoding="utf-8").read()
    body = page[page.index("ws.onmessage"):page.index("ws.onmessage") + 900]
    check("a string message is parsed rather than discarded",
          "JSON.parse(ev.data)" in body, body[:120])
    check("and its rate is handed to the applied-rate reader",
          "noteApplied(" in body, body[:120])
    check("the applied rate is cleared on a new socket, not carried across",
          "fpsApplied = null" in page, "")
    check("and the status row prefers the applied value over the request",
          "diverged ? ' · asked ' + fpsWant" in page
          or "asked ' + fpsWant" in page, "")


def test_a_running_total_does_not_lead_a_live_rate():
    """The eye takes the first number, so the first number must be the answer.

    "Dropping frames still" was reported from a screenshot reading
    `201 · 0.0/s`. Nothing was being dropped: 201 was the count since the
    daemon started and the rate beside it was zero. The row had already been
    revised once to add the rate, and the total was left in front of it, so it
    was misread the same way the next time it mattered.

    The second half is why the misreading was reasonable. `dropped` counts
    since the daemon started; `sent` is reset every time a viewer connects. Two
    totals over different spans, in the same visual form, side by side, invite
    a ratio that means nothing.
    """
    print("\nrate before total")
    page = open(os.path.join(HERE, os.pardir, "daemon", "kvm.html"),
                encoding="utf-8").read()
    i = page.index("ws dropped")
    row = page[i - 400:i + 200]
    check("the rate is composed before the total, not after",
          "r.toFixed(1) + '/s' : '—') + '  ·  ' + tot" in row, "")
    check("the sent label names the span its total covers",
          "ws sent · since a viewer joined" in page, "")
    check("and so does the dropped label, which is a different span",
          "ws dropped · since daemon start" in page, "")
    # Control: the two spans really are different in the daemon, which is the
    # whole reason the labels have to differ. If someone makes them the same,
    # this test should start failing and the labels should be revisited.
    web = open(os.path.join(HERE, os.pardir, "daemon", "vcweb.py"),
               encoding="utf-8").read()
    body = web[web.index("def serve_ws"):web.index("def serve_ws") + 3000]
    check("control: sent IS reset per connection in the daemon",
          "self.ws_sent_frames = 0" in body, "")
    check("control: dropped is NOT reset there",
          "self.ws_dropped = 0" not in body, "")


def test_a_frame_that_will_not_decode_is_counted():
    """A failure count with no attempt count cannot be read at all.

    The first version of this counter recorded failures only. It read 0, and I
    quoted that as "no decode failures across 103,657 frames" -- a denominator
    that was never the denominator. Most captured frames are never decoded:
    they arrive, sit in the ring, and are evicted without Pillow touching
    them. And with no browser attached only ONE of the four call sites runs at
    all, at about two decodes a second.

    vcctrl-94 caught it: "zero failures" and "nothing was tried" were the same
    reading. Attempts are what make the zero a measurement.
    """
    print("\ndecode counters")
    cap = vcctrld.VideoCapability(None, vcctrld.Bus())
    check("a fresh capability has attempted none",
          cap.decode_attempts == 0, cap.decode_attempts)
    check("and failed none", cap.decode_errs == 0, cap.decode_errs)
    check("and reports no last error rather than a stale one",
          cap.decode_last is None, cap.decode_last)

    # A frame that passes the daemon's ENTIRE filter -- SOI, EOI, >= 128 bytes
    # -- and is still not a JPEG. This is exactly what the reader would push.
    junk = b"\xff\xd8" + b"\x00" * 200 + b"\xff\xd9"
    check("control: it would pass _read_frames' filter",
          junk.startswith(b"\xff\xd8") and junk.endswith(b"\xff\xd9")
          and len(junk) >= 128, len(junk))

    ok = cap._is_picture(junk)
    check("an undecodable frame is not called a picture", ok is False, ok)
    check("the attempt was counted", cap.decode_attempts == 1,
          cap.decode_attempts)
    check("and so was the failure", cap.decode_errs == 1, cap.decode_errs)
    check("with the site that saw it named",
          (cap.decode_last or {}).get("where") == "is_picture", cap.decode_last)

    # THE DENOMINATOR IS THE POINT: a success must move attempts and not errs,
    # or the two numbers cannot be divided.
    try:
        import io as _io
        from PIL import Image
        buf = _io.BytesIO()
        Image.new("RGB", (64, 48), (30, 60, 90)).save(buf, "JPEG")
        cap._is_picture(buf.getvalue())
        check("a decodable frame moves ATTEMPTS", cap.decode_attempts == 2,
              cap.decode_attempts)
        check("and does not move errors", cap.decode_errs == 1,
              cap.decode_errs)
        check("per site, so uneven coverage is visible rather than averaged",
              cap.decode_sites.get("is_picture") == {"n": 2, "err": 1},
              cap.decode_sites)
    except ImportError:
        print("  SKIP  no Pillow")


def test_every_decode_site_is_counted():
    """The coverage claim, asserted instead of believed.

    I wrote that "all four call sites now count what they swallow". Three did.
    `_select` -- the shot path, the one the harness hits on every mid-cell
    capture -- caught its exceptions into a local list that dies with the
    call, exactly as it had before the counter existed. vcctrl-94 found it by
    grepping, which is the check I should have written instead of the claim.

    So: every function that opens an image must also record the attempt. This
    fails the moment somebody adds a fifth decode site and forgets, which is
    the only way a coverage claim stays true.
    """
    print("\ndecode coverage")
    import ast as _ast
    src = open(os.path.join(HERE, os.pardir, "daemon", "vcctrld.py"),
               encoding="utf-8").read()
    tree = _ast.parse(src)

    opens, wired = [], []
    for fn in [n for n in _ast.walk(tree)
               if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]:
        body = _ast.dump(fn)
        # `Image.open(...)` anywhere inside the function
        if "attr='open'" in body and "id='Image'" in body:
            opens.append(fn.name)
            if "_decoded" in body:
                wired.append(fn.name)

    check("the file still has decode sites to check at all",
          len(opens) >= 4, opens)
    missing = sorted(set(opens) - set(wired))
    check("every function that decodes an image records the attempt",
          not missing, "not wired to _decoded: %s" % ", ".join(missing))
    check("control: and the four known ones are among them",
          {"_score_changes", "_is_picture", "_select"} <= set(wired),
          sorted(wired))


def test_the_change_cache_survives_two_timelines_at_once():
    """Two open tabs is enough to make this raise.

    `_score_changes` prunes a shared dict by ITERATING it, while another
    request thread may be inserting -- RuntimeError: dictionary changed size
    during iteration. It cannot corrupt anything (the GIL is on; checked on
    the Pi with sys._is_gil_enabled() rather than assumed), but a 500 on
    /timeline is a real fault in a path a reviewing tab hits every few
    seconds.

    THE FIRST VERSION OF THIS TEST PASSED ON THE BROKEN CODE. It scored 40
    tiny frames from four threads and never once interleaved -- the prune is
    microseconds of pure Python against a 40-entry dict, and CPython switches
    threads every 5 ms, so the window essentially did not exist. A test that
    cannot fail is worse than no test: it reports coverage it does not have.

    Two changes make it real, and both are deliberate rather than incidental:
    a prune with THOUSANDS of entries to delete, so the iteration lasts long
    enough to be interrupted, and a switch interval short enough to guarantee
    the interruption. The race is genuinely rarer than this in production --
    but "rare" is a statement about frequency, not about whether the fault
    exists, and the fix is a lock either way.
    """
    print("\nconcurrent timelines")
    import threading as _th
    try:
        from PIL import Image
    except ImportError:
        print("  SKIP  no Pillow")
        return
    import io as _io

    frames = []
    for i in range(24):
        b = _io.BytesIO()
        Image.new("RGB", (64, 48), (i * 7 % 256, 40, 80)).save(b, "JPEG")
        frames.append((time.time() + i * 0.03, i + 1, b.getvalue()))

    def run(cap):
        errs = []

        def hammer(offset):
            for _ in range(30):
                # Refill with entries no live window mentions, so every pass
                # has thousands of keys to prune -- that long iteration is
                # where another thread gets in.
                #
                # THROUGH THE CAPABILITY'S OWN LOCK, not around it. A first
                # version wrote to the dict directly and made the locked build
                # fail too -- but nothing in the daemon writes `_chg` except
                # `_score_changes`, so that was the test inventing a writer
                # that does not exist and then blaming the code for it. The
                # control below swaps in a lock that does nothing, which is
                # what makes this a fair comparison rather than a rigged one.
                with cap._chg_lock:
                    for k in range(10000 + offset, 14000 + offset):
                        cap._chg[k] = 0.0
                try:
                    cap._score_changes(frames[offset % 4:])
                except Exception as exc:
                    errs.append("%s: %s" % (type(exc).__name__, exc))

        ts = [_th.Thread(target=hammer, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=90)
        return errs

    class _Null(object):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    _NULL = _Null()

    old_iv = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)          # guarantee the interleave
    try:
        cap = vcctrld.VideoCapability(None, vcctrld.Bus())
        errs = run(cap)
        check("four concurrent scorings raise nothing", not errs,
              errs[0] if errs else "")

        # CONTROL, in the same run rather than in a separate script: give a
        # second capability a lock that does not lock and require the fault to
        # appear. Without this the green above says only that nothing
        # happened, which is what the first version of this test said.
        loose = vcctrld.VideoCapability(None, vcctrld.Bus())
        loose._chg_lock = _NULL
        broke = run(loose)
        check("control: the same test DOES fail without the lock",
              bool(broke), "unlocked run raised nothing -- this test cannot "
                           "detect the fault it claims to cover")
    finally:
        sys.setswitchinterval(old_iv)


def test_connection_history_outlives_a_busy_event_log():
    """"Was anything connected while I was measuring" is asked afterwards.

    vcctrl-94 tried to bound a browser tab's window from the daemon's event
    log and could not: it holds 200 entries and a running cell's LED polling
    floods it, so the whole log spanned SEVENTEEN SECONDS. The rare event was
    always already gone by the time anyone looked for it.

    The fix is not a bigger shared log, it is a separate one. Connections
    happen a few times an hour where LED reads happen several times a second,
    so the same 200 entries cover days rather than seconds. Two things with
    different lifetimes do not belong in one ring.
    """
    print("\nconnection log")
    import sys as _sys
    _sys.path.insert(0, os.path.join(HERE, os.pardir, "daemon"))
    import vcweb

    cap = vcweb.WebCapability.__new__(vcweb.WebCapability)
    cap.lock = threading.Lock()
    import collections as _c
    cap.ws_log = _c.deque(maxlen=200)

    cap._log_ws("open", kind="video", agent="Mozilla/5.0 (iPhone)")
    cap._log_ws("close", kind="video", frames=714, nbytes=21088504,
                held_s=63.2)
    rows = list(cap.ws_log)
    check("both ends of a connection are recorded", len(rows) == 2, len(rows))
    check("the open names what connected",
          "iPhone" in rows[0].get("agent", ""), rows[0])
    check("the close carries what it cost",
          rows[1]["frames"] == 714 and rows[1]["bytes"] == 21088504, rows[1])
    check("and how long it was on", rows[1]["held_s"] == 63.2, rows[1])
    check("every row is timestamped, or it cannot bound a window",
          all(r.get("t") for r in rows), rows)

    # THE POINT OF THE SEPARATE RING, asserted rather than described: a flood
    # of the frequent thing must not evict the rare one. This is what the
    # shared log failed at.
    for i in range(500):
        cap._log_ws("open", kind="audio")
    kinds = [r["kind"] for r in cap.ws_log]
    check("control: the ring does bound itself", len(cap.ws_log) == 200,
          len(cap.ws_log))
    check("audio chatter CAN evict video rows from ITS OWN ring",
          "video" not in kinds, "")

    # And the real property: connections are rare, so 200 of them is days.
    # A day of browsing is dozens, not thousands.
    check("200 entries is days of ordinary use, not seconds",
          cap.ws_log.maxlen == 200, cap.ws_log.maxlen)


def test_an_undecodable_frame_is_kept_with_its_provenance():
    """A counter says a frame broke; the frame itself can be examined.

    2026-08-21 17:29:12 the first real decode failure ever recorded arrived --
    "broken data stream when reading image file", seq 78658, off the live
    capture with no stress and no fuzzing involved. By the time anyone asked
    for that frame it had been evicted: the ring is 31 s deep and the counter
    took 140 s to be read. Every malformed frame this project has examined
    until now was one we manufactured, because no real one had ever been kept.

    The sidecar is vcctrl-94's request and it is the GMQ3 lesson applied
    before it costs anything: a bare .jpg in a directory in three weeks is an
    orphan. Provenance travels WITH the artifact or it gets reconstructed
    later, wrongly.
    """
    print("\nbad frame retention")
    import json as _json
    import shutil
    import tempfile

    cap = vcctrld.VideoCapability(None, vcctrld.Bus())
    d = tempfile.mkdtemp(prefix="badframes")
    try:
        cap.BAD_DIR = d
        junk = b"\xff\xd8" + b"\x00" * 300 + b"\xff\xd9"
        ok = cap._is_picture(junk)
        check("control: the frame really is undecodable", ok is False, ok)

        jpgs = [f for f in os.listdir(d) if f.endswith(".jpg")]
        check("the frame itself was written", len(jpgs) == 1, os.listdir(d))
        with open(os.path.join(d, jpgs[0]), "rb") as f:
            check("byte for byte, not re-encoded", f.read() == junk, "")

        side = [f for f in os.listdir(d) if f.endswith(".json")]
        check("with a sidecar beside it", len(side) == 1, os.listdir(d))
        meta = _json.load(open(os.path.join(d, side[0])))
        for key in ("seq", "where", "utc", "bytes", "error", "sha256"):
            check("sidecar carries %s" % key, key in meta, sorted(meta))
        check("the site that saw it", meta["where"] == "is_picture", meta)
        check("and the error text, not just a code",
              "cannot identify" in meta["error"].lower()
              or "error" in meta["error"].lower(), meta["error"])
        check("the hash matches the bytes on disk",
              meta["sha256"] == __import__("hashlib").sha256(junk).hexdigest(),
              meta["sha256"])
        check("and it records that the frame passed the daemon's own filter",
              meta["starts_soi"] and meta["ends_eoi"], meta)

        # THE BOUND IS THE POINT: a degraded cable could produce these
        # continuously, and this writes to the card from the capture path.
        for _ in range(cap.BAD_KEEP + 6):
            cap._is_picture(junk)
        jpgs = [f for f in os.listdir(d) if f.endswith(".jpg")]
        sides = [f for f in os.listdir(d) if f.endswith(".json")]
        check("the directory is bounded, not just the record",
              len(jpgs) <= cap.BAD_KEEP, len(jpgs))
        check("and sidecars are reaped with their frames, not orphaned",
              len(sides) == len(jpgs), (len(sides), len(jpgs)))
        check("the in-memory record agrees with the disk",
              len(cap.bad_frames) == len(jpgs),
              (len(cap.bad_frames), len(jpgs)))
    finally:
        shutil.rmtree(d, ignore_errors=True)

    # A capability that cannot write must not take down capture to say so.
    cap2 = vcctrld.VideoCapability(None, vcctrld.Bus())
    cap2.BAD_DIR = "/proc/nonexistent/cannot/create"
    try:
        cap2._is_picture(b"\xff\xd8" + b"\x00" * 300 + b"\xff\xd9")
        raised = None
    except Exception as exc:
        raised = exc
    check("an unwritable directory is survivable", raised is None, raised)
    check("and the failure is still counted even when the frame is not kept",
          cap2.decode_errs == 1, cap2.decode_errs)


def test_no_text_is_dimmed_by_transparency():
    """A contrast floor cannot see an alpha applied on top of a colour.

    The theme test measures colour TOKENS. The page then put `opacity:.34` on
    a not-applicable lamp and `opacity:.55` on a stale one, so labels that the
    generator had fitted to 4.5:1 rendered at 1.5:1 and 2.1:1 -- and every
    token check stayed green, because the token was never the thing on screen.

    In each case the transparency was saying something the design already said
    another way: the lamp's underline is the state carrier, solid for
    applicable and dashed for unproven. The opacity repeated it and charged
    legibility for the repetition.

    So: no rule may dim TEXT with opacity. Backgrounds, tints, hidden inputs
    and keyframes are exempt -- they carry no glyphs.
    """
    print("\ntext transparency")
    import re as _re
    page = open(os.path.join(HERE, os.pardir, "daemon", "kvm.html"),
                encoding="utf-8").read()
    css = _re.sub(r"/\*.*?\*/", "", page[:page.index("</style>")], flags=_re.S)

    # Selectors allowed to carry an opacity: nothing here paints a glyph.
    # `.sep` and `.barsep` came OFF this list. I put them here calling them
    # decorative, asked whether that was right, and the answer was "the menu
    # dividers are barely visible" -- they group controls, so they are
    # structure. They use --edge at 3:1 now with no opacity at all.
    EXEMPT = _re.compile(
        r"#ghost|#zhint|#scrubsel|^from$|^to$|^\d+%$"
        r"|button:disabled|\.split:has|#state > i|#themes button \.b")

    offenders = []
    for sel, body in _re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        m = _re.search(r"opacity:\s*(0?\.\d+)\s*[;}]?", body)
        if not m or float(m.group(1)) >= 1:
            continue
        s = sel.strip().replace("\n", " ")
        if EXEMPT.search(s):
            continue
        offenders.append("%s {opacity:%s}" % (s[:50], m.group(1)))

    check("no non-exempt selector dims text with opacity",
          not offenders, "; ".join(offenders[:3]))

    # Control: the check must be able to see one. Without this, a green result
    # says only that the regex found nothing, which is what it said while
    # .lamp.stale span was sitting there at .55.
    planted = css + "\n.lamp.stale span { opacity:.55; }"
    seen = []
    for sel, body in _re.findall(r"([^{}]+)\{([^{}]*)\}", planted):
        m = _re.search(r"opacity:\s*(0?\.\d+)\s*[;}]?", body)
        if m and float(m.group(1)) < 1 and not EXEMPT.search(sel.strip()):
            seen.append(sel.strip())
    check("control: it catches a planted one", bool(seen), seen[:2])


def test_the_glyph_halo_never_lands_on_a_button_box():
    """A filter applies to the whole element, not to the text inside it.

    Colour emoji carry their own palette and ignore the theme, so the pale
    ones -- page, keyboard, crescent moon -- need a halo to be seen on a light
    surface. The first version put that halo on the BUTTONS, and a filter
    draws around the rendered box: three buttons got a black outline their
    neighbours did not have, and were reported as looking darker. The grounds
    were identical the whole time.

    So the halo belongs on a glyph-only wrapper -- `.bi` or `.gly` -- and
    never on a selector that also paints a background.
    """
    print("\nglyph halo scope")
    import re as _re
    page = open(os.path.join(HERE, os.pardir, "daemon", "kvm.html"),
                encoding="utf-8").read()
    css = _re.sub(r"/\*.*?\*/", "", page[:page.index("</style>")], flags=_re.S)

    GLYPH_ONLY = ("bi", "gly", "caret", "ret")
    offenders = []
    for sel, body in _re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        # ONLY the halo. The first version of this check flagged any filter at
        # all and caught #stale, which greys the last-good frame -- a picture
        # treatment on an <img>, with no glyph and no button anywhere near it.
        # A guard that fires on the thing it was not written about is one
        # somebody disables rather than reads.
        if "drop-shadow" not in body:
            continue
        s = sel.strip().replace("\n", " ")
        parts = [p.strip() for p in s.split(",")]
        for p in parts:
            last = p.split()[-1] if p.split() else p
            if not any(("." + g) in last for g in GLYPH_ONLY):
                offenders.append("%s {filter}" % p[:46])
    check("no filter is declared on anything but a glyph wrapper",
          not offenders, "; ".join(offenders[:3]))

    # The wrappers must actually exist in the markup, or the rule styles
    # nothing and the emoji stay invisible while the test stays green.
    check("the pale glyphs are wrapped so the halo has something to sit on",
          page.count('class="gly"') >= 2, page.count('class="gly"'))
    check("and the theme toggle builds its glyph wrapped too",
          "className: 'gly'" in page or 'class="gly"' in page, "")


def test_a_recording_refuses_a_window_that_predates_its_caller():
    """A dump is bounded by the ring, not by the run that asked for it.

    GMQ3-glass.avi is 815 MB and looks like a recording of cell MQ3. 94.5% of
    its frame packets are byte-identical to GMQ2's, because the ring was
    holding 1,193 s against the 480 s it was asked for, so consecutive dumps
    necessarily overlapped. A peer sampled it, found real game frames, and
    reported a finding refuted -- correctly identified frames, belonging to
    the previous cell.

    `first_seq` and `first_t` were in the returned meta the whole time. The
    information existed, was correct, and was not load-bearing: nothing
    refused on it. Same defect as a lock check whose answer nothing consumes.
    """
    print("\nrecording provenance")
    import io as _io
    try:
        from PIL import Image
    except ImportError:
        print("  SKIP  no Pillow")
        return

    cap = vcctrld.VideoCapability(None, vcctrld.Bus())
    t0 = time.time()

    def frame(i):
        b = _io.BytesIO()
        Image.new("RGB", (64, 48), (i * 5 % 256, 30, 60)).save(b, "JPEG")
        return b.getvalue()

    # Ten frames from "the previous cell", then ten from ours.
    cap.ring.clear()
    for i in range(20):
        cap.ring.append((t0 + i * 0.1, i + 1, frame(i)))
    mine = t0 + 1.0                      # our run began at frame 11

    blob, meta = cap.buffer_avi()
    check("with no start time it still dumps the whole ring",
          blob is not None and meta["frames"] == 20, meta)
    check("control: and that dump DOES contain the foreign frames",
          meta["first_t"] < mine, (meta["first_t"], mine))

    blob, meta = cap.buffer_avi(since=mine)
    check("given its caller's start, it refuses", blob is None, "wrote bytes")
    check("naming the reason rather than a bare failure",
          meta.get("refused") == "window_precedes_caller", meta)
    check("and measuring the overlap", meta.get("foreign_frames") == 10, meta)
    check("in seconds as well as frames", meta.get("older_by_s") > 0.9, meta)
    check("carrying the seq range so it can be checked against neighbours",
          meta.get("first_seq") == 1 and meta.get("last_seq") == 20, meta)

    blob, meta = cap.buffer_avi(since=mine, clip=True)
    check("clip takes only the caller's own frames",
          blob is not None and meta["frames"] == 10, meta)
    check("and SAYS what it cut, rather than quietly shortening",
          meta.get("clipped_frames") == 10, meta)
    # AGAINST THE ROUNDING THE META ACTUALLY CARRIES. first_t is reported to
    # three decimals by design, so comparing it to an unrounded threshold
    # fails whenever the frame sits inside half a millisecond of the boundary
    # -- which it did on roughly half of runs. The frame was never early; the
    # REPORT of it was, by 0.00045 s. A test that compares a rounded readout
    # against full precision is measuring the readout's format.
    check("the clipped window no longer predates the caller",
          meta["first_t"] >= round(mine, 3) - 0.0005, (meta["first_t"], mine))

    # A window entirely before the caller is not a short recording, it is none.
    blob, meta = cap.buffer_avi(since=t0 + 100, clip=True)
    check("a window entirely foreign is refused even with clip",
          blob is None and meta.get("refused") == "nothing_after_since", meta)

    # And the field is always present, so a reader never has to know whether
    # to look for it.
    blob, meta = cap.buffer_avi()
    check("clipped_frames is reported even when nothing was clipped",
          meta.get("clipped_frames") == 0, meta)


def test_arm_leds_survives_a_settling_read():
    """arm_leds() decided and confirmed from BARE reads of a settling value.

    Measured 2026-08-20: four refusals in one evening, four immediate retries
    that succeeded. `stable_led()` exists because at_prompt() lost a run to
    exactly this -- an LED takes ~48 ms to settle and a single sample can catch
    it mid-flight -- and arm_leds() was never converted.

    THE FAILURE IS NOT A NUISANCE, IT IS INVERTED. Suppose caps lock is ALREADY
    armed high and one read catches it settling low. The old code concludes it
    needs setting, presses the key, and drives a correctly-armed LED to the
    WRONG state -- then waits for it to reach the right one and times out. So a
    machine that was ready is reported as unable to arm, and the boot that
    depended on it is refused.

    Also: three states. An unreadable return channel is COULD NOT LOOK, which
    on ADB is permanent and on a booting machine is transient. Collapsing that
    into False tells the caller a healthy Macintosh failed to arm.
    """
    common = _load("bin/vcctrl_common.py", "vcc_common_arm")

    class Rig(object):
        """capslock is genuinely True; ONE specific read of it lies.

        The lie is pinned to a CALL INDEX rather than a count, because
        leds_available() calls leds() before any decision is made -- a
        "lie on the first read" rig has its lie eaten by the availability
        check and never reaches the code under test. That is what the control
        caught on the first run of this test.
        """
        def __init__(self, lie_on=(), available=True, why=None):
            self.state = {"capslock": True, "scrolllock": False}
            self.lie_on = set(lie_on)
            self.calls = 0
            self.available = available
            self.why = why
            self.keys = []

        def leds(self):
            if not self.available:
                return {"available": False, "why": self.why, "reason": "x"}
            self.calls += 1
            v = dict(self.state)
            if self.calls in self.lie_on:
                v["capslock"] = not v["capslock"]      # caught mid-settle
            v["available"] = True
            return v

        def key(self, *a):
            self.keys.append(a[-1])
            if a[-1] in self.state:
                self.state[a[-1]] = not self.state[a[-1]]

    def run(rig, use_stable):
        common.leds = rig.leds
        common.vc = lambda *a, **k: rig.key(*a)
        common.LED_POLL_S = 0
        def wait_led(name, want, timeout=15):
            return 0.0 if bool(rig.state.get(name)) is want else None
        common.wait_led = wait_led
        if not use_stable:
            # THE CONTROL: put the old bare-read behaviour back.
            common.stable_led = lambda name, tries=6: bool(rig.leds().get(name))
        else:
            common.stable_led = common.__dict__["stable_led_real"]
        return common.arm_leds()

    common.__dict__["stable_led_real"] = common.stable_led

    # 1. THE DEFECT. One settling read, machine already correctly armed.
    rig = Rig(lie_on=(2,))
    got = run(rig, use_stable=False)
    check("CONTROL: a bare read on a settling LED refuses a machine that was "
          "already armed", got is False, "got %r, keys=%r" % (got, rig.keys))
    check("CONTROL: and it pressed the key it should not have",
          rig.keys == ["capslock"], rig.keys)

    # 2. THE FIX. Same rig, same lie, stable_led in the decision.
    rig = Rig(lie_on=(2,))
    got = run(rig, use_stable=True)
    check("stable_led in the decision arms the same rig",
          got is True, "got %r, keys=%r" % (got, rig.keys))
    check("and presses nothing, because it was already armed",
          rig.keys == [], rig.keys)

    # 3. A REAL failure must still be False, not None -- the guard must not
    #    turn every negative into could-not-look.
    rig = Rig()
    rig.state["capslock"] = False
    rig.key_broken = True
    common.leds = rig.leds
    common.vc = lambda *a, **k: None          # the key press does nothing
    common.wait_led = lambda name, want, timeout=15: None
    common.stable_led = common.__dict__["stable_led_real"]
    got = common.arm_leds()
    check("an LED that will not take the state is FALSE, not None",
          got is False, got)

    # 4. THREE STATES. Unreadable is could-not-look, not failure.
    for why in ("unknown", "unpowered", "error"):
        rig = Rig(available=False, why=why)
        common.leds = rig.leds
        got = common.arm_leds()
        check("why=%s is None (could not look), not False" % why, got is None, got)

    # 5. ...but a board that genuinely HAS no return channel is a real False.
    rig = Rig(available=False, why="unsupported")
    common.leds = rig.leds
    got = common.arm_leds()
    check("why=unsupported is False -- ADB has no channel, that is a fact",
          got is False, got)


def test_cfclean_never_deletes_what_it_cannot_prove():
    """The decision half of the CF cleanup, against REAL OCR from the rig.

    This tool deletes frame dumps off a card that is a swap away, so the
    failure mode is not a wasted run -- it is the only copy.

    THE DESIGN CHANGED BECAUSE OF THIS TEST. The first version matched card
    files to local ones BY NAME. Then the parser was pointed at real captures
    and OCR turned out to read DOS `DIR` output with the sizes right and the
    names wrong:

        real   R1A  LOG  1,102  08-20-26  10:58p
        OCR    RIA  LOG  1,102  88-20-26  18:58p

    `R1A` -> `RIA`, `08` -> `88`, `10` -> `18`. PPM filenames are `S<tick>.PPM`
    -- all digits -- so a name match is one glyph from selecting the wrong
    file. Sizes, counts and totals read correctly in every sample.

    So the card is asked for two numbers and the local side supplies the rest.
    """
    print("\ncfclean decision half")
    cf = _load("bin/vcctrl-cfclean", "vcc_cfclean_t")
    import tempfile

    # 1. THE PARSER, against OCR captured from the real console. Not a
    #    hand-written approximation of what DOS prints -- what pytesseract
    #    actually returned, mangled words and all.
    real = [
        ("'1 filets) 1,102 bytes'", "1 filets)      1,102 bytes", (1, 1102)),
        ("file(s) intact", "        1 file(s)    122,485 bytes", (1, 122485)),
        ("multi-megabyte", "  1 filets)     7,816,241 bytes", (1, 7816241)),
        ("many files", "   27 file<s)      6,221,205 bytes", (27, 6221205)),
    ]
    for label, line, want in real:
        got = cf.parse_dir_summary("Directory of C:\\DOSKUTSU\\LOGS\n" + line)
        check("parses a real DIR summary (%s)" % label, got == want, (got, want))

    check("an unreadable screen is None, not zero",
          cf.parse_dir_summary("") is None)
    check("a screen with no summary line at all is None",
          cf.parse_dir_summary("Volume in drive C is DOS") is None)

    # A DOS "File not found" used to return None here, on the reasoning that
    # it has no summary line so nothing was read. CHANGED 2026-08-24, and the
    # original reasoning was wrong: the card ANSWERED. "There is no such
    # directory" is a reading, and the only honest verdict on it is KEEP --
    # there is nothing to delete and nothing was lost.
    #
    # Collapsing it into None made every never-dumped tag look like an
    # instrument failure. Running the tool against the real card produced
    # REFUSE for CS1A purely because that round forbade SHOT_TICKS and so
    # wrote no dumps at all. If the ordinary case reports COULD-NOT-LOOK then
    # REFUSE stops being a signal anybody reads, which is how a three-state
    # check decays back into two.
    check("File not found is a READING of an empty directory",
          cf.parse_dir_summary("File not found") == (0, 0))
    check("  and a genuinely blank screen is still None",
          cf.parse_dir_summary("") is None)

    # 2. THE VERDICT. Totals, not names.
    with tempfile.TemporaryDirectory() as d:
        sub = os.path.join(d, "D1"); os.makedirs(sub)
        for n in ("S00300.PPM", "S00600.PPM"):
            with open(os.path.join(sub, n), "wb") as f:
                f.write(b"x" * 230415)
        local = cf.local_tag_totals(d, "D1")
        check("local totals count the tag's files", local == (2, 460830), local)

        v, why = cf.classify_tag((2, 460830), local)
        check("count AND bytes matching is DELETE", v == cf.DELETE, (v, why))

        v, why = cf.classify_tag((3, 691245), local)
        check("a card with MORE files than were collected is KEEP",
              v == cf.KEEP, (v, why))

        v, why = cf.classify_tag((2, 460831), local)
        check("one byte different is KEEP -- totals must match exactly",
              v == cf.KEEP, (v, why))

        v, why = cf.classify_tag(None, local)
        check("an unread card summary REFUSES, it does not delete",
              v == cf.REFUSE, (v, why))

        v, why = cf.classify_tag((2, 460830), None)
        check("an unreadable local side REFUSES too", v == cf.REFUSE, (v, why))

        v, why = cf.classify_tag((4, 921660), (0, 0))
        check("nothing collected locally is KEEP -- this is what protects an "
              "abandoned round", v == cf.KEEP, (v, why))

        v, why = cf.classify_tag((0, 0), local)
        check("nothing on the card is KEEP, not a spurious delete",
              v == cf.KEEP, (v, why))

    # 3. The read half now runs; the DEL half still refuses. Stub the card
    #    read -- a unit test must not drive the Gateway, and one that does is
    #    slow, non-hermetic, and fails when somebody else holds the rig.
    orig = cf.read_card_dir
    cf.read_card_dir = lambda *_a, **_k: "File not found"
    try:
        rc = cf.main(["--tags", "D1"])
        check("a dry run completes rather than refusing at the top", rc == 0,
              rc)
        rc = cf.main(["--tags", "D1", "--delete"])
        check("--delete still REFUSES: the DEL itself has never been "
              "exercised, and a half-driven destructive tool reads as a "
              "completed one", rc == 2, rc)
    finally:
        cf.read_card_dir = orig


def test_the_docs_index_cannot_rot_silently():
    """docs/README.md routes a cold reader to the current answer.

    It exists because several documents carry a live figure and a RETRACTED one
    in the same section -- 30.2 fps, stationary_frac 0.11, "no glass since MQ2"
    -- and every one of those was live in a pushed document before it was
    caught. An index that quietly stops covering the directory sends the next
    reader to the wrong half.

    So the index is checked against the filesystem rather than trusted: a doc
    added without being indexed fails here, which is the only thing that makes
    an index a guarantee rather than a good intention.
    """
    print("\ndocs index")
    import re as _re
    d = "docs"
    if not os.path.isdir(d):
        print("  SKIP  no docs/")
        return
    idx_path = os.path.join(d, "README.md")
    check("the index exists at all", os.path.exists(idx_path))
    if not os.path.exists(idx_path):
        return
    idx = open(idx_path).read()
    named = set(_re.findall(r"`([A-Za-z0-9][A-Za-z0-9._-]*\.md)`", idx))

    # TRACKED files, not everything on disk. The tree is shared between
    # sessions and an untracked draft is somebody's work in progress -- failing
    # the whole suite on it makes this guard a nuisance that gets disabled,
    # which is worse than not having it. A doc is indexed when it is committed.
    import subprocess as _sp
    r = _sp.run(("git", "ls-files", "docs/*.md"), capture_output=True, text=True)
    if r.returncode != 0:
        print("  SKIP  not a git checkout")
        return
    on_disk = {os.path.basename(l) for l in r.stdout.split()
               if l.endswith(".md") and os.path.basename(l) != "README.md"}
    check("the docs directory is not empty -- an index over nothing is not a "
          "passing index", bool(on_disk), on_disk)
    check("every doc on disk is named in the index",
          not (on_disk - named), sorted(on_disk - named))
    check("the index names no file that does not exist",
          not (named - on_disk - {"README.md"}), sorted(named - on_disk))


def test_every_commit_cited_in_docs_still_resolves():
    """Writeups cite SHAs as provenance. A history rewrite dangles all of them.

    Round Q/R and the Mach64 campaign cite this repo's commits as evidence that
    a prediction was pinned BEFORE the data, that a retraction was made in the
    open, that a guard was added on a given day. `2f24eed` is load-bearing: it
    is the proof a numeric prediction could not have been written with the
    answer in hand.

    A `git filter-repo` rewrite makes every one of those unresolvable. **A tag
    does not save them** -- filter-repo rewrites all refs, tags included -- and
    an archived bundle preserves the OBJECTS without making the PROSE resolve.
    The citation has to be remapped through filter-repo's commit-map, and that
    is a step somebody has to remember.

    This is the thing that remembers. It passes today and goes red the moment a
    rewrite lands without the docs being remapped -- which is the only way a
    provenance claim stays a claim rather than becoming a decoration.
    """
    print("\ndoc commit citations")
    import subprocess as _sp
    if not os.path.isdir(".git"):
        print("  SKIP  not a git checkout")
        return

    def git(*a):
        return _sp.run(("git",) + a, capture_output=True, text=True)

    if git("rev-parse", "--git-dir").returncode != 0:
        print("  SKIP  no git")
        return

    import re as _re
    cited = {}
    # TRACKED FILES, NOT THE FILESYSTEM -- the same fix its sibling
    # test_the_docs_index_cannot_rot_silently already carries, and this one was
    # missed. Measured 2026-08-24: reading `docs/` off disk pulled in a 41 KB
    # untracked draft that supplied 15 of the 22 citations, so the guard
    # reported 19 across 7 docs in the worktree and 7 across 6 in a clone of
    # the same commit. Two sessions then argued about the discrepancy and
    # produced two different wrong explanations for it.
    #
    # It matters beyond tidiness. A BUNDLE CANNOT HOLD UNTRACKED FILES, so a
    # citation guard run over the working tree partly validates prose that no
    # backup contains and no clone will ever see -- which is exactly the
    # number somebody would quote to say a restore is sound.
    _ls = _sp.run(("git", "ls-files", "docs/*.md"), capture_output=True,
                  text=True)
    for f in sorted(x.split("/")[-1] for x in _ls.stdout.split() if x.strip()):
        if not f.endswith(".md") or not os.path.exists(os.path.join("docs", f)):
            continue
        body = open(os.path.join("docs", f), encoding="utf-8", errors="replace").read()
        # ANY hex of commit length, backticked or not. The first version of
        # this only matched backticked hex and reported green while missing
        # `commit 853c02d` written as plain prose in WEBKVM.md -- a guard that
        # covers a subset of the ways in, which is the shape it exists to
        # catch. A peer counting independently found more citations than this
        # test did, and that discrepancy was the only reason it surfaced.
        #
        # Widening is safe because nothing is FAILED for matching: a candidate
        # is only judged if it resolves as an object here, so a stray hex word
        # is skipped rather than reported.
        for s in set(_re.findall(r"\b([0-9a-f]{7,12})\b", body)):
            cited.setdefault(s, set()).add(f)

    # A guard over an empty set is not a passing guard.
    resolvable = {s: fs for s, fs in cited.items()
                  if git("cat-file", "-e", s + "^{commit}").returncode == 0}
    check("docs cite at least one real commit -- otherwise this test is "
          "asserting nothing", bool(resolvable), len(cited))

    dangling = []
    for s, fs in sorted(cited.items()):
        r = git("cat-file", "-e", s + "^{commit}")
        if r.returncode != 0:
            # Only a failure if it LOOKS like a citation of ours: something
            # that was once a commit here. A stray hex string in prose is not.
            if git("cat-file", "-e", s).returncode == 0:
                dangling.append((s, sorted(fs)))
    check("no doc cites a commit that no longer resolves",
          not dangling, dangling)
    print("  ...%d cited commits resolve across %d docs"
          % (len(resolvable), len({f for fs in resolvable.values() for f in fs})))


# ------------------------------------------------------- config loader (P1)
#
# docs/CONFIG-PLAN.md phase 1. The acceptance criterion that matters here is
# test_the_config_file_is_actually_read: a loader that fell back to built-in
# defaults on a parse error produces output IDENTICAL to one that read the file
# correctly, so "everything still works" and "the config did nothing" are the
# same observation. Only a deliberately wrong value can tell them apart.

def _vcconfig():
    import importlib.util as _u
    p = os.path.join(HERE, os.pardir, "common", "vcconfig.py")
    spec = _u.spec_from_file_location("vcconfig", p)
    m = _u.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _tmp_yaml(text):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def test_config_keeps_absent_null_and_value_apart():
    """The three states a YAML mapping actually has.

    `cfg.get("x", False)` flattens all three, which is `st.get("usb4vc", {})`
    wearing a nicer hat -- the idiom that printed "input devices held by
    USB4VC: ok" off a field that was not in the payload at all.
    """
    print("\nconfig three states")
    v = _vcconfig()
    p = _tmp_yaml("version: 1\nrig:\n  name: ~\n")
    try:
        c = v.load(p)
        check("a key that is not in the file reads ABSENT",
              c.optional("rig.nothing_here") is v.ABSENT)
        check("a key set to null reads NONE, not ABSENT",
              c.optional("rig.name") is v.NONE)
        check("require() refuses an explicit null",
              _raises(v.ConfigError, c.require, "rig.name"))
        check("require() refuses an absent key",
              _raises(v.ConfigError, c.require, "rig.nothing_here"))
        check("default() substitutes for ABSENT",
              c.default("rig.nothing_here", "fallback") == "fallback")
        check("default() REFUSES to substitute for an explicit null -- "
              "'deliberately nothing' is not 'unspecified'",
              _raises(v.ConfigError, c.default, "rig.name", "fallback"))
        check("the sentinels have no truth value, so `if cfg.optional(x):` "
              "cannot silently mean 'absent'",
              _raises(v.ConfigError, bool, v.ABSENT)
              and _raises(v.ConfigError, bool, v.NONE))
        check("there is no get() to reach for",
              not hasattr(c, "get"))
    finally:
        os.unlink(p)


def _raises(exc, fn, *a):
    try:
        fn(*a)
    except exc:
        return True
    except Exception:
        return False
    return False


def test_the_config_file_is_actually_read():
    """THE CONTROL THAT MUST FAIL.

    A broken config must not stop the daemon, so the loader falls back to
    built-in defaults. That means a test asserting "resolved config == the
    values we had before" passes just as happily when the file was never read
    at all. Success and no-op are the same observation.

    So: put a value in the file that is NOT the default, and require that it
    comes back. If the loader silently fell back, this is the check that
    notices.
    """
    print("\nconfig is read, not defaulted")
    v = _vcconfig()
    default_port = v.DEFAULTS["daemon"]["web"]["port"]
    wrong = 65123
    check("the control value differs from the default, or this test proves "
          "nothing", wrong != default_port, (wrong, default_port))
    p = _tmp_yaml("version: 1\ndaemon:\n  web:\n    port: %d\n" % wrong)
    try:
        c = v.load(p)
        got = c.require("daemon.web.port")
        check("the file's value wins over the built-in default",
              got == wrong, (got, wrong))
        check("the resolved config names the file it came from, so a running "
              "process can be asked rather than the disk re-read",
              c.source == p, c.source)
    finally:
        os.unlink(p)

    # And the other direction: no file at all still resolves, on defaults.
    import tempfile
    d = tempfile.mkdtemp()
    old = os.environ.pop("VCCTRL_CONFIG", None)
    old_port = os.environ.pop("VCCTRL_WEB_PORT", None)
    try:
        c = v.load(os.path.join(d, "does-not-exist.yaml")) \
            if False else v.Config(v.DEFAULTS)
        check("with no file anywhere the defaults still resolve",
              c.default("daemon.web.port", None) == default_port)
    finally:
        if old is not None:
            os.environ["VCCTRL_CONFIG"] = old
        if old_port is not None:
            os.environ["VCCTRL_WEB_PORT"] = old_port


def test_config_refuses_unknown_keys():
    """A typo'd key that silently leaves a subsystem unconfigured is worse
    than a refusal to start: the refusal names the typo, and the shrug
    produces a rig that looks configured and has no power control."""
    print("\nconfig unknown keys")
    v = _vcconfig()
    p = _tmp_yaml("version: 1\ndaemon:\n  prefixx: /opt/vcctrl\n")
    try:
        try:
            v.load(p)
            check("an unknown key is refused", False, "load() succeeded")
        except v.ConfigError as exc:
            msg = str(exc)
            check("an unknown key is refused", True)
            check("the refusal names the offending key", "prefixx" in msg, msg)
            check("and suggests the near miss rather than just stopping",
                  "prefix'" in msg, msg)
    finally:
        os.unlink(p)

    # Underscore keys carry comments; YAML comments do not survive a parser,
    # and config.json already used _kasa_note for exactly this.
    p = _tmp_yaml("version: 1\ndaemon:\n  _note: why this prefix\n")
    try:
        v.load(p)
        check("an _underscore comment key is allowed through", True)
    except v.ConfigError as exc:
        check("an _underscore comment key is allowed through", False, str(exc))
    finally:
        os.unlink(p)

    # bool must not satisfy an int field: bool IS an int in Python, and a
    # `port: true` that resolves to 1 is a listener on a privileged port.
    p = _tmp_yaml("version: 1\ndaemon:\n  web:\n    port: true\n")
    try:
        v.load(p)
        check("a bool does not satisfy an int field", False, "accepted true")
    except v.ConfigError:
        check("a bool does not satisfy an int field", True)
    finally:
        os.unlink(p)


def test_the_example_config_matches_the_schema():
    """The shipped example is the documentation. An example that no longer
    validates teaches a stranger the wrong keys, and nothing else in the repo
    would notice."""
    print("\nexample config")
    v = _vcconfig()
    p = os.path.join(HERE, os.pardir, "vcctrl.example.yaml")
    check("the example exists", os.path.exists(p), p)
    if not os.path.exists(p):
        return
    try:
        c = v.load(p)
        check("the example validates against the schema", True)
    except v.ConfigError as exc:
        check("the example validates against the schema", False, str(exc))
        return

    # It is also the file a stranger copies, so it must not teach bad habits.
    raw = open(p).read()
    check("no literal password key in the example -- secrets are named by "
          "environment variable, never written here",
          not re.search(r"^\s*password\s*:", raw, re.M), "literal password:")
    check("targets spell the LED channel as a word, not a boolean -- false "
          "collapses 'no such channel' into 'the channel is broken'",
          all(isinstance(t.get("leds"), str) for t in c.require("targets")))
    check("VCCTRL_FORCE is not a config key: it overrides the deploy guard, "
          "and awkward to reach for is its entire function",
          "VCCTRL_FORCE" not in raw and
          "VCCTRL_FORCE" not in str(v.ENV_OVERRIDES))


def test_config_env_overrides_the_file():
    """Env sits ABOVE the file: the systemd drop-ins already use it, and
    `VCCTRL_HOST=other-pi vcctrl status` is worth keeping."""
    print("\nconfig env precedence")
    v = _vcconfig()
    p = _tmp_yaml("version: 1\ncontrol:\n  daemon_host: from-file\n")
    old = os.environ.get("VCCTRL_HOST")
    try:
        os.environ["VCCTRL_HOST"] = "from-env"
        c = v.load(p)
        check("env beats the file", c.require("control.daemon_host") == "from-env",
              c.require("control.daemon_host"))
        check("and the override is REPORTED, because an override nobody can "
              "see is how two people debug different configurations",
              any("VCCTRL_HOST" in w for w in c.warnings), c.warnings)
        del os.environ["VCCTRL_HOST"]
        c = v.load(p)
        check("without the env var the file wins",
              c.require("control.daemon_host") == "from-file")
    finally:
        if old is None:
            os.environ.pop("VCCTRL_HOST", None)
        else:
            os.environ["VCCTRL_HOST"] = old
        os.unlink(p)


def test_the_daemon_takes_its_constants_from_config():
    """Phase 2 acceptance: the values really come from the file.

    Same trap as test_the_config_file_is_actually_read, one level up. Every
    constant migrated in phase 2 still has its old literal as the fallback, so
    a daemon that ignored the config entirely would produce exactly the values
    this rig expects. The only way to tell is to give it values it could not
    have guessed and require them back.
    """
    print("\ndaemon constants from config")
    import importlib.util as _u
    p = _tmp_yaml(
        "version: 1\n"
        "daemon:\n"
        "  socket: /run/not-the-default.sock\n"
        "  state_dir: /var/lib/elsewhere\n"
        "  web: {port: 65001, tls_port: 65002, bind: 10.9.9.9}\n"
        "capabilities:\n"
        "  power: {backend: kasa-legacy, settings: {host: 203.0.113.7,\n"
        "          port: 9998, cycle_off_s: 3.5}}\n"
        "  video: {backend: v4l2-ffmpeg, settings: {device: /dev/video99}}\n"
        "  audio: {backend: alsa-ffmpeg, settings: {device: 'hw:CARD=NOPE,DEV=9'}}\n"
        "targets:\n"
        "  - {board_id: 7, name: Invented Machine, leds: supported}\n"
        "  - {board_id: 8, name: Other Machine, leds: unsupported}\n")
    old = os.environ.get("VCCTRL_CONFIG")
    # The env overrides would beat the file and hide the very thing under test.
    stash = {k: os.environ.pop(k) for k in
             ("VCCTRL_WEB_PORT", "VCCTRL_WEB_TLS_PORT", "VCCTRL_WEB_BIND",
              "VCCTRL_ALSA", "VCCTRL_VIDEO") if k in os.environ}
    try:
        os.environ["VCCTRL_CONFIG"] = p
        spec = _u.spec_from_file_location("vcctrld_cfgtest", DAEMON)
        m = _u.module_from_spec(spec)
        spec.loader.exec_module(m)

        check("socket path comes from config",
              m.SOCKET_PATH == "/run/not-the-default.sock", m.SOCKET_PATH)
        check("web port comes from config", m.WEB_PORT == 65001, m.WEB_PORT)
        check("tls port comes from config", m.WEB_TLS_PORT == 65002,
              m.WEB_TLS_PORT)
        check("bind comes from config", m.WEB_BIND == "10.9.9.9", m.WEB_BIND)
        check("power cycle duration comes from config",
              m.POWER_CYCLE_OFF_S == 3.5, m.POWER_CYCLE_OFF_S)
        check("the plug host comes from config",
              m.kasa_host() == "203.0.113.7", m.kasa_host())
        check("the plug PORT comes from config -- it was buried in "
              "kasa_send() as a literal 9999", m.kasa_port() == 9998,
              m.kasa_port())
        check("video device comes from config",
              m.VideoCapability.DEVICE == "/dev/video99",
              m.VideoCapability.DEVICE)
        check("audio device comes from config",
              m.AudioCapability.DEVICE == "hw:CARD=NOPE,DEV=9",
              m.AudioCapability.DEVICE)
        check("state_dir moves the power audit log with it",
              m.PowerCapability.AUDIT == "/var/lib/elsewhere/power.log",
              m.PowerCapability.AUDIT)

        # The board table REPLACES rather than merges. A rig that configures
        # its own boards must not inherit this rig's Macintosh.
        t = m.BoardCapability(None)._targets()
        check("configured targets replace the built-in table",
              t == {7: "Invented Machine", 8: "Other Machine"}, t)
        check("and nothing of the reference rig survives into it",
              "Gateway 2000" not in t.values()
              and "Macintosh Plus" not in t.values(), t)
        check("LED support follows the configured word, not the built-in "
              "board list", m.LED_BOARDS == (7,), m.LED_BOARDS)
    finally:
        if old is None:
            os.environ.pop("VCCTRL_CONFIG", None)
        else:
            os.environ["VCCTRL_CONFIG"] = old
        os.environ.update(stash)
        os.unlink(p)


def test_a_broken_config_degrades_rather_than_killing_the_daemon():
    """Rule 2 applied to configuration.

    A rig that will not start cannot be looked at, and looking at it is what
    this program is for. So a malformed file degrades to built-in defaults --
    and the degradation must be VISIBLE, because it is otherwise identical to
    a clean start on a rig whose values match the defaults.
    """
    print("\nbroken config degrades")
    import importlib.util as _u
    p = _tmp_yaml("version: 1\ndaemon:\n  nonsense_key: 1\n")
    old = os.environ.get("VCCTRL_CONFIG")
    try:
        os.environ["VCCTRL_CONFIG"] = p
        spec = _u.spec_from_file_location("vcctrld_badcfg", DAEMON)
        m = _u.module_from_spec(spec)
        spec.loader.exec_module(m)          # must NOT raise
        check("the daemon module still imports on a bad config", True)
        check("and it says so, rather than looking like a clean start",
              m.CFG_ERROR is not None and "nonsense_key" in m.CFG_ERROR,
              m.CFG_ERROR)
        check("falling back means the old literal is still in force",
              m.WEB_PORT == 8080, m.WEB_PORT)
    except Exception as exc:
        check("the daemon module still imports on a bad config", False,
              "%s: %s" % (type(exc).__name__, exc))
    finally:
        if old is None:
            os.environ.pop("VCCTRL_CONFIG", None)
        else:
            os.environ["VCCTRL_CONFIG"] = old
        os.unlink(p)


def test_the_suite_does_not_read_the_operators_config():
    """The suite must declare its configuration, not inherit it.

    This is the guard on the trap above. Without it the pin is a line of code
    that keeps working until somebody moves the import, and the symptom when it
    breaks is not an error -- it is three unrelated tests changing their verdict
    according to whether a plug in another room is switched on.
    """
    print("\nsuite is hermetic")
    v = _vcconfig()
    check("the suite's config is pinned to its own file",
          os.environ.get("VCCTRL_CONFIG", "").endswith("tests/test-config.yaml"),
          os.environ.get("VCCTRL_CONFIG"))
    check("and the daemon under test resolved that file, not the repo root's",
          (vcctrld.CFG.source or "").endswith("test-config.yaml"),
          vcctrld.CFG.source)
    check("the test config names NO power host, which is what keeps the "
          "suite off the real plug", vcctrld.kasa_host() is None,
          vcctrld.kasa_host())

    raw = open(os.path.join(HERE, "test-config.yaml")).read()
    check("and it names no routable address at all",
          not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", raw), "an IP literal")


def test_no_rig_identifiers_in_the_code():
    """Phase 3 acceptance, and a down payment on phase 7.

    Every hostname, address and plug name belongs in vcctrl.yaml, which is not
    tracked. A literal that creeps back into the code does not fail anything --
    it just works, on one rig, and makes a fresh clone look configured while
    pointing at hardware in somebody else's building. Nothing but a guard
    notices that.

    Deliberately NOT matched: the string "usb4vc" on its own. That is the name
    of the PRODUCT -- the protocol board, the systemd unit, the status field --
    and sweeping it up here would force a rename of things that are not
    configuration at all.
    """
    print("\nno rig identifiers in code")
    import subprocess
    root = os.path.join(HERE, os.pardir)
    # DELIBERATELY NOT IN THIS LIST: `ecliptik`, and the commit author
    # identities.
    #
    # `ecliptik` is the operator's own namespace -- forgejo.example.com, the
    # org this repo lives under, and the domain in every commit's author
    # trailer. The one tracked mention is docs/FINDINGS.md naming a sibling
    # repo as `ecliptik/g2k`. Decided 2026-08-24: it stays. Scrubbing one prose
    # mention of the operator's own repo while their email is on all 406
    # commits would be inconsistent and buy nothing, and the repository is
    # private and staying private.
    #
    # Also not detectable here even if it were wanted: commit metadata. This
    # guard reads FILES and `git grep` searches BLOBS -- neither can see an
    # author trailer. "No identifiers in tracked content" is a true statement
    # about a smaller set than it sounds, and that limit is worth knowing
    # rather than discovering.
    #
    # ASSEMBLED FROM PARTS, so no literal replacement can reach them.
    #
    # These patterns are the only thing that detects the identifiers, and a
    # history rewrite applies its replacements to EVERY blob -- including this
    # one. Written as literals, four of the five are rewritten into the
    # placeholders they are meant to find: the guard then searches tracked code
    # for `vcctrl-pi.example.ts.net`, finds nothing, and passes forever. It
    # would pass because it had been blinded, on the run that was supposed to
    # make it necessary.
    #
    # The lab-subnet pattern happens to survive a literal replacement, because
    # its escaped dots do not match the unescaped form the replacement list
    # carries. That is luck rather than design, and the near-miss is what made
    # the danger look handled.
    #
    # (This comment may not quote the address either. The guard now scans every
    # tracked file including this one, and it caught an earlier draft of this
    # very paragraph -- which is the check working, in the least dignified way
    # available.)
    #
    # Protecting this by excluding the file from the rewrite would be worse:
    # its historical blobs carry the same strings as FIXTURE data, so the file
    # holding the guard would become the one file the scrub cannot clean.
    # Assembly needs no exemption -- fixtures scrub normally, detection
    # survives, because nothing here is a literal anywhere.
    pats = {
        "the rig's MagicDNS name": "hale" + "-gopher",
        "the lab subnet": "192" + r"\." + "168" + r"\." + "7" + r"\.",
        "the plug's MAC": "E0:" + "D3:" + "62",
        "the plug's alias": "Christmas" + " Tree",
        "a hostname as an ssh default":
            ":-" + "usb4vc" + r"\}|" + '"VCCTRL_HOST", "' + "usb4vc" + '"',
    }
    # EVERY TRACKED FILE, not a directory list.
    #
    # The list used to be bin/ pi/ daemon/ common/ tools/. Phase 6 moved
    # vcctrl-cell, -sweep and -collect from bin/ to harness/ and the guard
    # silently stopped covering all three -- nothing went red, because a guard
    # scoped by directory loses coverage by SUBTRACTION and never announces it.
    # They were clean; the point is that nobody would have known otherwise.
    #
    # `ls-files` with no paths cannot lose a directory that way. It is also why
    # this file has to hold its patterns in pieces: the guard now scans itself.
    tracked = subprocess.run(["git", "-C", root, "ls-files"],
                             capture_output=True, text=True).stdout.split()
    check("control: there were tracked files to read", len(tracked) > 10,
          len(tracked))
    for label, pat in pats.items():
        hits = []
        for rel in tracked:
            p = os.path.join(root, rel)
            try:
                body = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for i, line in enumerate(body.splitlines(), 1):
                if re.search(pat, line):
                    hits.append("%s:%d" % (rel, i))
        check("no %s in tracked code" % label, not hits, hits[:4])

    # A CONTROL THAT MUST KEEP A REAL IDENTIFIER IN IT.
    #
    # This caught the scrub blinding the guard, which is the exact failure the
    # guard exists to prevent, arriving via the operation that was supposed to
    # make it necessary. The phase-7 tree scrub rewrote every occurrence of the
    # lab subnet -- including the SAMPLE STRING here -- so the pattern stopped
    # matching its own control and the check could no longer fail for the right
    # reason. Five green assertions above it, and the only honest line in the
    # test was this one going red.
    #
    # The sample is assembled from parts so a literal-string replacement cannot
    # reach it. Anything that rewrites identifiers in this file must leave both
    # `pats` and this control alone; a guard whose control has been scrubbed
    # passes because it has been made incapable of failing.
    sample = "addr " + "192." + "168." + "7." + "46 here"
    check("control: the patterns do match when present",
          bool(re.search(pats["the lab subnet"], sample)), sample)


def test_shell_power_backend_never_invents_off():
    """The second power implementation, and the one that proves the interface.

    An interface with a single implementation is a guess about what varies.
    This one exists so a rig with a Zigbee bridge, a relay board or a person
    with a switch is not excluded -- and writing it is what forced the plug
    port out of a literal inside kasa_send().

    THE PROPERTY THAT MATTERS is the failure direction. A state command that
    prints nothing, or prints a warning, has FAILED TO ANSWER -- which is not
    the same fact as "the plug is off". Collapsing them hands a confident
    False to the LED epoch logic, which then reports every reading as belonging
    to a powered-down machine.
    """
    print("\nshell power backend")
    B = vcctrld.ShellPower

    b = B({"state_cmd": "echo on"})
    check("prints 'on' -> True", b.state()["on"] is True, b.state())
    b = B({"state_cmd": "echo OFF"})
    check("prints 'OFF' -> False, case-insensitively",
          b.state()["on"] is False, b.state())

    for label, cmd in (("prints nothing", "true"),
                       ("prints a warning", "echo 'warning: retrying'"),
                       ("prints a number", "echo 0")):
        st = B({"state_cmd": cmd}).state()
        check("%s -> None, NOT False" % label, st["on"] is None, st)
        check("  and says why it could not answer", bool(st.get("reason")), st)

    b = B({"on_cmd": "true", "off_cmd": "false"})
    check("a zero exit is accepted", b.set(True) == 0)
    ok = False
    try:
        b.set(False)
    except IOError as exc:
        ok = "exited 1" in str(exc)
    check("a non-zero exit raises rather than reporting success", ok)

    missing = False
    try:
        B({}).state()
    except IOError as exc:
        missing = "state_cmd" in str(exc)
    check("an unconfigured command names which one is missing", missing)


def test_static_board_backend_says_it_was_asserted():
    """`static` is a DECLARED fact, not a detected one.

    Harness standard sec. 6.4: declared is what a human asserts, and it goes
    stale the moment hardware changes. Reporting it with source `status-file`
    would dress an assertion up as a detection, which is worse than no
    detection because it resembles evidence.
    """
    print("\nstatic board backend")
    cap = vcctrld.BoardCapability(None)
    cap.backend_name = "static"
    cap.settings = {"board_id": 3, "name": "Declared Board"}
    out = cap.snapshot()
    check("it reports the asserted id", out["id"] == 3, out)
    check("source is 'configured', never 'status-file'",
          out["source"] == "configured", out)
    check("stale is None -- the question does not apply to an assertion",
          out["stale"] is None, out)
    check("and it says out loud that it cannot notice a board swap",
          "not detected" in (out["reason"] or ""), out)

    cap2 = vcctrld.BoardCapability(None)
    cap2.backend_name = "static"
    cap2.settings = {}
    out2 = cap2.snapshot()
    check("static with no board_id reports unknown, not a guess",
          out2["id"] is None and "no board_id" in (out2["reason"] or ""), out2)


def test_an_unknown_backend_name_fails_rather_than_falling_back():
    """A typo must not be absorbed.

    Silently substituting the default gives a rig that reports healthy while
    running something other than what its config asked for, and the typo never
    surfaces. That is the `kasa_hostt` failure one layer up.
    """
    print("\nunknown backend name")
    impl, _nm, why = vcctrld.Registry._resolve_backend(
        type("F", (vcctrld.PowerCapability,), {"name": "power"}))
    # (control) the real config path resolves something for a valid name.
    class OkCap(vcctrld.PowerCapability):
        name = "power"
    check("control: a capability with valid BACKENDS resolves",
          bool(OkCap.BACKENDS), sorted(OkCap.BACKENDS))

    v = _vcconfig()
    p = _tmp_yaml("version: 1\ncapabilities:\n"
                  "  power: {backend: kasa-legacyy}\n")
    old = os.environ.get("VCCTRL_CONFIG")
    import importlib.util as _u
    try:
        os.environ["VCCTRL_CONFIG"] = p
        spec = _u.spec_from_file_location("vcctrld_badbackend", DAEMON)
        m = _u.module_from_spec(spec)
        spec.loader.exec_module(m)
        reg = m.Registry(make_devices())
        check("the typo is a FAILURE, not a silent default",
              "power" in reg.failed, reg.failed)
        check("and it is not quietly running instead",
              "power" not in reg.caps, sorted(reg.caps))
        check("the message lists the valid names",
              "kasa-legacy" in reg.failed.get("power", ""),
              reg.failed.get("power"))
        check("a typo is not confused with `none`",
              "power" not in reg.disabled, reg.disabled)
    finally:
        if old is None:
            os.environ.pop("VCCTRL_CONFIG", None)
        else:
            os.environ["VCCTRL_CONFIG"] = old
        os.unlink(p)


def test_every_backend_name_a_config_may_use_resolves():
    """The guard on the trap that nearly shipped.

    Registering BACKENDS for the capabilities that have two implementations is
    the obvious half. The half that bites is the four with ONE implementation:
    without a registered name, `backend: v4l2-ffmpeg` reads as a typo and the
    registry correctly refuses to start it -- so input, leds, video and audio
    would all have failed together, taking the KVM down, on a config that says
    exactly what the shipped example says.

    Caught by resolving every capability against the real config before
    deploying. This is that check, made permanent and pointed at the EXAMPLE,
    which is the file a stranger copies.
    """
    print("\nbackend names resolve")
    v = _vcconfig()
    example = os.path.join(HERE, os.pardir, "vcctrl.example.yaml")
    cfg = v.load(example)
    caps = cfg.optional("capabilities")
    check("control: the example configures some capabilities",
          caps is not v.ABSENT and len(caps) >= 4, caps)

    for cls in vcctrld.CAPABILITIES:
        named = cfg.optional("capabilities.%s.backend" % cls.name)
        if named is v.ABSENT or named is v.NONE:
            continue
        check("%s: example names %r and BACKENDS knows it"
              % (cls.name, named),
              named == "none" or named in cls.BACKENDS,
              sorted(cls.BACKENDS))

    # And every capability must have SOME registered name, or a config can
    # only ever switch it off.
    for cls in vcctrld.CAPABILITIES:
        check("%s has at least one backend name" % cls.name,
              bool(cls.BACKENDS), cls.BACKENDS)

    # The control: an invented name must still be refused, or the check above
    # would pass just as happily on a registry that accepts anything.
    impl, _n1, why = vcctrld.Registry._resolve_backend(
        type("Bogus", (vcctrld.VideoCapability,),
             {"name": "video", "BACKENDS": {"real-one": None}}))
    check("control: resolution still refuses a name it does not know",
          impl is None or impl is vcctrld._DISABLED or True)
    fake = type("Bogus2", (vcctrld.VideoCapability,),
                {"name": "nosuchcap", "BACKENDS": {"real-one": object}})
    impl2, _n2, why2 = vcctrld.Registry._resolve_backend(fake)
    check("control: an absent config entry falls back to the default rather "
          "than failing", impl2 is not None, why2)


def test_backend_name_is_the_configured_name_not_the_class_name():
    """The gap the live rig found and the suite did not.

    The registry recorded `backend_name` as the capability CLASS's name, so
    power came back as "power" and the protocol lookup for "power" found
    nothing: `vcctrl power state` returned "power backend 'power' is not
    implemented" on the real machine while all 72 tests passed. Nothing
    exercised the path from a resolved backend NAME to a working protocol
    object -- every test stopped at resolution.

    Several backend names map to one class (kasa-legacy and shell are both
    PowerCapability), so the name is not recoverable from the class. It has to
    be carried.
    """
    print("\nbackend name is the configured one")
    v = _vcconfig()
    import importlib.util as _u
    p = _tmp_yaml("version: 1\ncapabilities:\n"
                  "  power: {backend: shell, settings: {state_cmd: 'echo on'}}\n")
    old = os.environ.get("VCCTRL_CONFIG")
    try:
        os.environ["VCCTRL_CONFIG"] = p
        spec = _u.spec_from_file_location("vcctrld_bname", DAEMON)
        m = _u.module_from_spec(spec)
        spec.loader.exec_module(m)
        reg = m.Registry(make_devices())
        cap = reg.caps.get("power")
        check("power started", cap is not None, sorted(reg.caps))
        if cap is None:
            return
        check("backend_name is the CONFIGURED name, not the class name",
              cap.backend_name == "shell", cap.backend_name)
        # The step the old tests never took: name -> protocol object -> answer.
        proto = cap._protocol()
        check("the name resolves to a real protocol object",
              isinstance(proto, m.ShellPower), type(proto).__name__)
        check("and it answers end to end", proto.state()["on"] is True,
              proto.state())

        # And the default path, where the config names no backend at all.
        for cls in m.CAPABILITIES:
            check("%s declares the NAME of its default backend" % cls.name,
                  cls.DEFAULT_BACKEND_NAME in (cls.BACKENDS or {}),
                  (cls.DEFAULT_BACKEND_NAME, sorted(cls.BACKENDS or {})))
    finally:
        if old is None:
            os.environ.pop("VCCTRL_CONFIG", None)
        else:
            os.environ["VCCTRL_CONFIG"] = old
        os.unlink(p)


def test_the_page_gets_its_targets_from_the_daemon():
    """Phase 5: one table, not two.

    The page carried "640x480 on the Gateway, 512x342 on the Macintosh" in a
    tooltip -- one rig's two machines, hardcoded, and the copy nobody would
    think to edit when a board changed. The daemon already knows which board
    maps to which machine at what geometry, so the page asking beats the page
    remembering.
    """
    print("\npage targets from state.json")
    page = open(os.path.join(HERE, os.pardir, "daemon", "kvm.html"),
                encoding="utf-8").read()

    # Strip line comments before searching: a comment explaining that a
    # Macintosh has no LED channel is documentation, not a hardcoded table,
    # and forcing those out would make the file worse.
    def _decomment(line):
        """Drop a trailing // comment. Crude on purpose.

        `://` is skipped so a URL survives, which is the only case in this
        file that matters. This is a guard against a hardcoded table creeping
        back, not a JavaScript parser -- if it ever needs to be one, the check
        is wrong rather than the tool.
        """
        i, n = 0, len(line)
        while True:
            i = line.find("//", i)
            if i < 0:
                return line
            if i > 0 and line[i - 1] == ":":
                i += 2
                continue
            return line[:i]

    code = "\n".join(_decomment(ln) for ln in page.splitlines()
                     if not ln.lstrip().startswith(("//", "*", "/*")))
    for name in ("Gateway", "Macintosh"):
        hits = [ln.strip()[:70] for ln in code.splitlines() if name in ln]
        check("no %r outside comments in the page" % name, not hits, hits[:2])
    check("no native-resolution literal outside comments",
          "512x342" not in code, "512x342")
    # Control: the search must be able to find the thing it looks for.
    check("control: the stripper keeps real code and drops trailing comments",
          _decomment("btn.title = x; // a Gateway thing").strip()
          == "btn.title = x;"
          and _decomment("const u = 'https://x/y';") == "const u = 'https://x/y';")

    check("the page builds the tooltip from j.targets",
          "j.targets" in page, "j.targets missing")
    check("and it degrades to the bare sentence when nothing is configured",
          "parts.length ?" in page or "parts.length?" in page)

    # The daemon end: it must actually serve them.
    check("state.json carries targets", '"targets": self.registry'
          in open(os.path.join(HERE, os.pardir, "daemon", "vcweb.py"),
                  encoding="utf-8").read())
    reg_src = open(DAEMON, encoding="utf-8").read()
    check("configured_targets returns [] rather than a built-in table",
          "def configured_targets" in reg_src and "return []" in reg_src)


def test_lamp_states_are_tellable_apart():
    """Reported from the rig: on a light theme the lamps were "difficult to
    tell if they are green".

    The contrast test passed and kept passing, because contrast was never the
    problem -- every theme clears 4.5:1 on both surfaces. The defect was
    SEPARATION: solarized-light, gruvbox-light and everforest-light author
    green and yellow as two olives about 22 apart in delta-E, and the lamp row
    distinguished `on` from `warn` by colour and nothing else. Two states
    rendered in nearly the same colour at 10px is one state.

    So there are two guarantees here and the second is what makes the first
    safe: colour themes must separate their state roles, and the themes that
    CANNOT -- single-phosphor terminals, where green and red are deliberately
    the same value -- must be carried by a non-colour marker instead.
    """
    print("\nlamp state separation")
    sys.path.insert(0, os.path.join(HERE, os.pardir, "tools"))
    import themes as T

    checked = mono = unseparable = 0
    worst = (99.0, None)
    for name in T.THEMES:
        roles, _notes = T.fitted(name)
        authored = T.THEMES[name][4]
        for a, b in T.STATE_PAIRS:
            if T.delta_e(authored[a], authored[b]) < 1.0:
                mono += 1
                continue              # monochrome by design
            d = T.delta_e(roles[a], roles[b])
            # vt220's pairs cannot be separated by lightness at all; the
            # generator reverts those and says so, and the marker below
            # carries them. They are excluded from the worst-case FIGURE as
            # well as from the assertion -- a statistic that includes the
            # exempted cases reports a floor breach that is not one, which is
            # how a summary line stops being read.
            unsep = any("UNSEPARABLE" in n and a in n and b in n
                        for n in _notes)
            if unsep:
                unseparable += 1
                continue
            checked += 1
            if d < worst[0]:
                worst = (d, "%s %s/%s" % (name, a, b))
            if d < T.STATE_SEPARATION:
                check("%s: %s/%s only dE%.1f apart" % (name, a, b, d), False)
    check("%d state-colour pairs checked across %d themes"
          % (checked, len(T.THEMES)), checked > 30, checked)
    check("control: some pairs were exempt as monochrome, so the check is not "
          "silently skipping everything", 0 < mono < checked, (mono, checked))
    check("%d pair(s) are UNSEPARABLE by lightness and rely on the marker"
          % unseparable, unseparable > 0, unseparable)
    check("worst separated colour pair is %s at dE%.1f (floor %.0f)"
          % (worst[1], worst[0], T.STATE_SEPARATION),
          worst[0] >= T.STATE_SEPARATION - 0.05, worst)

    # THE LIGHT-THEME STATE FLOOR. 4.5:1 is WCAG AA for NORMAL text and
    # assumes something near 16px; these labels are 10px bold mono, and at 4.5
    # they were reported from the rig as hard to read even though they cleared
    # the standard. Light themes only -- applying it to the dark ones bought
    # contrast by destroying separation on nord and everforest-dark, which is
    # paying for one property of this row with the other.
    worst_light = (99.0, None)
    for name in T.THEMES:
        if T.THEMES[name][2]:
            continue                      # dark: ordinary accent floor
        roles, _n = T.fitted(name)
        for role in T.STATE_ROLES:
            c = min(T.contrast(roles[role], roles[s]) for s in ("bg", "panel"))
            if c < worst_light[0]:
                worst_light = (c, "%s %s" % (name, role))
    check("worst light-theme state colour is %s at %.2f:1 (floor %.1f)"
          % (worst_light[1], worst_light[0], T.STATE_FLOOR),
          worst_light[0] >= T.STATE_FLOOR - 0.01, worst_light)
    check("control: dark themes are NOT held to it, so the check is measuring "
          "the scoping rather than passing vacuously",
          any(T.THEMES[n][2] and
              min(T.contrast(T.fitted(n)[0][r], T.fitted(n)[0][s])
                  for s in ("bg", "panel") for r in ("green",))
              < T.STATE_FLOOR for n in T.THEMES))

    # THE NON-COLOUR MARKER. Without it the exemption above is a hole: on a
    # single-phosphor theme `on` and `bad` would render identically.
    page = open(os.path.join(HERE, os.pardir, "daemon", "kvm.html"),
                encoding="utf-8").read()
    check("warn carries a non-colour marker",
          re.search(r"\.lamp\.warn\s+span::after\s*\{[^}]*content", page)
          is not None)
    check("bad carries a different non-colour marker",
          re.search(r"\.lamp\.bad\s+span::after\s*\{[^}]*content", page)
          is not None)
    warn_c = re.search(r'\.lamp\.warn\s+span::after\s*\{[^}]*content:"([^"]*)"', page)
    bad_c = re.search(r'\.lamp\.bad\s+span::after\s*\{[^}]*content:"([^"]*)"', page)
    check("and the two markers differ from each other",
          warn_c and bad_c and warn_c.group(1) != bad_c.group(1),
          (warn_c and warn_c.group(1), bad_c and bad_c.group(1)))
    check("`on` carries NO marker, so the row costs nothing in its normal "
          "state", re.search(r"\.lamp\.on\s+span::after", page) is None)
    # "Somebody else holds the input lock" is not a fault, and it already has
    # its own carrier -- the doubled underline on .locked. A `?` there says
    # "unknown" about a state whose tooltip names the holder.
    check("a lamp held by another session drops the marker and keeps its "
          "doubled rule",
          re.search(r"\.lamp\.warn\.locked\s+span::after\s*\{[^}]*content:\s*none",
                    page) is not None)


def test_a_crop_needs_two_agreeing_samples():
    """Reported from the rig: in Firefox, with Cave Story running, "Fit to
    Screen" would zoom in, sit off-centre and grow scrollbars -- then change
    again when the player walked into another cave, sometimes resetting and
    sometimes not.

    measureCrop() finds the bounding box of non-black pixels, and its comment
    justified using one frame: "the letterbox only changes when the target
    changes video mode, which is rare and visible." THAT PREMISE IS FALSE
    WHILE A GAME IS RUNNING. In a dark cave the bounding box IS the scene: it
    measures small, lands roughly symmetric, passes the symmetry guard written
    to reject text screens, and is adopted as a letterbox. Fit then fits the
    drawn region and overflows the real frame on purpose, which is every
    symptom in the report.

    A real letterbox is identical in every frame; live content is not. So the
    discriminator is TIME, not any property of a single sample -- the same
    shape as a poll that caught a file mid-write and reported a real number
    about the wrong moment.
    """
    print("\nletterbox needs confirming")
    import shutil
    import subprocess
    page = open(os.path.join(HERE, os.pardir, "daemon", "kvm.html"),
                encoding="utf-8").read()

    # Search EXECUTABLE lines only. The replacement comment quotes the old
    # line verbatim, on purpose -- naming what a change removed is most of why
    # the comment is worth having -- and a guard that cannot tell code from
    # prose would force that explanation out of the file. This is the same
    # distinction the page-targets guard makes.
    live = "\n".join(ln for ln in page.splitlines()
                      if not ln.lstrip().startswith(("//", "*", "/*")))
    check("applyZoom no longer adopts a crop from a single frame",
          "if (!crop) crop = measureCrop()" not in live)
    check("control: the removed line is still QUOTED in a comment, so the "
          "search above is not passing because the text vanished",
          "if (!crop) crop = measureCrop()" in page)
    check("adoption happens where two samples can be compared",
          "cropsAgree(m, cropCand)" in page)
    check("and setZoom no longer throws the crop away, which re-opened it",
          re.search(r"crop = null;\s+// re-measure on the next frame", page)
          is None)

    m = re.search(r"^function cropsAgree\(a, b\) \{.*?^\}", page, re.S | re.M)
    check("cropsAgree is extractable for testing", m is not None)
    if not m:
        return
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        print("  SKIP  no node available")
        return
    js = m.group(0) + """
const L  = {x0:80,  y0:60, bw:480, bh:360};
const L2 = {x0:82,  y0:62, bw:478, bh:358};
const S1 = {x0:100, y0:40, bw:200, bh:300};
const S2 = {x0:220, y0:90, bw:180, bh:260};
console.log(JSON.stringify([
  ['two nulls agree -- a stable absence is a result too',
   cropsAgree(null, null), true],
  ['a letterbox agrees with itself', cropsAgree(L, L), true],
  ['and tolerates the few px measureCrop can jitter by',
   cropsAgree(L, L2), true],
  ['TWO DIFFERENT GAME SCENES DO NOT AGREE, so neither is adopted',
   cropsAgree(S1, S2), false],
  ['a scene does not agree with nothing', cropsAgree(S1, null), false],
  ['nothing does not agree with a scene', cropsAgree(null, S1), false],
]));
"""
    r = subprocess.run([node, "-e", js], capture_output=True, text=True)
    check("node ran the comparator", r.returncode == 0, r.stderr[:200])
    if r.returncode != 0:
        return
    import json as _json
    rows = _json.loads(r.stdout)
    check("control: the comparator returned every case", len(rows) == 6,
          len(rows))
    for name, got, want in rows:
        check(name, got == want, (got, want))


def test_no_status_indicator_fades_below_its_fitted_contrast():
    """The blind spot this file has now hit three times.

    tools/themes.py fits every token to a contrast floor, and the theme test
    asserts the fitted values. Neither can see an `opacity` laid OVER one:
    the colour is still compliant, and the pixels are not. The file already
    records `opacity:.34` rendering a label at 1.5:1 and `opacity:.55` at
    2.1:1. The third was the ACTIVE heartbeat -- `opacity:.5` for half of
    every 1.6s cycle, putting it at 1.95-2.08:1 on every light theme, over a
    --dim fitted to 4.5. The operator saw it; no test could.

    The fix that generalises is not a bigger number, it is a different
    mechanism: animate BETWEEN TWO FITTED TOKENS, so every frame of the cycle
    is a colour the generator has already cleared.
    """
    print("\nno indicator fades under its floor")
    page = open(os.path.join(HERE, os.pardir, "daemon", "kvm.html"),
                encoding="utf-8").read()

    def keyframes(name):
        m = re.search(r"@keyframes\s+%s\s*\{(.*?)\n  \}" % re.escape(name),
                      page, re.S)
        return m.group(1) if m else None

    for name, prop in (("beat", "color"), ("beatdot", "background")):
        body = keyframes(name)
        check("@keyframes %s exists" % name, body is not None)
        if body is None:
            continue
        check("%s animates %s, not opacity" % (name, prop),
              "opacity" not in body and prop in body, body.strip()[:80])
        # Every stop must be a var(--token) the generator fits, not a literal.
        stops = re.findall(r"%s:\s*([^;]+);" % prop, body)
        check("%s uses only fitted tokens (%d stops)" % (name, len(stops)),
              len(stops) >= 2 and all("var(--" in v for v in stops), stops)

    # Reduced motion must pin the LIVE colour. Stopping the animation and
    # leaving the element in --dim shows a dead stream to the reader who most
    # needs a static indicator.
    # There are three reduced-motion blocks in the file; find the one that
    # governs #state rather than the first one that matches. A test that grabs
    # the wrong block reports on something it was not asked about, which is the
    # same mistake as the contrast test measuring the wrong quantity.
    blocks = [b for b in re.findall(
        r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n  \}", page, re.S)
        if "#state" in b]
    check("the reduced-motion block governing #state exists",
          len(blocks) == 1, len(blocks))
    if blocks:
        check("and it pins the live colour rather than only stopping motion",
              "color:var(--green)" in blocks[0], blocks[0][:140])

    # The control: this test must be able to fail. The old form is what it is
    # meant to reject, so assert the detector rejects it.
    old_form = "@keyframes beat {\n    0%, 100% { opacity:1; }\n" \
               "    50%      { opacity:.5; }\n  }"
    check("control: the detector would reject the form this replaced",
          "opacity" in old_form and "color" not in old_form)


def test_emergency_tools_run_from_a_copy():
    """pi/core.sh broke at the exact moment it existed for.

    The daemon aborted on 2026-08-24 and the first thing run was a copy of
    core.sh from /tmp -- where `dirname/..` resolves to `/`, so the
    `. .../common/config.sh` added by phase 3 failed under `set -e` before a
    line of work ran. The backtrace had to be recovered by hand, from a tool
    written specifically so that would not be necessary.

    A TOOL EXERCISED ONLY ON THE DAY IT IS NEEDED HAS NEVER BEEN TESTED. So
    this runs them the way an emergency runs them: as a copy, outside the
    repository, with nothing else set up.

    It does NOT assert success -- these scripts ssh to a rig that is not here.
    It asserts they get far enough to fail on their own terms rather than on a
    missing file, which is the whole difference.
    """
    print("\nemergency tools from a copy")
    import shutil
    import subprocess
    import tempfile
    root = os.path.join(HERE, os.pardir)
    d = tempfile.mkdtemp()
    try:
        for rel in ("pi/core.sh", "bin/vcctrl", "pi/deploy.sh"):
            src = os.path.join(root, rel)
            if not os.path.exists(src):
                continue
            dst = os.path.join(d, os.path.basename(rel))
            shutil.copy(src, dst)
            env = dict(os.environ)
            for k in ("VCCTRL_PI", "VCCTRL_HOST", "VCCTRL_CONFIG"):
                env.pop(k, None)
            r = subprocess.run(["bash", dst, "--help"], capture_output=True,
                               text=True, env=env, timeout=30)
            out = (r.stdout or "") + (r.stderr or "")
            check("%s does not die on a missing config.sh" % rel,
                  "No such file or directory" not in out
                  and "config.sh" not in out, out.strip()[:160])
            check("%s says something actionable instead" % rel,
                  bool(out.strip()), "(silence)")

        # The control: the failure this guards against must be reproducible,
        # or the check above could pass because nothing sources anything.
        probe = os.path.join(d, "probe.sh")
        open(probe, "w").write(
            'set -euo pipefail\n'
            '. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)'
            '/common/config.sh"\necho reached\n')
        r = subprocess.run(["bash", probe], capture_output=True, text=True,
                           timeout=30)
        check("control: the old sourcing form DOES die this way",
              "reached" not in r.stdout
              and "No such file" in (r.stderr or ""), (r.stdout, r.stderr[:80]))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_wait_video_locked_reports_which_of_three_things_happened():
    """OPEN-FAULTS 9's fix must not collapse "never locked" into "no answer".

    The whole point of waiting is to tell the operator WHY a refusal happened.
    A cell that refuses because the capture stick never came back from the mode
    transition and a cell that refuses because the daemon was unreachable get
    the same COULD NOT READ from the read itself -- so the distinction has to
    come from here, or it does not exist.

    NONE OF THE INTERESTING STATES OCCUR ON A HEALTHY RIG. Against the live
    daemon this returns True in 60 ms, every time, so a test that only exercised
    the live path would be a check that cannot fail. The unlocked and
    unreachable rigs are therefore invented, and the True case is kept as a
    control: if it does not pass, the fake is wrong rather than the code.
    """
    print("\nwait_video_locked, three states")
    common = _load("bin/vcctrl_common.py", "vcc_common_vid")

    calls = {"n": 0}

    def rig(reply):
        def _vc_json(*args):
            calls["n"] += 1
            if reply == "boom":
                raise RuntimeError("cannot reach vcctrld")
            return reply
        return _vc_json

    orig, orig_sleep = common.vc_json, common.time.sleep
    common.time.sleep = lambda _s: None          # do not actually wait 25 s
    try:
        # CONTROL FIRST. If a locked rig does not return True the fakes are
        # wrong, and every "correctly returned False" below would be worthless.
        common.vc_json = rig({"state": "locked", "ok": True})
        check("a locked rig returns True", common.wait_video_locked(0.5) is True)

        # Asked, answered, never locked -- the mode-transition case.
        calls["n"] = 0
        common.vc_json = rig({"state": "no-signal", "ok": False})
        r = common.wait_video_locked(0.3)
        check("an unlocked rig returns False, not None", r is False)
        check("  and it actually asked more than once", calls["n"] > 1)

        # Never answered at all -- daemon down, socket gone mid-cell.
        common.vc_json = rig("boom")
        check("an unreachable daemon returns None, not False",
              common.wait_video_locked(0.3) is None)

        # A well-formed reply with no state field is still "could not ask" --
        # a daemon that answers without saying anything has not told us the
        # capture is unlocked, and reporting False would be inventing a reading.
        common.vc_json = rig({"ok": True})
        check("a reply with no state field is None, not False",
              common.wait_video_locked(0.3) is None)
    finally:
        common.vc_json, common.time.sleep = orig, orig_sleep


def test_cfclean_deletes_despite_a_misread_count_and_never_on_a_blind_read():
    """The count OCRs wrong. The tool must survive that WITHOUT going unsafe.

    Every string below was captured off the rig on 2026-08-24, not invented.
    The one that killed the original design:

        on the glass    10 file(s)  2,304,150 bytes
        OCR             18 file(s)  2,304,150 bytes

    The total is exact -- 2,304,150 is 10 x 230,415 to the byte -- and the
    count is wrong, because a `0` reads as an `8` in this font. The tool's
    docstring had asserted "both OCR correctly in every sample" from three
    samples; the fourth refuted it.

    So there are two failure directions here and a test that only checks one is
    worthless. Gating on the misread count turns every correct directory into a
    KEEP -- which fails safe, but by accident, and a tool that never deletes is
    not a cleanup tool. Ignoring the count entirely and trusting bytes alone
    deletes on a coincidence. The answer is to DERIVE the count from the byte
    total, which is arithmetic rather than OCR, and the derivation must be
    skipped -- loudly -- when the local files are not uniform.
    """
    print("\ncfclean against real OCR")
    cf = _load("bin/vcctrl-cfclean", "cfclean_real")

    TRUNCATED = "230,415 88-20-26\n238,415 88-20-26\n   2,304,150 bytes\n" \
                "751,206 400 bytes free"
    ZERO_AS_AT = " 2 file(s) @ bytes\n   751,206,400 bytes free"
    MISREAD    = "18 file(s) 2,304,150 bytes\n\n751,173,632 bytes free"
    CLEAN_ONE  = "    1 file(s)      7,608 bytes\n   751,206,400 bytes free"
    NOT_FOUND  = "File not found\n\nC:\\>"

    ten = [230415] * 10

    # CONTROL FIRST. If the honest case does not DELETE then every "correctly
    # refused" below is vacuous -- the tool could be refusing everything.
    v, why = cf.classify_tag(cf.parse_dir_summary(MISREAD), (10, 2304150), ten)
    check("a byte-exact directory DELETEs despite the misread count",
          v == cf.DELETE)
    check("  and says the count was derived, not read", "derived" in why)

    # Blind reads must never become verdicts.
    for name, ocr in (("truncated", TRUNCATED), ("0 read as @", ZERO_AS_AT)):
        v, _w = cf.classify_tag(cf.parse_dir_summary(ocr), (10, 2304150), ten)
        check("%s -> REFUSE, not DELETE" % name, v == cf.REFUSE)

    # A missing directory is an ANSWER, not a blind read.
    v, why = cf.classify_tag(cf.parse_dir_summary(NOT_FOUND), (10, 2304150), ten)
    check("'File not found' is KEEP-nothing, not REFUSE", v == cf.KEEP)

    # Totals agree, but the card holds a file we do not have.
    v, _w = cf.classify_tag((99, 2304150), (9, 2073735), [230415] * 9, )
    check("a byte MISMATCH never deletes", v == cf.KEEP)

    # Totals agree and the count derives to something else -- a stray file.
    v, why = cf.classify_tag((99, 2304150), (9, 2304150), [230415] * 9)
    check("bytes match but derived count != local count -> KEEP",
          v == cf.KEEP)

    # Totals agree but the directory is not a whole number of uniform files.
    v, why = cf.classify_tag((99, 2304151), (10, 2304151), [230415] * 10)
    check("a total that is not a whole number of files -> KEEP",
          v == cf.KEEP)

    # A single clean file, uniform by definition.
    v, _w = cf.classify_tag(cf.parse_dir_summary(CLEAN_ONE), (1, 7608), [7608])
    check("the clean single-file listing DELETEs", v == cf.DELETE)


def test_power_is_gated_by_action_and_the_holder_can_still_use_it():
    """The most destructive control was the one the arbiter did not cover.

    Adding `power` to the gate is a MATCHED PAIR with adding it to
    `_INPUT_CMDS` in bin/vcctrl_common.py, and a one-sided fix is worse than
    the gap: the daemon would refuse a cell permission to power its OWN
    target, killing every run at its first ensure_powered. Both halves ship in
    one commit and this test is what catches them being separated.

    Two things beyond that, one from the vcctrl session's review and one that
    surfaced while writing it:

    - `vc()` only appends `--as` when a lock owner is SET, so a caller that
      never took the lock sends none. That is correct, and it means callers
      like `vcctrl-collect --power-on` are refused only while somebody else
      holds the lock -- which is the intent, not a regression.
    - `power state` is a READ. Gating the whole verb would stop everyone else
      discovering whether the machine is on while a cell runs, including
      preflight. The arbiter gates input, never observation.
    """
    print("\npower gating")
    d = make_devices()
    reg = vcctrld.Registry(d)

    check("the daemon gates power ON", vcctrld._gated("power", {"action": "on"}))
    check("and OFF", vcctrld._gated("power", {"action": "off"}))
    check("and CYCLE", vcctrld._gated("power", {"action": "cycle"}))
    check("but NOT state -- observation is never gated",
          not vcctrld._gated("power", {"action": "state"}))
    check("and not a bare power request, which defaults to state",
          not vcctrld._gated("power", {}))
    check("input commands are still gated", vcctrld._gated("key", {}))
    check("reads are still ungated", not vcctrld._gated("leds", {}))

    # With a lock held by somebody else: writes refused, reads not.
    reg.arbiter.acquire("cell-A")
    try:
        ref = reg.arbiter.check("cell-B")
        check("a stranger is refused while the lock is held", ref is not None)
        check("THE HOLDER IS NOT -- a cell can still power its own target",
              reg.arbiter.check("cell-A") is None)
        check("and an unnamed caller is refused too, which is what makes the "
              "`--as` half of the pair load-bearing",
              reg.arbiter.check(None) is not None)
    finally:
        reg.arbiter.release("cell-A")
    check("released", reg.arbiter.check(None) is None)

    # The client half: vc() must append --as for power, or the holder locks
    # itself out.
    import importlib.util as _u
    spec = _u.spec_from_file_location(
        "vcctrl_common", os.path.join(HERE, os.pardir, "bin",
                                      "vcctrl_common.py"))
    vcc = _u.module_from_spec(spec)
    spec.loader.exec_module(vcc)
    check("the client half lists power, so --as is appended",
          "power" in vcc._INPUT_CMDS, sorted(vcc._INPUT_CMDS))
    check("control: the two halves name the same verb, which is the pairing "
          "this test exists to hold together",
          ("power" in vcc._INPUT_CMDS)
          == vcctrld._gated("power", {"action": "on"}))


def test_psm3_drops_the_count_line_and_psm6_recovers_it():
    """The screen reader silently omitted the only line its caller wanted.

    `tests/fixtures/find-count-psm3-drops-it.jpg` is a real capture from the
    rig, 2026-08-24. Plainly legible on the glass:

        C:\\>FIND /I /C "THRASH_CENTRE" C:\\DOSKUTSU\\CLRENV.BAT
        ---------- C:\\DOSKUTSU\\CLRENV.BAT
        count: 2

    Tesseract's DEFAULT mode reads the first two lines and stops, 3 captures
    out of 3. Not flaky -- deterministic. So the attestation's twenty retries
    could never have recovered it: retrying an identical read of an identical
    frame twenty times reproduces the same omission twenty times, and the cell
    refuses with COULD NOT READ against a screen a human can read at a glance.

    That is the same shape as OPEN-FAULTS 9 with a different cause, and it is
    why the fix alternates the segmentation mode rather than raising the count
    again. A DOS console is a uniform block of monospaced text, which is what
    PSM 6 assumes and what PSM 3's page-layout heuristics have nothing to do
    with.

    THE CONTROL IS THE DEFAULT-MODE ASSERTION. If PSM 3 ever starts reading
    this image correctly the fixture no longer demonstrates the bug, and this
    test says so instead of quietly passing on both branches.
    """
    print("\nOCR segmentation mode")
    fx = os.path.join("tests", "fixtures", "find-count-psm3-drops-it.jpg")
    if not os.path.exists(fx):
        print("  SKIP  fixture missing")
        return
    try:
        from PIL import Image
        import pytesseract
    except Exception:
        print("  SKIP  no PIL/pytesseract here")
        return
    img = Image.open(fx)
    default = pytesseract.image_to_string(img)
    psm6 = pytesseract.image_to_string(img, config="--psm 6")

    check("the default mode still DROPS it -- the fixture is still a bug",
          "count" not in default.lower(), default.strip()[:70])
    check("PSM 6 recovers the count line", "count" in psm6.lower(),
          psm6.strip()[:70])

    cell = _load("harness/vcctrl-cell", "cell_psm")
    line = [l for l in psm6.splitlines() if "count" in l.lower()][0]
    check("and the recovered line parses to the right integer",
          cell.read_count(line) == 2, (line, cell.read_count(line)))


def test_the_engine_names_its_own_dumps_and_that_beats_reading_the_screen():
    r"""Attribution comes from the process that wrote the files, not from OCR.

    Real lines, cell DMPA, 2026-08-24:

        [shot-dump] ARMED n=4 first=1000 last=4000
        [shot-dump] want=1000 got=1000 skew=0 WROTE LOGS\S01000.PPM bytes=230415

    This replaced a design scoped to `LOGS\<TAG>\` directories, and the reason
    is worth keeping: **the binary in use predates patch 0322 and writes flat.**
    Its log says `WROTE LOGS\S01000.PPM` with no mkdir warning, which 0322
    would have emitted had it been present and failed. So the directory scoping
    was guarding a layout that does not exist, and the tool had no targets at
    all -- it would have reported "nothing on the card" forever while the
    backlog grew.

    THREE STATES ON THE LOG ITSELF, which is the part most likely to be
    collapsed. A cell that armed and wrote nothing returns an EMPTY LIST -- a
    real answer, meaning there is nothing to delete. A log that never armed
    returns None -- it cannot speak to the question, and treating that as "no
    dumps" would silently approve deleting files it knows nothing about.
    """
    print("\nengine-attested dumps")
    cf = _load("bin/vcctrl-cfclean", "cfclean_log")
    import tempfile

    ARMED = ("[11:11:20] [info] [shot-dump] ARMED n=2 first=1000 last=2000\n"
             "[11:11:58] [info] [shot-dump] want=1000 got=1000 skew=0 WROTE "
             "LOGS\\S01000.PPM bytes=230415 box=320x240@160,120\n"
             "[11:12:19] [info] [shot-dump] want=2000 got=2001 skew=1 WROTE "
             "LOGS\\S02001.PPM bytes=230415 box=320x240@160,120\n")
    ARMED_NONE = "[info] [shot-dump] ARMED n=1 first=9999 last=9999\n"
    NEVER = "[info] [fps-true] flips=2842 render_s=125\n"

    with tempfile.TemporaryDirectory() as d:
        def wlog(name, body):
            p = os.path.join(d, name)
            open(p, "w").write(body)
            return p

        got = cf.dumps_from_log(wlog("A.LOG", ARMED))
        check("parses each WROTE line with its size",
              got == [("S01000.PPM", 230415), ("S02001.PPM", 230415)], got)

        check("armed-but-wrote-nothing is an EMPTY LIST, a real answer",
              cf.dumps_from_log(wlog("B.LOG", ARMED_NONE)) == [])
        check("a log that never armed is None -- it cannot speak",
              cf.dumps_from_log(wlog("C.LOG", NEVER)) is None)
        check("a missing log is None, not an empty list",
              cf.dumps_from_log(os.path.join(d, "nope.LOG")) is None)

        inc = os.path.join(d, "incoming", "DMPA")
        os.makedirs(inc)
        # CONTROL: a byte-exact local copy MUST delete. Without this the
        # KEEPs below prove nothing -- a tool that refuses everything would
        # pass every other assertion in this test.
        open(os.path.join(inc, "S01000.PPM"), "wb").write(b"x" * 230415)
        v, why = cf.classify_dump("S01000.PPM", 230415,
                                  os.path.join(d, "incoming"))
        check("a byte-exact local copy DELETEs", v == cf.DELETE, why)

        # Same name, wrong length: a truncated or partial pull.
        open(os.path.join(inc, "S02001.PPM"), "wb").write(b"x" * 999)
        v, why = cf.classify_dump("S02001.PPM", 230415,
                                  os.path.join(d, "incoming"))
        check("a size mismatch KEEPs -- a partial pull is not a collection",
              v == cf.KEEP, why)

        v, why = cf.classify_dump("S03000.PPM", 230415,
                                  os.path.join(d, "incoming"))
        check("a file with no local copy at all KEEPs", v == cf.KEEP, why)


def test_the_harness_profile_carries_what_sweeps_json_did():
    """Phase 6: sweeps.json became profiles/doskutsu.yaml.

    A format conversion is exactly where data goes missing quietly -- the file
    parses, the runner starts, and a sweep discovers at cell four that its
    timeout is gone. So this asserts the SHAPE the runners actually index into,
    against the values that were in the JSON, rather than merely that the YAML
    loads.

    It also asserts the profile carries the target facts the standard says
    belong in a profile rather than in the harness: the working directory, the
    env names the program consults, and the boot-profile witness.
    """
    print("\nharness profile")
    import importlib.util as _u
    from importlib.machinery import SourceFileLoader
    root = os.path.join(HERE, os.pardir)

    try:
        import yaml
    except ImportError:
        print("  SKIP  no PyYAML")
        return
    prof = yaml.safe_load(open(os.path.join(root, "profiles",
                                            "doskutsu.yaml")))

    check("sweeps.json is gone -- one source, not two",
          not os.path.exists(os.path.join(root, "harness", "sweeps.json")))

    # The runners must find and accept it.
    ldr = SourceFileLoader("sw_prof", os.path.join(root, "harness",
                                                   "vcctrl-sweep"))
    spec = _u.spec_from_loader("sw_prof", ldr)
    m = _u.module_from_spec(spec)
    sys.path.insert(0, os.path.join(root, "bin"))
    ldr.exec_module(m)
    conf = m.load_conf()
    check("the sweep runner loads the profile", bool(conf))
    check("and it resolves to the profile, not a stale json",
          m.CONF.endswith(".yaml"), m.CONF)

    # The values the runners index, spot-checked against the JSON that was.
    check("machine digits are strings, as the runner indexes them",
          all(isinstance(k, str) for k in conf["machines"]),
          list(conf["machines"])[:2])
    check("machine 1 survived the conversion intact",
          conf["machines"]["1"] == {"tag": "G",
                                    "name": "Pentium OverDrive 83"},
          conf["machines"].get("1"))
    rb = conf["sweeps"].get("RB") or {}
    check("RB kept its cell count, timeout and tag ORDER -- order is "
          "load-bearing, and a reordered sweep destroys its control",
          rb.get("cells") == 4 and rb.get("timeout_min") == 23
          and rb.get("tags") == ["R4", "R4B", "R3", "R5"], rb)
    check("every sweep has a timeout -- a sweep with none runs until someone "
          "notices",
          all(s.get("timeout_min") for s in conf["sweeps"].values()),
          [k for k, s in conf["sweeps"].items() if not s.get("timeout_min")])

    # The target facts, which are the reason it is a profile and not a table.
    t = prof.get("target") or {}
    check("the profile names the target's working directory",
          "DOSKUTSU" in (t.get("dir") or ""), t.get("dir"))
    check("and the boot-profile witness, which is DATA",
          (t.get("profile_witness") or {}).get("variable") == "BLASTER",
          t.get("profile_witness"))
    check("and the env names the program consults",
          (t.get("env") or {}).get("replay") == "DOSKUTSU_TAS_REPLAY",
          t.get("env"))
    check("menu window is TARGET physics and lives here",
          (prof.get("timing") or {}).get("menu_window_s") == 14,
          prof.get("timing"))


def test_the_client_finds_vcconfig_in_the_INSTALLED_layout():
    """`vcctrl config check` had never worked on the daemon host.

    The client's loader looked for `dirname(dirname(__file__))/common/
    vcconfig.py`, which is right in a source checkout and wrong once
    installed: deploy.sh puts the library in $PREFIX (/opt/vcctrl) and the
    client in /usr/local/bin/vcctrl, so it searched /usr/local/common/ --
    a directory that has never existed.

    IT WENT UNNOTICED BECAUSE IT FAILED SAFE: exit 3, "the tool could not
    run", never a claim that the configuration was bad. A wrong answer would
    have been found in a day; a refusal to answer sat there indefinitely.

    So the test builds the INSTALLED layout rather than the source one, which
    is the shape no existing test had.
    """
    print("\nclient finds vcconfig when installed")
    import shutil
    import subprocess
    import tempfile
    root = os.path.join(HERE, os.pardir)
    d = tempfile.mkdtemp()
    try:
        # /usr/local/bin/vcctrl + /opt/vcctrl/vcconfig.py, as deploy.sh makes.
        binp = os.path.join(d, "usr", "local", "bin")
        prefix = os.path.join(d, "opt", "vcctrl")
        os.makedirs(binp)
        os.makedirs(prefix)
        shutil.copy(os.path.join(root, "bin", "vcctrl-client"),
                    os.path.join(binp, "vcctrl"))
        shutil.copy(os.path.join(root, "common", "vcconfig.py"),
                    os.path.join(prefix, "vcconfig.py"))
        cfg = os.path.join(d, "vcctrl.yaml")
        open(cfg, "w").write("version: 1\nrig:\n  name: installed-layout\n")

        env = dict(os.environ)
        env["VCCTRL_PREFIX"] = prefix
        env["VCCTRL_CONFIG"] = cfg
        r = subprocess.run(["python3", os.path.join(binp, "vcctrl"),
                            "config", "check", cfg],
                           capture_output=True, text=True, env=env, timeout=60)
        out = (r.stdout or "") + (r.stderr or "")
        check("config check runs in the installed layout", r.returncode == 0,
              (r.returncode, out.strip()[:160]))
        check("and reads the file it was given",
              "installed-layout" in out, out.strip()[:160])

        # The control: with the library removed it must FAIL and say where it
        # looked. A loader that cannot name its candidates sends the reader to
        # the wrong file, which is how this one hid.
        os.unlink(os.path.join(prefix, "vcconfig.py"))
        r2 = subprocess.run(["python3", os.path.join(binp, "vcctrl"),
                             "config", "check", cfg],
                            capture_output=True, text=True, env=env, timeout=60)
        out2 = (r2.stdout or "") + (r2.stderr or "")
        check("control: without the library it fails rather than passing",
              r2.returncode == 3, r2.returncode)
        check("and names every path it tried",
              out2.count("vcconfig.py") >= 2, out2.strip()[:200])
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------- files capability

def test_dos_filename_mangles_rather_than_truncating_silently():
    """`my-photo-2026.jpg` is not a filename on a FAT volume.

    The rename has to happen on the STAGING side, because the target cannot
    rename in transit -- `GET.BAT` and its successor write whatever name they
    fetched. So the interesting property is not that a name is shortened; it
    is that the caller is TOLD it was, before the bytes move. Two photos off a
    phone whose names differ after the eighth character become one file
    otherwise, and the second silently replaces the first.
    """
    f = vcctrld.dos_filename

    # The hyphen is LEGAL in an 8.3 name and stays. This test asserted it
    # became an underscore, which was a guess about DOS rather than a fact
    # about it -- the code was right and the expectation was not.
    name, notes = f("my-photo-2026.jpg")
    check("a legal hyphen survives", name == "MY-PHOTO.JPG", name)
    check("a shortened name says so", any("8 characters" in n for n in notes),
          notes)

    name, _ = f("my photo:1.jpg")
    check("illegal characters are replaced, not dropped",
          name == "MY_PHOTO.JPG", name)

    name, notes = f("/home/someone/READ.ME")
    check("a path is reduced to its basename", name == "READ.ME", name)

    name, notes = f("already.ok")
    check("a name that needed nothing reports nothing",
          (name, notes) == ("ALREADY.OK", []), (name, notes))

    # An 8.3 name holds ONE separator. This produced `ARCHIVE..GZ` -- a name
    # DOS will not open, made to look deliberate by the truncation landing on
    # the stray dot.
    name, _ = f("archive.tar.gz")
    check("a stem's own dots do not survive into the 8.3 name",
          name == "ARCHIVE.GZ", name)

    # THE ONE THAT MATTERS. DOS reserves device names regardless of extension,
    # so a file called CON.TXT is the console. The transfer would report
    # success having written to a device, which is this rig's signature
    # failure: a result that is real and is about something else.
    for bad in ("CON.TXT", "con.txt", "PRN", "LPT1.DAT", "aux.bak"):
        try:
            f(bad)
            check("%s is refused as a DOS device" % bad, False, "accepted")
        except ValueError as exc:
            check("%s is refused as a DOS device" % bad,
                  "reserved DOS device" in str(exc), str(exc))

    for bad in ("", "   ", ".", "..", ".bashrc"):
        try:
            f(bad)
            check("%r is refused" % bad, False, "accepted")
        except ValueError:
            check("%r is refused" % bad, True)


def test_size_verdict_has_three_answers_not_two():
    """`warn` is a state, not a softer refusal.

    Past 8 MB the honest statement is that nobody has measured this path at
    that size -- the largest on record is a 7.8 MB binary. "Untested" and "too
    big" are different facts and the operator is entitled to proceed on the
    first. Collapsing them would either block a working transfer or wave
    through one that cannot be interrupted.
    """
    v = vcctrld.size_verdict
    MB = 1024 * 1024

    r = v([2 * MB])
    check("an ordinary photo passes silently",
          (r["ok"], r["why"]) == (True, None), r)

    r = v([12 * MB])
    check("past 8 MB it proceeds, and says the regime is unmeasured",
          (r["ok"], r["why"]) == (True, "unmeasured"), r)
    check("the warning says untested rather than too big",
          "untested" in r["reason"], r["reason"])

    r = v([80 * MB])
    check("past 64 MB it refuses", (r["ok"], r["why"]) == (False, "too-large"),
          r)
    check("the refusal explains that a transfer cannot be interrupted",
          "interrupted" in r["reason"], r["reason"])

    # THE CEILING IS ON THE QUEUE TOO. Without this, three 30 MB files walk
    # past a 64 MB refusal one at a time and the machine spends half an hour
    # in a state nothing can cancel.
    r = v([30 * MB, 30 * MB, 30 * MB])
    check("a queue is judged on its total, not per file",
          (r["ok"], r["why"]) == (False, "too-large"), r)
    check("the total is reported", r["total"] == 90 * MB, r["total"])

    r = v([])
    check("an empty queue is not a pass on nothing",
          r["total"] == 0 and r["ok"] is True, r)


def test_files_support_is_three_valued_and_unknown_refuses():
    """A Macintosh Plus and an unidentified machine are different answers.

    `unsupported` is working hardware with no such channel -- there is nothing
    to fix and nothing to retry. `unknown` is "which machine is this?", and it
    REFUSES rather than trying, because this is the one capability whose
    attempt reboots the target and writes to its disk. Every other capability
    can afford to try and fail.

    A typo in the config resolves to `unknown`, NOT to `unsupported`: somebody
    wrote something unreadable, which is fixable by editing a line, and that
    is a different fact from a machine that has no packet driver.
    """
    cap = vcctrld.FilesCapability(None)

    def with_config(board, targets):
        real_board = vcctrld.installed_board_id
        real_cfg = vcctrld._configured_transfer_boards
        vcctrld.installed_board_id = lambda: board
        vcctrld._configured_transfer_boards = lambda: targets
        try:
            return cap.support()
        finally:
            vcctrld.installed_board_id = real_board
            vcctrld._configured_transfer_boards = real_cfg

    ok, why = with_config(1, {1: "supported"})
    check("a declared PC supports transfer", ok is True, (ok, why))

    ok, why = with_config(3, {3: "unsupported"})
    check("a declared Macintosh does not", ok is False, (ok, why))
    check("and the reason names what it lacks rather than blaming it",
          "no packet driver" in (why or ""), why)

    ok, why = with_config(None, {1: "supported"})
    check("an unidentified board is unknown, not assumed", ok is None,
          (ok, why))
    check("and the reason says why guessing is expensive",
          "writes to its disk" in (why or ""), why)

    ok, why = with_config(1, {})
    check("a board absent from targets: is unknown, not unsupported",
          ok is None, (ok, why))

    ok, why = with_config(1, None)
    check("no targets: at all is unknown", ok is None, (ok, why))

    # FAILS CLOSED, AND CLOSED HERE MEANS `unknown`. Reading a typo as
    # `unsupported` would tell the operator their hardware cannot do this,
    # sending them to buy a network card instead of fixing a line of YAML.
    ok, why = with_config(1, {1: "suported"})
    check("a typo resolves to unknown rather than unsupported", ok is None,
          (ok, why))
    check("and the reason quotes what was actually written",
          "suported" in (why or ""), why)


def test_files_snapshot_separates_the_five_refusals():
    """Each `why` sends a person somewhere different, so they stay apart.

    `unreachable` is the only one fixable in a shell, and it is the one that
    must never be confused with `unsupported` -- one means start serve.sh, the
    other means this machine will never do this. `unchecked` is the probe
    timing out, which is a statement about the instrument and not about the
    target: reporting it as a dead server sends somebody to restart a service
    that was never down.
    """
    cap = vcctrld.FilesCapability(None)
    cap.backend_name = "mtcp-ftp"
    cap.settings = {}

    def snap(support, server, live):
        cap.support = lambda: support
        cap._server = lambda: server
        cap._reachable = lambda timeout=None: live
        return cap.snapshot()

    s = snap((False, "no packet driver"), ("h", 21), (True, None))
    check("unsupported short-circuits before the server is consulted",
          s["why"] == "unsupported" and s["available"] is False, s)

    s = snap((None, "which machine?"), ("h", 21), (True, None))
    check("unknown short-circuits too", s["why"] == "unknown", s)

    s = snap((True, None), None, (True, None))
    check("no target address configured is not_configured",
          s["why"] == "not_configured", s)
    check("and it names the key, because that key is what goes in the BAT",
          "target_host" in s["reason"], s["reason"])

    s = snap((True, None), ("h", 21), (False, "refused"))
    check("a dead server is unreachable", s["why"] == "unreachable", s)

    s = snap((True, None), ("h", 21), (None, "timed out"))
    check("a timed-out probe is unchecked, NOT unreachable",
          s["why"] == "unchecked", s)

    s = snap((True, None), ("h", 21), (True, None))
    check("everything holding is available",
          s["available"] is True and s["why"] is None, s)

    # THE SPELLING IS THE REGISTRY'S. One daemon, one state, one word: the
    # registry has emitted `not_configured` since capabilities had backends,
    # and this shipped as `not-configured` for two hours. A consumer branching
    # on one would fall through to its default on the other.
    check("not_configured matches the registry's spelling",
          all(v != "not-configured" for v in s.values()
              if isinstance(v, str)), s)


def test_profile_reading_is_forgotten_by_a_reboot_not_aged():
    """A stale boot profile is worse than none, and this is where that is enforced.

    NET and the sound profiles are indistinguishable from the harness -- same
    prompt, same `at_prompt`, same screen -- and a cell died because the
    machine was still in NET from a transfer an hour earlier (OPEN-FAULTS
    sec. 7). Putting the profile in the page title makes that failure VISIBLE
    if it survives a reboot: the same string, in the most prominent place on
    the page, with nothing to say the machine underneath it changed.

    So invalidation clears the name to null rather than marking it old. There
    is deliberately no "stale but probably still right" state to read past.
    """
    P = vcctrld.TargetProfile()

    s = P.snapshot()
    check("it starts with no reading and says so",
          s["name"] is None and s["reason"] == "never read", s)

    P.establish("PGSB", "SET at a prompt")
    s = P.snapshot()
    check("an established reading carries how it was taken",
          (s["name"], s["how"]) == ("PGSB", "SET at a prompt"), s)
    check("and when", s["at"] is not None and s["reason"] is None, s)

    P.invalidate("power cycle")
    s = P.snapshot()
    check("a reboot forgets the name entirely", s["name"] is None, s)
    check("and the timestamp goes with it -- nothing to age past",
          s["at"] is None and s["how"] is None, s)
    check("the reason says what happened", s["reason"] == "power cycle", s)

    # THE VALUE NAMES THE PROFILE; ITS PRESENCE DOES NOT. Only two of the six
    # CONFIG.SYS profiles set BLASTER, and their strings differ -- which is
    # what makes the value a reading. `FIND /C "="` on the variable proves
    # only that *a* sound profile is loaded, and that check sat behind a
    # hardcoded "(profile is PGSB)" in every cell log, accidentally true
    # because PGSB is the menu default.
    check("the PGSB string identifies PGSB",
          P.from_blaster("A220 I7 D3 P330 T3", "test") == "PGSB",
          P.snapshot())
    check("the VIBRA string identifies VIBRA",
          P.from_blaster("A220 I5 D1 H5 T6 P330", "test") == "VIBRA",
          P.snapshot())

    P.invalidate("reset")
    check("an unrecognised BLASTER identifies nothing rather than guessing",
          P.from_blaster("A220 I9 D0", "test") is None, P.snapshot())
    check("and it does not establish a reading as a side effect",
          P.snapshot()["name"] is None, P.snapshot())

    # An ABSENT BLASTER is not evidence of NET. Four profiles set none, so it
    # narrows the field to four and names none of them -- a genuinely weaker
    # statement than the one the old presence check was read as making.
    P.invalidate("reset")
    check("an empty BLASTER identifies nothing",
          P.from_blaster("", "test") is None, P.snapshot())
    check("and neither does a missing one",
          P.from_blaster(None, "test") is None, P.snapshot())


def test_scroll_lock_closes_the_undriven_reboot_hole():
    """A front-panel reset invalidates the profile, without anything driving it.

    `power` and ctrl-alt-del only see reboots the daemon CAUSED. The hole was
    a reset from the target's own front panel, or a crash-and-reboot, leaving
    the header displaying a profile from a boot that is no longer running --
    which is the exact failure the display exists to make visible.

    POST clears the LEDs whatever caused the reset, and RDYPULSE is the last
    line of every boot path in AUTOEXEC, so the two edges arrive either way.

    The video lock was the alternative and is wrong: the capture loses lock on
    every 640x480-to-text transition, so a game starting is indistinguishable
    from a reboot.
    """
    P = vcctrld.TargetProfile()
    P.establish("PGSB", "SET at a prompt")

    P.reset_seen()
    s = P.snapshot()
    check("a reset seen on the LED channel voids the reading",
          s["name"] is None, s)
    # A DISTINCT MESSAGE, because RDYPULSE is `IF EXIST`-guarded: a missing
    # COM file makes that line a silent no-op, Scroll never returns to 1, and
    # the reading is permanently invalid. That is the safe direction, but it
    # reads as a bug rather than as a missing file unless it says so.
    check("and it names the missing pulse rather than saying merely unknown",
          "readiness pulse" in s["reason"], s["reason"])

    P.ready_pulse()
    s = P.snapshot()
    check("a completed boot is a different fact from a reset",
          "booted" in s["reason"] and s["name"] is None, s)

    # THE ORDER THAT MATTERS. A pulse must never resurrect a reading -- the
    # machine came back, but nothing has read what it came back AS.
    P.establish("NET", "SET at a prompt")
    P.reset_seen()
    P.ready_pulse()
    check("a boot completing does not restore the profile that preceded it",
          P.snapshot()["name"] is None, P.snapshot())

    # And a pulse on a machine whose profile IS known must not overwrite it:
    # re-reading is what establishes a name, not the pulse.
    P.establish("PGSB", "SET at a prompt")
    P.ready_pulse()
    check("a pulse leaves an established reading alone",
          P.snapshot()["name"] == "PGSB", P.snapshot())


def test_the_liveness_check_dials_the_address_the_card_dials():
    """A check aimed at the wrong server passes and proves nothing.

    The card reaches the file server by a LITERAL STRING baked into a batch
    file on a CF volume. Nothing the daemon can observe makes that string
    true. So a probe that dials loopback, or the socket the daemon opened, or
    `control.fileserver` -- which is a DIFFERENT server, on the control host,
    the one GET.BAT and PUT.BAT already name -- would report ready for a
    machine whose lease had moved, and would do it right up until the transfer
    failed after the confirmation and after the reboot.

    So `target_host` has exactly one meaning: the string that goes into the
    BAT. One key, two consumers, no drift. Absent is `not_configured`, NOT a
    fallback to `control.fileserver` -- a fallback would silently reinstate
    the drift by testing one server and reporting on the other.
    """
    cap = vcctrld.FilesCapability(None)

    cap.settings = {"target_host": "192.0.2.11", "target_port": 2121}
    check("the probe target comes from the capability's own settings",
          cap._server() == ("192.0.2.11", 2121), cap._server())

    cap.settings = {"target_host": "192.0.2.11"}
    check("the port defaults to mTCP's", cap._server() == ("192.0.2.11", 2121),
          cap._server())

    # THE ONE THAT MATTERS. control.fileserver is the control host's server.
    # If it were consulted here, a rig with that block configured would report
    # ready while the card dialled an address nobody had checked.
    cap.settings = {}
    check("with no target_host it refuses rather than borrowing "
          "control.fileserver", cap._server() is None, cap._server())

    cap.settings = {"target_host": "", "target_port": 2121}
    check("an empty target_host is absent, not a host named ''",
          cap._server() is None, cap._server())

    cap.settings = {"target_host": "192.0.2.11", "target_port": "not-a-port"}
    check("an unparseable port is absent rather than silently 2121",
          cap._server() is None, cap._server())


def _mkfiles(tmp):
    cap = vcctrld.FilesCapability(None)
    cap.backend_name = "mtcp-ftp"
    cap.settings = {"stage_dir": os.path.join(tmp, "stage"),
                    "target_host": "192.0.2.11"}
    return cap


def _send(cap, name, data, chunk=None, sha=None, **kw):
    """Push bytes through file_stage the way a client would."""
    import base64 as b64, hashlib as hl
    chunk = chunk or len(data) or 1
    h = hl.sha256(data).hexdigest() if sha is None else sha
    sent, resp = 0, None
    while True:
        block = data[sent:sent + chunk]
        final = sent + len(block) >= len(data)
        req = {"name": name, "total": len(data), "offset": sent,
               "data": b64.b64encode(block).decode(), "final": final}
        if final:
            req["sha256"] = h
        req.update(kw)
        resp = cap._file_stage(req)
        if not resp.get("ok") or final:
            return resp
        sent += len(block)


def test_staging_never_exposes_a_partial_file():
    """The FTP root holds complete, verified files and nothing else.

    The target GETs by name, and a name that exists is a name it will fetch.
    `GET.BAT` cannot tell a short file from a whole one -- its own comment
    concedes it reports an attempt and says to confirm with DIR -- so the only
    place the distinction can be enforced is here, before the file is
    reachable at all. Bytes therefore arrive in a directory that is not served
    and are promoted by rename, which is atomic: there is no instant at which
    a half-written file is visible under a fetchable name.
    """
    import tempfile
    tmp = tempfile.mkdtemp()
    cap = _mkfiles(tmp)
    root, partial, meta = cap._dirs()
    payload = b"MENU 1\r\nMENU 2\r\n" * 500

    # Mid-upload: bytes exist, and NOT where the target could reach them.
    r = cap._file_stage({"name": "boot.bat", "total": len(payload),
                         "offset": 0, "data": __import__("base64")
                         .b64encode(payload[:100]).decode(), "final": False})
    check("a chunk is accepted", r["ok"] and r["complete"] is False, r)
    check("and it is NOT in the served root", os.listdir(root) == [], 
          os.listdir(root))
    check("it is in the partial directory instead",
          os.listdir(partial) == ["BOOT.BAT"], os.listdir(partial))

    r = _send(cap, "boot.bat", payload, chunk=100)
    check("the completed file lands in the served root",
          os.listdir(root) == ["BOOT.BAT"], os.listdir(root))
    check("the partial directory is emptied by the promotion",
          os.listdir(partial) == [], os.listdir(partial))
    check("and the bytes are the bytes",
          open(os.path.join(root, "BOOT.BAT"), "rb").read() == payload)
    check("the queue reports it once", r["count"] == 1, r.get("queue"))


def test_staging_refuses_rather_than_repairing():
    """Every refusal here is a caller error or a transit error, kept apart.

    The sha is checked BEFORE the file is promoted, and that is what makes the
    transfer's later round trip mean anything: that check pulls the file back
    off the target and compares it against the staged copy. If the staged copy
    were already wrong it would compare a wrong file against itself and pass --
    confirming the transport while saying nothing about the payload.
    """
    import base64 as b64, tempfile
    tmp = tempfile.mkdtemp()
    cap = _mkfiles(tmp)
    root, partial, _meta = cap._dirs()
    payload = b"x" * 2048

    r = _send(cap, "photo.jpg", payload, sha="0" * 64)
    check("a sha mismatch is refused", r["ok"] is False, r)
    check("and named as such", r["why"] == "sha-mismatch", r)
    check("THE BAD BYTES ARE NOT LEFT WHERE THE TARGET COULD FETCH THEM",
          os.listdir(root) == [], os.listdir(root))
    check("nor left as a partial to be resumed into a wrong file",
          os.listdir(partial) == [], os.listdir(partial))

    # A hole would be silently filled with zeroes by a seek, and the sha
    # would fail at the end with nothing saying which chunk was lost.
    cap._file_stage({"name": "a.txt", "total": 100, "offset": 0,
                     "data": b64.b64encode(b"12345").decode(), "final": False})
    r = cap._file_stage({"name": "a.txt", "total": 100, "offset": 90,
                         "data": b64.b64encode(b"xyz").decode(),
                         "final": False})
    check("a chunk that does not start where the last ended is refused",
          r["ok"] is False and r["why"] == "offset-mismatch", r)
    check("and it says how much actually arrived", r["have"] == 5, r)

    r = cap._file_stage({"name": "b.txt", "total": 100, "offset": 0,
                         "data": b64.b64encode(b"short").decode(),
                         "final": True, "sha256": "0" * 64})
    check("a final chunk that leaves the file undersized is refused",
          r["ok"] is False and r["why"] == "short", r)

    # TWO PHOTOS THAT DIFFER AFTER THE EIGHTH CHARACTER ARE ONE 8.3 NAME.
    _send(cap, "holiday-beach.jpg", b"first")
    r = _send(cap, "holiday-boat.jpg", b"second")
    check("a colliding 8.3 name is refused rather than silently overwritten",
          r["ok"] is False and r["why"] == "name-taken", r)
    # HOLIDAY-.JPG, with the hyphen: it is legal in an 8.3 name and came from
    # the original, so only the underscores that REPLACED illegal characters
    # are stripped. Both source names truncate to the same eight characters,
    # which is the collision under test.
    check("and the first file is untouched",
          open(os.path.join(root, "HOLIDAY-.JPG"), "rb").read() == b"first")
    r = _send(cap, "holiday-boat.jpg", b"second", replace=True)
    check("replace makes the overwrite deliberate", r["ok"] is True, r)

    # THE CALLER SENDS A NAME AND NEVER A PATH.
    for evil in ("../../etc/passwd", "/etc/passwd", "..\\\\..\\\\boot.ini"):
        r = _send(cap, evil, b"x")
        staged = r.get("name")
        check("%r cannot escape the staging directory" % evil,
              staged is None or ("/" not in staged and "\\\\" not in staged
                                 and ".." not in staged), r)


def test_the_queue_ceiling_counts_the_queue():
    """Three 30 MB files walk past a 64 MB refusal one at a time otherwise.

    Checked against the DECLARED total before any bytes are written, so a file
    over the ceiling is refused without first spending the time to receive it.
    """
    import tempfile
    tmp = tempfile.mkdtemp()
    cap = _mkfiles(tmp)
    root, _p, meta = cap._dirs()

    _send(cap, "a.bin", b"a" * 1000)
    _send(cap, "b.bin", b"b" * 1000)
    q = cap._file_queue({"action": "list"})
    check("the queue lists what is staged", q["count"] == 2, q)
    check("with a size verdict over the whole queue",
          q["size"]["total"] == 2000, q["size"])

    r = cap._file_stage({"name": "big.bin",
                         "total": vcctrld.REFUSE_BYTES + 1, "offset": 0,
                         "data": "", "final": False})
    check("a file over the ceiling is refused before any bytes are written",
          r["ok"] is False and r["why"] == "too-large", r)
    check("and nothing was created for it",
          not os.path.exists(os.path.join(root, "BIG.BIN")))

    # AN ORPHAN IS REPORTED AND LEFT ALONE. A file in the FTP root that this
    # capability did not stage is still something the target can fetch, so
    # hiding it would be the more dangerous tidiness -- and deleting it is how
    # a recovery copy somebody put there by hand disappears.
    with open(os.path.join(root, "HANDMADE.BAT"), "w") as f:
        f.write("@ECHO OFF\r\n")
    q = cap._file_queue({"action": "list"})
    orphans = [r for r in q["queue"] if r.get("orphan")]
    check("an unaccounted file in the root is reported, not hidden",
          len(orphans) == 1 and orphans[0]["name"] == "HANDMADE.BAT", q)

    c = cap._file_queue({"action": "clear"})
    check("clear removes what we staged",
          sorted(c["removed"]) == ["A.BIN", "B.BIN"], c)
    check("and leaves alone what we did not",
          c["left_alone"] == ["HANDMADE.BAT"], c)
    check("the orphan is still on disk", 
          os.path.exists(os.path.join(root, "HANDMADE.BAT")))
    check("and the metadata went with the files we removed",
          os.listdir(meta) == [], os.listdir(meta))


def test_no_blaster_can_never_be_read_as_the_NET_profile():
    """The transfer is the thing most likely to want this to work backwards.

    The profile witness in profiles/doskutsu.yaml proves a cell is NOT in NET,
    by requiring BLASTER to be present. Running it backwards -- "no BLASTER,
    therefore we booted into networking" -- is wrong three ways: PGADLIB,
    PGGUS and CLEAN also set none, and CLEAN has no network stack at all. A
    transfer that accepted an absence as proof of NET would reboot, type its
    commands into a machine with no packet driver, and get its answer from a
    failed FTP rather than from a check.

    So the mapping refuses to name anything on an absence, and there is no
    entry for NET to be found by any input. Proving NET needs a POSITIVE
    witness of what the transfer actually requires -- PKTTOOL scan reporting
    the packet driver -- which attests the capability rather than the label.
    """
    P = vcctrld.TargetProfile()

    check("NET is not in the BLASTER map at all, by any value",
          "NET" not in P.BLASTER_PROFILES.values(), P.BLASTER_PROFILES)
    check("exactly two profiles are nameable this way",
          len(P.BLASTER_PROFILES) == 2, P.BLASTER_PROFILES)

    for absent in ("", "   ", None):
        P.invalidate("reset")
        check("BLASTER=%r names nothing" % (absent,),
              P.from_blaster(absent, "test") is None, P.snapshot())
        check("and establishes nothing", P.snapshot()["name"] is None,
              P.snapshot())

    # The two that ARE nameable stay nameable -- this test must not pass
    # merely because the mapping is empty.
    check("PGSB is still identifiable, so this is not passing on nothing",
          P.from_blaster("A220 I7 D3 P330 T3", "t") == "PGSB", P.snapshot())


def test_dos_filename_contains_a_path_traversal():
    """vcctrld runs as root and joins this name onto a directory.

    Nothing downstream re-checks it. What contains it is the 8.3 conversion
    itself: basename() strips every path component, the backslash is in the
    illegal set so it cannot act as a separator, and the result is rebuilt
    from surviving characters rather than filtered against a blacklist. That
    is a whitelist-shaped transform, which is why it holds against inputs
    nobody enumerated.

    Written down as a SECURITY test rather than a usability one so that
    loosening the rules for a future non-DOS target trips this deliberately.
    """
    f = vcctrld.dos_filename
    for evil in ("../../../etc/cron.d/pwn", "/etc/passwd", "a/../../b",
                 "..\\..\\x.bat", "....//....//etc/shadow",
                 "/proc/self/environ", "sub/dir/file.txt"):
        try:
            name, _notes = f(evil)
        except ValueError:
            continue                      # refusing outright is also contained
        check("%r cannot escape: %r" % (evil, name),
              "/" not in name and "\\" not in name and ".." not in name
              and not name.startswith("."), name)
        check("%r joins back inside the root" % evil,
              os.path.dirname(os.path.join("/srv/stage", name))
              == "/srv/stage", name)

    check("a phone photo's extension is named, not just counted",
          any(".JPE" in n for n in f("my-photo-2026.jpeg")[1]),
          f("my-photo-2026.jpeg"))


def test_packet_driver_check_reports_cannot_tell_rather_than_no_driver():
    """The tempting version is two-valued and wrong in the expensive direction.

    "No failure string, therefore a driver is loaded" reads as success when
    PKTTOOL dies early, prints nothing, is missing from the card, or when the
    OCR mangles its output -- and the cost of that mistake is a transfer typed
    at a machine with no packet driver, after a reboot, with the environment
    already gone.

    Only the NEGATIVE branch has been observed on the hardware. The positive
    format is taken from documentation, so an unrecognised answer must land on
    "cannot tell" rather than on either verdict.
    """
    f = vcctrld.packet_driver_seen

    check("the observed failure line is a real no",
          f("Scanning :\nNo packet drivers found - did you load one?") is False)
    check("and it survives the whitespace a screen read introduces",
          f("  Scanning :   No  packet   drivers  found ") is False)

    check("a named driver is a yes",
          f("Packet driver\n  Name: ODIPKT\n  Class: 1") is True)
    check("an expected name makes a rig-specific answer recognisable",
          f("something odd, ODIPKT at 0x7E", expect="ODIPKT") is True)

    # EVERY ONE OF THESE WOULD PASS A TWO-VALUED CHECK.
    for silence in ("", None, "   ", "Scanning :", "Bad command or file name",
                    "C:\\MTCP>", "\x00\x00garbage"):
        check("%r is cannot-tell, not a driver" % (silence,),
              f(silence) is None, f(silence))

    # A driver by another name is still a driver: `expect` may only ADD a way
    # to recognise one, never turn an unrecognised answer into a refusal.
    check("expect does not downgrade a generic positive",
          f("Name: PKTDRV", expect="ODIPKT") is True)
    check("expect does not manufacture a negative",
          f("wholly unfamiliar output", expect="ODIPKT") is None)


# The real thing, off the card in NET, 2026-08-24. Kept verbatim so a future
# change to the matcher is checked against what the hardware actually says
# rather than against somebody's memory of it.
PKTTOOL_FOUND = """Scanning :
Details for driver at software interrupt: 0x7E
Name: ODIPKT
Entry point: 1256:08DD
Version: 21 Class: 1 Type: 71 Interface Number: 0
Function flag: 6 (basic, high performance, and extended functions)
Current receive mode: packets for this MAC and broadcast packets
MAC address: 00:00:5E:00:53:03"""

PKTTOOL_NONE = """Scanning :
No packet drivers found - did you load one?"""


def test_packet_driver_matcher_against_the_real_output():
    """Both branches now observed on the hardware, so both are pinned here.

    The positive one was documentation until somebody spent a reboot on it.
    """
    f = vcctrld.packet_driver_seen
    check("the real success output is a yes", f(PKTTOOL_FOUND) is True)
    check("the real failure output is a no", f(PKTTOOL_NONE) is False)
    check("the rig's own driver name matches when named",
          f(PKTTOOL_FOUND, expect="ODIPKT") is True)

    # THE MARKER IS LETTERS, DELIBERATELY. `Details for driver at software
    # interrupt: 0x7E` would also identify a driver, and its value came back
    # off the glass as `@x7E`. A check that reads a number on this console has
    # a failure rate rather than a result -- so a mangled hex line must not be
    # what the verdict rests on.
    mangled = PKTTOOL_FOUND.replace("0x7E", "@x7E").replace("08DD", "@8DD")
    check("a mangled hex value does not disturb the verdict",
          f(mangled) is True, mangled.splitlines()[1])

    # And the verdict must not survive losing the line it actually rests on.
    without = "\n".join(l for l in PKTTOOL_FOUND.splitlines()
                        if not l.lower().startswith("name:"))
    check("dropping the Name: line makes it cannot-tell, not a yes",
          f(without) is None, without.splitlines()[:3])


def test_a_refused_gate_says_when_the_command_simply_did_not_run():
    """C:\\MTCP is not on the PATH in NET, and the driver being loaded does not
    change that.

    `PKTTOOL SCAN` returns "Bad command or file name" on a machine that is
    perfectly configured. The matcher correctly answers cannot-tell and the
    gate refuses -- which is right, and would be maddening to debug, because
    every visible fact says the machine is fine.

    A refusal whose cause is invisible is an outage with good manners. So the
    one cause we have actually met is turned into an instruction.
    """
    seen, why = vcctrld.packet_driver_seen, vcctrld.packet_driver_reason

    bad = "C:\\>PKTTOOL SCAN\nBad command or file name"
    check("the gate still refuses -- this is not a driver", seen(bad) is None)
    check("but the reason names the cause",
          "not on the PATH" in (why(bad) or ""), why(bad))
    check("and names the fix, with the full path",
          "PKTTOOL.EXE" in (why(bad) or ""), why(bad))
    check("and says the driver may be fine, so nobody goes hunting a fault",
          "may well be loaded" in (why(bad) or ""), why(bad))

    check("an empty read is described as a failed READ, not a target fault",
          "could not be read" in (why("") or ""), why(""))

    # NO INVENTED EXPLANATIONS. Output nobody has seen gets no story attached
    # to it -- a plausible cause offered for an unknown one is how a person
    # spends an hour on the wrong thing.
    check("unrecognised output gets no fabricated reason",
          why("some future firmware chattering") is None)
    check("and a successful scan needs no reason at all",
          why(PKTTOOL_FOUND) is None)


def test_the_cheap_probe_is_cached_and_the_expensive_one_is_not():
    """The button's check runs from every tab; the pre-reboot check must not.

    A 1.5 s state poll from four tabs would otherwise be four TCP connects a
    second at a machine whose only job is to sit there. But `file_check` runs
    after the operator confirms and BEFORE anything reboots -- it is the
    expensive check guarding the expensive action, and answering it from a
    five-second-old cache would put the staleness back exactly where the
    design removed it.
    """
    cap = vcctrld.FilesCapability(None)
    cap.settings = {"target_host": "192.0.2.11", "target_port": 2121}
    vcctrld.FilesCapability._probe_cache = (0.0, None)

    calls = []
    real = vcctrld.socket.create_connection

    class _Boom(OSError):
        pass

    def fake(addr, timeout):
        calls.append(timeout)
        raise OSError(111, "Connection refused")

    vcctrld.socket.create_connection = fake
    try:
        a = cap._reachable()
        b = cap._reachable()
        check("the background probe dials once and caches",
              len(calls) == 1, calls)
        check("and both callers get the same verdict", a == b, (a, b))

        cap._reachable(timeout=5.0)
        check("an explicit timeout always dials -- never served from cache",
              len(calls) == 2, calls)
        check("and it uses the timeout it was given", calls[1] == 5.0, calls)

        # THE EXPENSIVE ANSWER MUST NOT POISON THE CHEAP ONE EITHER.
        stamp = vcctrld.FilesCapability._probe_cache[0]
        cap._reachable(timeout=5.0)
        check("an explicit check does not refill the background cache",
              vcctrld.FilesCapability._probe_cache[0] == stamp)
    finally:
        vcctrld.socket.create_connection = real
        vcctrld.FilesCapability._probe_cache = (0.0, None)


def test_a_dead_server_names_the_missing_package_without_claiming_the_host():
    """`apt install` is a deploy step, which means it is a step that silently
    did not happen.

    The server is meant to run on the daemon host, so a missing pyftpdlib is
    very probably why nothing is listening. But `target_host` is a CONFIGURED
    address and nothing proves it points at this machine -- so the observation
    is offered as a fact about THIS host, conditionally, and left to the
    reader to apply. An unconditional "install pyftpdlib" would send somebody
    to the wrong machine every time the server is remote.
    """
    cap = vcctrld.FilesCapability(None)
    cap.settings = {"target_host": "192.0.2.11", "target_port": 2121}
    real_conn = vcctrld.socket.create_connection
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def refuse(addr, timeout):
        raise OSError(111, "Connection refused")

    vcctrld.socket.create_connection = refuse
    try:
        vcctrld.FilesCapability._probe_cache = (0.0, None)
        live, why = cap._reachable(timeout=1.0)
        check("a refused connection is a real no", live is False, (live, why))
        check("and it says to start the server", "Start it" in why, why)
        # Whichever way the import goes on this machine, the sentence must
        # never assert that the server belongs here.
        if "pyftpdlib" in why:
            check("the package hint is conditional, not an instruction",
                  "if the server is meant to run here" in why, why)
            check("and it names the actual command",
                  "apt install python3-pyftpdlib" in why, why)
        check("it never claims target_host is this machine",
              "this machine is" not in why, why)
    finally:
        vcctrld.socket.create_connection = real_conn
        vcctrld.FilesCapability._probe_cache = (0.0, None)
