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
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VCCTRL = os.path.join(HERE, "vcctrl")

# Measured 2026-08-19 at an idle prompt. NOT measured during POST, where calls
# were observed to block substantially longer -- a spam loop budgeted at 40 s
# ran 71 s. Treat this as a floor, not a bound.
def _cfg():
    """The resolved configuration, or None when it cannot be loaded.

    None is a real answer and callers must handle it. A tool that cannot read
    the config still runs; it simply has no defaults to offer, and saying so
    beats inventing a hostname that belongs to somebody else.
    """
    try:
        import vcconfig
    except ImportError:
        try:
            import importlib.util as _u
            _p = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "common", "vcconfig.py")
            _s = _u.spec_from_file_location("vcconfig", _p)
            vcconfig = _u.module_from_spec(_s)
            _s.loader.exec_module(vcconfig)
        except Exception:
            return None
    try:
        return vcconfig.load(strict=False)
    except Exception:
        return None


def cfg_get(path, fallback=None):
    """One setting, with a fallback. Never raises."""
    c = _cfg()
    if c is None:
        return fallback
    try:
        return c.default(path, fallback)
    except Exception:
        return fallback


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


# Identity to present to the daemon's input lock. While a cell holds the lock,
# EVERY input call has to identify as the holder or the daemon refuses it --
# including the harness's own. Taking the lock without setting this locks the
# caller out of its own machine, which is precisely what happened the first
# time vcctrl-cell acquired one.
LOCK_OWNER = None


def set_lock_owner(name):
    global LOCK_OWNER
    LOCK_OWNER = name


def get_lock_owner():
    """Read it through a call, never by importing the name. `from x import
    LOCK_OWNER` binds the value at import time and never sees a later set,
    which left a cell holding a lock it then could not name to release."""
    return LOCK_OWNER


# Commands that actually deliver input, and so are subject to the lock. Reads
# (status, leds, video, framestats) are never gated -- observation must not
# depend on holding a lock, or diagnosing a stuck run would require taking
# input away from it.
_INPUT_CMDS = {"key", "type", "hold", "keydown", "keyup", "release-all",
               "release_all", "combo", "mouse"}


def vc(*args, check=True):
    args = list(args)
    if LOCK_OWNER and args and args[0] in _INPUT_CMDS and "--as" not in args:
        args += ["--as", LOCK_OWNER]
    r = subprocess.run([VCCTRL] + args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError("vcctrl %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout.strip()


def vc_json(*args):
    out = vc(*args)
    return json.loads(out) if out else {}


def leds():
    """The LED object: {"available": bool, "why": ..., + values when available}.

    Values stay FLAT inside this object, so `leds().get("capslock")` keeps
    working exactly as it did. When `available` is false the value keys are
    absent rather than zero, deliberately -- see the daemon's
    LedsCapability.snapshot(). Callers that need a value must therefore treat
    a missing key as "could not look", never as "off".
    """
    return vc_json("leds").get("leds", {})


def leds_available():
    """(available, why, reason). `why` is one of unsupported/error/unknown."""
    st = leds()
    return (bool(st.get("available")), st.get("why"), st.get("reason"))


def power_on():
    """True, False, or None. THREE states, because the plug has three.

    This used to be `bool(...get("power", {}).get("on"))`, and both defaults
    were claims. A plug that cannot be reached returns `on: null` -- the
    daemon makes it tri-state deliberately, because "the machine is off" and
    "I cannot reach the plug" are opposite facts that must not share a value
    (BOARD-IDENTITY, and PowerCapability's own docstring). bool(None) is
    False, so the harness threw that distinction away one line after the
    daemon had carefully preserved it.

    What it cost: ensure_powered() would read an unreachable plug as "powered
    off" and, with --power-on, send `power on` to a machine that was already
    running and then wait 240 s for a cold boot that could not happen. The
    power command is idempotent so nothing was cut, but the answer was a
    confident false statement about the world followed by a four-minute wait
    and a wrong refusal.
    """
    st = vc_json("power", "state").get("power")
    if not isinstance(st, dict) or "on" not in st:
        return None
    return st["on"]          # may itself be None -- pass the unknown through


# A poll used to cost ~1.5 s, which is why this loop had no sleep: a delay
# would have been coarser than the thing it measured. On the Pi 5 a call is
# ~3 ms locally and ~57 ms from the VM, so that justification is wrong by
# between one and three orders of magnitude and the loop became a busy-wait.
# Measured: 280 polls in one 16 s window. Small enough to poll often, large
# enough not to spin.
LED_POLL_S = 0.02


def wait_led(name, want, timeout):
    """Wait for LED `name` to read `want`. Returns elapsed seconds, or None.

    LEVEL-triggered, so it is only trustworthy when the starting level is
    known. Across a power cycle it is NOT: see wait_cold_boot(). And when the
    starting level is merely ASSUMED, this fails in the worst direction --
    see stable_led() and at_prompt().
    """
    want = bool(want)
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = leds()
        if not st.get("available"):
            # ONLY `unsupported` means never. Everything else is transient and
            # must be waited through.
            #
            # This bit within an hour of the epoch change that introduced it.
            # After a power-on the epoch advances, leds correctly reports
            # `unproven` until the target publishes -- and the first version
            # of this abandoned on any unavailable state, so wait_cold_boot()
            # returned None in 0.0 s on a machine that was booting perfectly.
            # The function whose reasoning MOTIVATED the epoch work was the
            # first thing the epoch work broke.
            #
            # `unpowered` is transient here too, and for a reason easy to
            # miss: PowerCapability's cache has a 60 s heartbeat, so for up to
            # a minute after power-on the daemon still believes the target is
            # off. Abandoning on it would fail every cold boot.
            if st.get("why") == "unsupported":
                return None
            time.sleep(LED_POLL_S)
            continue
        if bool(st.get(name)) is want:
            return time.time() - t0
        time.sleep(LED_POLL_S)
    return None


def stable_led(name, tries=6):
    """Read an LED until two consecutive reads agree. None if they never do.

    MEASURED FAILURE, 2026-08-20: at_prompt() took a single reading of caps
    lock as its starting level, and on one run in five that reading was taken
    while the value was still settling. It then waited 8 s for a state the LED
    was already leaving, missed it, and missed the restore symmetrically --
    16.28 s and a confident False, on a machine that was perfectly healthy.

    That is the worst failure direction this harness has: at_prompt() returning
    False reads as "something is still running", so the caller declines to type
    and an unattended sweep stalls looking exactly like a wedge.

    The flip itself takes 48 ms against an 8 s timeout -- a margin of 168x --
    so the timeout was never the problem. One sample was.
    """
    last = None
    for _ in range(tries):
        st = leds()
        if not st.get("available"):
            # NOT False. An LED that cannot be read at all is "could not
            # look", and on ADB it is not even a fault -- the board has no
            # return channel. Returning False here would make at_prompt()
            # report "something is still running" on a healthy Macintosh, and
            # the caller would decline to type at a machine that was fine.
            return None
        now = bool(st.get(name))
        if last is not None and now == last:
            return now
        last = now
        time.sleep(LED_POLL_S)
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
    before = stable_led("capslock")
    if before is None:
        # Refusing is right: a probe whose starting level will not settle
        # cannot answer the question, and answering anyway is how a healthy
        # machine gets reported as busy.
        sys.stderr.write("at_prompt: caps lock level would not settle; "
                         "declining to guess\n")
        return None
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

    THREE RETURNS, because there are three outcomes:

        True   armed and confirmed
        False  a real failure -- the LED would not take the state
        None   COULD NOT LOOK. The return channel is unreadable, which on ADB
               is permanent and on a booting machine is transient. The caller
               acts differently on each, and collapsing None into False tells
               it a healthy Macintosh failed to arm.

    Every existing caller uses `if not arm_leds()` or `if arm_leds()`, so None
    is falsy and their behaviour is unchanged. The distinction is available to
    anyone who wants it, and nothing is forced to take it.

    BOTH READS GO THROUGH stable_led(). They used to be bare `leds()` calls --
    one to decide whether to press the key, one to confirm the result -- and
    that is exactly the single-sample failure stable_led() was written for
    after at_prompt() lost a run to it. Measured 2026-08-20: four refusals in
    one evening, four immediate retries that succeeded. A settling race in the
    function that arms the boot edges every reboot depends on.

    The false-negative direction is the dangerous one and it is not symmetric.
    A false "could not arm" refuses a boot that would have worked -- annoying.
    A false "armed" lets a reboot proceed with an edge that cannot be detected,
    and the harness then waits for an event that has already happened.
    """
    ok, why, reason = leds_available()
    if not ok:
        print("  cannot arm the LEDs: %s (%s)" % (reason, why))
        return None if why in ("unknown", "unpowered", "error") else False
    for name, want in (("capslock", True), ("scrolllock", False)):
        cur = stable_led(name)
        if cur is None:
            print("  could not read %s to decide -- not the same as wrong" % name)
            return None
        if cur is want:
            continue
        vc("key", name)
        if wait_led(name, want, 15) is None:
            print("  could not set %s to %s" % (name, want))
            return False
    caps, scroll = stable_led("capslock"), stable_led("scrolllock")
    if caps is None or scroll is None:
        print("  could not read the LEDs back to confirm arming")
        return None
    return caps is True and scroll is False


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
ECHO_DRAW_S = 3.0


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
        # Force the console into UPPER CASE first. DOS does not care, but OCR
        # does, enormously: mode 12h lowercase glyphs read back as "he L Lowor
        # Ld" for "helloworld", which made the echo check reject commands that
        # had arrived perfectly. Caps Lock ends up off because at_prompt()
        # toggles it as its probe and does not always restore it -- so the
        # readiness check was silently degrading the legibility of the screen
        # that the command check depends on.
        if not bool(leds().get("capslock")):
            vc("key", "capslock", check=False)
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


class StagedChange:
    """Make a mutating procedure either complete or revert -- never neither.

    THE PROPERTY THIS ENCODES. On 2026-08-19 vcctrl-uvconfig backed up, began
    an interactive configurator, then correctly refused to drive a screen it
    could not read -- and stopped there. Refusing to type blind was the right
    instinct. Leaving the target half-modified was not, and the harness treated
    "did not complete" and "reverted" as the same outcome when they are
    opposites: one leaves a machine in a state nobody designed, and it ran that
    way for six hours.

    Not a rule about uvconfig. Any procedure that writes to the target should be
    able to answer "and if I stop halfway?" with something other than silence.

    Usage:

        with StagedChange("univbe driver") as st:
            st.revert_with("COPY C:\\UNIVBE\\UNIVBE.BAK C:\\UNIVBE\\UNIVBE.DRV")
            ...                       # do the mutation
            st.completed()            # ONLY on the success path

    Leaving the block without calling completed() -- by exception, by return,
    or by an explicit bail -- runs the revert commands in reverse order and says
    so. Reverting is best-effort and always reports what it could not undo,
    because a failed revert is exactly the state this exists to make visible.
    """

    def __init__(self, what):
        self.what = what
        self.reverts = []
        self.done = False

    def revert_with(self, dos_command):
        """Register the command that undoes what you are ABOUT to do.

        Register it BEFORE the mutation, not after: a procedure that dies during
        the write has still written, and a revert registered afterwards never
        gets recorded.
        """
        self.reverts.append(dos_command)

    def completed(self):
        self.done = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.done:
            return False
        if not self.reverts:
            print("  *** %s DID NOT COMPLETE and has no revert registered. ***"
                  % self.what)
            print("  The target may be in a state nobody designed. Check it by"
                  " hand.")
            return False
        print("  *** %s DID NOT COMPLETE -- reverting %d change(s) ***"
              % (self.what, len(self.reverts)))
        for cmd in reversed(self.reverts):
            try:
                vc("type", cmd, check=False)
                vc("key", "enter", check=False)
                time.sleep(1.5)
                vc("key", "y", check=False)      # answer an overwrite prompt
                vc("key", "enter", check=False)
                ok = wait_for_prompt(60) is not None
            except Exception:
                ok = False
            print("     %-58s %s" % (cmd, "ok" if ok else "FAILED -- undo by hand"))
        return False


def ensure_powered(allow_power_on):
    """Bring the target up if permitted, else refuse. Returns True if usable.

    The refusal used to be the whole story, which meant the cold path was
    improvised at a prompt every time -- and that improvisation is exactly
    where the stale-LED trap bit. Making it code makes it reviewable.
    """
    state = power_on()
    if state is True:
        return True
    if state is None:
        # NOT a power cycle. Acting on an unknown here means sending `power
        # on` to a machine that may be mid-run, and then waiting four minutes
        # for a boot edge that will never arrive. Refusing is the only honest
        # move: could-not-look is not a finding.
        print("REFUSED: cannot tell whether the target is powered.\n"
              "  The plug did not report a state -- that is NOT the same as\n"
              "  the machine being off, and this will not act on the\n"
              "  difference. Check `vcctrl power state`.")
        return False
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
