---
name: vcctrl-webkvm-copy
description: Voice and wording rules for user-facing copy in daemon/kvm.html (tooltips, hint() toasts, confirm() dialogs, button labels, on-screen status text) -- what makes a tooltip too long, which internal harness words must not leak into it, and when to name the system instead of saying "the target". Use before writing or editing any title=, hint(), confirm(), or visible label/text in kvm.html.
---

# Wording user-facing copy in kvm.html

This file's comments carry a lot of engineering reasoning (why a state is
drawn the way it is). That reasoning is for the next editor, not the operator
-- it must not leak into what actually renders. These rules are about the
rendered copy itself.

**Tooltips, hints and non-destructive dialogs should be as short as possible.**
A hover tooltip is not the place to justify a design decision or narrate a
mechanism (`"mjpeg, fixed 12 fps but it works through any proxy"` shrinks to
`"mjpeg"`; `"Audio is being captured from X, the speaker button controls
whether this browser plays it"` shrinks to `"Audio is active"`). If the
surrounding comment already explains *why* the wording was chosen, that is
the signal the copy itself can be shorter -- the reasoning belongs in the
comment, not in the string.

**Destructive actions keep a real, plain-language warning.** Power off,
power cycle, and the Ctrl+Alt+Delete reboot chord are the exception to
the brevity rule above: say what happens (what gets cut, what shuts down) and
follow with an actionable instruction -- `"Close all programs and save all
work first."` -- rather than trimming the warning away. Don't state the
warning in harness-internal terms (see next rule); the person clicking Off
doesn't run sweeps, they run whatever's on screen.

**No internal harness jargon in rendered copy.** This codebase's own test
/ automation vocabulary is not the operator's vocabulary:
- `"sweep"` (a harness test batch) → don't reference it; the actionable
  warning is "close programs and save your work," which covers it without
  naming it.
- `"chord"` / `"latched"` (this file's name for a held multi-key combo) →
  "keys together" / "held".
- `"Cycle mains at X?"` / `"cycle the mains"` → keep **"Power Cycle"** as the
  button label and lead the confirm with **"Power cycle X?"** -- the operator
  specifically prefers this term over "Reset" for the plug action, so don't
  rename it again. What was actually wrong with the old copy was "mains" as
  the object of the verb ("cycle the mains"), not "cycle" itself.

Diagnostic terms that are *actually accurate hardware/protocol names* (PS/2,
WebSocket, mjpeg, scan code, DOS 8.3) are not jargon to fix — see the next
rule for the line between the two.

**Measured data is not narrative — don't colloquialize it away.** A tooltip
that reports a scan code, a file's size/date, or a board/profile reading with
its age is reporting a measurement, not telling a story. Trim the sentence
around it, never the number or the fact itself. The rule that shortens
tooltips is about removing *explanation*, not about removing *information*.

**Name the system, not "the target", once the daemon has said what it is.**
`daemon/kvm.html` defines `targetName()` — `boardNow.target || boardNow.name
|| 'the target'` — read from what the daemon reports about the board fitted
(ultimately the operator's own system config), with `'the target'` only as
the honest fallback when nothing is known yet. Use it in any confirm()/hint()/
title string that currently hardcodes "the target". **Exception, and it must
stay an exception:** the mains-plug wording (`showPower`'s `plug` variable
and the on/off/cycle confirm text) names the *plug*, never a guessed machine
-- one smart-plug socket can serve more than one physical machine over its
life and the daemon cannot see what's plugged into it, so asserting a system
name there would be a guess dressed as a fact. That distinction is also in
`vcctrl-rig-hazards`; don't "fix" the plug wording to match `targetName()`.

**The activity log is a record, not copy.** Log lines written through
`addLine()` are the persistent factual history of the session (mirroring the
`vcctrl-repo-conventions` rule that a commit message is documentation) —
keep them precise and measured rather than applying the tooltip-brevity rule
to them. The brevity and no-jargon rules above are about what renders in a
tooltip, hint toast, or dialog a person reads once and dismisses, not about
what the log retains.
