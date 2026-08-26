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
    test_harness_workflow_tools_are_absent_in_daemon_mode()
    print("\n%s" % ("ALL PASS" if not FAILURES
                    else "FAILED: %s" % ", ".join(FAILURES)))
