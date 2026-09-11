# Three jobs, planned before starting any of them

**See also `lab/OPEN-FAULTS.md`** -- the live register of what is still broken
and what to check before trusting a result. Item 1 below is superseded; the
register says why.

Written 2026-08-20 at the end of Round Q, for work that should be done fresh
rather than at the end of a long session. Each section states what is
**measured**, what is **guessed**, and what the first step is — because on
this rig the expensive mistake is always a plausible mechanism acted on
before it was tested.

---

## 1. The capture regression: no Mach64 game lock since MQ2

**This is the one worth doing first**, not because it blocks anything today
but because it is the only item whose cost grows. No usable Mach64 glass
exists. The surfaces carried Round Q because the defect happened to be
engine-side; the next defect may be one only glass can see, and it will be
found with an instrument that has been quietly broken for hours.

### SUPERSEDED 2026-08-21 -- THE REGRESSION IS NOT REPRODUCING

**Read this before acting on anything below.** Since a reboot to the default
profile and a daemon restart, the Mach64 has locked game glass on **every**
cell attempted: 8 of 8 in the invalid Round R, 8 of 8 in the re-run, plus a
full 210 s recording at 30 fps with 11 repeated frames in 6,301. The
`no usable Mach64 glass` premise below is **stale**.

Two of the six rows in the table are also wrong, and wrong in ways worth
keeping:

- **MQ3's glass exists** and shows no game -- but the file is 94.5% MQ2's
  frames (sec. 36), so the row was right for the wrong reason.
- **MQ4 had NO RECORDING AT ALL** and was reported as `NO LOCK`. That is the
  instrument's state published as the target's, in a table built to diagnose
  the instrument.

What actually cleared it is **not established**. Candidates are the reboot,
the 22:36 daemon restart, and the ring age-eviction fix; nothing has separated
them and nothing should be asserted. **If it returns, the instrumentation plan
below is still the right first step** -- but start by checking whether a
reboot clears it, which is one cheap experiment that was never run because the
fault was assumed to be the stick latching.

### What is measured

    cell   pre-launch screenshot   mid-cell capture
    MQ0    ok                      brightness 23.2
    MQ1    ok                      brightness  3.6
    MQ2    ok                      brightness 23.6
    MQ3    None                    None (NO LOCK)
    MQ4    ok                      None (NO LOCK)
    MQ5    ok                      None (NO LOCK)

- **The console stays capturable throughout.** MQ4 and MQ5 read their whole
  environment back off the screen. Only the game's mode stopped locking.
- **MQ3's pre-launch failure was a one-off**, not the start of a permanent
  console failure.
- **Not the binary.** MQ0–MQ4 all ran `build_sha12=c292a24f649f`.
- **Not a killswitch.** MQ5 was stock defaults and failed the same way.
- **Not the ring pin.** `pinned=False` on every check since the 45 s bound.
- **Not the daemon.** ffmpeg respawned repeatedly across this period,
  `spawns` reset by restarts, no change.

So: a persistent, mode-specific failure that began between MQ2's cell and
MQ3's, and survives daemon restarts.

### The leading guess, and why it is only a guess

Earlier the same evening the stick latched and **only a physical reseat
cleared it** — a USB unbind/rebind re-binds the driver and never drops power
to the device. A partial latch, where the analog front end holds one timing
and refuses to re-acquire another, would produce exactly this: console mode
locks, game mode does not.

**It is a guess.** Nothing has tested it, and five mechanisms were confidently
wrong about the backdrop void before a bisection settled it.

### First step: instrument, do not theorise

The whole diagnosis so far rests on **one sample per cell** — a single frame
grabbed at `shot_at` seconds. That is the same shape as every other failure
this week: a proxy asked once and trusted.

Run one cell with a **1 Hz time series** of `video state`, `framestats`
distinct-count and `pinned`, from before launch to after exit, written to a
file. That answers, without guessing:

- does lock drop exactly at the DOS-text → game mode switch?
- does it ever return during the cell?
- is it `frozen` (uniform frames arriving) or no frames at all?

`frozen` versus starved distinguishes "the stick is emitting its no-lock
constant" from "nothing is arriving", which are different faults.

**Two more series the webkvm session asked for, and both are one field each:**

- **`spawns`.** If it climbs when lock is lost, ffmpeg is dying and being
  restarted — a device or driver fault. If it stays put, the capture process
  is alive and the signal is the problem. That single number separates two
  investigations.
- **`framestats` distinct-count**, as above: uniform frames arriving is a
  stick that lost lock and is emitting its constant; no frames at all is a
  starved pipe. The page renders those differently and so should we.

**And this belongs in the log as a known capture fault before anyone meets it
cold.** From the KVM's side a Mach64 cell now shows No Signal mid-run, which
reads as the page breaking. It is not.

### Then, in order, stopping as soon as one works

1. `video release` / `acquire` **at the moment lock is lost** rather than
   before the cell — tests whether re-acquiring against the live game signal
   succeeds where re-acquiring against a console does not.
2. USB unbind/rebind mid-cell. Expected to fail if the latch theory holds;
   worth doing because it is free and it *distinguishes* driver state from
   device state.
3. **Operator reseat**, which is the only thing that cleared it last time.
   If this is what works, that is a real finding about the hardware and
   belongs in FINDINGS rather than being treated as a workaround.
4. Only then, mode timings: `MODE12` on the console reads 640x480@59.6 Hz;
   what the game emits on the Mach64 has never been measured. An external
   monitor's OSD answers it in one look and no capture stack can.

### Do not

Do not change `shot_at`, add retries, or widen the brightness threshold to
make the symptom go away. The single-sample design is what hid this; making
the sample luckier hides it better.

---

## 2. `arm_leds` has no `stable_led` guard  — DONE 2026-08-23

**Fixed and tested.** Both reads go through `stable_led()`; returns three
states (True / False / None for could-not-look); `why=unsupported` stays a
real False because ADB genuinely has no return channel. Test carries its own
control — the old bare-read behaviour is reinstated in-run and required to
fail. See `test_arm_leds_survives_a_settling_read`.


**Four refusals tonight, four immediate retries that succeeded.** It is a
settling race in the function that arms the boot edges every reboot depends
on, and I have been papering over it with retry loops in my own callers.

### The bug

`arm_leds()` reads `leds().get(name)` **once** to decide whether to press a
key, and checks the final state **once** to decide whether it worked. Both
are single reads of a value that takes ~48 ms to settle — exactly the failure
`stable_led()` was written for after `at_prompt()` lost a run to it
(FINDINGS, and `stable_led`'s own docstring).

The consequence is not just noise: a false "could not arm" refuses a boot
that would have worked, and a false "armed" would let a reboot proceed with
an edge that cannot be detected, which is worse.

### The fix

- Decide with `stable_led(name)` rather than a bare read.
- Confirm with `stable_led` too.
- **Return three states, not two.** `stable_led` returns `None` for
  could-not-look, and `arm_leds` currently collapses that into `False`
  alongside genuine failure. `None` means the LEDs are unreadable — on ADB
  that is permanent and on a booting machine it is transient — and the caller
  acts differently on each.

### Test it can fail

A fake `leds()` that returns a settling value on the first read and a stable
one after, asserting the current code refuses and the fixed code arms. And a
control that removes the guard and shows the test going red.

---

## 3. Round self-cleanup of the CF card  — DECISION HALF DONE 2026-08-23

**`bin/vcctrl-cfclean` exists and REFUSES AT THE TOP.** The half that decides
what is safe to delete is complete and tested
(`test_cfclean_never_deletes_what_it_cannot_prove`); the half that reads
`DIR LOGS\<TAG>\` off the screen needs the Gateway and is not written.

**The four open questions below are answered, three of them structurally:**

1. **Historical debris** — answered by patch 0322. A round's output is now a
   DIRECTORY, so a directory-scoped cleanup cannot touch the flat backlog even
   by accident. The backlog stays a separate deliberate act.
2. **Abandoned rounds** — protect themselves. A file never collected has no
   local copy, so it never verifies, so it is never deleted. The rule that
   makes deletion safe is the rule that makes abandonment safe.
3. **Same rules for both types** — same verification, but PPMs only by
   default. `--logs` opts text in.
4. **Free-space warning** — 100 MB, in the tool.

**THE DESIGN CHANGED once the parser met real OCR.** DOS `DIR` output reads
back with the SIZES right and the NAMES wrong:

    real   R1A  LOG  1,102  08-20-26  10:58p
    OCR    RIA  LOG  1,102  88-20-26  18:58p

`R1A` -> `RIA`, `08` -> `88`, `10` -> `18`. **PPM filenames are `S<tick>.PPM`,
all digits**, so per-file name matching is one glyph from selecting the wrong
file — and a delete tool cannot carry that. Sizes, counts and totals OCR
correctly in every captured sample.

**So the card is asked for two numbers only — how many files and how many
bytes — and a count-and-total match over a tag's directory is the proof.**
`parse_dir_summary()` and `classify_tag()` are tested against OCR captured
from the rig, including `1 filets)` and `file<s)`.

**What remains is one call**: issue `DIR LOGS\<TAG>\*.PPM`, hand the OCR to
`parse_dir_summary()`, pass the result to `classify_tag()`. A `None` from the
parser must stay `None` — `File not found` must not become `(0, 0)`.



**Shape settled by the operator: automatic, round-scoped, gated on
proven-collected.** The benchmarking session supplied the doskutsu-side facts.

### Why it is needed

`LOGS` grows monotonically between populates — deliberate and harmless for
~150 KB text logs. Frame dumps are **230,415 bytes each, four per cell**, and
they are in neither glob:

    logback-qa.sh collects   *.LOG *.TXT *.CFG *.NFO      no *.PPM
    install-qa.sh clears     *.LOG *.TXT *.CFG            no *.PPM

Not collected, not cleared, invisible to both halves of the tooling meant to
manage exactly this. **Nothing has been lost only because they have been
pulled by hand after every cell.**

**Capacity is NOT the problem** — 762 MB free, 110 files, 10.4 MB used. The
problems are correctness: silent overwrites (`S02400.PPM` and `S04851.PPM`
were each written by two different cells tonight, the second overwriting the
first with no error and no log line) and invisibility to the tooling.

### The one hard constraint

**Never delete anything not PROVEN collected.** Not "the copy returned
success" — proven, by size or hash against the local copy, per file. This is
destructive and irreversible on a machine that is a card swap away.

Given the week, the failure mode writes itself: **a collection check that can
return a false yes deletes the only copy.** `all({}.values())` is `True`; a
substring survived in nearby prose; a clip check passed on zero frames. Any
of those shapes in a verify-before-delete path costs data rather than a run.
So the verification must assert its population is non-empty (sec. 35) and
must compare content, not status codes.

### Shape

- **Round-scoped.** Blast radius is the tags this round produced — a much
  easier thing to make safe than a general "delete old files" sweep.
- **Verified-then-delete, per file**, which also makes it resumable: one file
  failing verification leaves that file and cleans the rest.
- **`--dry-run`, and a sanity gate that refuses if the target does not look
  like the game directory.** Both stolen from `cf-clean.sh`.
- **Expect two layouts**: flat `LOGS\*.PPM` for everything written up to now,
  and `LOGS\<TAG>\*.PPM` from patch 0322 onward.

**0322 LANDED 2026-08-21 and it simplifies this job.** A round's output is now
a DIRECTORY rather than a filename pattern, so the silent-overwrite problem
that destroyed two frames (`S02400.PPM` written by two cells, the second
overwriting the first with no error and no log line) is structurally
impossible rather than prevented by collecting between cells. Verified tagged,
untagged and regression by its author: `LOG_TAG=D1A` gives
`LOGS\D1A\S00300.PPM`, no tag gives flat `LOGS\S00300.PPM` as 0318 did.

**Consequence for the cleanup: scope by directory, not by glob.** "Delete the
tags this round produced" becomes "delete these directories once every file in
them is proven collected", which is easier to make safe and easier to make
resumable. The historical flat debris is a separate, one-off problem — which
is the answer to open question 1 below.

### Open questions — the operator's, not mine

1. **Historical debris.** A round that cleans only its own tags never touches
   what is already on the card, including every PPM written tonight. Does the
   first run sweep the backlog, or does that stay a separate deliberate act
   like `cf-clean.sh`? *My lean: separate. A cleanup that quietly widens its
   scope beyond the round it belongs to is much harder to reason about — but
   something still has to deal with the backlog.*
2. **Abandoned rounds.** If a round dies mid-way and is never collected, its
   files must not become the next round's orphans and get swept. **From the
   card, uncollected data and stale data look identical.** Whatever rule
   covers orphans must not eat uncollected data.
3. **Same rules for both types?** Dumps are pure diagnostic and enormous;
   once verified off the card there is no reason for them to stay. Text logs
   are small enough that keeping the current round's costs nothing and
   occasionally saves a trip.
4. **A free-space warning threshold**, and where it lives.

---

## Order

1. **Capture regression** — the only one whose cost grows, and the only one
   that needs the rig.
2. **`arm_leds`** — small, self-contained, testable without hardware, and it
   removes a workaround from every caller.
3. **Cleanup** — needs answers to the four questions above before any code.
