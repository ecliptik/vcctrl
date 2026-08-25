#!/usr/bin/env python3
"""vcctrl_mcp -- MCP server for vcctrl. Runs on the control host (the VM),
stdio transport, one tool per CLI verb. See internal/MCP-PLAN.md for the
design this implements -- this file is Phases 1 and 2 of that plan (read-only
tools plus input tools), not the whole thing.

WHY IT SHELLS OUT TO bin/vcctrl RATHER THAN SPEAKING vcctrld'S SOCKET
DIRECTLY: bin/vcctrl already carries two hard-won fixes -- SSH
ControlMaster/ControlPersist (without it, a fresh ssh per call saturated
journald on the Pi 3 and took it off the network for 30 minutes, 2026-08-19),
and the --out/--out-dir host-boundary rewrite (a local path is meaningless on
the far side of an ssh call, and the dangerous failure is the one that
returns 0). Reimplementing vcctrld's JSON socket protocol here would be a
second place either bug could reappear differently. See MCP-PLAN.md sec. 2.

WHAT IS DELIBERATELY NOT HERE:
  - `power on` / `power off` / `power cycle`. Blocked on the board-scoped
    power fix in daemon/vcctrld.py (this same worktree, see PowerCapability
    and the accompanying test) being DEPLOYED to and VERIFIED against the
    real daemon -- code-complete and unit-tested is not the same claim as
    proven on hardware, and this repo's own discipline is not to conflate
    them. `power state` (a read, never gated) is here; the three that touch
    the plug are not. See MCP-PLAN.md sec. 4 and sec. 9 (phase 3).
  - `lock break`. Force-taking a lock a human or peer holds is exactly the
    kind of irreversible, other-affecting action this project's operating
    rules say needs a human in the loop, not a retry path. MCP-PLAN.md
    sec. 3.
  - File transfer and harness-workflow tools (Phases 4-5). Both need an
    async launch-and-poll pattern this file does not attempt to invent
    speculatively -- see MCP-PLAN.md sec. 8.
"""

import atexit
import json
import os
import socket
import subprocess
import tempfile
import threading
import time

from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------- transport

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VCCTRL_BIN = os.path.join(REPO_ROOT, "bin", "vcctrl")

# One MCP server process is one Arbiter identity. Two concurrent tool calls
# from the same Claude Code session are "the same owner" as far as the lock
# is concerned -- correct, because the lock exists to keep OTHER callers out,
# not to serialize a caller against itself. See MCP-PLAN.md sec. 3.
OWNER = "mcp:%s:%d" % (socket.gethostname(), os.getpid())

# Where shot/frame/burst/record land. Not auto-cleaned -- these are evidence
# a caller asked for, on purpose, and the OS temp reaper is the right thing
# to eventually clear them, not this process guessing when they're done with
# a file it wrote for them.
SCRATCH_DIR = tempfile.mkdtemp(prefix="vcctrl-mcp-")


def _scratch_path(hint, ext):
    fd, path = tempfile.mkstemp(prefix="%s-" % hint, suffix=ext, dir=SCRATCH_DIR)
    os.close(fd)
    os.remove(path)   # vcctrl's own two-valued --out contract wants to
                       # create this itself; existing-but-empty would read as
                       # a previous run's leftover on a failure.
    return path


def _run_vcctrl(args, timeout=60.0):
    """Run bin/vcctrl, and hand back exit code + parsed JSON WITHOUT
    collapsing either. `exit_code` rides alongside whatever the daemon's own
    JSON said, because several verbs (`verify-input`, `file-check`,
    `preflight`) carry a four-valued reading (0 answered / 1 real fault /
    2 could-not-look / 3 tool failure) IN the exit code, and flattening that
    to a boolean is the single most common defect class this repo's own
    FINDINGS.md catalogs ("absence read as a value")."""
    cmd = [VCCTRL_BIN] + [str(a) for a in args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {"ok": False, "exit_code": None,
                "error": "vcctrl timed out after %ss: %s"
                         % (timeout, " ".join(cmd)),
                "stdout": (exc.stdout or ""), "stderr": (exc.stderr or "")}
    except FileNotFoundError:
        return {"ok": False, "exit_code": None,
                "error": "bin/vcctrl not found at %s" % VCCTRL_BIN}

    out = {"exit_code": proc.returncode}
    stdout = proc.stdout or ""
    stderr = (proc.stderr or "").strip()
    parsed = None
    try:
        parsed = json.loads(stdout)
    except ValueError:
        pass
    if isinstance(parsed, dict):
        out.update(parsed)
        if "ok" not in out:
            out["ok"] = (proc.returncode == 0)
    else:
        # Not every verb answers JSON (config check, keymap's usage text on
        # error, etc). The caller still gets the exit code and the words.
        out["ok"] = (proc.returncode == 0)
        if stdout.strip():
            out["raw_stdout"] = stdout.strip()
    if stderr:
        out["stderr"] = stderr
    return out


# --------------------------------------------------------------- the keymap
# Fetched once, lazily, and used ONLY to decide whether a combo is the reboot
# chord before it is sent -- never re-derived. The daemon already publishes
# this for exactly this reason (`vcctrl keymap`, daemon/vcctrld.py `keymap()`
# docstring: "the point is that there is ONE copy" -- the web KVM used to
# carry its own transcription of the alias table, kept honest by a test that
# compared the two files, and CLI-PARITY.md sec. 5a is the record of that
# going wrong once already). This class is a THIRD reader of the same
# published data, not a third copy of the policy.

class Keymap(object):
    def __init__(self):
        self._lock = threading.Lock()
        self._aliases = None
        self._reboot = None

    def _ensure_loaded(self):
        with self._lock:
            if self._aliases is not None:
                return
            res = _run_vcctrl(["keymap"])
            km = (res.get("keymap") or {}) if res.get("ok") else {}
            self._aliases = km.get("aliases") or {}
            self._reboot = set(km.get("reboot") or [])

    def is_reboot_combo(self, keys):
        """Same superset test as daemon/vcctrld.py's is_reboot_combo(): the
        reboot chord's keys, aliases collapsed, all present -- extra keys
        (Ctrl-Alt-Shift-Del) still count, because the BIOS does not care
        about the spare finger."""
        self._ensure_loaded()
        if not self._reboot:
            # Keymap unreachable -- fail toward requiring confirmation, not
            # away from it. An unconfirmed reboot chord sent because this
            # daemon call happened to fail is the wrong direction to be wrong
            # in.
            return True
        chord = {self._aliases.get(str(k).lower(), str(k).lower())
                 for k in (keys or [])}
        return self._reboot <= chord


KEYMAP = Keymap()


# ----------------------------------------------------------------- the lock
# MCP-PLAN.md sec. 3: acquire on first need (never on startup -- a read-only
# session never touches the lock at all), never force a refusal, release on
# disconnect and on idle. `ensure()` returns None on success or the
# daemon's own refusal dict on failure, verbatim -- never retried, never
# paraphrased, never broken.

class LockManager(object):
    IDLE_TIMEOUT_S = 300.0      # operator's figure, MCP-PLAN.md sec. 3/10
    POLL_S = 15.0

    def __init__(self, owner):
        self.owner = owner
        self._held = False
        self._last_touch = 0.0
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._idle_watch,
                                        name="vcctrl-mcp-idle", daemon=True)
        self._thread.start()
        atexit.register(self.release)

    def ensure(self):
        with self._state_lock:
            if self._held:
                self._last_touch = time.time()
                return None
        res = _run_vcctrl(["lock", "acquire", "--as", self.owner])
        if res.get("ok") is True and res.get("owner") == self.owner:
            with self._state_lock:
                self._held = True
                self._last_touch = time.time()
            return None
        # THE DAEMON'S OWN REFUSAL, RETURNED AS-IS. "input locked by
        # 'operator' since ..." is what a human running the CLI by hand
        # would see; an agentic caller gets the identical sentence, not a
        # summary of it, and does not retry or escalate on it.
        return res

    def touch(self):
        with self._state_lock:
            if self._held:
                self._last_touch = time.time()

    def release(self):
        with self._state_lock:
            if not self._held:
                return
            self._held = False
        _run_vcctrl(["lock", "release", "--as", self.owner])

    def _idle_watch(self):
        while not self._stop.wait(self.POLL_S):
            with self._state_lock:
                idle = self._held and (
                    time.time() - self._last_touch > self.IDLE_TIMEOUT_S)
            if idle:
                self.release()

    def stop(self):
        self._stop.set()


LOCK = LockManager(OWNER)


def _gated_run(args, timeout=60.0):
    """Acquire the lock (refusing rather than forcing on conflict), run a
    lock-requiring vcctrl command with --as attached, touch the idle clock.

    --as MUST be on every gated call, not only on `lock acquire`: the
    Arbiter compares the CALLER'S OWN name against who holds the lock
    (daemon/vcctrld.py Arbiter.check), so a gated call made with no --as is
    checked as `who=None`, which is a stranger to any held lock -- including
    one we hold ourselves. bin/vcctrl_common.py's _INPUT_CMDS carries the
    same rule for the harness scripts; this is the MCP side of that pairing.
    """
    refusal = LOCK.ensure()
    if refusal is not None:
        return refusal
    LOCK.touch()
    return _run_vcctrl(list(args) + ["--as", OWNER], timeout=timeout)


# ------------------------------------------------------------------ server

mcp = MCPServer(
    "vcctrl",
    instructions=(
        "Controls real hardware over vcctrl/vcctrld: keyboard, mouse, "
        "video/audio capture, and status for the g2k (IBM PC, PS/2) and "
        "the Mac Plus (ADB) boards this rig can carry. Every result "
        "carries the daemon's own exit_code and JSON verbatim -- 0 "
        "answered, 1 a real fault, 2 could-not-look, 3 the tool itself "
        "failed; treat 'unsupported' and 'unknown' as different facts, "
        "not both as failure. Input tools take a shared hardware lock and "
        "refuse outright (never force) if a human or another session holds "
        "it. `vcctrl_combo` requires confirm=\"reboot\" when the chord "
        "matches Ctrl-Alt-Delete."
    ),
)


# ---- Phase 1: read-only tools, no lock -------------------------------

@mcp.tool()
def vcctrl_status() -> dict:
    """Device paths, USB4VC hold state, LED state -- the Pi's own end of
    the wire. Does not prove anything reached the target; see
    vcctrl_verify_input for that."""
    return _run_vcctrl(["status"])


@mcp.tool()
def vcctrl_board() -> dict:
    """Which USB4VC protocol board is installed, and therefore which
    machine input reaches. `unknown` is a real, honest answer -- never
    guessed as the IBM PC."""
    return _run_vcctrl(["board"])


@mcp.tool()
def vcctrl_caps() -> dict:
    """Capability health across input, leds, power, video, audio, files."""
    return _run_vcctrl(["caps"])


@mcp.tool()
def vcctrl_config_show() -> dict:
    """What the RUNNING daemon actually resolved at start, with source and
    any VCCTRL_* overrides -- different from reading the file, and the one
    worth asking when something behaves oddly."""
    return _run_vcctrl(["config", "show"])


@mcp.tool()
def vcctrl_keymap() -> dict:
    """Key names this daemon accepts, chord modifier order, and the alias
    table -- read this before building a combo. Does NOT say which keys
    reach the target; only a measured sweep at the machine can."""
    return _run_vcctrl(["keymap"])


@mcp.tool()
def vcctrl_leds() -> dict:
    """PS/2 LED return channel snapshot. `available: false` with
    `why: "unsupported"` on ADB (the Mac Plus) is working hardware, not a
    fault -- see `why` for which of the (open-ended) set of reasons this
    is."""
    return _run_vcctrl(["leds"])


@mcp.tool()
def vcctrl_ledwait(timeout_s: float = 5.0) -> dict:
    """Block until LED state changes, or timeout_s elapses."""
    return _run_vcctrl(["ledwait", timeout_s], timeout=timeout_s + 10)


@mcp.tool()
def vcctrl_led_changes(n: int = 20) -> dict:
    """The last n LED transitions, newest first. Shows that an intermittent
    left a trace -- does NOT establish a reading is current, since the byte
    only arrives on a lock-key change and an idle machine publishes
    nothing."""
    return _run_vcctrl(["led-changes", n])


@mcp.tool()
def vcctrl_power_state() -> dict:
    """Smart-plug identity and last known relay state. A read, never
    gated -- diagnosing a stuck run must never require the input lock or
    knowing which board is seated. `board_match` (added alongside the
    board-scoped power fix) says whether a WRITE to this plug would be
    honoured right now for the installed board; null when the board is
    unknown."""
    return _run_vcctrl(["power", "state"])


@mcp.tool()
def vcctrl_powerlog(n: int = 50) -> dict:
    """Append-only record of every mains action taken, across daemon
    restarts."""
    return _run_vcctrl(["powerlog", n])


@mcp.tool()
def vcctrl_video_state() -> dict:
    """Capture device state."""
    return _run_vcctrl(["video", "state"])


@mcp.tool()
def vcctrl_shot(n: "int | None" = None) -> dict:
    """One selected (judged) frame, written to a local file on THIS
    machine, or an explicit 'no picture'. Two-valued: the file exists iff
    the daemon says picture; a failed shot removes any stale file at that
    path rather than leaving it for a caller to mistake for this run's."""
    out_path = _scratch_path("shot", ".jpg")
    args = ["shot", "--out", out_path]
    if n is not None:
        args.append(n)
    res = _run_vcctrl(args)
    res["out"] = out_path if os.path.exists(out_path) else None
    return res


@mcp.tool()
def vcctrl_lastgood() -> dict:
    """The last frame that was positively picture, aged. Same two-valued
    --out contract as vcctrl_shot."""
    out_path = _scratch_path("lastgood", ".jpg")
    res = _run_vcctrl(["lastgood", "--out", out_path])
    res["out"] = out_path if os.path.exists(out_path) else None
    return res


@mcp.tool()
def vcctrl_frame(seq: int) -> dict:
    """One RAW frame by sequence number (from vcctrl_timeline). RAW means
    the daemon passes no picture judgement on it -- use vcctrl_shot for a
    judged frame."""
    out_path = _scratch_path("frame-%d" % seq, ".jpg")
    res = _run_vcctrl(["frame", seq, "--out", out_path])
    res["out"] = out_path if os.path.exists(out_path) else None
    return res


@mcp.tool()
def vcctrl_burst(n: int = 5) -> dict:
    """n RAW frames at once, written to a local directory. All or
    nothing -- a burst with silent holes in it would look like a complete
    capture and is not, so a failure leaves no partial directory behind."""
    out_dir = tempfile.mkdtemp(prefix="burst-", dir=SCRATCH_DIR)
    os.rmdir(out_dir)   # vcctrl creates it; an empty dir already existing
                        # would defeat the "not empty -> refuse" guard.
    res = _run_vcctrl(["burst", n, "--out-dir", out_dir])
    res["out_dir"] = out_dir if os.path.isdir(out_dir) else None
    return res


@mcp.tool()
def vcctrl_timeline() -> dict:
    """Index of every frame in the scrub buffer: seq, timestamp, size. No
    pixels, so cheap to poll -- use this to find a seq for vcctrl_frame."""
    return _run_vcctrl(["timeline"])


@mcp.tool()
def vcctrl_framestats(n: int = 100) -> dict:
    """Duplicate-hash stats over the last n frames."""
    return _run_vcctrl(["framestats", n])


@mcp.tool()
def vcctrl_pin(action: str = "status") -> dict:
    """Stop the ring evicting frames while you examine them: action is
    status, on, or off. Auto-releases after 300s daemon-side regardless, so
    an interrupted session cannot wedge it."""
    return _run_vcctrl(["pin", action])


@mcp.tool()
def vcctrl_record(since: str, from_seq: "int | None" = None,
                  to_seq: "int | None" = None, clip: bool = False) -> dict:
    """The scrub buffer as an AVI, written to a local file.

    `since` IS REQUIRED, not optional, and must be the caller's own start
    time (a unix timestamp, or the string "now" passed at the start of
    whatever this is recording). Without it the file is bounded by the
    RING, not by the thing that asked for it -- a real capture on this rig
    was 94.5% the PREVIOUS cell's frames because the caller skipped this,
    and a confident wrong conclusion followed three cells later. See
    docs/CLI-PARITY.md sec. 5.

    `clip` takes only frames from calls made under this MCP session's own
    lock ownership instead of refusing on frames from someone else's run.
    """
    out_path = _scratch_path("record", ".avi")
    args = ["record", "--out", out_path]
    if from_seq is not None:
        args.append(from_seq)
    if to_seq is not None:
        args.append(to_seq)
    args += ["--since", since]
    if clip:
        args.append("--clip")
    res = _run_vcctrl(args, timeout=180.0)
    res["out"] = out_path if os.path.exists(out_path) else None
    return res


@mcp.tool()
def vcctrl_audio_state() -> dict:
    """ALSA capture device state."""
    return _run_vcctrl(["audio", "state"])


@mcp.tool()
def vcctrl_level(ms: int = 3000) -> dict:
    """Mean/peak dBFS over the last ms, from the PCM ring."""
    return _run_vcctrl(["level", ms], timeout=ms / 1000.0 + 20)


@mcp.tool()
def vcctrl_activity() -> dict:
    """What is running, for how long, and who holds input -- check this
    before acting, not only for a human watching the rig."""
    return _run_vcctrl(["activity"])


@mcp.tool()
def vcctrl_events(since: "int | None" = None) -> dict:
    """Activity log since a sequence number (omit for the recent window)."""
    args = ["events"]
    if since is not None:
        args.append(since)
    return _run_vcctrl(args)


@mcp.tool()
def vcctrl_lock_status() -> dict:
    """Input-lock status: who holds it, for how long. Never gated -- an
    observation, not an action."""
    return _run_vcctrl(["lock", "status"])


# ---- Phase 2: input tools, lock-gated ----------------------------------

@mcp.tool()
def vcctrl_key(keys: "list[str]") -> dict:
    """Tap keys in sequence (e.g. ["enter"], ["esc","f1"]). Acquires the
    input lock first; refuses (does not force) if a human or peer holds
    it."""
    return _gated_run(["key"] + list(keys))


@mcp.tool()
def vcctrl_type(text: str) -> dict:
    """Type literal text, shifted characters handled."""
    return _gated_run(["type", text])


@mcp.tool()
def vcctrl_hold(key: str, ms: int) -> dict:
    """Press, dwell ms, release -- the primitive gameplay/interactive
    testing needs; a keystroke with no duration cannot hold a direction."""
    return _gated_run(["hold", key, ms], timeout=ms / 1000.0 + 30)


@mcp.tool()
def vcctrl_keydown(key: str) -> dict:
    """Press and hold, no release -- pair with vcctrl_keyup or
    vcctrl_release_all."""
    return _gated_run(["keydown", key])


@mcp.tool()
def vcctrl_keyup(key: str) -> dict:
    """Release a key held by vcctrl_keydown."""
    return _gated_run(["keyup", key])


@mcp.tool()
def vcctrl_release_all() -> dict:
    """Release every key currently held by vcctrl_keydown."""
    return _gated_run(["release-all"])


@mcp.tool()
def vcctrl_combo(keys: "list[str]", confirm: "str | None" = None) -> dict:
    """Chord (e.g. ["ctrl","alt","delete"]). Keys press modifiers-first
    regardless of the order given, release in reverse; the reply echoes the
    order actually sent.

    If this chord matches the reboot combo (Ctrl-Alt-Delete, extra keys and
    all -- a superset test, same as the daemon's own is_reboot_combo), it is
    refused unless confirm="reboot" is passed explicitly. This is not a
    human-in-the-loop gate -- an agentic caller can confirm autonomously
    when a reboot is genuinely what the task calls for -- it exists so a
    reboot is never the ACCIDENTAL shape of a call, only the deliberate one.
    """
    if KEYMAP.is_reboot_combo(keys) and confirm != "reboot":
        return {"ok": False,
                "error": ("this combo matches the reboot chord -- pass "
                          "confirm=\"reboot\" to send it"),
                "keys": list(keys)}
    return _gated_run(["combo"] + list(keys))


@mcp.tool()
def vcctrl_mouse_move(dx: int, dy: int) -> dict:
    """Relative move. PS/2 and ADB mice are both relative-only -- there is
    no absolute positioning; home the cursor by moving into a screen corner
    first if you need a known starting point. Movement is silently clamped
    at screen edges."""
    return _gated_run(["mouse", "move", dx, dy])


@mcp.tool()
def vcctrl_mouse_click(button: str = "left") -> dict:
    """Click left, right, or middle."""
    return _gated_run(["mouse", "click", button])


@mcp.tool()
def vcctrl_verify_input() -> dict:
    """Prove the input path by PS/2 LED round trip -- the only check that
    says anything about the FAR end of the wire; every other input status
    describes the Pi's own end. Refuses (exit 2, available: false) on
    boards with no LED channel (ADB / the Mac Plus) rather than reporting a
    false alarm about working hardware.

    ACQUIRES THE LOCK, even though daemon/vcctrld.py does not currently
    require it for this command. _verify_input() sends a real Caps Lock
    keystroke (toggle, then restore) to get its round trip -- that is
    exactly the class of action PLAN.md sec. 2.1 warns never to send
    mid-sweep, and the daemon's GATED_COMMANDS set does not yet include it
    (a gap worth closing daemon-side too; see internal/MCP-PLAN.md sec. 10).
    This tool closes it on the MCP side regardless of what the daemon
    enforces.
    """
    return _gated_run(["verify-input"], timeout=15.0)


@mcp.tool()
def vcctrl_lock_acquire() -> dict:
    """Explicitly take the input lock without sending any input. Most
    tools acquire it automatically on first need; call this only when you
    want the lock held before doing anything else observable."""
    refusal = LOCK.ensure()
    if refusal is not None:
        return refusal
    return {"ok": True, "owner": OWNER}


@mcp.tool()
def vcctrl_lock_release() -> dict:
    """Release the input lock now, without waiting for the idle timeout --
    use this when you know you're done with input for a while and want to
    let a human or peer back in."""
    LOCK.release()
    return {"ok": True, "owner": None}


if __name__ == "__main__":
    mcp.run()
