# Moving a file to the card, and back off it

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
system in a state its author had not chosen.

## Getting a file back off the card

    vcctrl file-refresh --return       read C:\XFER\OUT (REBOOTS the target)
    vcctrl file-list                   what that reading found, and its age
    vcctrl get-file SCORES.DAT --return    fetch it (REBOOTS the target)
    vcctrl pulled list                 what is on the daemon host now
    vcctrl pulled save SCORES.DAT --out ./scores.dat

`--all` fetches everything listed. `--paranoid` fetches each file twice and
compares. `--return` or `--stay` is mandatory here for the same reason it is
on the way out. In the KVM it is **File → Download from target**.

**Nothing is ever deleted from the target.** `C:\XFER\OUT` is emptied by a
person, not by this tool.

### The listing is a file, not a screen

The harness cannot read the target's console — it turns `13,800` into `13,808`
and `10 file(s)` into `18 file(s)` — so a picker built on OCR would offer files
that do not exist and hide files that do. `VCLIST.BAT` redirects a `DIR` into a
file and sends **the file**, which arrives byte for byte and **states its own
totals**, so it can be reconciled against itself. A listing whose sizes do not
add up to the total `DIR` printed, or which has no `N file(s)` trailer at all,
is refused: **a listing that arrived short reads as a directory with fewer
files in it, and nothing about it looks wrong.**

Reading that directory means rebooting the machine, so **the picker shows the
LAST reading with its age beside it**, and refreshing is a separate, explicit
act. An unread directory is reported as unread — never as empty.

### What a pulled file is verified against, and what that is not

**This direction is one step weaker than the upload, and the words are kept
apart on purpose.** A push is proved byte for byte because the staged copy was
sha256'd on this host before anything moved. Nothing on DOS 6.22 can hash a
file, so a pull has no such quantity to compare against. What it has:

- **`verified: size`** — the file came back the length the target's own `DIR`
  said it was. A truncated transfer cannot match it, and truncation is the
  failure that otherwise looks exactly like success.
- **`verified: size+repeat`** — `--paranoid`: fetched twice, and the two
  copies agree. **That is a claim about the path being repeatable, not about
  either copy equalling what is on the card.**

The result carries which check ran rather than letting one word cover both.
`vcctrl pulled save` re-checks the bytes it writes against the sha the daemon
recorded when it verified them, so a short read on the way out to your disk is
caught too.

### What is refused before anything is typed at the machine

Selection happens against the target's own `DIR`, on this side. The target
never hears a name it did not itself report:

- **`not-listed`** — asked for by name and not on the card. Refused here
  rather than spending a minute of the run on an FTP session for a file that
  does not exist.
- **`unsafe-name`** — the name does not survive the DOS 8.3 round trip. A name
  with a space in it is the ordinary case: FAT permits one and
  `VCCHK C:\XFER\OUT\MY FILE.TXT` is two arguments. Shown in the picker,
  greyed, with the reason.
- **`empty`** — zero bytes on the card. Arrival is proved by bytes appearing
  and settling, so **a zero-byte file cannot be told from one that never
  came**; there is no reading of the wait that means "it worked".

### Reading a directory other than `C:\XFER\OUT`

    vcctrl get-file --from C:\DOSKUTSU\LOGS GMN.LOG --return
    vcctrl file-list --from C:\DOSKUTSU\LOGS

**The directory is now input, and it is whitelisted rather than filtered.** It
ends up inside `VCLIST.BAT %s` and `VCCHK.BAT %s\%s` on a machine with no
quoting of any kind — the caret escapes nothing on DOS 6.22 — so there is no
safe way to escape a bad path. `dos_dir_path()` accepts an absolute DOS 8.3
path and refuses everything else: a space (which ends the argument), a
redirect (which would write to the card), a relative component, a device name,
a wildcard, and the root of a drive. **Refused rather than repaired** — a path
this had to alter is not the path the caller meant.

`--already-net` **skips the reboot and not the proof.** The arrival gate still
runs, so a caller who is wrong about where the machine is gets a refusal
instead of a fetch typed at a profile with no network stack. That is what lets
two directories be read in one reboot pair: the first job stays in NET, the
second is told so and re-establishes it for itself.

**PRECONDITION: the card needs the CURRENT `VCLIST.BAT`.** The version
installed on 2026-08-24 creates the directory it is asked to list. That is
right for the default `C:\XFER\OUT` and wrong for a named one, because it
turns a mistyped path into an **empty listing** — which reads as "that
directory has nothing in it" when the truth is "that directory does not
exist". The generated BAT now only creates the default. **Until it is
reinstalled, a wrong `--from` will be reported as empty rather than as
missing.**

### The harness collects over this path now

`vcctrl-collect` asks the daemon to fetch `C:\DOSKUTSU\LOGS` instead of
driving `PUT.BAT` from the control host. The old path is still there behind
`--via-put`.

**What that buys is a distinction PUT could not make.** `PUT.BAT <tag>` sends a
fixed pair of names and "fails quietly when asked to send a file that is not
there" — its own documentation says so — so a missing log and a broken
transfer looked identical. The fetch path reads the card's own `DIR` first, so
a log that was never written comes back as `not-listed` and a transfer that
failed comes back as `no-return` or `size-mismatch`. That is the first
question anybody asks of a missing log, answered by the tool instead of by a
trip to the card.

**Files still land in `~/doskutsu-netiter/incoming`**, because `cfclean` and
the collector's own log inspection read that directory. The bytes come off the
target onto the daemon host and are saved down by name — and **a file that was
not collected this run is deleted from `incoming/` rather than left**, because
`inspect_logs` reads by path and a log from a previous collect would otherwise
be read as this one's.

### Where the bytes land, and where they do not stay

They arrive in the server's `incoming/`, which is inside the FTP root and
therefore both served and writable by the target. **A verified file is promoted
out of it** — `<root>-pulled/`, outside the root, with its sha, its size, where
it came from and which check passed it recorded beside it. A file that fails
its check is removed rather than left looking like a result.

`vcctrl pulled list` reads that directory from disk rather than from memory,
the same discipline the staging queue follows.

## What the upload path does that a hand-run `GET.BAT` does not

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
packet driver answering, not by a missing `BLASTER` — see `lab/OPEN-FAULTS.md`
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

## What has actually been measured

Everything below was read from the **file server's own log**, not off the
target's screen. That distinction is the point: this console reads `13,800` as
`13,808`, and the target's FTP transcript once reported an elapsed time of
`8.055 s` for a transfer that took `0.055`.

    10,485,760 bytes  server -> card   14.952 s   685 KiB/s
    10,485,760 bytes  card -> server   10.781 s   950 KiB/s
    whole job, both reboots included              89 s

**10 MB is the largest transfer verified end to end**, out and back and
byte-for-byte. `LARGEST_VERIFIED_BYTES` in the daemon carries that number with
its provenance, and `WARN_BYTES` **is** that constant — so the warning fires
exactly above what somebody has watched work, and the threshold cannot drift
away from the evidence for it. The text quoted `7.8 MB` for a while after that
was beaten, which is what tying them together prevents.

**Throughput rises with size, so a single figure is a figure about one file.**

    13.8 KB    122 KiB/s
     2 MB      384 KiB/s
    10 MB      685 KiB/s

A curve fitted to the small end and extrapolated missed a 2 MB transfer by
3.4x. **The reboots are the wall clock in any case** — about a minute of them
against a few seconds of transfer — which is why progress is reported for the
reboots and not for the bytes.

**Writing to the CF is slower than reading from it**, consistently and by
roughly 40%. That is a hypothesis about the mechanism, not a finding.

### The download direction, 2026-08-24

Everything here is from the file server's own log and the job record, not off
the target's screen.

    vcctrl get-file TCP.CFG VCGET.BAT --return     whole job  69.5 s

    +26.1 s  the reset was seen
    +53.5 s  NET confirmed by arrival
    +58.4 s  the DIR of C:\XFER\OUT was back and reconciled   (4.9 s)
    +63.8 s  TCP.CFG    130 B verified                        (5.4 s)
    +69.5 s  VCGET.BAT 2434 B verified                        (5.7 s)

**The reboots are the wall clock here too, and so is the FTP session.** The
server logged `0.046 s` for the listing, `0.050` for 130 bytes and `0.052` for
2,434 — so at this size a fetch costs about five seconds of session setup and
a twentieth of a second of transfer. A per-file cost, not a per-byte one.

**The strongest check available was not the size check.** `VCGET.BAT` was
fetched off the card and compared against the copy `vcctrl file-bats`
generates on this host: **byte-identical, sha256 `a94449b7…`**. That is a real
end-to-end proof of the path — card, FTP, promotion, `pulled save` — and it is
evidence about this run rather than a guarantee the tool can offer, which is
why `verified` still reads `size`.

**The `DIR` format is now read rather than assumed.** Two things the parser
had to guess are settled: individual sizes DO carry thousands separators
(`2,434`), and `N file(s)` DOES count `.` and `..` — a directory with two
files in it reported `4 file(s)`. There is also a `Volume Serial Number` line,
which the parser skips.

### The fetch path, walked 2026-08-24

Every leg is proved by BYTE COMPARISON rather than by the size check the tool
itself applies — each fetched file was compared against a copy of the same file
collected that morning over the old `PUT.BAT` path. **Two independent
transports, three days of code apart, agreeing on every byte.**

    get-file --from C:\DOSKUTSU\LOGS ACC1SDL.LOG      43,462 B   identical
    vcctrl-collect --tags CLN1   (the harness default)  702 + 4,282 B
    vcctrl-collect --tags ACC1                      122,811 + 43,462 B identical

166 KB is 68× the largest previously proven fetch, and the cost stayed
per-file rather than per-byte: the whole two-file ACC1 job, **including the
return reboot**, was 62 seconds. `--already-net` ran three jobs in one NET
session with no reboot between them, each proving the network by arrival.

**The collector earned its keep on its first real run.** CLN1's files arrived
intact and `inspect_logs` failed the cell anyway, on
`[critical] ack, sdl_init failed: No BLASTER environment variable`. Arrival is
not health, and a complete envelope around a dead cell is exactly what that
function exists to catch.

### What the walk cost, and what it found

**The first attempt refused with `no-net` on a machine whose transfer was
sitting completed in the FTP server's own log.** Five links, and only the last
was the defect: a stale Caps Lock reading → `type_line` pressed to "correct"
it and turned caps ON → every typed command inverted → the proof file landed
as `netproof.txt` → it met a stale `NETPROOF.TXT` from an earlier session →
the finder returned the FIRST case-insensitive match, the freshness guard
correctly refused it as too old, and the fresh one two entries away was never
looked at.

Fixed in `67e16ab`: the finder takes the **newest** match and there is one
implementation of it (`_incoming_path` had the same bug, and it is what the
sha comparison reads); the proof file and the `.CHK` copies are consumed
rather than left; and **`type_line` no longer presses Caps Lock** — it reads,
records, and the job reports it. A wrong read reports something false; a wrong
press changes the target in the direction that breaks what follows.

**A run that leaves artifacts leaves landmines for the next run, and the
failure surfaces as a wrong verdict about the machine.** A file from 21:09
defeated a transfer at 21:37.

### Two faults this deploy found

**`COPY x C:\MTCP\` is `Invalid directory` on DOS 6.22.** The trailing
backslash on the destination is rejected. The install instructions printed by
`vcctrl file-bats` carried one from the day they were written; they now do
not. `COPY x C:\MTCP` works.

**And `COPY` prompts `Overwrite … (Yes/No/All)?` on this machine.** Which
matters more than it sounds, because **a DOS prompt waiting for a key
consumes a typed command looking for a valid answer, and finds one inside an
ordinary word** — the `Y` in `COPY` answered Yes, and the rest of the line
queued against the next prompt. That is the failure this whole feature is
built to avoid (one typed command, one arrival to wait for, no second command
whose readiness nobody can establish), reproduced by hand at a prompt ten
minutes after deploying the thing that avoids it. **If a keypress appears not
to reach the target, look at the screen before concluding the keyboard is
dead: something on it may be eating them.**

**The return reboot was never witnessed, in either direction.** `wait_boot()`
waits for Scroll Lock to READ 1, and RDYPULSE had already left it at 1 — so on
the return leg it returned on its first poll and the log said *the machine
booted* zero seconds after the Ctrl-Alt-Del, having observed nothing. The
outward leg has always armed Scroll Lock first so that POST clearing it is an
EDGE; the return leg did not. **`left_in_net` was set false on the strength of
that**, and the warning that says the target may still be in NET — the one
state this feature is careful about — could never fire. Both legs now arm, and
an unwitnessed return keeps `left_in_net` true rather than guessing.

## What has NOT been tested

Named because a feature that works is the easiest thing to over-claim.

- **Nothing between 10 MB and the 64 MB refusal.** The ceiling has only been
  met by a file well past it, never by one just over.
- **Nothing larger than 2.4 KB has been fetched OFF the card.** The direction
  works and is proved byte-for-byte at that size; the throughput curve above
  was measured on the way out, not back.
- **No fetch of more than two files in one run**, and none cancelled
  mid-queue.
- **`--paranoid` still has not run on the hardware**, and it is now the only
  part of the fetch path that has not.
- **No transfer with a viewer attached to the KVM.** The Pi's video stream and
  the target's transfer share the wifi, and this is the one place a viewer
  measurably costs the harness something.
- **No transfer while anything else was driving the target**, which the daemon
  refuses anyway but which has not been provoked.

## Preconditions, and how to tell

`VCGET.BAT`, `VCCHK.BAT` and `VCLIST.BAT` must be on the card. **They are
generated per system by `vcctrl file-bats`, not shipped**, so the address they
dial cannot drift from the one the daemon binds and the liveness check probes.

**`VCLIST.BAT` is only needed for the download direction**, and a card without
it fails in exactly one way: `vcctrl file-refresh` reports `no-listing`.
Uploads are unaffected. **Fetching needs no new batch file at all** — `VCCHK`
already puts a named path back on this host, which is what a download is; the
listing is the part that had no answer.

    vcctrl file-check     answers whether a server is really there

**A capability that reports `unsupported`, `unknown`, `not_configured`,
`unreachable` or `unchecked` is telling you five different things.** They want
different responses from a person; `unknown` refuses, because an unidentified
target is the wrong thing to reboot and write to.
