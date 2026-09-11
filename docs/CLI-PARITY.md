# CLI/KVM parity: why, and what is left

Written 2026-08-20, trimmed 2026-09-11 to the parts still worth reading —
the build log behind them is in git history if you need it. The
requirement: **vcctrl should be able to do everything the web KVM can**, so
the benchmark and test harness can use it from scripts rather than a
person driving a page.

## The gap, and what shipped

Parsing every `commands()` map in the daemon against every verb in
`bin/vcctrl-client` found 26 capability commands, 6 with no CLI verb:
`buffer`, `burst`, `frame`, `pin`, `timeline`, `verify_input`. Five of the
six were the scrub buffer — built daemon-side, driveable only from
`kvm.html`, which is backwards for a harness that runs unattended. The
sixth, `verify_input`, is the only check that proves a keystroke reached
the target rather than just describing the Pi's own end.

All six shipped, plus `pi/deploy.sh --client` (a fast, restart-free path to
update just the CLI). Proven against the live system: `timeline` (622
frames, 31.9 s span), `buffer`, `pin on`/`off`, `frame <seq> --out` landing
locally as a valid frame, `burst 5 --out-dir` writing five, `verify-input`
returning 0 against the target, and `vcctrl record --out F.avi` producing a
valid MJPEG AVI via the web KVM's own muxer.

## Parity is not only "is there a verb" — 2026-08-25

The count above asks whether the CLI can *reach* every command. Building
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

**The generalisation:** parity is not "both sides can call it". It is "both
sides get the same thing when they do". Anywhere one caller normalises,
validates or reorders before dispatch, that logic belongs under the
dispatch — or the other caller is running a different command with the
same name.

## Still open

Task-shaped verbs, built on the thin ones — `save-around <seq> --seconds 10`
is the shape the harness will actually reach for. Deliberately not invented
ahead of seeing which ones get used.

**And one audit worth doing cold rather than at the end of a long day:**
sweep both sides for places where a key's ABSENCE is read as a value. Two
turned up within an hour of each other. The webkvm session's ring pin
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
