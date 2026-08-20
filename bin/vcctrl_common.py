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

import difflib
import json
import os
import re
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VCCTRL = os.path.join(HERE, "vcctrl")

# Measured 2026-08-19 at an idle prompt. NOT measured during POST, where calls
# were observed to block substantially longer -- a spam loop budgeted at 40 s
# ran 71 s. Treat this as a floor, not a bound.
CALL_COST_S = 1.5

# The CONFIG.SYS menu appears within a few seconds of the POST edge and times
# out in 5 s, so selection is blind and has to cover a window.
#
# BUDGET IN KEYSTROKES, NOT ATTEMPTS. This was the whole bug. Each attempt is
# digit-then-Enter, so a cap of 20 attempts is a cap of FORTY keystrokes into a
# BIOS buffer that holds fifteen. Two get consumed by the menu; the rest either
# overflow -- one beep per rejected key, audible from the next room -- or sit in
# the buffer and flush into the command line afterwards.
#
# The flush is the dangerous half, and it is why "quieter" was mistaken for
# "fixed" on 2026-08-19. A surviving digit landed on the FRONT of the next
# command typed:
#
#     C:\>5C:\MTCP\PUT.BAT M64A
#     Bad command or file name
#
# That is a log collection silently turned into a no-op by keystrokes the
# harness sent itself, eight minutes earlier, for a different purpose.
#
# So: a budget the buffer can absorb whole, and verification afterwards instead
# of volume beforehand. Three pairs covers the jitter; select_boot_profile
# reports what it spent so a caller can see the margin rather than assume it.
KBD_BUFFER_KEYS = 15
MENU_WINDOW_S = 14
MENU_MAX_KEYS = 6              # 3 x (digit + Enter)


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
    """Is the BIOS keyboard handler alive? NOT "is DOS at a prompt".

    READ THIS BEFORE TRUSTING IT. The name is aspirational and the docstring
    used to match the name, which is how it cost a transfer on 2026-08-19.

    Caps Lock is serviced by the BIOS INT 09h handler and updates the keyboard
    controller's LED directly. **DOS is not involved.** So this flips whenever
    the ISR is intact -- including while an ordinary program is running and not
    reading input at all. It returns True during an FTP transfer.

    What it genuinely detects is a program that HOOKS INT 09h, which is why it
    correctly reports the game as not-at-a-prompt: SDL3's DOS backend owns the
    vector. That is a real and useful signal, and it is the only one here.

    The failure it produced: wait_for_prompt returned as soon as FTP.EXE was
    still finishing its BAT, a 41-character command was typed into a machine
    that was not reading, fifteen characters fit in the BIOS buffer, the rest
    beeped audibly across the room, and the truncated remains executed --
    "COPY C:\\DOSKUTSy". Use type_command() for anything long enough to
    overflow; it confirms from the screen instead of from the ISR.

    Uses the LED channel rather than the screen, so it works when capture is
    unavailable. Safe ONLY when no sweep is running.

    Closed-loop, not timed. The original slept 1.0 s between the toggle and
    the re-read; it passed, but only because the two leds() calls bracketing
    the sleep added ~3 s of their own. The sleep was never doing the work.
    """
    before = bool(leds().get("capslock"))
    vc("key", "capslock")
    flipped = wait_led("capslock", not before, 8) is not None
    # Restore either way. On the failure path the keystroke was usually only
    # BUFFERED, not lost -- DOS processes it a moment later and the LED flips
    # after we have already given up. Leaving it unrestored means a failed
    # probe silently corrupts the state the next probe reads.
    vc("key", "capslock")
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


def select_boot_profile(digit, edge_timeout=60, ready_timeout=200):
    """Reboot and pick a CONFIG.SYS menu entry, without flooding the buffer.

    The menu is invisible (text mode 03h does not capture) and times out in
    5 s, so selection is blind and timed. The digit alone does not commit --
    it highlights; Enter commits (FINDINGS sec. 8).

    WHY THIS POLLS, WHEN THE ORIGINAL DELIBERATELY DID NOT: the first version
    blind-fired a fixed number of attempts across a window, because a leds()
    check cost ~1.5 s of ssh and would have pushed the cadence past the 5 s
    window it had to hit. After ControlMaster (FINDINGS sec. 19) a poll costs
    ~0.2 s, so checking between attempts is affordable and the whole tradeoff
    disappears.

    The operator hears the difference: every attempt beyond the one that lands
    goes into the BIOS 15-key buffer, and once full the machine beeps per
    rejected key -- "four rapid beeps after the PnP boot beep". The survivors
    then flush into the prompt as "Bad command or file name". Stopping at
    RDYPULSE removes both.

    Pass digit=None to take the menu default without pressing anything.
    """
    if not arm_leds():
        return None
    vc("combo", "ctrl", "alt", "delete")
    edge = wait_led("capslock", False, edge_timeout)
    if edge is None:
        return None

    t0 = time.time()
    attempts, keys = spam_menu(digit)
    if wait_led("scrolllock", True, ready_timeout - (time.time() - t0)) is None:
        return None
    ready = time.time() - t0
    if wait_for_prompt(90) is None:
        return None
    return {"edge_s": edge, "ready_s": ready, "attempts": attempts,
            "keys": keys, "buffer_margin": KBD_BUFFER_KEYS - keys}


def spam_menu(digit):
    """Blind-select a CONFIG.SYS menu entry, within the keyboard buffer budget.

    THE ONE COPY. This logic lived in both select_boot_profile() and
    vcctrl-collect's reboot_into_net(), with different constants -- 20 attempts
    in one, 12 in the other -- so the 2026-08-19 overflow fix had to be found
    twice and was applied to neither for as long as it merely sounded better.
    Same reason at_prompt() became shared: see the module header.

    Returns (attempts, keys). Sends nothing and returns (0, 0) for digit=None,
    which is how a caller takes the menu default.
    """
    if digit is None:
        return 0, 0
    t0 = time.time()
    attempts, keys = 0, 0
    while time.time() - t0 < MENU_WINDOW_S and keys + 2 <= MENU_MAX_KEYS:
        if bool(leds().get("scrolllock")):
            break                # already booted through; nothing to select
        vc("key", str(digit), "enter", check=False)
        attempts += 1
        keys += 2
    return attempts, keys


def flush_input_line():
    """Clear anything sitting unentered on the DOS command line.

    Blind menu selection leaves a few keystrokes in the BIOS buffer, and they
    flush into COMMAND.COM whenever it next reads input. If that happens to be
    the moment the harness types a command, the stray lands on the FRONT of it:

        C:\\>5C:\\MTCP\\PUT.BAT M64A
        Bad command or file name

    Esc is COMMAND.COM's cancel-line key, so this discards a partial line
    without executing it. Call it after a reboot, before the first real
    command -- it costs two keystrokes and removes a whole class of silent
    no-op.

    NOT a substitute for keeping the buffer under budget. This clears what is
    already on the line; it cannot clear what has not arrived yet, which is why
    the caller should also wait for the screen to stop changing.
    """
    vc("key", "escape", check=False)
    vc("key", "enter", check=False)
    return wait_for_prompt(30)


# A command can only be corrupted by a full keyboard buffer if it is long
# enough to fill one. Anything at or under this fits whole, so it is typed
# without the cost of a verification round-trip.
SHORT_CMD_CHARS = 12

# How long the mode 12h console needs to render a typed line before it can be
# read back. Measured generously: the cost of waiting too long is latency, the
# cost of waiting too little is discarding a command that arrived fine.
ECHO_DRAW_S = 1.5


def _screen_text():
    """OCR of the current screen, or None if there is no picture.

    grab() lives in vcctrl-sweep and is loaded lazily here rather than
    imported, because vcctrl-sweep imports this module. Since the KVM daemon
    took ownership of the capture device this is a read of its frame ring, so
    it costs about 0.2 s -- which is the only reason verifying every long
    command is affordable at all.
    """
    try:
        import importlib.util
        from importlib.machinery import SourceFileLoader
        from PIL import Image
        import pytesseract
        spec = importlib.util.spec_from_loader(
            "vcsweep", SourceFileLoader("vcsweep",
                                        os.path.join(HERE, "vcctrl-sweep")))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        shot, _ = mod.grab("cmdcheck")
        if shot is None:
            return None
        return pytesseract.image_to_string(Image.open(shot))
    except Exception:
        return None


def _ocr_norm(s):
    """Project text onto the alphabet OCR gets right on this console font.

    The folds are measured, not guessed: this font's OCR reads UNIVBE as
    UNIUBE and DOSKUTSU as DOSKUISU, so V/U and T/I are folded together along
    with the usual 0/O, 1/I, 5/S, 8/B. Punctuation is dropped entirely --
    backslashes and colons are where OCR is least reliable and they carry no
    information a command echo needs.
    """
    s = re.sub(r"[^A-Z0-9]", "", s.upper())
    for a, b in (("V", "U"), ("0", "O"), ("1", "I"), ("5", "S"),
                 ("8", "B"), ("T", "I")):
        s = s.replace(a, b)
    return s


def command_echoed(screen, cmd, tail=16, threshold=0.80):
    """Did DOS echo this command back?

    An echo is proof COMMAND.COM READ the command, which is the thing
    at_prompt cannot establish. Compares the TAIL, because a truncated command
    shares its head with the intended one and differs only at the end -- the
    exact failure this exists to catch.
    """
    want = _ocr_norm(cmd)[-tail:]
    hay = _ocr_norm(screen or "")
    if not want or not hay:
        return False
    if want in hay:
        return True
    best = 0.0
    for i in range(0, max(1, len(hay) - len(want) + 1)):
        best = max(best, difflib.SequenceMatcher(
            None, want, hay[i:i + len(want)]).ratio())
    return best >= threshold


def type_command(cmd, retries=3, settle=4.0, press_enter=True):
    """Type a command and confirm it arrived intact before committing it.

    THE POINT: Enter is only pressed once the command is visible on screen. A
    command that overflowed the BIOS buffer is never executed in its truncated
    form, which is what turned a working FTP transfer into
    "COPY C:\\DOSKUTSy -- File not found" and beeped twenty-odd times doing it.

    Short commands skip the check: they cannot overflow, and a verification
    round-trip on every SET would double the cost of a cell for nothing.

    Returns True if the command was typed and committed, False if it could not
    be got onto the line intact -- which means the machine is busy, not that
    the command failed.
    """
    if len(cmd) <= SHORT_CMD_CHARS:
        vc("type", cmd)
        if press_enter:
            vc("key", "enter")
        return True

    for attempt in range(retries):
        flush_input_line()
        vc("type", cmd)
        # Let the console actually DRAW it before looking. Mode 12h text is
        # planar read-modify-write and visibly crawls -- reading the screen the
        # instant the keys are sent checks whether the echo has happened yet,
        # not whether DOS accepted it, and rejected three perfectly good
        # commands in a row the first time this ran.
        time.sleep(ECHO_DRAW_S)
        if command_echoed(_screen_text(), cmd):
            if press_enter:
                vc("key", "enter")
            return True
        # Not echoed: DOS was not reading, so some of it is in the buffer and
        # the rest beeped. Clear the line and let the machine finish whatever
        # it is doing rather than typing over it again.
        vc("key", "escape", check=False)
        time.sleep(settle)
    return False


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
