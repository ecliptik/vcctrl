"""Tests for agent/vcctrl_mcp.py -- the MCP tool layer itself.

BEFORE THIS FILE, ZERO TESTS ANYWHERE EXERCISED agent/vcctrl_mcp.py.
Confirmed by grep before writing a line here: every test in test_core.py
touches daemon/vcctrld.py or daemon/kvm.html, never the MCP layer between
an agent and the CLI. That gap is exactly why the 2026-08-26 bug (three of
six lock-releasing tools missing the LOCK.release() call b8bdcc2 already
established as the fix for the other three) shipped and was found live on
real hardware instead of here.

THE STUBBED `mcp` PACKAGE, AND WHY. agent/vcctrl_mcp.py needs `mcp>=2.0` to
import (agent/requirements.txt), installed only in agent/.venv -- which
has no pytest, and the environment that runs this suite has no `mcp`. Two
real environments, never both at once, confirmed live rather than assumed.
Rather than require a second test invocation under a second interpreter
(a maintenance burden nobody would remember to run), this stubs just enough
of `mcp.server.mcpserver.MCPServer` -- a constructor that stores its
arguments and a `.tool()` that returns its function unchanged -- for the
real module to import and every `@mcp.tool()`-decorated function to stay
directly callable. This is a substitute for the transport layer, never for
daemon logic: nothing about `_run_vcctrl`, `LOCK`, or `JOBS` is faked here,
only imported and then monkeypatched per test the same way FakeTarget
stands in for RegistryDriver elsewhere in this suite.
"""
import importlib.util
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_PATH = os.path.join(HERE, os.pardir, "agent", "vcctrl_mcp.py")


def _install_mcp_stub():
    """Put a minimal fake `mcp.server.mcpserver` in sys.modules, if the
    real package is not already importable. Returns True if a stub was
    installed (so the caller knows to leave it alone afterward), False if
    the real `mcp` was already there (e.g. this suite somehow runs under
    agent/.venv one day) and nothing needed faking.
    """
    try:
        import mcp.server.mcpserver  # noqa: F401
        return False
    except ImportError:
        pass

    class _FakeMCPServer(object):
        def __init__(self, name, instructions=None):
            self.name = name
            self.instructions = instructions

        def tool(self):
            def decorator(fn):
                return fn
            return decorator

        def run(self, *a, **kw):
            raise RuntimeError("stub MCPServer cannot actually run")

    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    mcpserver_mod = types.ModuleType("mcp.server.mcpserver")
    mcpserver_mod.MCPServer = _FakeMCPServer
    server_mod.mcpserver = mcpserver_mod
    mcp_mod.server = server_mod
    sys.modules["mcp"] = mcp_mod
    sys.modules["mcp.server"] = server_mod
    sys.modules["mcp.server.mcpserver"] = mcpserver_mod
    return True


def _load_agent(role="control"):
    """A fresh import of agent/vcctrl_mcp.py under VCCTRL_MCP_ROLE=role.

    Fresh EVERY TIME, not cached -- module-level state (LOCK's background
    thread, JOBS's counter) must not leak between tests, the same reason
    _mkfiles()/_mkpull() in test_core.py build a new capability per test
    rather than sharing one.
    """
    stubbed = _install_mcp_stub()
    old_role = os.environ.get("VCCTRL_MCP_ROLE")
    os.environ["VCCTRL_MCP_ROLE"] = role
    try:
        spec = importlib.util.spec_from_file_location(
            "vcctrl_mcp_under_test", AGENT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if old_role is None:
            os.environ.pop("VCCTRL_MCP_ROLE", None)
        else:
            os.environ["VCCTRL_MCP_ROLE"] = old_role
        del stubbed  # nothing to undo -- sys.modules stub is harmless to leave


class _RecordingLock(object):
    """Stands in for LOCK, recording exactly one thing: was release()
    called, and where in the call order relative to the real work."""

    def __init__(self, calls):
        self.calls = calls

    def release(self):
        self.calls.append("LOCK.release")

    def ensure(self):
        self.calls.append("LOCK.ensure")
        return None


def _recording_run_vcctrl(calls, result=None):
    def fake(args, timeout=60.0):
        calls.append(("_run_vcctrl", list(args)))
        return result if result is not None else {"ok": True}
    return fake


class _RecordingJobs(object):
    """Stands in for JOBS -- records launch() calls without ever spawning
    a real subprocess. Real JobManager.launch() calls subprocess.Popen()
    directly; letting that run for real in a test would try to exec
    harness/vcctrl-cell (or -sweep, -collect) against nothing."""

    def __init__(self, calls):
        self.calls = calls
        self._n = 0

    def launch(self, argv, cwd=None):
        self._n += 1
        self.calls.append(("JOBS.launch", list(argv)))
        return "job-%d" % self._n


FAILURES = []


def check(name, cond, detail=""):
    """Own FAILURES list, not a delegate to test_core.check().

    tests/conftest.py's pytest hook looks at `item.module.FAILURES` -- THIS
    module's attribute, never test_core's -- to turn a recorded failure into
    a real pytest failure (see that file's own docstring for why: a test
    that only appends to a list otherwise returns normally and pytest
    counts it as passed). Delegating to test_core.check() appends to the
    WRONG list and every check in this file silently stops meaning anything
    -- measured live while writing this file: with the regression
    reintroduced by hand, this test suite still reported "5 passed" until
    this was fixed to keep its own list.
    """
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s %s" % (name, detail))
        FAILURES.append(name)


def test_the_three_file_transfer_tools_release_the_lock_before_launching():
    """THE REGRESSION TEST FOR THE 2026-08-26 BUG. send_file/get_file/
    file_refresh must call LOCK.release() BEFORE _run_vcctrl() -- the bug
    was that they called _run_vcctrl() first (or not at all), so the
    daemon-side transfer job's own Ctrl-Alt-Del, sent under the "transfer"
    arbiter identity, was silently refused by whatever this session's own
    lock was still holding.
    """
    print("\nfile-transfer tools release the lock before the CLI call")
    mod = _load_agent(role="control")
    calls = []
    mod.LOCK = _RecordingLock(calls)
    mod._run_vcctrl = _recording_run_vcctrl(calls)

    mod.vcctrl_send_file(mode="return", confirm="send")
    check("send_file: lock released before the CLI call",
          calls[0] == "LOCK.release", calls)
    check("send_file: exactly one release, one CLI call", len(calls) == 2,
          calls)
    calls.clear()

    mod.vcctrl_file_refresh(mode="return", confirm="refresh")
    check("file_refresh: lock released before the CLI call",
          calls[0] == "LOCK.release", calls)
    calls.clear()

    mod.vcctrl_get_file(fetch_all=True, mode="return", confirm="get")
    check("get_file: lock released before the CLI call",
          calls[0] == "LOCK.release", calls)


def test_the_three_harness_launchers_release_the_lock_before_launching():
    """The b8bdcc2-era trio -- confirmed still correct, not just assumed
    still correct because nobody touched this file. Same shape of test as
    the file-transfer trio above, against JOBS.launch() instead of
    _run_vcctrl().
    """
    print("\nharness-workflow launchers release the lock before JOBS.launch")
    mod = _load_agent(role="control")
    calls = []
    mod.LOCK = _RecordingLock(calls)
    mod.JOBS = _RecordingJobs(calls)

    mod.vcctrl_run_cell(tag="TEST1", card="mach64", confirm="run")
    check("run_cell: lock released before JOBS.launch",
          calls[0] == "LOCK.release" and calls[1][0] == "JOBS.launch", calls)
    calls.clear()

    mod.vcctrl_run_sweep(sweep="MINE", machine_digit="1", confirm="run")
    check("run_sweep: lock released before JOBS.launch",
          calls[0] == "LOCK.release" and calls[1][0] == "JOBS.launch", calls)
    calls.clear()

    mod.vcctrl_collect(tags=["TEST1"], confirm="run")
    check("collect: lock released before JOBS.launch",
          calls[0] == "LOCK.release" and calls[1][0] == "JOBS.launch", calls)


def test_confirm_gate_refuses_before_touching_the_lock_at_all():
    """CONTROL: a call that never confirms must not release anything or
    launch anything -- otherwise the two tests above would pass on a
    tool that releases the lock unconditionally regardless of whether it
    is actually about to do the consequential thing, which is not what
    was fixed and not what should be asserted.
    """
    print("\nan unconfirmed call touches neither the lock nor the launcher")
    mod = _load_agent(role="control")
    calls = []
    mod.LOCK = _RecordingLock(calls)
    mod._run_vcctrl = _recording_run_vcctrl(calls)
    mod.JOBS = _RecordingJobs(calls)

    r = mod.vcctrl_send_file(mode="return")  # no confirm
    check("send_file without confirm is refused", r["ok"] is False, r)
    check("and nothing was touched -- no release, no CLI call",
          calls == [], calls)

    r2 = mod.vcctrl_run_cell(tag="TEST1", card="mach64")  # no confirm
    check("run_cell without confirm is refused", r2["ok"] is False, r2)
    check("and nothing was touched -- no release, no launch",
          calls == [], calls)


def test_a_read_only_tool_does_not_touch_the_lock_at_all():
    """CONTROL, THE OTHER DIRECTION: a tool with no reason to release
    anything should not -- otherwise the assertions above (checking calls[0]
    == "LOCK.release") would be trivially satisfiable by a version of this
    file that called LOCK.release() from EVERY tool regardless of need,
    which would pass every test above for the wrong reason.
    """
    print("\na read-only tool never calls LOCK.release")
    mod = _load_agent(role="control")
    calls = []
    mod.LOCK = _RecordingLock(calls)
    mod._run_vcctrl = _recording_run_vcctrl(calls, result={"ok": True})

    mod.vcctrl_files()
    check("a plain read-only call does not touch the lock",
          "LOCK.release" not in calls, calls)


def test_burst_default_n_is_3_not_5():
    """Lowered 2026-08-30 to cut per-call size for the routine post-type
    confirm case (vcctrl-mcp-workflows). A regression test, not just a
    docstring claim -- pins the actual default against silent drift.
    """
    import inspect

    print("\nvcctrl_burst defaults n to 3")
    mod = _load_agent(role="control")
    sig = inspect.signature(mod.vcctrl_burst)
    check("default n is 3", sig.parameters["n"].default == 3,
          sig.parameters["n"].default)
    check("context still defaults to empty string",
          sig.parameters["context"].default == "",
          sig.parameters["context"].default)


def test_burst_logs_call_metadata_for_guardrail_correlation():
    """vcctrl_burst appends one JSON line per call to internal/burst-
    calls.jsonl -- added 2026-08-30 so a later session can correlate call
    volume/frequency/context against an Anthropic-side safety-classifier
    interruption instead of guessing at the trigger pattern after the
    fact. Redirects mod.BURST_LOG rather than touching the real
    internal/ directory, the same isolation _RecordingLock/_RecordingJobs
    give the other tests here.
    """
    import json

    print("\nvcctrl_burst logs n/context/result to BURST_LOG")
    mod = _load_agent(role="control")
    log_dir = tempfile.mkdtemp(prefix="burst-log-test-")
    mod.BURST_LOG = os.path.join(log_dir, "burst-calls.jsonl")
    mod._run_vcctrl = _recording_run_vcctrl(
        [], result={"ok": True, "exit_code": 0})

    mod.vcctrl_burst(n=3, context="confirm MD command landed")

    check("log file was created", os.path.isfile(mod.BURST_LOG),
          mod.BURST_LOG)
    with open(mod.BURST_LOG) as f:
        lines = f.readlines()
    check("exactly one line for one call", len(lines) == 1, lines)
    entry = json.loads(lines[0])
    check("n recorded", entry.get("n") == 3, entry)
    check("context recorded", entry.get("context") == "confirm MD command landed",
          entry)
    check("ok recorded", entry.get("ok") is True, entry)
    check("exit_code recorded", entry.get("exit_code") == 0, entry)
    check("out_dir recorded", "out_dir" in entry, entry)
    check("timestamp recorded", isinstance(entry.get("ts"), float), entry)

    mod.vcctrl_burst(n=2)
    with open(mod.BURST_LOG) as f:
        lines = f.readlines()
    check("a second call appends rather than overwrites", len(lines) == 2,
          lines)
    check("context defaults to empty string, not missing/None",
          json.loads(lines[1]).get("context") == "", lines[1])


def test_burst_logging_failure_does_not_break_the_call():
    """A BURST_LOG that can't be written (e.g. a read-only deployment)
    must not turn a working capture into a failed tool call -- the log
    is diagnostic, not load-bearing for the capture it describes.
    """
    print("\na BURST_LOG write failure is swallowed, not raised")
    mod = _load_agent(role="control")
    # A path whose parent cannot exist as a directory (it's a file):
    # os.makedirs on top of it raises OSError/FileExistsError, which
    # _log_burst_call must catch.
    blocker = tempfile.mktemp(prefix="burst-log-blocker-")
    with open(blocker, "w") as f:
        f.write("not a directory")
    mod.BURST_LOG = os.path.join(blocker, "sub", "burst-calls.jsonl")
    mod._run_vcctrl = _recording_run_vcctrl(
        [], result={"ok": True, "exit_code": 0})

    res = mod.vcctrl_burst(n=1)
    check("the call still returns the real result despite the log failure",
          res.get("ok") is True, res)
    os.remove(blocker)


def _fake_vcctrl_for_lock(calls, owner, file_running=False):
    """A _run_vcctrl fake that understands enough of the lock/file-status
    vocabulary for the tests below: acquire/release succeed as this owner,
    file-status reports `file_running`, everything else is a generic ok."""
    def fake(args, timeout=60.0):
        calls.append(list(args))
        if args[:2] == ["lock", "acquire"]:
            return {"ok": True, "owner": owner}
        if args[:2] == ["lock", "release"]:
            return {"ok": True, "owner": None}
        if args[:1] == ["file-status"]:
            return {"ok": True, "job": {"running": file_running}}
        return {"ok": True}
    return fake


def test_gated_run_releases_the_lock_after_one_action_by_default():
    """THE FIX FOR THE 2026-08-31 BUG: a single vcctrl_key (or any other
    _gated_run-routed tool) must not leave the lock held afterward -- that
    is exactly what silently blocked an in-flight vcctrl_get_file job's own
    later return-reboot from acquiring the same lock. Default footprint is
    one action, not a standing hold.
    """
    print("\na single gated call releases the lock it took, by default")
    mod = _load_agent(role="control")
    calls = []
    mod._run_vcctrl = _fake_vcctrl_for_lock(calls, mod.OWNER)

    mod.vcctrl_key(keys=["enter"])
    check("the lock was acquired for this call",
          any(c[:2] == ["lock", "acquire"] for c in calls), calls)
    check("and released again right after, as the LAST call made",
          calls[-1][:2] == ["lock", "release"], calls)


def test_sticky_lock_survives_across_gated_calls_until_released():
    """vcctrl_lock_acquire's whole point: hold the lock across a sequence.
    Confirms the opposite of the test above under an explicit sticky
    acquire, and that vcctrl_lock_release both releases and clears sticky.
    """
    print("\nvcctrl_lock_acquire keeps the lock held across later gated calls")
    mod = _load_agent(role="control")
    calls = []
    mod._run_vcctrl = _fake_vcctrl_for_lock(calls, mod.OWNER)

    r = mod.vcctrl_lock_acquire()
    check("lock_acquire succeeds when nothing else is running",
          r.get("ok") is True, r)
    check("lock_acquire reports the hold as sticky", r.get("sticky") is True, r)
    calls.clear()

    mod.vcctrl_key(keys=["enter"])
    check("no re-acquire needed -- already held",
          not any(c[:2] == ["lock", "acquire"] for c in calls), calls)
    check("and no release either -- sticky",
          not any(c[:2] == ["lock", "release"] for c in calls), calls)
    calls.clear()

    mod.vcctrl_type(text="hello")
    check("still no release/re-acquire across a second gated call",
          not any(c[:2] in (["lock", "acquire"], ["lock", "release"])
                  for c in calls), calls)

    mod.vcctrl_lock_release()
    check("explicit release now sends the CLI release call",
          calls and calls[-1][:2] == ["lock", "release"], calls)

    calls.clear()
    mod.vcctrl_key(keys=["enter"])
    check("sticky is cleared -- the next gated call acquires and releases "
          "again on its own",
          any(c[:2] == ["lock", "acquire"] for c in calls)
          and calls[-1][:2] == ["lock", "release"], calls)


def test_vcctrl_power_releases_the_lock_it_took():
    """2026-09-19 (reported by the sdldos peer on a dosags 40/40 cell): a
    worker's vcctrl_power left the hardware lock held for the full 300 s
    idle window with `inflight: []` the whole time, and the recovery guard
    that needed to power-cycle was refused by it -- surfacing as a send-file
    verification failure, not a lock problem. vcctrl_power called
    LOCK.ensure() itself but bypassed _gated_run, so the `finally` that
    releases the lock unless it is sticky never ran for it. Same class as
    the 2026-08-31 bug _gated_run's docstring describes; that fix covered
    every tool routed through _gated_run and missed the one that took the
    lock by hand.
    """
    print("\nvcctrl_power releases the lock it took, like every gated tool")
    mod = _load_agent(role="control")
    calls = []
    mod._run_vcctrl = _fake_vcctrl_for_lock(calls, mod.OWNER)

    for action in ("on", "off", "cycle"):
        calls.clear()
        r = mod.vcctrl_power(action, confirm=action)
        check("power %s ran" % action, r.get("ok") is True, r)
        power_calls = [c for c in calls if c[:1] == ["power"]]
        check("power %s carried --as OWNER (the daemon compares it)" % action,
              len(power_calls) == 1 and power_calls[0][-2:] == ["--as", mod.OWNER],
              calls)
        check("power %s took the lock BEFORE acting" % action,
              calls[0][:2] == ["lock", "acquire"], calls)
        check("power %s released it AFTER, as the last call made" % action,
              calls[-1][:2] == ["lock", "release"], calls)


def test_vcctrl_power_does_not_release_a_sticky_hold():
    """CONTROL for the test above: a caller who deliberately holds the lock
    with vcctrl_lock_acquire keeps it across a power action, exactly as it
    does across vcctrl_key. Without this the test above would pass on a
    vcctrl_power that released unconditionally and broke sticky holds."""
    print("\nvcctrl_power leaves a deliberate sticky hold alone")
    mod = _load_agent(role="control")
    calls = []
    mod._run_vcctrl = _fake_vcctrl_for_lock(calls, mod.OWNER)

    mod.vcctrl_lock_acquire()
    calls.clear()
    r = mod.vcctrl_power("on", confirm="on")
    check("power on ran under the sticky hold", r.get("ok") is True, r)
    check("and neither re-acquired nor released",
          not any(c[:2] in (["lock", "acquire"], ["lock", "release"])
                  for c in calls), calls)


def test_vcctrl_power_refused_by_a_held_lock_never_reaches_the_plug():
    """CONTROL: when someone else holds the lock, the daemon's own refusal is
    returned as-is and the plug is not touched -- routing power through
    _gated_run must not have turned a refusal into an attempt."""
    print("\na refused vcctrl_power returns the refusal and sends no power call")
    mod = _load_agent(role="control")
    calls = []

    def fake(args, timeout=60.0):
        calls.append(list(args))
        if args[:2] == ["lock", "acquire"]:
            return {"ok": False, "error": "input locked by 'operator' since 1",
                    "locked_by": "operator"}
        return {"ok": True}
    mod._run_vcctrl = fake

    r = mod.vcctrl_power("cycle", confirm="cycle")
    check("refusal is the daemon's own", r.get("locked_by") == "operator", r)
    check("no power command was sent",
          not any(c[:1] == ["power"] for c in calls), calls)
    check("and nothing was released that we never held",
          not any(c[:2] == ["lock", "release"] for c in calls), calls)


def test_vcctrl_power_stays_on_the_primary_whatever_the_session_default():
    """vcctrl_power has no `profile` parameter and always spoke to the
    primary instance. Routing it through _gated_run must not let a
    vcctrl_profile_set session default redirect a MAINS action to a different
    target's daemon."""
    print("\nvcctrl_power ignores the session's default profile")
    mod = _load_agent(role="control")
    calls = []
    mod._run_vcctrl = _fake_vcctrl_for_lock(calls, mod.OWNER)
    mod._CURRENT_PROFILE = "someotherprofile"

    mod.vcctrl_power("on", confirm="on")
    check("no --profile flag on any call made",
          not any("--profile" in c for c in calls), calls)


def test_lock_acquire_refuses_while_a_harness_job_is_running():
    """A sticky hold taken while THIS SESSION's own run_cell/run_sweep/
    collect job is still in flight is the dangerous pattern: that job's own
    later typed step needs the same lock and would be silently refused,
    not forced. Refuse the acquire itself, before ever touching the lock.
    """
    print("\nvcctrl_lock_acquire refuses while a harness job is still running")
    mod = _load_agent(role="control")
    calls = []
    mod._run_vcctrl = _fake_vcctrl_for_lock(calls, mod.OWNER)
    mod.JOBS._jobs["job-1"] = mod.Job("job-1", ["fake", "argv"])  # returncode
    # stays None (the real Job default) -- exactly "still running".

    r = mod.vcctrl_lock_acquire()
    check("the acquire is refused", r.get("ok") is False, r)
    check("the lock itself was never touched -- refused before LOCK.ensure()",
          not any(c[:2] == ["lock", "acquire"] for c in calls), calls)


def test_lock_acquire_refuses_while_a_file_transfer_job_is_running():
    """Same guard, the other job type: file-status reporting a running
    send/get/refresh/scan job also refuses a sticky acquire attempt."""
    print("\nvcctrl_lock_acquire refuses while a file-transfer job is running")
    mod = _load_agent(role="control")
    calls = []
    mod._run_vcctrl = _fake_vcctrl_for_lock(calls, mod.OWNER, file_running=True)

    r = mod.vcctrl_lock_acquire()
    check("the acquire is refused", r.get("ok") is False, r)
    check("the lock itself was never touched -- refused before LOCK.ensure()",
          not any(c[:2] == ["lock", "acquire"] for c in calls), calls)


def test_lock_acquire_succeeds_when_nothing_is_running():
    """CONTROL for the two refusal tests above: an idle rig (no harness job,
    file-status not running) must still let vcctrl_lock_acquire succeed --
    otherwise those tests would pass for the wrong reason (an acquire that
    always refuses)."""
    print("\nvcctrl_lock_acquire succeeds on an idle rig")
    mod = _load_agent(role="control")
    calls = []
    mod._run_vcctrl = _fake_vcctrl_for_lock(calls, mod.OWNER, file_running=False)

    r = mod.vcctrl_lock_acquire()
    check("the acquire succeeds", r.get("ok") is True, r)
    check("the CLI acquire call did happen this time",
          any(c[:2] == ["lock", "acquire"] for c in calls), calls)


def test_harness_workflow_tools_are_absent_in_daemon_mode():
    """docs/MCP-SERVER.md's own claim (59 control tools, 55 daemon) is a
    count in prose. This is the same claim checked against the live
    module object, the way FINDINGS.md insists on measuring rather than
    asserting -- daemon mode must not even DEFINE run_cell/run_sweep/
    collect/sweep_list, not merely refuse to run them.
    """
    print("\nharness workflows do not exist at all under daemon role")
    mod = _load_agent(role="daemon")
    for name in ("vcctrl_run_cell", "vcctrl_run_sweep", "vcctrl_collect",
                 "vcctrl_sweep_list"):
        check("daemon-mode module has no %s" % name,
              not hasattr(mod, name), name)


if __name__ == "__main__":
    test_the_three_file_transfer_tools_release_the_lock_before_launching()
    test_the_three_harness_launchers_release_the_lock_before_launching()
    test_confirm_gate_refuses_before_touching_the_lock_at_all()
    test_a_read_only_tool_does_not_touch_the_lock_at_all()
    test_burst_default_n_is_3_not_5()
    test_burst_logs_call_metadata_for_guardrail_correlation()
    test_burst_logging_failure_does_not_break_the_call()
    test_gated_run_releases_the_lock_after_one_action_by_default()
    test_sticky_lock_survives_across_gated_calls_until_released()
    test_vcctrl_power_releases_the_lock_it_took()
    test_vcctrl_power_does_not_release_a_sticky_hold()
    test_vcctrl_power_refused_by_a_held_lock_never_reaches_the_plug()
    test_vcctrl_power_stays_on_the_primary_whatever_the_session_default()
    test_lock_acquire_refuses_while_a_harness_job_is_running()
    test_lock_acquire_refuses_while_a_file_transfer_job_is_running()
    test_lock_acquire_succeeds_when_nothing_is_running()
    test_harness_workflow_tools_are_absent_in_daemon_mode()
    print("\n%s" % ("ALL PASS" if not FAILURES
                    else "FAILED: %s" % ", ".join(FAILURES)))
