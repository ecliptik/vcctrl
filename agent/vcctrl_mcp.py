#!/usr/bin/env python3
"""vcctrl_mcp -- the vcctrl-mcp server. One tool per CLI verb. See
internal/MCP-PLAN.md for the design this implements -- Phases 1-5, PC/DOS
only for now (Mac Plus testing is deferred; see the note on JOBS and on
`vcctrl_power` below).

TWO DEPLOYMENT MODES, same file, chosen by VCCTRL_MCP_ROLE. Named after
vcctrl.yaml's own `control:`/`daemon:` sections, not after a specific board
-- the daemon host has already changed hardware once
(docs/PI5-MIGRATION.md), and a role called "pi" would be a lie the next
time it does.

  control (default) -- runs on the control host, stdio transport, shells
    out to bin/vcctrl (which SSHes to the daemon host). This is the
    original shape and needs no environment variables set at all.

  daemon -- runs ON THE DAEMON HOST itself, alongside (NOT inside) vcctrld,
    as its own systemd service. Talks to /usr/local/bin/vcctrl directly --
    no SSH hop, so bin/vcctrl's ControlMaster and --out host-boundary fixes
    (see below) are simply not needed here; there is no host boundary to
    cross. Serves MCP over streamable-http instead of stdio, so it can be
    reached over the network (see docs/MCP-SERVER.md for why this gives up
    stdio's "no listener at all" security property, and what replaces it).

    DELIBERATELY A SEPARATE PROCESS FROM vcctrld, not built into it. Two
    reasons, decided 2026-08-25: vcctrld is threading-based (plain
    http.server for its own web UI, daemon/vcweb.py); the `mcp` package's
    HTTP transport is Starlette+uvicorn, a genuinely different (asyncio)
    runtime model, not just an extra route. And a bug in the MCP-serving
    code must not be able to take down the process that owns the uinput
    devices and the input lock -- today a crashed MCP layer means "the
    tools stop working"; built into vcctrld it would mean "input control
    stops working," a much bigger blast radius. The daemon host is a Pi 5
    now (4GB RAM, docs/PI5-MIGRATION.md) -- the old Pi-3 memory-pressure
    concern that shaped a lot of this project's caution does not apply to
    the dependency footprint; the coupling/stability risk is the real
    reason.

    THE HARNESS TOOLS (Phase 5) ARE EXCLUDED IN THIS MODE. `harness/
    vcctrl-cell`, `-sweep` and `-collect` are control-host-side
    orchestration scripts -- bin/vcctrl_common.py's vc()/vc_json() always
    resolve to the bin/vcctrl SSH-wrapper sitting next to them (VCCTRL =
    os.path.join(HERE, "vcctrl")), so running them on the daemon host would
    mean either SSHing to itself (fragile, nothing this project does
    elsewhere) or a separate fix to that resolution -- not done. Phases 1-4
    (status, capture, input, power, file transfer) are genuine vcctrld
    capabilities and port over with no code changes beyond the binary path
    and the transport.

WHY IT SHELLS OUT TO vcctrl RATHER THAN SPEAKING vcctrld'S SOCKET DIRECTLY
(true in both modes): in control mode, bin/vcctrl carries two hard-won
fixes -- SSH ControlMaster/ControlPersist (without it, a fresh ssh per call
saturated journald on the Pi 3 and took it off the network for 30 minutes,
2026-08-19), and the --out/--out-dir host-boundary rewrite (a local path is
meaningless on the far side of an ssh call, and the dangerous failure is
the one that returns 0). In daemon mode there is no SSH hop, but
/usr/local/bin/vcctrl is still the same tested CLI with the same
two-valued exit-code contracts -- reimplementing vcctrld's JSON socket
protocol here would still be a second place those contracts could disagree
with themselves. See MCP-PLAN.md sec. 2.

File-transfer commands (Phase 4) need no job-manager machinery in either
mode: `send-file`/`get-file`/`file-refresh` already "return at once" from
the DAEMON's own job model (TransferJob/PullJob) -- the CLI call itself is
fast, only the underlying transfer is slow, and `file-status` polls the
daemon's existing tracking. Confirmed by reading daemon/vcctrld.py: neither
job class calls `self.devs.key`/`type_text`/`combo`, which is also why file
transfer is not in GATED_COMMANDS -- it never touches the input lock at all.

WHAT IS DELIBERATELY NOT HERE:
  - `lock break`. Force-taking a lock a human or peer holds is exactly the
    kind of irreversible, other-affecting action this project's operating
    rules say needs a human in the loop, not a retry path. MCP-PLAN.md
    sec. 3.
  - Anything Mac-Plus-specific. The operator's direction, 2026-08-25: prove
    out the PC/DOS configuration first, Mac Plus later. Every tool below
    still behaves correctly for whichever board is actually installed
    (three-valued supported/unsupported/unknown, never assumed) because
    that discipline costs nothing extra to keep -- it just has not been
    exercised against the Mac Plus board, because the Mac Plus board is not
    what is seated right now. `POWER_BOARDS`/board-scoping is real and
    deployed (daemon/vcctrld.py); a REAL board swap test of it is still
    outstanding, tracked in internal/MCP-PLAN.md sec. 11.
"""

import atexit
import itertools
import json
import os
import socket
import subprocess
import tempfile
import threading
import time

from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------- transport

ROLE = os.environ.get("VCCTRL_MCP_ROLE", "control")
if ROLE not in ("control", "daemon"):
    raise SystemExit(
        "VCCTRL_MCP_ROLE must be 'control' or 'daemon', got %r" % ROLE)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Shared with daemon/vcctrld.py: the frequency-analysis math (and the
# verdict/similarity logic built on it) has one implementation, not one per
# side that has to be kept in agreement by hand. Two candidate locations
# because this file itself has two deployed layouts (see VCCTRL_BIN below):
# a source checkout keeps it at <repo>/common/audio_bands.py, while
# pi/install.sh flattens it to $PREFIX/audio_bands.py, a sibling of
# vcctrld.py -- which, in that layout, is also REPO_ROOT (this file sits at
# $PREFIX/agent/vcctrl_mcp.py).
try:
    import audio_bands
except ImportError:
    import importlib.util as _ilu
    _ab = next((p for p in (
        os.path.join(REPO_ROOT, "common", "audio_bands.py"),
        os.path.join(REPO_ROOT, "audio_bands.py"),
    ) if os.path.isfile(p)), None)
    if _ab:
        _spec = _ilu.spec_from_file_location("audio_bands", _ab)
        audio_bands = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(audio_bands)
    else:
        audio_bands = None

# control mode: bin/vcctrl next to this checkout, which SSHes to the
# daemon host. daemon mode: /usr/local/bin/vcctrl, the same vcctrl-client
# pi/install.sh already places there -- called directly, no SSH, no host
# boundary. VCCTRL_MCP_BIN overrides either default explicitly, for a
# nonstandard install layout.
_DEFAULT_BIN = ({"control": os.path.join(REPO_ROOT, "bin", "vcctrl"),
                 "daemon": "/usr/local/bin/vcctrl"})[ROLE]
VCCTRL_BIN = os.environ.get("VCCTRL_MCP_BIN", _DEFAULT_BIN)

# Harness workflows (Phase 5) are control-mode-only -- see the module
# docstring.
HARNESS_DIR = os.path.join(REPO_ROOT, "harness") if ROLE == "control" else None

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
    """One instance per vcctrld INSTANCE, not one for the whole MCP server.

    Phase 3 (making the CLI/MCP profile-aware) is what makes this matter:
    there are now two independent daemon-side Arbiters (one per vcctrld
    process, see vcctrl-modernpc.example.yaml's own comment on why it's a
    second process rather than a second target inside one), and a single
    shared LockManager would either acquire/release the WRONG daemon's lock
    for a profile-targeted call, or -- worse -- report a profile's commands
    as lock-free when the primary's lock happens to be held by someone else,
    since `ensure()`/`release()` would always be talking to whichever socket
    the LAST call happened to resolve to. Keyed by profile name in LOCKS
    below, `_get_lock()` is the only thing that constructs one.
    """
    IDLE_TIMEOUT_S = 300.0      # operator's figure, MCP-PLAN.md sec. 3/10
    POLL_S = 15.0

    def __init__(self, owner, profile_args=None):
        self.owner = owner
        # Baked in at construction, not passed to every ensure()/release()
        # call -- one LockManager always speaks to the same socket for its
        # whole life, so there is nowhere for this to drift out of sync with
        # which daemon it is actually locking.
        self.profile_args = list(profile_args or [])
        self._held = False
        self._sticky = False
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
        res = _run_vcctrl(["lock", "acquire", "--as", self.owner]
                          + self.profile_args)
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

    @property
    def sticky(self):
        with self._state_lock:
            return self._sticky

    def mark_sticky(self):
        """Called only by vcctrl_lock_acquire: the caller asked, explicitly,
        to keep the lock held across a sequence of calls rather than the
        default single-action footprint. See _gated_run's release-unless-
        sticky for the other half of this."""
        with self._state_lock:
            self._sticky = True

    def release(self):
        with self._state_lock:
            self._sticky = False
            if not self._held:
                return
            self._held = False
        _run_vcctrl(["lock", "release", "--as", self.owner] + self.profile_args)

    def _idle_watch(self):
        while not self._stop.wait(self.POLL_S):
            with self._state_lock:
                idle = self._held and (
                    time.time() - self._last_touch > self.IDLE_TIMEOUT_S)
            if idle:
                self.release()

    def stop(self):
        self._stop.set()


# One LockManager per profile, created lazily on first use so a profile that
# is never addressed never spins up an idle-watch thread for it. Keyed by
# the SAME string a tool's own `profile` parameter carries (None/"default"
# both mean the primary instance and share one entry) -- see
# _resolve_profile below for where that string actually comes from.
_LOCKS = {}
_LOCKS_STATE_LOCK = threading.Lock()


def _get_lock(profile):
    key = profile or "default"
    with _LOCKS_STATE_LOCK:
        lock = _LOCKS.get(key)
        if lock is None:
            profile_args = ["--profile", profile] if profile else []
            lock = LockManager(OWNER, profile_args)
            _LOCKS[key] = lock
        return lock


# THE PRIMARY INSTANCE'S OWN LOCK, kept as a module-level name because most
# of Phase 4/5 below (file transfer, harness workflows) is deliberately
# primary-only -- see their own comments -- and addresses it directly rather
# than through a `profile` parameter that would never be anything but None
# for them.
LOCK = _get_lock(None)

# The profile a caller's OMITTED `profile` argument resolves to. None until
# vcctrl_profile_set changes it -- so a fresh MCP server, or one nobody has
# ever called vcctrl_profile_set on, behaves EXACTLY as it did before Phase
# 3 existed: every profile-aware tool call with no explicit `profile`
# targets the primary instance, unconditionally. This is a hard requirement,
# not a preference -- existing callers (including this very session's own
# tool use) must see zero behavior change from adding this feature.
_CURRENT_PROFILE = None
_CURRENT_PROFILE_LOCK = threading.Lock()


def _resolve_profile(explicit):
    """None/"default" always mean "the primary instance", explicitly passed
    or resolved from the session default -- never confused with "whatever
    profile happens to be current", which is exactly the ambiguity that
    caused the Funnel incident this repo's own tailscaled-ro.service
    comments describe (an unqualified call silently hitting the wrong
    target). An explicit profile= on a call ALWAYS wins over the session
    default, so a caller who wants to be certain never has to touch
    vcctrl_profile_set at all.
    """
    if explicit is not None and explicit != "default":
        return explicit
    if explicit == "default":
        return None
    with _CURRENT_PROFILE_LOCK:
        return _CURRENT_PROFILE


def _profile_flag(profile):
    return ["--profile", profile] if profile else []


def _run_vcctrl_p(args, profile=None, timeout=60.0):
    """_run_vcctrl for a READ-ONLY, profile-aware tool: resolves `profile`
    through the session default and appends --profile if one applies.
    _gated_run (below) is the equivalent for lock-gated ones -- kept
    separate rather than one function branching on gating, because a
    read-only call must never touch a LockManager at all (MCP-PLAN.md sec.
    3: "a read-only session never touches the lock")."""
    resolved = _resolve_profile(profile)
    return _run_vcctrl(list(args) + _profile_flag(resolved), timeout=timeout)


def _active_job_conflict(action_desc, profile=None):
    """Is a file-transfer job (send/get/refresh/scan) or a harness job
    (run_cell/run_sweep/collect) currently mid-flight? If so, taking or
    holding the input lock right now risks the exact collision found live
    2026-08-31: this session's own vcctrl_verify_input + vcctrl_key calls
    acquired the lock between two phases of an in-flight vcctrl_get_file
    job and left it held, so when that job's own automation next tried to
    acquire the SAME lock (for its abort-detection reboot back to the menu
    default) it was silently refused -- refuse rather than force is correct
    behavior for the Arbiter, but it meant the job's return-reboot never
    sent its Ctrl-Alt-Del at all, and the target was left sitting in NET
    ("no measured run may start from there") until a human noticed the KVM
    looked stuck and asked about it. vcctrl_send_file/_get_file/_file_refresh/
    _file_scan already guard the OTHER half of this (a lock held BEFORE they
    launch, LOCK.release()'d proactively -- see vcctrl_send_file's own
    comment on bug b8bdcc2); this is the half where the lock gets taken
    AFTER the job has already started. Checked fresh every call, not cached,
    because a job that finished since the last check must not go on
    blocking forever.

    PRIMARY-ONLY, REGARDLESS OF `profile`: harness jobs (run_cell/run_sweep/
    collect) and file-transfer jobs only ever run against the primary
    vcctrld instance -- vcctrl-modernpc.example.yaml's own capabilities.files
    block says why (a real Linux box has its own networking; there is no
    packet-driver FTP path to modernpc at all). A sticky lock request against
    a NON-primary profile cannot collide with either, so this check is
    skipped rather than answered with a conflict that could never be real.
    """
    if profile is not None and profile != "default":
        return None
    for job_id, job in list(JOBS._jobs.items()):
        with job.lock:
            running = job.returncode is None
        if running:
            return {"ok": False,
                    "error": ("%s refused: harness job %s is still running "
                              "(vcctrl_job_status) -- its own typed steps "
                              "need this same lock. Wait for it to finish, "
                              "or vcctrl_job_cancel it first, rather than "
                              "holding the lock into its next phase."
                              % (action_desc, job_id))}
    fstat = _run_vcctrl(["file-status"], timeout=10.0)
    if bool((fstat.get("job") or {}).get("running")):
        return {"ok": False,
                "error": ("%s refused: a file-transfer job is still running "
                          "(vcctrl_file_status) -- its own typed steps need "
                          "this same lock, and a held lock is refused rather "
                          "than forced. Wait for it to finish, or "
                          "vcctrl_file_cancel it first, rather than holding "
                          "the lock into its next phase." % action_desc)}
    return None


def _gated_run(args, profile=None, timeout=60.0):
    """Acquire the RIGHT profile's lock (refusing rather than forcing on
    conflict), run a lock-requiring vcctrl command against that SAME
    profile's socket with --as attached, touch its idle clock.

    `profile` is resolved through _resolve_profile before anything else --
    every subsequent step (which LockManager, which --profile flag on the
    actual command) uses the resolved value, so the lock taken and the
    command sent are GUARANTEED to be about the same daemon. Getting this
    wrong -- locking one instance while acting on another -- would either
    block a profile's input on an unrelated daemon's lock, or worse, send
    input to a profile with NO arbitration at all while believing the
    primary's lock covers it.

    --as MUST be on every gated call, not only on `lock acquire`: the
    Arbiter compares the CALLER'S OWN name against who holds the lock
    (daemon/vcctrld.py Arbiter.check), so a gated call made with no --as is
    checked as `who=None`, which is a stranger to any held lock -- including
    one we hold ourselves. bin/vcctrl_common.py's _INPUT_CMDS carries the
    same rule for the harness scripts; this is the MCP side of that pairing.

    RELEASES THE LOCK AFTER THIS ONE ACTION UNLESS vcctrl_lock_acquire made
    it sticky. Before 2026-08-31 this never released on its own, relying on
    the 300s idle timer -- harmless for a single call in isolation, but it
    meant ANY gated call (not just an explicit vcctrl_lock_acquire) could be
    the one left holding the lock when an in-flight file/harness job next
    needed it. Default to the minimal footprint; vcctrl_lock_acquire is the
    explicit opt-in for a caller that actually wants the lock held across a
    sequence.
    """
    resolved = _resolve_profile(profile)
    lock = _get_lock(resolved)
    refusal = lock.ensure()
    if refusal is not None:
        return refusal
    lock.touch()
    try:
        return _run_vcctrl(list(args) + ["--as", OWNER]
                           + _profile_flag(resolved), timeout=timeout)
    finally:
        if not lock.sticky:
            lock.release()


# ------------------------------------------------------------------ server

mcp = MCPServer(
    "vcctrl-mcp",
    instructions=(
        "Controls real hardware over vcctrl/vcctrld: keyboard, mouse, "
        "video/audio capture, and status for whichever target machine(s) "
        "this rig is configured to carry -- call vcctrl_board and "
        "vcctrl_profiles to see which boards/targets are actually present "
        "before assuming a specific one. Every result carries the "
        "daemon's own exit_code and JSON verbatim -- 0 answered, 1 a real "
        "fault, 2 could-not-look, 3 the tool itself failed; treat "
        "'unsupported' and 'unknown' as different facts, not both as "
        "failure. Input tools take a shared hardware lock and refuse "
        "outright (never force) if a human or another session holds it. "
        "`vcctrl_combo` requires confirm=\"reboot\" when the chord matches "
        "Ctrl-Alt-Delete. Many tools accept an optional `profile` argument "
        "to address a SECOND vcctrld instance on the same host (a named "
        "sibling target this rig also carries) instead of the primary "
        "target -- call vcctrl_profiles to see what is actually running "
        "and its name(s), and vcctrl_profile_set to change which profile "
        "an omitted `profile` argument resolves to for the rest of the "
        "session. File-transfer and harness-workflow tools are "
        "primary-only and have no `profile` argument at all -- a second "
        "profile has no FTP path or harness profile of its own."
    ),
)


# ---- profile management -------------------------------------------------
# See _resolve_profile's own comment for the backward-compatibility
# guarantee this rests on: omitting `profile` everywhere below behaves
# EXACTLY as it did before these two tools existed, until vcctrl_profile_set
# is actually called.

@mcp.tool()
def vcctrl_profiles() -> dict:
    """Every vcctrld instance actually running on this host, discovered by
    socket presence (bin/vcctrl-client's own `profiles` command) -- not a
    maintained list that can drift out of sync with what is real. Always
    includes "default", the primary instance every tool targets unless told
    otherwise."""
    return _run_vcctrl(["profiles"])


@mcp.tool()
def vcctrl_profile_set(profile: "str | None" = None) -> dict:
    """Change which profile a tool call's OMITTED `profile` argument
    resolves to, for the rest of this MCP server's session -- so a sequence
    of calls can target modernpc (say) without passing profile="modernpc" on
    every single one. An explicit `profile` argument on any individual call
    always overrides this, regardless of what was last set here.

    Pass profile=None (or "default") to reset to the primary instance --
    the same state a fresh server starts in. This tool touches no lock and
    no hardware; it only changes how THIS process resolves the argument on
    later calls."""
    global _CURRENT_PROFILE
    resolved = None if profile in (None, "default", "") else profile
    with _CURRENT_PROFILE_LOCK:
        _CURRENT_PROFILE = resolved
    return {"ok": True, "current_profile": resolved or "default"}


# ---- Phase 1: read-only tools, no lock -------------------------------

@mcp.tool()
def vcctrl_status(profile: "str | None" = None) -> dict:
    """Device paths, USB4VC hold state, LED state -- the Pi's own end of
    the wire. Does not prove anything reached the target; see
    vcctrl_verify_input for that."""
    return _run_vcctrl_p(["status"], profile)


@mcp.tool()
def vcctrl_board(profile: "str | None" = None) -> dict:
    """Which USB4VC protocol board is installed, and therefore which
    machine input reaches. `unknown` is a real, honest answer -- never
    guessed as the IBM PC. Always `id: null` on a profile with no
    swappable protocol board (e.g. modernpc) -- that is the correct answer
    for a target that was never on a board-swap bridge to begin with."""
    return _run_vcctrl_p(["board"], profile)


@mcp.tool()
def vcctrl_sysinfo() -> dict:
    """The DOS target's own hardware -- CPU, memory, video, sound -- as a
    dinspect report last pulled off the card. A READING, not a live poll:
    `age_s` says how old it is and `source: null` means none has ever
    been pulled. `stale: true` means the USB4VC board changed since this
    reading was taken; `stale: false` does NOT mean nothing changed, only
    that nothing this daemon can observe did -- most hardware this
    reports on (CPU, sound card, video adapter) cannot be detected by
    the daemon at all. Use vcctrl_file_scan to take a fresh reading."""
    return _run_vcctrl(["sysinfo"])


@mcp.tool()
def vcctrl_public_telemetry() -> dict:
    """Aggregate, non-identifying usage stats for the public read-only KVM
    mirror (kvm-ro, e.g. straylight): visit counts by day, first-frame
    load-time samples (ms), and client-side error counts by message. No
    per-visitor data of any kind -- no IP, no user-agent, no cookie, no
    session id -- is ever collected, let alone returned here; this is a
    local file daemon/vcweb_public.py writes and this daemon reads directly
    off the same Pi's filesystem, never served back to a visitor over the
    network. `available: false` means no telemetry file exists yet (the
    mirror has never run, or hasn't completed its first save cycle)."""
    return _run_vcctrl(["public-telemetry"])


@mcp.tool()
def vcctrl_caps(profile: "str | None" = None) -> dict:
    """Capability health across input, leds, power, video, audio, files."""
    return _run_vcctrl_p(["caps"], profile)


@mcp.tool()
def vcctrl_note(text: str, profile: "str | None" = None) -> dict:
    """Set the one-sentence 'what's happening right now' shown on the public
    read-only KVM page -- e.g. "rebooting into NET profile to copy log files
    for review". Not gated by the input lock: it touches no hardware, so it
    needs no --as arbitration beyond attribution. Has no expiry, so a stale
    sentence reads as a live one until the next call replaces it. The public
    mirror currently shows the primary/DOS profile only regardless of what a
    non-primary profile's own note is set to (operator's call, revisit if
    that changes)."""
    resolved = _resolve_profile(profile)
    return _run_vcctrl(["note-set", "--text", text, "--as", OWNER]
                       + _profile_flag(resolved))


@mcp.tool()
def vcctrl_config_show(profile: "str | None" = None) -> dict:
    """What the RUNNING daemon actually resolved at start, with source and
    any VCCTRL_* overrides -- different from reading the file, and the one
    worth asking when something behaves oddly."""
    return _run_vcctrl_p(["config", "show"], profile)


@mcp.tool()
def vcctrl_keymap(profile: "str | None" = None) -> dict:
    """Key names this daemon accepts, chord modifier order, and the alias
    table -- read this before building a combo. Does NOT say which keys
    reach the target; only a measured sweep at the machine can."""
    return _run_vcctrl_p(["keymap"], profile)


@mcp.tool()
def vcctrl_leds(profile: "str | None" = None) -> dict:
    """PS/2 LED return channel snapshot. `available: false` with
    `why: "unsupported"` on ADB (the Mac Plus) is working hardware, not a
    fault -- see `why` for which of the (open-ended) set of reasons this
    is. Always empty on a hid-gadget profile (e.g. modernpc) -- there is no
    PS/2 return channel over a generic USB HID gadget at all."""
    return _run_vcctrl_p(["leds"], profile)


@mcp.tool()
def vcctrl_ledwait(timeout_s: float = 5.0, profile: "str | None" = None) -> dict:
    """Block until LED state changes, or timeout_s elapses."""
    return _run_vcctrl_p(["ledwait", timeout_s], profile, timeout=timeout_s + 10)


@mcp.tool()
def vcctrl_led_changes(n: int = 20, profile: "str | None" = None) -> dict:
    """The last n LED transitions, newest first. Shows that an intermittent
    left a trace -- does NOT establish a reading is current, since the byte
    only arrives on a lock-key change and an idle machine publishes
    nothing."""
    return _run_vcctrl_p(["led-changes", n], profile)


@mcp.tool()
def vcctrl_power_state() -> dict:
    """Smart-plug identity and last known relay state. A read, never
    gated -- diagnosing a stuck run must never require the input lock or
    knowing which board is seated. `board_match` (added alongside the
    board-scoped power fix) says whether a WRITE to this plug would be
    honoured right now for the installed board; null when the board is
    unknown.

    ON A METERED PLUG (a Wemo Insight, currently -- not the rig's Kasa
    EP10, which has no energy-monitoring hardware at all), `power_mw` is
    the live draw and answers a DIFFERENT question from `on`: `on` says the
    relay is closed, `power_mw` says whether the machine on the other end
    is actually pulling current. They come apart exactly when it matters --
    wedged at its own power switch, a cable out, a PSU that never came up --
    and `power_mw` is the one signal here that reaches the target without
    going through video capture at all. Null, always, on an unmetered
    backend -- never 0, which would be indistinguishable from "plugged in
    and drawing nothing." `load_on_s` beside it is seconds spent above the
    plug's own standby threshold, NOT relay-on time -- see the daemon's own
    PowerCapability.snapshot() for why the two are never merged into one
    field."""
    return _run_vcctrl(["power", "state"])


@mcp.tool()
def vcctrl_powerlog(n: int = 50) -> dict:
    """Append-only record of every mains action taken, across daemon
    restarts."""
    return _run_vcctrl(["powerlog", n])


@mcp.tool()
def vcctrl_video_state(profile: "str | None" = None) -> dict:
    """Capture device state."""
    return _run_vcctrl_p(["video", "state"], profile)


@mcp.tool()
def vcctrl_shot(n: "int | None" = None, profile: "str | None" = None) -> dict:
    """One selected (judged) frame, written to a local file on THIS
    machine, or an explicit 'no picture'. Two-valued: the file exists iff
    the daemon says picture; a failed shot removes any stale file at that
    path rather than leaving it for a caller to mistake for this run's."""
    out_path = _scratch_path("shot", ".jpg")
    args = ["shot", "--out", out_path]
    if n is not None:
        args.append(n)
    resolved = _resolve_profile(profile)
    res = _run_vcctrl(args + _profile_flag(resolved))
    res["out"] = out_path if os.path.exists(out_path) else None
    return res


@mcp.tool()
def vcctrl_camera_state() -> dict:
    """State of the second, discretionary UVC webcam pointed at the
    physical target itself (the room/hardware), not its captured video
    signal -- a photographic companion view, independent of the analog
    capture stick's own ring/lock/frozen-frame machinery, so it can still
    answer when vcctrl_video_state cannot. Absent/unconfigured on a rig
    with capabilities.camera.backend: none (`device_present: false`,
    `state: "starting"` forever). Not gated by the input lock."""
    return _run_vcctrl(["camera", "state"])


@mcp.tool()
def vcctrl_camera_shot() -> dict:
    """The single latest frame from the second camera, written to a local
    file, or an explicit no-frame-yet. RAW, like vcctrl_frame/vcctrl_burst,
    not judged like vcctrl_shot -- CameraCapability keeps no ring and makes
    no picture/no-lock judgement (a UVC webcam either delivers frames or
    the process exits, unlike the analog stick's "no data can be correct"
    text-mode case), so there is no picture/considered/live/state field to
    read, only whether a frame exists yet. Right-side up as of 2026-08-30
    (was physically mounted upside down before, with kvm.html correcting it
    client-side only via CSS -- see vcctrl-camera skill); nothing here has
    ever rotated the bytes either way, so a frame's orientation always
    follows the physical mount directly."""
    out_path = _scratch_path("camshot", ".jpg")
    res = _run_vcctrl(["camera", "shot", "--out", out_path])
    res["out"] = out_path if os.path.exists(out_path) else None
    return res


@mcp.tool()
def vcctrl_lastgood(profile: "str | None" = None) -> dict:
    """The last frame that was positively picture, aged. Same two-valued
    --out contract as vcctrl_shot."""
    out_path = _scratch_path("lastgood", ".jpg")
    resolved = _resolve_profile(profile)
    res = _run_vcctrl(["lastgood", "--out", out_path] + _profile_flag(resolved))
    res["out"] = out_path if os.path.exists(out_path) else None
    return res


@mcp.tool()
def vcctrl_frame(seq: int, profile: "str | None" = None) -> dict:
    """One RAW frame by sequence number (from vcctrl_timeline). RAW means
    the daemon passes no picture judgement on it -- use vcctrl_shot for a
    judged frame."""
    out_path = _scratch_path("frame-%d" % seq, ".jpg")
    resolved = _resolve_profile(profile)
    res = _run_vcctrl(["frame", seq, "--out", out_path] + _profile_flag(resolved))
    res["out"] = out_path if os.path.exists(out_path) else None
    return res


# Diagnostic log for correlating vcctrl_burst calls against Anthropic-side
# safety-classifier interruptions seen during burst-heavy porting sessions
# -- the trigger pattern is not established yet (2026-08-30), and tuning
# burst usage against a guess risks fixing noise instead of the real
# pattern. Gitignored as part of internal/ (CLAUDE.md), and a logging
# failure here must never block the actual capture it's describing.
#
# The trip repeated 2026-08-30 ~20:47 and this log caught the run leading
# into it: two ~3.5-4min stretches of bursts polling a DOS game's video RAM
# every 30-40s while it progressively corrupted into color noise/static,
# then a switch back to real audio -- the log stops one call short of that.
# Volume and image content are both plausible and not separable from one
# log; see vcctrl-mcp-workflows' own note on this for the guidance that
# follows from it (don't poll a progressing corruption bug via repeated
# bursts -- let the ring record it and review with vcctrl_timeline after).
BURST_LOG = os.path.join(REPO_ROOT, "internal", "burst-calls.jsonl")


def _log_burst_call(n, context, res):
    try:
        os.makedirs(os.path.dirname(BURST_LOG), exist_ok=True)
        entry = {
            "ts": time.time(),
            "ts_local": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "n": n,
            "context": context,
            "ok": res.get("ok"),
            "exit_code": res.get("exit_code"),
            "out_dir": res.get("out_dir"),
        }
        with open(BURST_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


@mcp.tool()
def vcctrl_burst(n: int = 3, context: str = "", profile: "str | None" = None) -> dict:
    """n RAW frames at once, written to a local directory. All or
    nothing -- a burst with silent holes in it would look like a complete
    capture and is not, so a failure leaves no partial directory behind.

    Default lowered from 5 to 3 on 2026-08-30 (see BURST_LOG above): the
    common call -- confirming a just-typed command actually landed, per
    vcctrl-mcp-workflows -- wants a couple of FRESH raw frames, not a
    deep sample; settle frames only appear after a device open or mode
    change (docs/WEBKVM.md sec. 4.4), not after an ordinary keystroke, so
    3 is not chasing a known failure mode, just cutting per-call size.
    Pass a larger n explicitly for motion/animation diagnostics, where
    more frames genuinely buy more evidence.

    `context` is a short free-text note of what this burst is for (e.g.
    "confirm MD command landed") -- never sent to the daemon, only appended
    to internal/burst-calls.jsonl alongside n and the result. Lets a later
    session correlate call volume/frequency/timing against a guardrail
    interruption instead of guessing at the trigger after the fact.
    """
    out_dir = tempfile.mkdtemp(prefix="burst-", dir=SCRATCH_DIR)
    os.rmdir(out_dir)   # vcctrl creates it; an empty dir already existing
                        # would defeat the "not empty -> refuse" guard.
    resolved = _resolve_profile(profile)
    res = _run_vcctrl(["burst", n, "--out-dir", out_dir] + _profile_flag(resolved))
    res["out_dir"] = out_dir if os.path.isdir(out_dir) else None
    _log_burst_call(n, context, res)
    return res


@mcp.tool()
def vcctrl_timeline(profile: "str | None" = None) -> dict:
    """Index of every frame in the scrub buffer: seq, timestamp, size. No
    pixels, so cheap to poll -- use this to find a seq for vcctrl_frame."""
    return _run_vcctrl_p(["timeline"], profile)


@mcp.tool()
def vcctrl_framestats(n: int = 100, profile: "str | None" = None) -> dict:
    """Duplicate-hash stats over the last n frames."""
    return _run_vcctrl_p(["framestats", n], profile)


@mcp.tool()
def vcctrl_pin(action: str = "status", profile: "str | None" = None) -> dict:
    """Stop the ring evicting frames while you examine them: action is
    status, on, or off. Auto-releases after 300s daemon-side regardless, so
    an interrupted session cannot wedge it."""
    return _run_vcctrl_p(["pin", action], profile)


@mcp.tool()
def vcctrl_record(since: str, from_seq: "int | None" = None,
                  to_seq: "int | None" = None, clip: bool = False,
                  profile: "str | None" = None) -> dict:
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
    resolved = _resolve_profile(profile)
    res = _run_vcctrl(args + _profile_flag(resolved), timeout=180.0)
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
def vcctrl_spectrum(ms: int = 3000) -> dict:
    """Per-band energy and active_bands over the last ms, from the PCM ring.

    Frequency-domain complement to vcctrl_level: real music/SFX spreads
    energy across multiple bands, a steady tone or hum concentrates it in
    one. active_bands compares bands to EACH OTHER, so unlike mean_db/
    peak_db it stays meaningful regardless of the physical volume knob.
    """
    return _run_vcctrl(["spectrum", ms], timeout=ms / 1000.0 + 20)


@mcp.tool()
def vcctrl_audio_verdict(ms: int = 3000) -> dict:
    """Is music/SFX playing right now -- one call, no thresholds to know.

    Combines vcctrl_level and vcctrl_spectrum into the same NO_SIGNAL /
    SILENT / AUDIO_PRESENT judgement bin/vcctrl-audio prints as text (same
    common/audio_bands.verdict() function, so the two can never quietly
    disagree). This is the automated stand-in for a human listening: call
    it instead of reading mean_db/active_bands yourself and re-deriving the
    floor/margin logic.

    `tone_like` is True when the signal looks more like a steady tone/hum
    than music/SFX, False when it looks like real content, and None when
    there wasn't enough information to judge (no spectrum reading, and the
    amplitude spread alone didn't already settle it). Sequencing pitfall:
    a title/start-gate screen can hold real content silent until a keypress
    dismisses it -- confirm the keypress landed (a visible on-screen change)
    before reading a SILENT verdict as a bug.
    """
    if audio_bands is None:
        return {"ok": False, "error": "audio_bands module not found "
                "(expected beside common/ in a checkout, or beside "
                "vcctrld.py in a deployed install)"}
    lv = _run_vcctrl(["level", ms], timeout=ms / 1000.0 + 20)
    if not lv.get("ok") or lv.get("mean_db") is None:
        return {"ok": False, "error": "no level reading", "level": lv}
    sp = _run_vcctrl(["spectrum", ms], timeout=ms / 1000.0 + 20)
    active = sp.get("active_bands") if sp.get("ok") else None
    n_bands = len(sp.get("bands_hz") or []) if sp.get("ok") else None
    v = audio_bands.verdict(lv["mean_db"], lv["peak_db"], lv.get("hist") or {},
                            active, n_bands)
    return dict({"ok": True, "mean_db": lv["mean_db"], "peak_db": lv["peak_db"],
                "active_bands": active, "n_bands": n_bands}, **v)


# CONTROL-MODE-ONLY, same reasoning as vcctrl_sweep_list/vcctrl_run_cell
# below: a reference audio file lives beside the git checkout on the
# control host, not on the Pi's filesystem, so daemon mode has nothing
# correct to do with a file_path either.
if ROLE == "control":
    @mcp.tool()
    def vcctrl_audio_match(file_path: str, ms: int = 3000) -> dict:
        """Does the live audio resemble a reference file's frequency shape?

        Decodes `file_path` (a LOCAL file on this control host -- a game's
        own music/SFX asset, say) via ffmpeg into the same 8-band signature
        vcctrl_spectrum produces, then compares it against a fresh live
        reading with a Jaccard overlap of each side's "active bands" (same
        active-band test vcctrl_audio_verdict uses). 1.0 is an identical
        shape, 0.0 is no overlap at all.

        COARSE BY CONSTRUCTION -- read the number as "does the live signal's
        frequency balance resemble the reference's", never as "is this exact
        track playing right now". 8 bands cannot distinguish two tracks with
        similar broad frequency balance, and this checks nothing about
        tempo, melody, or timing. No match/no-match threshold is asserted
        here: there is no real-file measurement calibrating one yet (see
        common/audio_bands.similarity()'s own docstring for why a similar
        shortcut already went wrong once for this feature). Judge the score
        yourself until that calibration exists.
        """
        if audio_bands is None:
            return {"ok": False, "error": "audio_bands module not found"}
        try:
            ref_bands = audio_bands.decode_file_to_band_db(file_path)
        except Exception as exc:
            return {"ok": False, "error": "could not decode %r: %s"
                    % (file_path, exc)}
        if ref_bands is None:
            return {"ok": False, "error": "reference file too short to "
                    "analyze (needs at least one %d-sample window)"
                    % audio_bands.SPECTRUM_FFT_N}
        sp = _run_vcctrl(["spectrum", ms], timeout=ms / 1000.0 + 20)
        if not sp.get("ok") or sp.get("band_db") is None:
            return {"ok": False, "error": "no live spectrum reading", "live": sp}
        live_bands = sp["band_db"]
        return {"ok": True, "similarity": audio_bands.similarity(ref_bands, live_bands),
                "reference_bands": ref_bands, "live_bands": live_bands,
                "bands_hz": sp.get("bands_hz"),
                "caveat": "coarse spectral-shape match only -- does not "
                          "verify tempo, melody, or exact track identity"}


@mcp.tool()
def vcctrl_activity(profile: "str | None" = None) -> dict:
    """What is running, for how long, and who holds input -- check this
    before acting, not only for a human watching the rig."""
    return _run_vcctrl_p(["activity"], profile)


@mcp.tool()
def vcctrl_events(since: "int | None" = None, profile: "str | None" = None) -> dict:
    """Activity log since a sequence number (omit for the recent window)."""
    args = ["events"]
    if since is not None:
        args.append(since)
    return _run_vcctrl_p(args, profile)


@mcp.tool()
def vcctrl_lock_status(profile: "str | None" = None) -> dict:
    """Input-lock status: who holds it, for how long. Never gated -- an
    observation, not an action. Each profile has its OWN lock (its own
    vcctrld instance, its own Arbiter) -- a lock held on the primary
    instance says nothing about whether modernpc's is free, and vice versa."""
    return _run_vcctrl_p(["lock", "status"], profile)


# ---- Phase 2: input tools, lock-gated ----------------------------------

@mcp.tool()
def vcctrl_key(keys: "list[str]", profile: "str | None" = None) -> dict:
    """Tap keys in sequence (e.g. ["enter"], ["esc","f1"]). Acquires the
    input lock first; refuses (does not force) if a human or peer holds
    it."""
    return _gated_run(["key"] + list(keys), profile)


@mcp.tool()
def vcctrl_type(text: str, profile: "str | None" = None) -> dict:
    """Type literal text, shifted characters handled."""
    return _gated_run(["type", text], profile)


@mcp.tool()
def vcctrl_hold(key: str, ms: int, profile: "str | None" = None) -> dict:
    """Press, dwell ms, release -- the primitive gameplay/interactive
    testing needs; a keystroke with no duration cannot hold a direction."""
    return _gated_run(["hold", key, ms], profile, timeout=ms / 1000.0 + 30)


@mcp.tool()
def vcctrl_keydown(key: str, profile: "str | None" = None) -> dict:
    """Press and hold, no release -- pair with vcctrl_keyup or
    vcctrl_release_all."""
    return _gated_run(["keydown", key], profile)


@mcp.tool()
def vcctrl_keyup(key: str, profile: "str | None" = None) -> dict:
    """Release a key held by vcctrl_keydown."""
    return _gated_run(["keyup", key], profile)


@mcp.tool()
def vcctrl_release_all(profile: "str | None" = None) -> dict:
    """Release every key currently held by vcctrl_keydown."""
    return _gated_run(["release-all"], profile)


@mcp.tool()
def vcctrl_combo(keys: "list[str]", confirm: "str | None" = None,
                 profile: "str | None" = None) -> dict:
    """Chord (e.g. ["ctrl","alt","delete"]). Keys press modifiers-first
    regardless of the order given, release in reverse; the reply echoes the
    order actually sent.

    If this chord matches the reboot combo (Ctrl-Alt-Delete, extra keys and
    all -- a superset test, same as the daemon's own is_reboot_combo), it is
    refused unless confirm="reboot" is passed explicitly. This is not a
    human-in-the-loop gate -- an agentic caller can confirm autonomously
    when a reboot is genuinely what the task calls for -- it exists so a
    reboot is never the ACCIDENTAL shape of a call, only the deliberate one.

    The reboot-chord CHECK itself is asked of the PRIMARY instance's keymap
    regardless of `profile` (KEYMAP is fetched once, lazily, from whichever
    daemon answered first) -- Ctrl-Alt-Delete is the same three USB HID
    usages on every backend this daemon has, so this is a shared fact, not
    a per-profile one, and re-fetching it per profile would just be the
    same answer fetched twice.
    """
    if KEYMAP.is_reboot_combo(keys) and confirm != "reboot":
        return {"ok": False,
                "error": ("this combo matches the reboot chord -- pass "
                          "confirm=\"reboot\" to send it"),
                "keys": list(keys)}
    return _gated_run(["combo"] + list(keys), profile)


@mcp.tool()
def vcctrl_mouse_move(dx: int, dy: int, profile: "str | None" = None) -> dict:
    """Relative move. PS/2 and ADB mice, and the hid-gadget backend, are all
    relative-only -- there is no absolute positioning; home the cursor by
    moving into a screen corner first if you need a known starting point.
    Movement is silently clamped at screen edges."""
    return _gated_run(["mouse", "move", dx, dy], profile)


@mcp.tool()
def vcctrl_mouse_click(button: str = "left", profile: "str | None" = None) -> dict:
    """Click left, right, or middle."""
    return _gated_run(["mouse", "click", button], profile)


@mcp.tool()
def vcctrl_mouse_down(button: str = "left", profile: "str | None" = None) -> dict:
    """Press and hold a mouse button, for a drag. The matching
    vcctrl_mouse_up may never come from your side -- a disconnect (or the
    web KVM releasing capture) still releases it, the same guarantee
    release_all already gives a key left down by vcctrl_keydown."""
    return _gated_run(["mouse", "down", button], profile)


@mcp.tool()
def vcctrl_mouse_up(button: str = "left", profile: "str | None" = None) -> dict:
    """Release a mouse button held by vcctrl_mouse_down."""
    return _gated_run(["mouse", "up", button], profile)


@mcp.tool()
def vcctrl_mouse_release_all(profile: "str | None" = None) -> dict:
    """Release every mouse button vcctrl_mouse_down left held. Separate
    from vcctrl_release_all (the keyboard's own version): a viewer can hold
    keyboard and mouse capture independently, and giving up one must not
    silently drop whatever the other is mid-operation on."""
    return _gated_run(["mouse", "release-all"], profile)


@mcp.tool()
def vcctrl_mouse_wheel(dy: int, profile: "str | None" = None) -> dict:
    """Scroll. Confirmed live 2026-09-02 over the hid-gadget backend (a
    terminal window on modernpc visibly scrolled). Unmeasured on PS/2 --
    see docs/MOUSE.md sec 7 -- whether it reaches DOS at all."""
    return _gated_run(["mouse", "wheel", dy], profile)


@mcp.tool()
def vcctrl_verify_input(profile: "str | None" = None) -> dict:
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
    enforces. Always `available: false` on a hid-gadget profile -- see
    vcctrl_leds; there is no LED channel to round-trip through there
    either.
    """
    return _gated_run(["verify-input"], profile, timeout=15.0)


@mcp.tool()
def vcctrl_lock_acquire(profile: "str | None" = None) -> dict:
    """Explicitly take the input lock AND KEEP IT HELD across every
    subsequent gated call against this SAME profile, until
    vcctrl_lock_release(profile) or the 300s idle timer. Most tools acquire
    the lock automatically on first need and release it again as soon as
    that one action finishes (see _gated_run); call this only when you
    deliberately want a longer, visible hold across a whole sequence.

    Refuses if a file-transfer job (send/get/refresh/scan) or a harness job
    (run_cell/run_sweep/collect) is currently running -- checked only for
    the primary instance, since neither kind of job exists for any other
    profile (see _active_job_conflict). A sticky hold taken while your own
    background job is still in flight is exactly what silently stalled a
    DOSSAGE fetch's return-reboot on 2026-08-31 -- the job's own next typed
    step needed this lock and was refused, not forced, so it sat stuck
    rather than failing loudly. Wait for the job to finish (poll
    vcctrl_file_status / vcctrl_job_status) before asking for a sticky hold
    on the primary instance, or vcctrl_file_cancel / vcctrl_job_cancel it
    first."""
    resolved = _resolve_profile(profile)
    conflict = _active_job_conflict("vcctrl_lock_acquire", resolved)
    if conflict is not None:
        return conflict
    lock = _get_lock(resolved)
    refusal = lock.ensure()
    if refusal is not None:
        return refusal
    lock.mark_sticky()
    return {"ok": True, "owner": OWNER, "sticky": True,
            "profile": resolved or "default"}


@mcp.tool()
def vcctrl_lock_release(profile: "str | None" = None) -> dict:
    """Release the input lock now (and clear any sticky hold from
    vcctrl_lock_acquire) for this profile, without waiting for the idle
    timeout -- use this when you know you're done with input for a while
    and want to let a human, peer, or one of your own in-flight jobs back
    in. Releasing one profile's lock has no effect on any other profile's --
    each has its own."""
    resolved = _resolve_profile(profile)
    _get_lock(resolved).release()
    return {"ok": True, "owner": None, "profile": resolved or "default"}


# ---- Phase 3: power tools ----------------------------------------------
# Board-scoped as of daemon/vcctrld.py's PowerCapability.support() (deployed
# and CLI-verified 2026-08-25 -- `board_match: true` for the installed g2k).
# NOT YET PROVEN against a real board swap; see the module docstring and
# MCP-PLAN.md sec. 11. `on`/`off`/`cycle` will refuse rather than act if the
# installed board is not one the configured plug is declared to control --
# that refusal is unit-tested but not yet hardware-tested, because there is
# only one board on this rig right now.

@mcp.tool()
def vcctrl_power(action: str, confirm: "str | None" = None,
                 off_seconds: "float | None" = None) -> dict:
    """Mains control. action is "on", "off", or "cycle" -- each REQUIRES
    confirm to equal the action name exactly (confirm="cycle" for a cycle),
    the same discipline as vcctrl_combo's reboot gate: a named confirmation
    that has to be typed on purpose, not a bare boolean one accidental True
    turns into nothing.

    Refused by the daemon itself, before the plug is touched, if the
    installed board is not one the configured plug is declared to control
    (docs/BOARD-IDENTITY.md sec. 5) -- check vcctrl_power_state's
    board_match first if you want to know in advance rather than by
    refusal. `cycle` is unconditional (off, wait off_seconds, on) even if
    the plug already reads off, because a wedged machine can report on
    while being useless.
    """
    if action not in ("on", "off", "cycle"):
        return {"ok": False, "error": 'action must be "on", "off", or "cycle"'}
    if confirm != action:
        return {"ok": False,
                "error": ("power actions require confirm to equal the "
                          "action name -- pass confirm=%r to send this" % action)}
    refusal = LOCK.ensure()
    if refusal is not None:
        return refusal
    LOCK.touch()
    args = ["power", action]
    if action == "cycle" and off_seconds is not None:
        args.append(off_seconds)
    # cycle blocks for the full off_seconds itself (PowerCapability's own
    # docstring: "touches no device and holds no lock ... `cycle` blocks for
    # 15s") -- give it real headroom rather than the 60s default.
    return _run_vcctrl(args + ["--as", OWNER],
                       timeout=(off_seconds or 20.0) + 30.0)


# ---- Phase 4: file transfer -------------------------------------------
# NONE of these need the job manager below -- send-file/get-file/
# file-refresh already "return at once" from the DAEMON's own job tracking
# (TransferJob/PullJob), so the CLI call itself is fast; file-status polls
# what the daemon is already doing in the background. See the module
# docstring. None of them touch the input lock either (confirmed by reading
# daemon/vcctrld.py: neither job class calls devs.key/type_text/combo) --
# consistent with file transfer being absent from GATED_COMMANDS.
#
# send-file / get-file / file-refresh DO reboot the target and destroy its
# environment, though, which is disruptive in a different way than an input
# collision -- so those three require the same named-confirm discipline as
# vcctrl_power, even though the daemon itself does not gate them.

@mcp.tool()
def vcctrl_files() -> dict:
    """Can the currently attached machine be sent a file at all? A WORD,
    not a boolean: available, unsupported (e.g. the Mac Plus -- no packet
    driver, working hardware, nothing to fix), unknown, not-configured,
    unreachable, or unchecked."""
    return _run_vcctrl(["files"])


@mcp.tool()
def vcctrl_file_check(timeout: "float | None" = None) -> dict:
    """Is the file server actually answering? Run BEFORE a transfer, not
    after -- a dead server discovered post-reboot has cost the target's
    whole environment for nothing. Four-valued exit code like
    vcctrl_verify_input: 0 ready, 1 not answering, 2 could-not-look,
    3 this machine cannot receive files at all."""
    args = ["file-check"]
    if timeout is not None:
        args.append(timeout)
    return _run_vcctrl(args, timeout=(timeout or 20.0) + 15.0)


@mcp.tool()
def vcctrl_stage_file(path: str, name: "str | None" = None,
                      replace: bool = False) -> dict:
    """Queue a LOCAL file (on this control host) for the next transfer.
    Nothing is sent to the target yet -- this only stages it. `path` must
    exist on the machine running this MCP server; bin/vcctrl handles the
    copy to the daemon host itself (the same --out-style host-boundary
    handling as every capture tool, mirrored the other direction)."""
    args = ["stage-file", path]
    if name is not None:
        args.append(name)
    if replace:
        args.append("--replace")
    return _run_vcctrl(args, timeout=120.0)


@mcp.tool()
def vcctrl_file_queue(action: str = "list", name: "str | None" = None) -> dict:
    """What is staged: action is "list" or "clear". `clear` with no name
    drops everything staged; a name drops just that one. Never touches
    anything this MCP session did not stage itself... actually it is
    daemon-global, so `clear` drops what ANY caller staged -- check
    vcctrl_file_queue("list") first if that matters."""
    args = ["file-queue", action]
    if name is not None:
        args.append(name)
    return _run_vcctrl(args)


@mcp.tool()
def vcctrl_file_name(name: str) -> dict:
    """What `name` becomes on the target: DOS 8.3, uppercase. The rename
    happens here because the target cannot rename in transit -- check this
    before staging if the shortened name matters."""
    return _run_vcctrl(["file-name", name])


@mcp.tool()
def vcctrl_file_bats() -> dict:
    """The CONTENTS of the two batch files this rig's CF card needs
    (VCGET.BAT, VCCHK.BAT), carrying this rig's own server address and
    login -- returned as text, not written anywhere. Deliberately no
    output-directory option: the CLI's `file-bats DIR` form writes on
    whichever host runs the command, and unlike shot/frame/record that path
    is NOT covered by bin/vcctrl's --out host-boundary rewrite (it takes a
    bare positional DIR, not a --out flag), so a naive wrapper here would
    silently write on the daemon host while a control-mode caller expected
    its own disk. Getting the returned text onto a card is a manual
    follow-up step."""
    return _run_vcctrl(["file-bats"])


@mcp.tool()
def vcctrl_send_file(mode: str, dest: "str | None" = None,
                     confirm: "str | None" = None) -> dict:
    """Send everything staged to the target. mode is "return" (reboot back
    to the measurement-clean menu default afterward) or "stay" (leave the
    machine in NET -- no measured run may start from there). REBOOTS THE
    TARGET TWICE and destroys its environment, so this requires
    confirm="send". Returns at once -- poll with vcctrl_file_status."""
    if mode not in ("return", "stay"):
        return {"ok": False, "error": 'mode must be "return" or "stay"'}
    if confirm != "send":
        return {"ok": False,
                "error": ('reboots the target twice -- pass confirm="send" '
                          "to proceed")}
    args = ["send-file", "--%s" % mode]
    if dest is not None:
        args += ["--dest", dest]
    # THE SAME BUG b8bdcc2 FIXED FOR run_cell/run_sweep/collect, missed here
    # because this job runs IN-PROCESS in vcctrld rather than as a separate
    # script -- so it does not hard-crash the way vcctrl-cell did, it just
    # sends its own Ctrl-Alt-Del under the "transfer" identity, gets silently
    # refused by THIS session's still-held lock (a prior vcctrl_preflight or
    # vcctrl_combo call), and times out 100+ seconds later reporting
    # "the machine never reset" -- a real reboot that was never attempted,
    # not a slow or swallowed one. Found live 2026-08-26 chasing exactly that
    # symptom: `vcctrl_lock_status` showed this session's own identity still
    # holding the arbiter minutes after the calls that took it, every
    # send_file in that window refused at the chord, none of them reported
    # why. Release proactively, same as the harness-workflow tools -- harmless
    # if nothing was held.
    LOCK.release()
    return _run_vcctrl(args, timeout=60.0)


@mcp.tool()
def vcctrl_file_status(n: int = 40) -> dict:
    """How the running transfer (send/refresh/get) is getting on, with the
    last n log lines. Exit 0 while still running -- nothing has failed yet;
    0 also means fully done and verified. 1 means at least one file did
    not. 2 means it finished but work was left (cancelled or stopped
    early) -- `remaining` names it."""
    return _run_vcctrl(["file-status", n])


@mcp.tool()
def vcctrl_file_cancel() -> dict:
    """Stop after the file currently in flight. NOT mid-transfer -- the
    target's FTP client cannot be interrupted from here, so between files
    is the only honest boundary."""
    return _run_vcctrl(["file-cancel"])


@mcp.tool()
def vcctrl_file_list(from_dir: "str | None" = None) -> dict:
    """What the target's OUT directory held when it was LAST READ, with the
    age of that reading. Touches nothing -- reading it for real means
    rebooting, which is what vcctrl_file_refresh is for."""
    args = ["file-list"]
    if from_dir is not None:
        args += ["--from", from_dir]
    return _run_vcctrl(args)


@mcp.tool()
def vcctrl_file_refresh(mode: str, from_dir: "str | None" = None,
                        already_net: bool = False,
                        confirm: "str | None" = None) -> dict:
    """Go and actually read the target's OUT directory. mode is "return" or
    "stay", same meaning as vcctrl_send_file. REBOOTS THE TARGET, so this
    requires confirm="refresh". already_net skips the reboot but NOT the
    arrival proof -- being wrong about where the machine already is costs a
    refusal, not a command typed into a profile with no network. Returns at
    once -- poll with vcctrl_file_status."""
    if mode not in ("return", "stay"):
        return {"ok": False, "error": 'mode must be "return" or "stay"'}
    if confirm != "refresh":
        return {"ok": False,
                "error": ('reboots the target unless already_net -- pass '
                          'confirm="refresh" to proceed')}
    args = ["file-refresh", "--%s" % mode]
    if from_dir is not None:
        args += ["--from", from_dir]
    if already_net:
        args.append("--already-net")
    LOCK.release()   # see vcctrl_send_file's comment on the same call
    return _run_vcctrl(args, timeout=60.0)


@mcp.tool()
def vcctrl_get_file(names: "list[str] | None" = None, fetch_all: bool = False,
                    mode: str = "return", paranoid: bool = False,
                    from_dir: "str | None" = None, already_net: bool = False,
                    confirm: "str | None" = None) -> dict:
    """Fetch files out of the target's OUT directory. Pass either
    fetch_all=True or a names list (from vcctrl_file_list) -- a name not
    currently listed there is refused here rather than typed at the
    machine. REBOOTS THE TARGET (unless already_net), so this requires
    confirm="get". paranoid fetches each file twice and compares them,
    which proves the path is repeatable and still not that either copy
    equals what is on the card -- nothing on DOS 6.22 can hash a file.
    NOTHING IS EVER DELETED FROM THE TARGET. Returns at once -- poll with
    vcctrl_file_status; fetched files land in vcctrl_pulled."""
    if mode not in ("return", "stay"):
        return {"ok": False, "error": 'mode must be "return" or "stay"'}
    if not fetch_all and not names:
        return {"ok": False, "error": "pass fetch_all=True or a names list"}
    if confirm != "get":
        return {"ok": False,
                "error": ('reboots the target unless already_net -- pass '
                          'confirm="get" to proceed')}
    args = ["get-file"]
    args += ["--all"] if fetch_all else list(names)
    args.append("--%s" % mode)
    if paranoid:
        args.append("--paranoid")
    if from_dir is not None:
        args += ["--from", from_dir]
    if already_net:
        args.append("--already-net")
    LOCK.release()   # see vcctrl_send_file's comment on the same call
    return _run_vcctrl(args, timeout=60.0)


@mcp.tool()
def vcctrl_file_scan(mode: str = "return",
                     confirm: "str | None" = None) -> dict:
    """Take a fresh dinspect reading of the DOS target's own hardware.
    REBOOTS THE TARGET TWICE: once to the menu default (typed blind --
    nothing here can confirm the machine is at a usable prompt first, so
    only call this when you know it is not mid-boot or mid-something-else)
    to run DINSPECT.EXE there, then into NET to fetch its report. Requires
    confirm="scan". DINSPECT.EXE must already be staged at C:\\XFER\\IN on
    the card (see the vcctrl-dinspect-sysinfo skill) -- if it is not, this
    still reboots twice and comes back `not-listed` rather than silently
    doing nothing. Returns at once -- poll with vcctrl_file_status, then
    read the result with vcctrl_sysinfo."""
    if mode not in ("return", "stay"):
        return {"ok": False, "error": 'mode must be "return" or "stay"'}
    if confirm != "scan":
        return {"ok": False,
                "error": ('reboots the target twice -- pass confirm="scan" '
                          "to proceed")}
    args = ["file-scan", "--%s" % mode]
    LOCK.release()   # see vcctrl_send_file's comment on the same call
    return _run_vcctrl(args, timeout=60.0)


@mcp.tool()
def vcctrl_pulled(action: str = "list", name: "str | None" = None) -> dict:
    """What has been fetched off the target and is sitting on the daemon
    host: action is "list" or "clear" (+name). Use vcctrl_pulled_save to
    copy one to THIS machine."""
    args = ["pulled", action]
    if name is not None:
        args.append(name)
    return _run_vcctrl(args)


@mcp.tool()
def vcctrl_pulled_save(name: str) -> dict:
    """Copy one previously-fetched file to a local path on this machine
    (via bin/vcctrl's normal --out host-boundary handling)."""
    out_path = _scratch_path("pulled-%s" % name, "")
    res = _run_vcctrl(["pulled", "save", name, "--out", out_path])
    res["out"] = out_path if os.path.exists(out_path) else None
    return res


# ---- Phase 5: harness workflows, and the job manager they need --------
# vcctrl-cell/-sweep/-collect run ON THE CONTROL HOST and block for their
# real duration (a sweep is 12-23 minutes, profiles/doskutsu.yaml) -- see
# the module docstring for why that's a different shape from Phase 4.
# Launched detached, polled by job id. THE HARNESS SCRIPTS' OWN GUARDS ARE
# NOT DUPLICATED HERE: vcctrl-cell already refuses to run concurrently with
# another poller (its own lock file, per its docstring point 4), and
# vcctrl-sweep already refuses keyboard probing mid-sweep by construction.
# This layer's only job is process supervision and result shaping.
#
# PROVEN LIVE, 2026-08-25: a real cell (Mach64, POD-83, visually confirmed
# running mid-cell), a real sweep (MINE, 2 cells, DOS-side completion
# banner confirmed), and collect (both cell logs fetched, size-verified).
# See docs/MCP-SERVER.md sec. 6 for the full status table.

class Job(object):
    def __init__(self, job_id, argv):
        self.id = job_id
        self.argv = argv
        self.lock = threading.Lock()
        self.lines = []
        self.returncode = None
        self.started_at = time.time()
        self.finished_at = None
        self.proc = None


class JobManager(object):
    """Launch a long-running control-host-side script detached, poll it by
    id.

    Output is streamed line-by-line into the job's own buffer as it is
    produced (not collected at the end via communicate()), so a poll mid-run
    shows real progress on a 20-minute sweep instead of nothing until it
    finishes. stderr is merged into the same stream -- a single
    chronological log is more useful here than two that have to be
    interleaved by hand afterward.
    """

    def __init__(self):
        self._jobs = {}
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def launch(self, argv, cwd=None):
        with self._lock:
            job_id = "job-%d" % next(self._counter)
        job = Job(job_id, argv)
        self._jobs[job_id] = job

        def run():
            try:
                proc = subprocess.Popen(
                    argv, cwd=cwd, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1)
                with job.lock:
                    job.proc = proc
                for line in iter(proc.stdout.readline, ""):
                    with job.lock:
                        job.lines.append(line.rstrip("\n"))
                proc.wait()
                with job.lock:
                    job.returncode = proc.returncode
                    job.finished_at = time.time()
            except Exception as exc:
                with job.lock:
                    job.lines.append("[vcctrl_mcp job error] %s" % exc)
                    job.returncode = -1
                    job.finished_at = time.time()

        threading.Thread(target=run, name="vcctrl-mcp-%s" % job_id,
                         daemon=True).start()
        return job_id

    def status(self, job_id, tail=60):
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "no such job: %s" % job_id}
        with job.lock:
            running = job.returncode is None
            lines = list(job.lines)
            elapsed = (job.finished_at or time.time()) - job.started_at
            return {"ok": True, "job_id": job.id, "argv": job.argv,
                    "running": running, "returncode": job.returncode,
                    "elapsed_s": round(elapsed, 1),
                    "output_lines": len(lines), "output_tail": lines[-tail:]}

    def list(self):
        return {"ok": True,
                "jobs": [self.status(j) for j in self._jobs]}

    def cancel(self, job_id):
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "no such job: %s" % job_id}
        with job.lock:
            proc, running = job.proc, job.returncode is None
        if not running:
            return {"ok": True, "already_finished": True,
                    "returncode": job.returncode}
        if proc is not None:
            proc.terminate()
        return {"ok": True, "terminated": True}


JOBS = JobManager()


@mcp.tool()
def vcctrl_preflight(no_input: bool = False) -> dict:
    """ONE gate for "is this rig fit to run": caps, board, power, video,
    scrub ring, the input lock, and (unless no_input) the input round trip.
    Exit codes like vcctrl_verify_input: 0 fit to run, 1 a real fault named
    in decided_by, 2 could not tell, 3 the tool itself failed. Scope is the
    HARNESS, not the target -- it says this rig can drive the machine,
    never that the machine is correctly configured for a measured run."""
    if no_input:
        return _run_vcctrl(["preflight", "--no-input"], timeout=30.0)
    return _gated_run(["preflight"], timeout=30.0)


# CONTROL-MODE-ONLY: harness/vcctrl-cell, -sweep, -collect are control-
# host-side orchestration scripts (see the module docstring) -- there is
# nothing correct for them to do in daemon mode, so they are not registered
# as tools there at all, rather than registered and left to fail on every
# call.
if ROLE == "control":
    @mcp.tool()
    def vcctrl_sweep_list() -> dict:
        """Every sweep name this rig's profile knows, with its cell count and
        timeout -- from profiles/doskutsu.yaml. Safe, fast, no hardware
        touched."""
        proc = subprocess.run(
            [os.path.join(HARNESS_DIR, "vcctrl-sweep"), "--list"],
            capture_output=True, text=True, timeout=30.0)
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode,
                "output": proc.stdout.strip()}

    @mcp.tool()
    def vcctrl_run_cell(tag: str, card: str,
                        set_vars: "list[str] | None" = None,
                        forbid: "list[str] | None" = None,
                        expect_log: "list[str] | None" = None,
                        ticks: "int | None" = None, cfg: "str | None" = None,
                        shot_at: "int | None" = None,
                        hw: "str | None" = None,
                        confirm: "str | None" = None) -> dict:
        """Run ONE doskutsu cell by hand -- the diagnostic single-cell shape,
        not a sweep. Launched detached; poll with vcctrl_job_status(job_id).

        tag: the cell's identifier. card: mach64|virge|cirrus. set_vars: list
        of "VAR=VAL" strings, set BEFORE launch. forbid: variable names that
        must NOT be set, verified positively (a count of 0 read back, not
        absence). expect_log: strings the ENGINE's own log must contain --
        the guard that survives a lever nobody enumerated (see the script's
        own docstring point 4 in the source for why this exists). hw: what is
        PHYSICALLY FITTED right now, if different from the standing
        configuration.

        Types at the target and typically power-cycles it, so this requires
        confirm="run"."""
        if confirm != "run":
            return {"ok": False,
                    "error": ('drives the target -- pass confirm="run" to '
                              'launch')}
        argv = [os.path.join(HARNESS_DIR, "vcctrl-cell"), tag, "--card", card]
        for v in (set_vars or []):
            argv += ["--set", v]
        for v in (forbid or []):
            argv += ["--forbid", v]
        for v in (expect_log or []):
            argv += ["--expect-log", v]
        if ticks is not None:
            argv += ["--ticks", str(ticks)]
        if cfg is not None:
            argv += ["--cfg", cfg]
        if shot_at is not None:
            argv += ["--shot-at", str(shot_at)]
        if hw is not None:
            argv += ["--hw", hw]
        # vcctrl-cell takes the input lock ITSELF, under its own identity
        # (vcctrl-cell-<tag>), not this process's OWNER -- if this session's
        # own gated calls (e.g. a prior vcctrl_preflight) are still holding
        # the MCP lock, vcctrl-cell's own acquire is refused and it presses
        # on anyway (only warning), then hard-crashes on its first `vcctrl
        # key` call. Release proactively rather than relying on the 300s
        # idle timeout, which would otherwise silently break every run_cell
        # called soon after another gated tool. Harmless if nothing was held.
        LOCK.release()
        job_id = JOBS.launch(argv, cwd=REPO_ROOT)
        return {"ok": True, "job_id": job_id, "argv": argv}

    @mcp.tool()
    def vcctrl_run_sweep(sweep: str, machine_digit: str,
                         dry_run: bool = False,
                         no_power_recovery: bool = False,
                         collect: bool = False, power_on: bool = False,
                         confirm: "str | None" = None) -> dict:
        """Run a full unattended QA sweep -- 12 to 23 minutes per
        profiles/doskutsu.yaml, one of the sweep names from
        vcctrl_sweep_list. Launched detached; poll with
        vcctrl_job_status(job_id). Ties up the rig for its full duration, so
        this requires confirm="run".

        power_on: bring the target up if it's off, instead of refusing.
        collect: on completion, hand off to vcctrl-collect automatically
        (reboot into NET, PUT every cell log, confirm each landed)."""
        if confirm != "run":
            return {"ok": False,
                    "error": ("ties up the rig for the sweep's full "
                              'duration -- pass confirm="run" to launch')}
        argv = [os.path.join(HARNESS_DIR, "vcctrl-sweep"), sweep,
                machine_digit]
        if dry_run:
            argv.append("--dry-run")
        if no_power_recovery:
            argv.append("--no-power-recovery")
        if collect:
            argv.append("--collect")
        if power_on:
            argv.append("--power-on")
        LOCK.release()   # see vcctrl_run_cell's comment on the same call
        job_id = JOBS.launch(argv, cwd=REPO_ROOT)
        return {"ok": True, "job_id": job_id, "argv": argv}

    @mcp.tool()
    def vcctrl_collect(sweep: "str | None" = None,
                       machine_digit: "str | None" = None,
                       tags: "list[str] | None" = None,
                       power_on: bool = False, already_net: bool = False,
                       via_put: bool = False, stay_net: bool = False,
                       incoming: "str | None" = None,
                       confirm: "str | None" = None) -> dict:
        """Bring a sweep's logs back off the target, unattended. Pass either
        (sweep, machine_digit) or tags (a list like ["GMN","GMF"]). Reboots
        the target (unless already_net) -- CONFIRMATION COMES FROM THE
        FILESYSTEM, not the screen: arrival of each log in the FTP server's
        incoming/ is what's checked, which also proves everything upstream
        of it (the reboot took, NET loaded, the packet driver answered).
        Launched detached; poll with vcctrl_job_status(job_id). Requires
        confirm="run"."""
        if confirm != "run":
            return {"ok": False,
                    "error": ('reboots the target -- pass confirm="run" to '
                              'launch')}
        if tags:
            argv = [os.path.join(HARNESS_DIR, "vcctrl-collect"), "--tags",
                    ",".join(tags)]
        elif sweep and machine_digit:
            argv = [os.path.join(HARNESS_DIR, "vcctrl-collect"), sweep,
                    machine_digit]
        else:
            return {"ok": False,
                    "error": "pass either tags, or both sweep and machine_digit"}
        if power_on:
            argv.append("--power-on")
        if already_net:
            argv.append("--already-net")
        if via_put:
            argv.append("--via-put")
        if stay_net:
            argv.append("--stay-net")
        if incoming is not None:
            argv += ["--incoming", incoming]
        LOCK.release()   # see vcctrl_run_cell's comment on the same call
        job_id = JOBS.launch(argv, cwd=REPO_ROOT)
        return {"ok": True, "job_id": job_id, "argv": argv}


@mcp.tool()
def vcctrl_job_status(job_id: str, tail: int = 60) -> dict:
    """Poll a job launched by vcctrl_run_cell / vcctrl_run_sweep /
    vcctrl_collect: running state, exit code once finished, and the last
    `tail` lines of its combined stdout/stderr."""
    return JOBS.status(job_id, tail=tail)


@mcp.tool()
def vcctrl_job_list() -> dict:
    """Every job this MCP server has launched this session, with status."""
    return JOBS.list()


@mcp.tool()
def vcctrl_job_cancel(job_id: str, confirm: "str | None" = None) -> dict:
    """Terminate a running job (SIGTERM). LAST RESORT, NOT GRACEFUL: unlike
    vcctrl_file_cancel (which waits for a clean file boundary), this can
    land mid-keystroke or mid-reboot on the target, because the harness
    scripts have no external stop signal designed in -- see
    docs/PLAN.md/vcctrl-sweep's own docstring on why input mid-sweep is
    dangerous. Requires confirm="terminate"."""
    if confirm != "terminate":
        return {"ok": False,
                "error": ('not graceful -- can land mid-keystroke or '
                          'mid-reboot on the target; pass confirm='
                          '"terminate" to proceed anyway')}
    return JOBS.cancel(job_id)


if __name__ == "__main__":
    transport = os.environ.get(
        "VCCTRL_MCP_TRANSPORT",
        "stdio" if ROLE == "control" else "streamable-http")
    if transport == "stdio":
        mcp.run()
    else:
        # host defaults to loopback -- same security posture as
        # daemon/vcweb.py's own web UI (`daemon.web.bind: 127.0.0.1` in
        # vcctrl.yaml): a reverse proxy (tailscale serve) is the
        # authentication boundary, not this process. Do not change this
        # default to 0.0.0.0 without adding one.
        #
        # DNS-REBINDING PROTECTION, EXPLICITLY ENABLED. The mcp package's own
        # default (transport_security=None) is Host/Origin validation OFF --
        # its own source: "disable DNS rebinding protection by default for
        # backwards compatibility." That default is wrong for a service with
        # a real network listener, so this always builds explicit settings
        # rather than relying on it.
        #
        # Reached directly at 127.0.0.1:PORT (local testing on the daemon
        # host itself) the loopback host:port below covers it. Reached through
        # `tailscale serve`'s proxy, the Host header the proxy forwards is
        # the tailnet hostname, not 127.0.0.1:PORT, and the request is
        # refused (measured: HTTP 421 "Invalid Host header") until that
        # hostname is added via VCCTRL_MCP_ALLOWED_HOSTS (comma-separated) --
        # deliberately not auto-trusted from something this process could
        # infer about itself, since that would mean any Host header CLAIMING
        # to be this rig's hostname gets waved through by a value a caller
        # also controls.
        from mcp.server.transport_security import TransportSecuritySettings
        mcp_host = os.environ.get("VCCTRL_MCP_HOST", "127.0.0.1")
        mcp_port = int(os.environ.get("VCCTRL_MCP_PORT", "8090"))
        allowed = ["%s:%d" % (mcp_host, mcp_port), "localhost:%d" % mcp_port]
        allowed += [h.strip() for h in
                   os.environ.get("VCCTRL_MCP_ALLOWED_HOSTS", "").split(",")
                   if h.strip()]
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed, allowed_origins=allowed)
        mcp.run(transport=transport, host=mcp_host, port=mcp_port,
                transport_security=security)
