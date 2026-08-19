"""Shared harness helpers for vcctrl-sweep and vcctrl-collect.

These functions lived in both tools as copies. That is how bug 4 came to exist
twice: at_prompt() was written once, copied, and then fixed in neither place
for as long as it happened to work. Anything that talks to the LED channel or
counts on call timing belongs here now, so a fix lands once.

THE ONE NUMBER THAT GOVERNS EVERYTHING HERE: a vcctrl call is an ssh
round-trip to the Pi and costs about 1.5 s (measured 1.52-2.55 s). The PS/2
LED round-trip underneath is much faster. So the harness is dominated by its
own instrumentation cost, and any loop written as though the calls were free
is wrong by roughly a factor of three. Never sleep-then-read; wait for the
state you want and let the call cost be the poll interval.
"""

import json
import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VCCTRL = os.path.join(HERE, "vcctrl")

# Measured 2026-08-19 at an idle prompt. NOT measured during POST, where calls
# were observed to block substantially longer -- a spam loop budgeted at 40 s
# ran 71 s. Treat this as a floor, not a bound.
CALL_COST_S = 1.5


def vc(*args, check=True):
    r = subprocess.run([VCCTRL] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("vcctrl %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout.strip()


def vc_json(*args):
    out = vc(*args)
    return json.loads(out) if out else {}


def leds():
    return vc_json("leds").get("leds", {})


def power_on():
    return bool(vc_json("power", "state").get("power", {}).get("on"))


def wait_led(name, want, timeout):
    """Wait for LED `name` to read `want`. Returns elapsed seconds, or None.

    No sleep between polls -- each poll is already a ~1.5 s round-trip, so
    adding a delay only makes the loop coarser than the thing it measures.

    LEVEL-triggered, so it is only trustworthy when the starting level is
    known. Across a power cycle it is NOT: see wait_cold_boot().
    """
    want = bool(want)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if bool(leds().get(name)) is want:
            return time.time() - t0
    return None


def at_prompt():
    """Is DOS at a prompt and accepting input?

    Uses the LED channel rather than the screen, so it works when capture is
    unavailable. Safe ONLY when no sweep is running.

    Closed-loop, not timed. The original slept 1.0 s between the toggle and
    the re-read; it passed, but only because the two leds() calls bracketing
    the sleep added ~3 s of their own. The sleep was never doing the work.
    """
    before = bool(leds().get("capslock"))
    vc("key", "capslock")
    flipped = wait_led("capslock", not before, 8) is not None
    if flipped:
        vc("key", "capslock")          # restore
        wait_led("capslock", before, 8)
    return flipped


def wait_for_prompt(timeout=90):
    """Block until DOS is back at a prompt and taking input.

    Distinct from at_prompt(), which asks the question once. This is for the
    case where the machine is KNOWN to be busy and we are waiting it out.

    The case that produced it: put_tag() confirms a transfer by watching the
    file arrive on the server -- but at the instant it arrives, the DOS side
    is still inside FTP.EXE, which has yet to print, quit, return to the BAT,
    and drop back to the prompt. Arrival proves the TRANSFER; it says nothing
    about READINESS. Those are different questions and conflating them meant
    the next keystroke went into the BIOS buffer instead of being processed,
    so arm_leds() timed out and the return reboot was skipped -- leaving the
    machine sitting in NET, which is the one profile a measured run must never
    start from.
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if at_prompt():
            return time.time() - t0
    return None


def arm_leds():
    """Put the two signal LEDs into states that make the next boot legible.

    Caps Lock ARMED HIGH: POST clears it, so caps 1 -> 0 is the reboot edge.
    Scroll Lock ARMED LOW: RDYPULSE sets it, so scroll 0 -> 1 is readiness.
    Either one armed the wrong way makes its transition invisible, and the
    harness then waits for an event that has already happened.
    """
    for name, want in (("capslock", True), ("scrolllock", False)):
        if bool(leds().get(name)) is want:
            continue
        vc("key", name)
        if wait_led(name, want, 15) is None:
            print("  could not set %s to %s" % (name, want))
            return False
    st = leds()
    return bool(st.get("capslock")) and not st.get("scrolllock")


def wait_cold_boot(timeout=240):
    """Wait for a machine that was POWERED OFF to reach a live prompt.

    Cannot use a level check, and the reason reads as working: the Pi's
    /sys/class/leds RETAINS THE LAST STATE the host published, and a host that
    is off publishes nothing. So after `power on` the LEDs still read whatever
    they read before the power was cut -- and since RDYPULSE is the last thing
    a healthy boot runs, that is normally scrolllock=1.

    A level check for "scrolllock is 1" therefore returns TRUE IMMEDIATELY,
    reporting ready 2.5 s after power-on on a machine that has not begun to
    POST. Observed exactly that on 2026-08-19.

    An edge pair is immune: POST clears the LEDs, so wait for scrolllock -> 0
    (proving the reading now belongs to THIS boot) and only then for -> 1.
    """
    t0 = time.time()
    if wait_led("scrolllock", False, timeout) is None:
        return None
    print("  POST cleared the LEDs at t+%.1fs -- readings are now this boot's"
          % (time.time() - t0))
    left = timeout - (time.time() - t0)
    if left <= 0 or wait_led("scrolllock", True, left) is None:
        return None
    return time.time() - t0


def ensure_powered(allow_power_on):
    """Bring the target up if permitted, else refuse. Returns True if usable.

    The refusal used to be the whole story, which meant the cold path was
    improvised at a prompt every time -- and that improvisation is exactly
    where the stale-LED trap bit. Making it code makes it reviewable.
    """
    if power_on():
        return True
    if not allow_power_on:
        print("REFUSED: target is powered off.\n"
              "  Run `vcctrl power on`, or pass --power-on to let this do it.")
        return False
    print("== power on ==")
    vc("power", "on")
    if wait_cold_boot() is None:
        print("REFUSED: powered on but never reached a prompt.")
        return False
    print("  cold boot complete, prompt live")
    return True
