---
name: vcctrl-rig-hazards
description: Facts about the vcctrl rig (USB4VC PS/2 bridge, VGA capture stick, DOS 6.22 target) that read as a bug, a success, or a no-op and are actually the opposite. Use before judging target state from a capture/status read, writing or editing DOS batch files this rig runs, or trusting a single frame/keystroke/config file as ground truth.
---

# vcctrl rig hazards

Each of these was found by measurement, cost real debugging time, and is not
guessed at from the code — see `docs/FINDINGS.md` for the underlying
evidence if you need it.

**A black frame is not a black screen.** The capture stick emits flat-black
frames while it is locked to signal or re-syncing, indistinguishable from a
genuinely blank target display in a single still. Never conclude "the screen
is black" from one frame — check `vcctrl video_state` / a short burst for a
run of frames, not a single shot.

**The capture volume survives the source dying.** fps stays flat even after
the signal source is gone — the capture stick keeps producing frames at the
same rate. Do not use fps or frame count alone as a liveness signal for the
target; count *distinct* frames (a diff against a prior frame you actually
looked at), not frame volume.

**A config file is not the running configuration.** Attest the graphics/sound
mode the target is actually running from `vcctrl status` / the mode list the
daemon reports, never from a config file's contents alone — a file can be
correct and stale, or correct and not yet applied.

**MS-DOS 6.22 has no escape character.** `^` is a `cmd.exe`-ism; COMMAND.COM
treats it as a literal character and still lets `<`, `>`, `|` redirect right
through it. A batch line like `ECHO test -^> arrow` does **not** produce a
literal `->` — it writes `test -^` to a file named `arrow`. Any DOS batch
file for this rig that reaches for `^` to protect a redirect character is
silently broken; quote or restructure instead.

**COMMAND.COM parses redirection inside `REM` comments.** `REM (env > CFG by
design)` is not a no-op comment — it creates an empty file named `CFG`. A
`REM` line containing `<`, `>`, or `|` still redirects, even though it
produces no visible output, which makes the resulting file's existence and
size (usually 0 bytes) the only clue it happened.

**Typed input can silently not land, two different ways.** Caps Lock state on
the target inverts the case of everything vcctrl types, with no error
anywhere in the chain. Separately, OCR read off the captured screen cannot
reliably read digits. Neither failure mode announces itself — after typing
something you depend on, verify by a method that isn't the same channel that
might be wrong (e.g. a distinguishing keypress + visual check, not another
OCR read).

**A cell inherits its boot profile in ways the harness can't see from
outside.** Two cells can look identical from vcctrl's own vantage (same NET
setting, same apparent sound profile) and still differ, because the actual
witness is the `BLASTER` environment variable set at boot, not anything the
harness observes externally. Don't infer sound-profile identity from harness-visible
state alone.

**vcctrl can know which board is installed, and cannot know which machine is
on the socket.** The rig has one smart plug and swaps boards by hand
(`docs/BOARD-IDENTITY.md`). A power action being "board-scoped" means the
board is displayed alongside the control so a mismatch is *visible* to a
human — it is not, and cannot be, a refusal keyed to which physical machine
is actually plugged in. Don't treat board-scoping as machine-safety.

**A working Ctrl-Alt-Del can take far longer to register than it looks
like it should.** Measured 2026-08-26: three separate sends each produced a
clean, correctly-timed Scroll Lock clear→set cycle (~11-12s apart, matching
`docs/FINDINGS.md` sec. 7) — but the gap between *sending* the chord and
the clear *starting* was as long as 85 seconds on a chord that worked fine.
A short timeout reads a working chord as swallowed and resends into a reset
already under way, which is a race the caller creates for itself, not a
recovery from anything the target did. If you're timing a reboot by hand
(not through `vcctrl_send_file`/`_get_file`, which already carry this fix),
give one send a long wait before concluding it failed.

**A genuine PS/2 hang looks exactly like a lock problem until you check the
lock.** Measured 2026-08-26: `vcctrl_verify_input` failed persistently
("no LED change — the PS/2 link is not carrying keystrokes") while the
Arbiter lock was confirmed free and power/video/board all read healthy —
not the file-transfer lock bug (that one is silent and doesn't touch
`verify_input` directly), a real hang. **The operator's standing recovery
procedure, to follow exactly, not to embellish:** run `vcctrl_verify_input`
once. If it fails, do a single `vcctrl_power` `cycle` (not a soft
Ctrl-Alt-Del — the target isn't listening to it), then run
`vcctrl_verify_input` again. If it now succeeds, proceed normally. **If it
still fails, stop.** Do not retry, do not power-cycle a second time, do not
try a different chord or a longer wait — power the target off (`vcctrl_power
off`) and tell the operator. This is a deliberate one-shot policy, not a
retry loop: the harness does not attempt to recover a genuine hang without
a human, and pretending a second cycle might work where the first didn't is
exactly the kind of automated escalation this rule exists to prevent.

**A hand-written `expect_log`/`set_vars` string is a guess until it's been
read off a real log.** Measured 2026-08-26: an `expect_log` string built
from a spec document's prose (not copied from an actual engine log)
produced a confident, wrong refusal — `returncode=1`, "the cell ran, but
NOT in the configuration this arm means" — on a cell that had in fact run
correctly; the string simply never appeared anywhere in the real output.
Before trusting a new `expect_log`/`set_vars` string for a tag or cell type
you haven't run before, pull one real log for that tag and grep it for the
literal string first. This cost a full extra cell run (reboot, ~130s
gameplay, attestation) to discover the hard way.
