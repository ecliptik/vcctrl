---
name: vcctrl-mcp-workflows
description: Safe sequencing for vcctrl's MCP tools (vcctrl_key, vcctrl_type, vcctrl_power, vcctrl_run_cell, vcctrl_send_file, and similar) -- locking, confirmation, and verification discipline for a tool that drives real hardware. Use before calling any vcctrl_ MCP tool that sends input, changes power state, reboots the target, or runs a harness workflow.
---

# vcctrl MCP tool discipline

Full reference: `docs/MCP-SERVER.md`. This is the operational summary for an
agent about to call one of these tools.

**Input tools take a shared hardware lock and refuse rather than force.** Any
tool that sends keyboard/mouse input acquires the daemon's Arbiter lock under
identity `mcp:<host>:<pid>`. If a human or another session already holds it,
the call **refuses outright** -- there is no override. Do not retry the same
call expecting a different result; check `vcctrl_lock_status`, and either
wait or ask, the same as you would for `vcctrl_lock_acquire` returning held.
Release with `vcctrl_lock_release` when you're done with a sequence of input
calls rather than leaving it to the 300s idle timeout, if another session may
be waiting.

**The 300s idle timeout lives in the MCP client process, not the daemon --
a crashed client leaves the lock stuck forever.** Measured 2026-08-26: a
lock sat held for 2.3+ hours, well past 300s, with `vcctrld`'s own log
showing its owner (`mcp:<host>:<pid>`) went silent right after a normal
power-off and never called anything again -- a killed/crashed MCP session,
not a live one. The daemon's own `Arbiter` class (`daemon/vcctrld.py`) has
no idle logic at all; the 300s watch is `LockManager._idle_watch` inside
*this session's own* `agent/vcctrl_mcp.py` process, polling every 15s and
releasing only if that same process is still alive to run it (`atexit` also
only fires on a clean exit). Kill that process any other way and nothing
ever releases the lock -- the daemon holds it exactly as designed, forever,
because from its side nothing is wrong. Before assuming a long-held lock is
someone's live work: check `vcctrl_activity`/ask peers, then look at
whether the owner's identity matches any session you can account for.
**The lightweight fix is the CLI's `vcctrl lock break --as <name>`** (in
`bin/vcctrl-client`) -- it force-clears the lock and marks the run tainted
for auditability, purpose-built for exactly this. **It is not exposed as an
MCP tool** (`vcctrl_lock_status`/`_acquire`/`_release` are, `break` is not),
so an MCP-only session has no lightweight recovery today and has to escalate
to an operator with CLI/SSH access -- restarting `vcctrld` also clears it
(confirmed working) but is heavier than necessary and untracked/untainted.

**`vcctrl_send_file`/`_get_file`/`_file_refresh` and `_run_cell`/`_run_sweep`/
`_collect` release THIS session's own lock automatically before they start,
and you do not need to do it yourself first.** Found the hard way,
2026-08-26: the real work for all six happens under a DIFFERENT identity than
this session's (`transfer` for the file-transfer trio, `vcctrl-cell-<tag>` or
similar for the harness trio) -- so if this session's own lock from an
earlier gated call (a `vcctrl_preflight`, a `vcctrl_combo`) was still held
when one of these six started, the daemon's own internal Ctrl-Alt-Del was
silently refused by THIS session's lock, not sent to the target at all. That
produced a misleading `no-reset` timeout close to two minutes later --
"the machine never reset" about a machine that was never asked to. All six
now call the daemon-side equivalent of `vcctrl_lock_release` first, so a lock
you forgot to release will not break them. It does mean the lock reads
unheld immediately after calling one of these, even if you held it the
moment before -- expected, not a sign something else is wrong.

**Consequential actions require a named `confirm`, not a boolean.** Power
actions, any `vcctrl_combo` matching the Ctrl-Alt-Delete chord, and anything
that reboots the target (file send/refresh/get, `run_cell`, `run_sweep`,
`collect`) require `confirm` set to a string that names the action itself
(`confirm="cycle"`, `confirm="reboot"`, `confirm="run"`). This exists so an
accidental `true` can't trigger it -- passing the confirm string is a
deliberate act, not a flag to default on. Supplying it when the task actually
calls for the action is correct and expected; don't treat the requirement as
a reason to avoid the action, only as a reason to mean it.

**A keystroke or click landing is not the same as the tool call succeeding.**
Because of the input hazards in `vcctrl-rig-hazards` (Caps Lock inversion,
OCR unreliable on digits, DOS's caret/REM quirks if you're driving the target
via typed batch commands, and rapid-fire `vcctrl_type`/`vcctrl_key` calls
dropping or merging characters -- see `vcctrl-rig-hazards`), a call that
returns without error is not proof the target received what you intended.
Use `vcctrl_verify_input` or a follow-up `vcctrl_shot`/`vcctrl_frame` to
confirm, especially before a step that depends on prior input having landed
(e.g. before a `confirm`-gated action). **Prefer `vcctrl_burst` over
`vcctrl_shot` for this right after typing.** Measured 2026-08-26: `shot()`
returned a "judged" frame 12.85s stale -- the previous screen content --
immediately after two commands that had, per a `burst()` taken seconds
later, both landed and rendered correctly. `shot()`'s picture-judgement
pipeline can lag a fast-changing screen; a raw `burst()` is the fresher
read when you need "what does the screen show right now," not merely "is
there a picture at all."

**Board-scoped power is visibility, not enforcement** (see
`vcctrl-rig-hazards`) -- check `vcctrl_board` before a `vcctrl_power` call if
which physical machine you're powering matters to the task, since the tool
itself cannot verify that for you.

**Harness workflows (`vcctrl_preflight`, `_run_cell`, `_run_sweep`,
`_collect`, `_job_status`) exist only in control mode**, launched as
background jobs -- poll `vcctrl_job_status` rather than assuming a long-running
call has finished when the tool call itself returns. Daemon mode omits these
entirely; if you're connected to `vcctrl-mcp-daemon` and need a harness
workflow, that's the wrong server, not a missing feature to route around.

**Nothing here is a side channel.** Every call shows up in `vcctrl_activity`
/ the power audit log as `mcp:...`, visible to a human watching the rig the
same way any other caller is. Don't treat MCP access as unobserved.
