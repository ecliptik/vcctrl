# Putting a file on the card

**Use `vcctrl file-stage` then `vcctrl send-file`. Do not improvise a reboot
and a `GET.BAT` by hand.** This document exists because the improvised version
was run twice on 2026-08-24 and both times it worked, which is the problem: it
worked while skipping checks the standard path makes unskippable.

## The standard path

    vcctrl file-check                 is a server actually answering?
    vcctrl file-stage FILE            8.3 rename happens HERE, notes printed
    vcctrl send-file --return         reboots twice, ends on the menu default
    vcctrl file-status                the reboots are the wall clock

**`--return` or `--stay` is mandatory and neither is the default.** `--stay`
leaves the machine in NET, **which no measured run may start from** — the ODI
stack and packet driver are resident TSRs, and the standing rule is never load
TSRs and measure in the same boot. A script that did not say would leave the
rig in a state its author had not chosen.

## What it does that a hand-run `GET.BAT` does not

**Renames before the bytes move.** The target cannot rename in transit —
`GET.BAT` writes the name it fetched — so `my-photo-2026.jpeg` has to become
`MY-PHOTO.JPE` on this side, and the notes put that in front of a person
rather than in a log afterwards.

**Refuses to serve a partial file.** Bytes arrive in a sibling directory
outside the FTP root and are promoted by an atomic rename once the sha
matches. **`GET.BAT` cannot tell a short file from a whole one** — its own
comment concedes it reports an attempt — so before-it-is-reachable is the only
place that distinction can exist.

**Proves arrival by round trip, and the check rides INSIDE `VCGET.BAT`.**
The target's copy goes back with `VCCHK.BAT` and is compared by sha256 against
the staged bytes. **No OCR anywhere in the verdict**, which matters on a
console that reads `13,800` as `13,808` and `10 file(s)` as `18 file(s)`.

**Typing the check as a SECOND command did not work, and why is the useful
part.** It needed the harness to know DOS was back at a prompt, and the only
non-OCR readiness signal available tests whether the BIOS keyboard ISR is
alive — which it is, the whole way through `FTP.EXE`. So a 50-character
command went into a machine that was not reading, fifteen characters fit in
the BIOS buffer, and what executed was `C:\MTCP\VCCHK.B`.

**The fix was not a longer wait.** There is no honest readiness signal here
without OCR, so the design stopped needing one: DOS runs a batch file's lines
in order and needs no help doing it. One typed command, one arrival to wait
for, question gone. **When a probe cannot answer honestly, remove the
dependency rather than tuning the probe.**

**Lands in `C:\XFER\IN`, never `C:\DOSKUTSU`.** `VCGET.BAT` creates it
beside `C:\XFER\OUT` so the pair is discoverable from a `DIR` rather than
only from a document. **Do not set `dest` in `vcctrl.yaml` without a reason**
— the code default is the tested path, and a second opinion in config is a
second thing to drift. `C:\DOSKUTSU` holds `DOSKUTSU.EXE`, `CLRENV.BAT` and
the `LOGS\` tree, and a fetch overwrites by name without asking.

**Attests the profile positively.** A reboot into NET is confirmed by the
packet driver answering, not by a missing `BLASTER` — see `OPEN-FAULTS.md`
sec. 15 for why the sound witness cannot be run backwards.

## `GET.BAT` is the bootstrap, not the routine path

**It stays pointed at the control host and is never replaced.** `VCGET.BAT`
and `VCCHK.BAT` are separate files on purpose: using the transfer path to
replace the transfer path is the one step that can strand the machine, and a
truncated write would leave no way to fetch the fix short of a card swap.

**So `GET.BAT` is the recovery path.** If the daemon host's address goes stale
or its server stops answering, the control host's endpoint is still how you
fix the card. **Do not "tidy up" the two endpoints into one** — that removes
the only way to repair `VCGET.BAT` without pulling the CF.

## What this replaced

Two hand-run pushes on 2026-08-24, both of which needed a human to notice
things the standard path checks:

- **`CLRENV.BAT`** — reboot to NET, `GET.BAT`, `COPY` into place. Verified
  afterwards by reconciling a `DIR` total arithmetically, because both
  individual sizes were misread. **A `.BAK` taken first is the only reason it
  was safe**, since `GET.BAT` overwrites `C:\DOSKUTSU\<name>` silently.
- **`CHK.BAT`** — the same sequence, to install a file that was in the repo
  and had never reached the machine at all.

**Neither had a verification step that did not go through OCR**, and neither
would have stopped a truncated transfer from landing under a live filename.

## Preconditions, and how to tell

`VCGET.BAT` and `VCCHK.BAT` must be on the card. **They are generated per rig
by `vcctrl file-bats`, not shipped**, so the address they dial cannot drift
from the one the daemon binds and the liveness check probes.

    vcctrl file-check     answers whether a server is really there

**A capability that reports `unsupported`, `unknown`, `not_configured`,
`unreachable` or `unchecked` is telling you five different things.** They want
different responses from a person; `unknown` refuses, because an unidentified
target is the wrong thing to reboot and write to.
