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
