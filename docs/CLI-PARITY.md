# CLI/KVM parity: why, and what is left

Written 2026-08-20. The operator's requirement: **vcctrl should be able to do
everything the web KVM can**, so the benchmark and test harness can use it from
scripts rather than a person driving a page.

## 1. The gap, measured rather than assumed

The webkvm session found it; this is the independent count. Parsing every
`commands()` map in the daemon and every verb in `bin/vcctrl-client`:

    26 capability commands in the daemon
     6 with no CLI verb

    buffer  burst  frame  pin  timeline  verify_input

`mouse_click` and `mouse_move` look missing to a naive diff and are not —
they are reachable as `mouse move` / `mouse click`. Five registry-level verbs
(`status`, `caps`, `events`, `activity`, `lock`) the CLI already had.

**Five of the six are the scrub buffer.** It was built daemon-side, correctly,
and the only thing that could drive it was `kvm.html`. A sweep could not save
the seconds before a crash, step back through what the screen did, or pin the
ring while it looked. That is the wrong way round: **the CLI is the thing that
runs unattended, and unattended is exactly when nobody is watching the screen
at the moment it matters.**

The sixth is worse in a quieter way. `verify_input` is the round trip that
proves a keystroke reached the target — the only check that says anything
about the far end of the wire, since every other input status describes the
Pi's own end. A headless sweep could not assert its own input path before
typing. It could only type and hope. After a day spent on a bug where every
diagnostic was green and nothing was driven (FINDINGS sec. 29), that is the
one to close first.

## 2. Phase 0, which had to come first: `--out` means the caller's disk

`bin/vcctrl` forwards to the Pi over ssh, so `--out` was a path **there**.
Every frame-returning verb would have written its output onto the wrong
machine, which makes the whole exercise useless to a VM-side harness.

The failing case was merely confusing — an ENOENT that reads like a local
permissions problem. **The succeeding case is the dangerous one**: a path that
exists on both machines writes on the Pi and returns 0, and the caller reads
whatever its own copy holds, possibly a frame from an earlier run. That is
precisely the stale-frame failure `--out`'s two-valued contract was written to
prevent, reappearing one host over where the contract cannot see it.

Now: run to a temp path on the Pi, copy back, remove the remote copy, and
rewrite the reported path so the JSON names where the file actually is.

**The obvious cleaner design is deliberately not used.** Streaming bytes on
stdout and letting the shell redirect is simpler, but the shell creates the
target file *before* the command runs, so a failure leaves a zero-byte file
behind. A file that looks like evidence and is not is the thing this rig keeps
producing.

## 3. Exit codes carry the reading

Settled with the operator. `shot --out` already worked this way, so this
extends a precedent rather than inventing one.

    0  the target answered
    1  it did not          -- a real fault
    2  could not look      -- no LED channel on this board, or unreadable
    3  the tool itself failed

`vcctrl verify-input || abort` is the intended use, and **2 is the reason the
scheme needs four codes rather than two**. On a Macintosh over ADB there is no
LED return channel at all; if that exited 1, every harness guarded that way
would abort on working hardware. Same three-state discipline as the rest of
the rig.

Note `--out` misapplied to a command now exits 3 rather than 2, so that 2
means could-not-look unambiguously. Nothing branched on the old value.

## 4. What shipped

All six verbs, plus `pi/deploy.sh --client`.

That deploy mode matters more than it looks. A full deploy runs `install.sh`,
which restarts `vcctrld` and drops both uinput devices — a sweep cannot
survive it and is not resumable. The client is a fresh process per invocation,
so replacing the file needs no restart at all, for exactly the reason `--page`
does not. **Making the safe path available is what stops the unsafe path being
used out of impatience.** It syntax-checks before and after copying, because a
client that cannot parse takes out every verb at once and would do so on the
next call rather than at deploy time — so the deploy would look like it worked.

Proven against the live rig: `timeline` (622 frames, 31.9 s span), `buffer`,
`pin on`/`off`, `frame <seq> --out` landing locally as a valid 640x480 frame,
`burst 5 --out-dir` writing five, and **`verify-input` returning 0 against the
Gateway** — the input path proven from a script for the first time.

## 5. Phase B: done

`vcctrl record --out F.avi [from_seq] [to_seq]`, on the webkvm session's
`avi_mjpeg()` muxer. Proven against the live rig:

    frames 692 in the ring, written 1032, repeated 340, span 34.37 s
    71,833,864 bytes landed on the CALLER's disk
    ffprobe: mjpeg 640x480, nb_frames 1032, duration 34.40

**`written` and `frames` are reported separately, and the note spells out
why.** The muxer preserves timing by repeating a frame across a gap where the
ring was thinned, so the file holds more frames than the ring did. Reporting
`written` as "frames captured" would overstate what was observed — and the
difference is *exactly the stalls*, which is the thing a crash investigation
cares about most. 340 repeats here means about a third of the file is the
screen not changing.

It takes the pin for the copy and releases it in a `finally`. The muxer
snapshots the ring under the lock in one `list()`, so the pin is belt on top
of braces rather than the load-bearing part — but it costs nothing, and the
daemon expires it after 300 s if the process dies holding it.

Two-valued, verified: an impossible range returns 503, exits 1, and **removes
a stale file that was there before**. A truncated AVI still opens, still
plays, and still looks like a recording, so a half-written one is worse than
none — the caller would read a copy that lost half its frames as evidence of
what the screen did.

It goes over HTTPS rather than the unix socket: the socket protocol is
line-delimited JSON, and base64-ing 70 MB through it would work and would be
a poor idea.

## 5a. Parity is not only "is there a verb" — 2026-08-25

The count in sec. 1 asks whether the CLI can *reach* every command. Building
the KVM's full on-screen keyboard turned up a second kind of gap it cannot
see: **the same command behaving differently depending on which side called
it.**

`Devices.combo()` presses in the order it is handed and releases in reverse.
The web KVM sorted modifiers to the front before posting. The CLI passed argv
straight through. So `vcctrl combo delete ctrl alt` pressed Delete before
either modifier arrived — a keystroke, then two modifiers going down after it,
and at a DOS prompt a character in the buffer. The same chord built in the
browser was correct. Both verbs existed; only one of them worked.

It never bit anything because every caller in the tree happens to write
`ctrl alt delete` in that order. It was latent in the surface that is scripted
into sweeps and has no dialog to catch a mistake.

**Fixed by moving the guarantee under both callers**, not by teaching the CLI
what the page knew:

- `order_chord()` in `vcctrld.py`, applied in `InputCapability._combo` — the
  command both callers reach. Deliberately **not** in `Devices.combo()`: the
  primitive presses what it is handed, because something that genuinely wants
  a raw press sequence must still be able to say so. Reordering is part of
  what the word *chord* means, which is the command's business.
- The `combo` reply now echoes the order actually used, so a caller can see
  what happened rather than assume its own argv was it.
- The page's sort is gone. Two implementations agreeing today is how they come
  to disagree later.

**Same shape, second instance: the alias table.** The page needed to know
whether a chord was the reboot *before* sending it — that is what the
confirmation is — and carried its own transcription of `_CHORD_ALIASES`, kept
honest by a test that read both files and compared them. A test that two
tables agree is a good answer to a question that should not have been asked.
So the daemon publishes: `keymap` as a command, `vcctrl keymap` on the CLI,
`/keymap.json` for the page. One copy.

The page fails **closed** on it: with no table it cannot tell a reboot from a
chord, so `isReboot()` returns null rather than false and chords are held
until the table arrives. A page that quietly stopped warning would be the
failure that hides itself.

### The evidence for "the sort is harmless" was gathered over the wrong set

Worth recording, because the argument was mine and it was not sound as stated.

The justification for reordering was: every `combo` caller in the tree writes
`ctrl alt delete` already — six sites, all modifier-first — so the sort is a
no-op on all of them. True, and the `vcctrl` session pointed out before deploy
that **the same push adds a caller the argument was not made over**: the KVM's
sticky chords, where the keys go in whatever order a thumb tapped them. The
population changed in the change that reasoned about the population.

Two questions came out of it, both now measured rather than argued:

- **Does `order_chord()` disturb a chord with no modifier in it?** No. Every
  non-modifier ranks equal and the sort is stable, so nothing moves —
  asserted over five sequences including reversed ones.
- **Can the sticky UI express a sequence whose ORDER was the intent?** No.
  Latching `B` then `A` and pressing Send posts `combo b+a` and the daemon
  leaves it alone, so tap order survives end to end. And the ordered-sequence
  capability was never in `combo` anyway: `key` is the verb that taps in
  sequence, and it does not go near `order_chord()`.

The finding is not that the sort was wrong. It is that "no caller does X" is
a claim about a population, and a change that adds callers has to be checked
against the population it leaves behind, not the one it found.

**The generalisation, for the audit in sec. 6:** parity is not "both sides can
call it". It is "both sides get the same thing when they do". Anywhere one
caller normalises, validates or reorders before dispatch, that logic belongs
under the dispatch — or the other caller is running a different command with
the same name.

## 6. Still open

Task-shaped verbs, built on the thin ones — `save-around <seq> --seconds 10`
is the shape the harness will actually reach for. Deliberately not invented
ahead of seeing which ones get used.

**And one audit worth doing cold rather than at the end of a long day:**
sweep both sides for places where a key's ABSENCE is read as a value. Two
turned up today within an hour of each other. The webkvm session's ring pin
was correct code wired to nothing, so the save path walked the ring without
holding it and eviction destroyed frames mid-copy. `write_frame` judged raw
responses by `resp["picture"]`, which is *absent* on that path rather than
false — so every frame fetched by sequence number would have reported "no
picture", and the two-valued contract would then have deleted the caller's
file.

Neither is a bug in the function that failed. Both are bugs in the seam: a
new caller arrived at an old contract carrying a premise nobody had written
down. The second is the worse kind, because it fails by destroying the thing
it was asked to produce rather than by doing nothing.
