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

**As of 2026-08-31, a single gated call (`vcctrl_key`, `_type`, `_combo`,
`_verify_input`, `_mouse_move`/`_click`, `_hold`, `_keydown`/`_keyup`,
`_release_all`) releases the lock again right after that one action, by
default.** You do not need to call `vcctrl_lock_release` after an isolated
call. Only `vcctrl_lock_acquire` creates a *sticky* hold that survives across
later gated calls until you explicitly `vcctrl_lock_release` (or the 300s
idle timer) -- call it when you deliberately want a visible, continuous hold
across a whole sequence, not as a default habit. `vcctrl_lock_acquire` itself
refuses if a file-transfer job (`send_file`/`get_file`/`file_refresh`/
`file_scan`) or a harness job (`run_cell`/`run_sweep`/`collect`) is currently
running -- see the next point for why.

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

**That fix only covers a lock held BEFORE one of the six starts -- a lock
taken WHILE it is already running is the other half, found live 2026-08-31.**
A `vcctrl_get_file` job was mid-flight (several files in, one file's FTP
transfer running unusually long); a `vcctrl_verify_input` call to check the
target was still alive, followed by a `vcctrl_key(["enter"])` to test whether
a command line had failed to submit, both acquired the lock and -- before
this date -- left it held afterward (the 300s idle timer was the only thing
that would ever have freed it, and an explicit `vcctrl_lock_acquire` for "hold
the lock for this whole session" made it immediate rather than 300s-delayed).
When the `get_file` job's own watchdog then detected the stalled file and
tried to abort and reboot back to the menu default, its Ctrl-Alt-Del under
the `transfer` identity was silently refused by this session's still-held
lock -- refused, not forced, so it produced no error, just a job that sat at
`"returning to the menu default"` with `running: true` and never got there.
The target was left sitting in the NET profile (`left_in_net: true`, "no
measured run may start from there") until a human watching the KVM noticed it
looked stuck. Fixed by the auto-release-by-default behavior above (so an
isolated diagnostic call like `verify_input` no longer has a lingering
footprint at all) plus a job-conflict check inside `vcctrl_lock_acquire`
itself (so the one action that DOES create a lasting hold refuses outright if
a file/harness job is already in flight, rather than setting up the same
collision for later). If you hit a similarly "stuck at a bare prompt, nothing
progressing" symptom on a `vcctrl_file_status`/`vcctrl_job_status` job:
check `vcctrl_lock_status` for who holds it before assuming a target-side
hang -- `verify_input` succeeding (PS/2 link alive) while a job sits
`running: true` with no new log line is the same shape as this incident, not
proof the target itself is wedged.

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

**`vcctrl_burst`'s default `n` was lowered from 5 to 3 on 2026-08-30, and
it now logs every call (`n`, `context`, result) to `internal/burst-
calls.jsonl`** -- diagnostic instrumentation for an Anthropic-side safety
classifier that has intermittently interrupted turns during burst-heavy
porting sessions, trigger pattern not yet established. Pass a short
`context=` on each call (what the burst is for, e.g. "confirm MD command
landed") so a later session can correlate. Keep `n` at the default for a
routine post-type confirm; pass a larger `n` explicitly only when you
actually need more frames (motion/animation diagnostics), not as a habit.
`vcctrl_verify_input`'s LED round trip is cheaper than a burst and worth
trying first when the question is "is the target even responsive at all"
-- but it proves the link is alive, not that any particular text
rendered, so it does not replace a burst when the thing in doubt is
*what* landed on screen. Before a burst-heavy stretch (a porting session's
type-a-command/confirm-it-landed loop is the main case), call `vcctrl_note`
with what the sequence actually is -- e.g. `vcctrl_note("porting <game> to
DOS: typing build steps, confirming each with a short burst")` -- rather
than a generic note or none at all. That's the same call the rule below
already asks for at the start of any driving sequence; naming the porting
work specifically here costs nothing extra and is the one lever available
if the classifier turns out to be reading session context rather than
just call volume.

**The trip repeated on 2026-08-30 ~20:47, and `internal/burst-calls.jsonl`
caught the shape of the session leading into it.** Two back-to-back
~3.5-4min stretches of `vcctrl_burst` calls -- contexts like "checking
corruption state at ~35s/70s/110s/150s/190s into dummy-audio run" --
polled a DOS game's video RAM every 30-40s while it progressively
corrupted into color noise/static, immediately followed by a switch back
to real audio and a relaunch; the log simply stops there, one call short
of what the next step needed. That is a call-VOLUME correlation (a
dozen-plus bursts of increasingly staticky frames inside four minutes,
twice in a row) as much as it is a content one, and a single log can't
separate the two. **For this specific pattern -- watching a
corruption/glitch bug progress over minutes, not confirming one
keystroke -- don't re-burst every 30-40s just to see the current state.**
The ring already records continuously whether or not you poll it (see
`vcctrl_record`/`vcctrl_timeline` above), so let it run and pull
`vcctrl_timeline()` plus a couple of `vcctrl_frame(seq)` samples once,
*after* the stretch, to characterize how far it got -- rather than
showing the model a fresh corrupted frame every half-minute. Save a live
`vcctrl_burst` mid-run for an actual decision point (e.g. whether to
abort early), not routine progress-watching. Still not a confirmed
mechanism -- this is a second correlation, not a diagnosis.

**A mid-run health-check loop of `vcctrl_burst` calls checks video and only
video -- it does not check audio, even when a run's validity criteria
depend on audio being present.** Found live 2026-08-31: a full
pre-registered gameplay A/B benchmark (launch, several `vcctrl_burst`
health checks, 90+s of play, exit, fetch logs) went out the door with
every visual check done and zero audio checks done -- caught only because
the operator happened to be listening and said so afterward. Video and
audio are separate capture pipelines with separate rings (`vcctrl-caps`);
a clean frame proves nothing about sound. If a run's pass/fail criteria
mention audio at all, fold one `vcctrl_audio_verdict` call into the same
mid-run moment you're already reaching for `vcctrl_burst` -- see
`vcctrl-common-workflows`' "Checking audio" section for the full tool
rundown. Two separate questions, two separate tool calls.

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

**Call `vcctrl_note` at the start of any sequence of MCP calls that drives
the rig, and again whenever what you're doing changes.** The KVM page's
status bar (`#worklabel` in `kvm.html`/`kvm-ro.html`, fed by `state.json`'s
`note` field) shows nothing but "Idle" unless a driving session sets it --
it is `NoteCapability` in `daemon/vcctrld.py`, in-memory only, not derived
from the activity log, and nothing sets it automatically. It is the only
field that tells a human watching the KVM (or the public read-only mirror)
*why* the picture is doing what it's doing, not just that a command ran.
One sentence, e.g. `vcctrl_note("running a dinspect hardware re-scan for a
peer session, target rebooting")`. Not gated by the input lock and touches
no hardware, so there's no reason to skip it even for a read-only or
diagnostic sequence.

**When a driving sequence needs the operator's go-ahead (a power/reboot/
swap action, or anything else genuinely their call), don't just ask in
plain text and sit idle.** A rig-driving session runs long with waits
between turns, so a prose question can sit unseen for a while. Call
`PushNotification` (pushes to the operator's phone if Remote Control is
connected) alongside `AskUserQuestion` (the actual interactive yes/no
prompt) rather than either alone -- the operator's own instruction,
2026-09-03: *"ask it in a way so remote control sees it and sends an
alert to the claude mobile app, this way I can get pinged and you're not
just sitting there waiting for me to manually check this session."* A
plain "Go ahead?" in text gets seen late and lacks the interactive
yes/no UI a real `AskUserQuestion` prompt gives -- that gap is exactly
what prompted this note.
