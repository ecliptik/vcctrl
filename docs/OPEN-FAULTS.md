# Open faults, and what to do about them

`FINDINGS.md` records what happened. **This file is the forward-looking half:
what is still broken, what is worked around rather than fixed, and what to
check before trusting a result.** If you are picking this up cold, read this
before running anything measured.

Last reviewed 2026-08-21, after Round R.

---

## 1. `vcctrld` aborts with a glibc double-free  — OPEN, UNEXPLAINED

    double free or corruption (top)     23:09, after 32 min / 1m13s CPU
    double free or corruption (!prev)   23:43, after 28 min / 3m10s CPU

Two aborts in 62 minutes on 2026-08-20, both SIGABRT, both auto-restarted by
systemd in ~3 s. `PIL._imaging` is loaded; **no thread was inside PIL at the
abort** — glibc raises on whoever calls `free()` and finds the arena damaged,
so the corrupting write happened earlier on another thread.

**If it fires, do this FIRST, before anything else touches the box:**

    pi/core.sh              # newest vcctrld core, `thread apply all bt`
    sudo journalctl -u vcctrld --since '-45 min'   # faulthandler stacks

Cores are armed and `libc6-dbg` + debuginfod are installed, so Pillow's frames
resolve. **Verify on the RUNNING PROCESS, not the unit** — the unit reports the
*hard* limit and Debian's soft limit is 0:

    grep 'Max core' /proc/$(systemctl show vcctrld -p MainPID --value)/limits

Both columns must read `unlimited`. This is why the first abort left no core
despite the unit looking configured.

**What is ruled out:** not a deploy, not the ring-eviction change (a dict of
names to timestamps, no `free`).

**The daemon lifetimes, from the journal:**

    136204  22:36:51 -> 23:09:03   32.2 min   ABORT (top)     cells + browsers
    143721  23:15:03 -> 23:43:15   28.2 min   ABORT (!prev)   cells + browsers
    167771  23:43:18 -> 00:56:52   73.6 min   clean           12 cells, 2 collects,
                                                              a 216 MB AVI mux,
                                                              NO browsers

**The clean window is 73.6 minutes, not the 55 quoted earlier**, and it
carried more harness load than either aborting window: twelve cells, two
NET-boot collects, and a 216 MB mux, including a period with the ring raised
to 335 MB. It beats both abort intervals by better than 2x.

**What is not ruled out:** the websocket frame streamer, which hands ring
bytes to SSL from a thread that is not the capture reader.

**The combination present at BOTH aborts has never been tested**: cells
running AND streamers connected. vckvm's 12.2-minute stress had streamers and
no cells; the 73.6-minute clean window had cells and no streamers. Neither
arm reproduces it alone, and nobody has run both together. **None of this is a
controlled test and it should not be presented as one.**

**A cell that straddles a restart still finishes and writes a plausible
number** — one did, 122,485 bytes. `roundR/run.sh` pins the daemon's MainPID
and refuses to continue across a change. Keep that guard in any new runner.

### The combination run of 2026-08-21 19:16 — CEILING WRITTEN BEFORE THE RESULT

Three arms together for the first time, daemon 519329:

    real-frame decode stress   ~2,900 decodes/s, ~790/s FAILING on the
                               GENUINE short frame the hardware emitted,
                               interleaved with good frames at the rig's ratio
    inputload                  4 threads, uinput + LED reads
    2 browser-shaped tabs      video + audio ws, /state.json, /shot.jpg,
                               reviewing tab on /timeline.json

**What this arm has that no previous one did:**

- **Real byte churn.** 52.0 KB/frame, `distinct 90/90` — ABOVE the game's
  ~44 KB and far above console's ~17. Age eviction is freeing 52 KB objects
  thirty times a second. This is the axis the morning's ceiling said was
  "not the combination at reduced byte churn", and it is finally open.
- **The genuine artifact**, not constructed mutants.
- **All three load types at once**, which neither abort window ever lacked.

**What it still does NOT have, stated in advance:**

- **`inputload` is running at 27-29 ops/s, not the 57 it managed alone** — the
  Pi is saturated by the decode stress. **A null from this arm is a null at
  half the input throughput previously measured.**
- **No cells are running**, so there are no mode transitions. Every real
  malformed frame ever observed came from a cell boundary; here they are
  supplied artificially instead.
- **~790 failures/s against roughly 2 per cell naturally** — about five orders
  of magnitude above the natural rate. That is the point of a stress, but it
  means the *allocator pattern* is not the service pattern however well the
  good/bad ratio is matched.

**If it runs 55 minutes clean:** thousands of synthetic mutants did not abort,
and roughly 2.5 million real-frame failures did not either. **That is about as
strong a negative as this mechanism can be given** — and it is still a bound,
not an exoneration.

### RESULT: it ran clean. 2.69 million real-frame failures, no abort.

    daemon 519329, NRestarts 0, spawns 1, up 2h16m, zero abort lines

    real-frame arm   55.0 min   2,687,682 GENUINE malformed-frame decode
                                failures (814/s), interleaved with
                                7,215,513 good decodes
    input arm        53.6 min   92,009 ops, 0 errors
    browser arm      55.1 min   72,207 video frames, 329,976 audio chunks,
                                1,029 shots, 1,029 lastgood, 253 timelines

Load average north of 7 on a 4-core box for the whole window.

**THE MALFORMED-FRAME HYPOTHESIS IS AS CLOSE TO SETTLED AS IT CAN GET WITHOUT
A CORE.** Thousands of synthetic mutants, then 2.69 million real ones, plus two
genuine truncated frames that reached Pillow through the live system during
actual cells. **A decoder overrunning on a malformed frame is not what aborted
that daemon.**

**AND THE COMBINATION IS FINALLY TESTED** — cells-shaped input load, browser
clients and heavy decode simultaneously, which is what was present at both
aborts and had never been run together. **Both aborts happened at 32 and 28
minutes under a load LIGHTER than this one in every dimension we can measure**,
which makes "it is load-related" harder to hold, not easier.

**The model was wrong too, not just the mechanism.** `decode_errs` stayed at 1
for the entire 55 minutes — no new malformed frames, because **nothing changed
video mode**. Both frames ever observed came from a cell teardown, 11 and 13
seconds before exit. **That is a mode-transition artefact on a schedule, not a
rate.** "Malformed frames arrive at some frequency" was never the right model.

**What remains is the armed core.** It fires once and names the frame, and it
is worth more than any further stress either session can design. We have run
out of hypotheses that are cheap to test.

### The hunt of 2026-08-21: four arms, no reproduction

Run against one daemon (214909, `NRestarts 0`, target powered OFF):

    B  well-formed decode   ~3,800 decodes/s          85 min   4.1M decodes
    A  browser-shaped tabs  video + audio ws,
                            /state.json, /shot.jpg,
                            reviewing tab             50 min   65,700 video frames
                                                              299,983 audio chunks
                                                              3,868 polls
    C  input path           56.8 ops/s uinput
                            writes + LED reads      48.6 min   166,021 ops, 0 errs
    D  malformed decode     ~1,500 attempts/s,
                            ~700/s failing,
                            grow-dominant SOF         35 min

Track A logged **966 `http-err` over 50 minutes and every one is the
`/shot.jpg` 503** described above. Expected, not a fault -- recorded here so
nobody later reads the count as one.

**Not reproduced under any of them, alone or combined. This is a bound, not an
exoneration.** What it does NOT cover:

- **A live source.** The ring held the stick's uniform 14.7 KB no-lock
  constant, against 35-54 KB of real game screen. Age eviction was freeing
  roughly a third of the real churn, thirty times a second.
- **`/shot.jpg`**, which returns 503 on an all-duplicate window and
  short-circuits *before* any decode. Never exercised.
- **A real cell**, as opposed to cell-shaped input load: no mid-cell `shot` on
  a live frame, no OCR over real screen content.

### The leading hypothesis, and the measurement that will settle it

A glibc double-free is heap **metadata** damage — characteristically a write
past an allocation. The only third-party C extensions are Pillow's and
evdev's. **The daemon's entire frame validation is SOI + EOI + 128 bytes**, so
a garbled frame off the capture stick reaches `Image.open` in four places, and
**every decode failure is swallowed by a bare `except` with no counter.**

Frames pulled from a recorded AVI are **well-formed by selection** — they
survived being muxed — so a stress built on them cannot test this however fast
it runs. Two measurements that shape the fuzzing:

- **Pillow refuses extreme SOF growth** (`DecompressionBombError` above
  ~178.9M pixels), so those mutants never reach the decoder.
- **Shrinking a SOF makes the decoder read FEWER MCUs and stop early** — safe.
  **Growing it moderately (2x-8x) makes it fabricate output while its input
  runs dry**: a rewrite to 2560x1920 produced 4.9M pixels from entropy data
  for 307,200. That is the direction worth hammering.

`video state` now carries **`decode_errs`** and **`decode_last`**.

**THE PREMISE IS CONFIRMED AND THE MECHANISM IS LOOKING WRONG, 2026-08-21.**

Two malformed frames have now reached the decoder through the real system, and
both were caught:

    T1 / S2B    17:29:12   11 s before the cell exited
    YIELD / YA  18:57:16   13 s before the cell exited

**Both at the same point in a cell: the game tearing down 640x480 and DOS
returning to text.** Not gameplay, not the prompt. A VGA mode change alters
pixel clock, sync polarity and line count mid-capture.

**The artifact is SHORT, not corrupt.** Every marker valid and in order, SOF
correctly declaring 640x480, 23,636 bytes of entropy data, ending `ff d6` RST6
then a genuine `ff d9` EOI. Exactly what a capture device produces when it
flushes a partial frame at a mode change and terminates it cleanly.

**So the daemon's filter can never catch these.** SOI, EOI and >=128 bytes are
all satisfied by a frame missing half its picture. The gate is not wrong; it
checks properties this failure preserves.

**AND THE DAEMON DID NOT FALL OVER — EITHER TIME.** It decoded as far as it
could, raised, counted it, kept the bytes and carried on. The second time,
`/timeline.json` — the site that received it — was under continuous load from
two browser tabs. Together with a fuzz corpus of thousands of far nastier
mutants surviving the same call sequences without an abort:

**The malformed-frame hypothesis has a confirmed premise and a mechanism that
is looking wrong.** Malformed frames reach Pillow in normal operation, roughly
twice per cell, and nothing has ever come apart. **Do not read the confirmed
premise as support for the crash theory** — it is the opposite.

**FIRST LIVE-SOURCE READING, 2026-08-21.** Snapshotted before a restart
destroyed it:

    daemon 388273, up 22,419 s, state locked
    frames 672,372 total, ~37,000 of them live 640x480
    decode_errs 0    decode_last null    spawns 1    no abort

**Zero decode failures across ~37,000 real frames of live game content**, with
age eviction freeing 40-60 KB objects thirty times a second throughout — the
byte-churn condition present at both aborts, sustained for 22 minutes without
incident. Three measurement cells ran inside that window.

**This is a real null on the exact input the hypothesis is about, and the
hypothesis is looking weak.** It is not a week and it does not close the
question, but it is no longer "no data".

**A zero reading only counts with a LIVE SOURCE.** With the target off the
stick emits well-formed JPEG for its no-lock constant, so there is nothing
malformed to count and a clean counter means nothing. **Zero after a week of
real sessions kills the hypothesis; zero on a dark rig is an artifact.**

### Coordination hazard: a change-detector cannot tell an upgrade from a fault

`pi/inputload.py` and `roundR/run.sh` both halt on a MainPID change and print
"run pi/core.sh before anything else touches the box". **A deliberate restart
fires that banner verbatim**, and it lands in the log as a false reproduction
that outlives everyone's memory of the evening. Stop PID-watching runners
before any planned restart, and confirm they are down first.

### Ruled out

- **Not a bad deploy.** 167771 is byte-identical to 143721 and ran 73.6 min
  clean. The code did not change between an aborting run and a clean one.
- **Not `_read_pcm`/audio, on stack evidence.** There is only ONE stack dump —
  faulthandler was armed after the first abort — and the PCM reader is a
  permanent thread that sits in a healthy daemon's stack right now. The audio
  *streaming* path is still untested; the stack is simply not evidence for it.
- **Not `self._chg`.** Concurrent `/timeline.json?change=1` could raise
  `RuntimeError: dictionary changed size during iteration`. Real bug, now
  locked — but it raises, it does not corrupt.

---

## 2. `type_text` cannot produce a requested case  — OPEN

`vcctrl type` makes uppercase by holding SHIFT; **Caps Lock inverts SHIFT**;
and `at_prompt()` toggles Caps Lock as its probe. The case of everything the
harness types depends on a bit the prompt detector is flipping.

**Workaround, and it is mandatory: `FIND /I` for every DOS-side match.** A case
mismatch returns "not found", which is indistinguishable from a real absence.
Do not rely on the case of a `SET` *value* either.

**Proper fix, not done:** the daemon should read the LED and invert the shift
for letters.

---

## 3. `CLRENV.BAT` — the generator exists and the CARD never got it  — FIXED 2026-08-24

Including `SHOT_TICKS`, `BACKDROP_CACHE`, `BG_SUBREGION_BLIT`,
`PIN_NATIVE_MODE` and every `TAS_*`. **Any cell that runs without a reboot in
front of it inherits every lever any earlier cell set.** This invalidated a
whole eight-cell round: all eight ran with the patch under test stood down,
including the four meant to be stock, and every summary statistic looked
healthy because the comparison was empty.

**A refusal is not a rollback** — a cell that sets its variables and then
refuses at a later gate has still set them.

**Do not rely on enumerating levers.** The two guards that work:

    --forbid VAR        clears the name AND verifies it absent, positively,
                        after BLASTER has proven the pipeline
    --expect-log STR    requires the ENGINE to have reported this arm in its
                        own log, read off the card after the cell

`--expect-log` is the one that survives a lever nobody enumerated, because the
engine's log is downstream of the environment, the config file, the defaults
and every killswitch. **Give the control arm the same rigour as the treatment
arm** — forbid the *other* arm's lever, not just the ones that spoil the class
of measurement.

**MEASURED ON THE CARD 2026-08-24, and this is the live half of the fault.**
`tests/qa/gen-clrenv.sh` was written, `CLRENV.BAT` in the repo was regenerated
to **189 names / 13,800 bytes**, and `--check` reports it up to date. **The card
still has the old one.**

    repo  tests/qa/CLRENV.BAT   13,800 bytes   189 names
    card  C:\DOSKUTSU\CLRENV.BAT  7,608 bytes  ~104 names, dated 8-20-26

Read by size rather than by name, deliberately — sizes OCR reliably on this
console font and names do not. The arithmetic corroborates it independently:
at ~73 bytes per name (two spellings, CRLF) 7,608 bytes is ~104 names, against
the documented ~102 of the stale file.

**The repo being fixed says nothing about the machine being fixed**, and the
generated file's own header says so: *"this file only reaches the card on a
populate"*. `install-qa.sh` populates, and it runs on the operator's laptop with
the CF physically mounted.

**There may be a path that avoids a card swap.** `C:\MTCP\FTP.EXE` is driven by
a response file (`PUT.RSP`), and mTCP's client supports `get` as well as `put` —
so a `GET.RSP` could pull the new `CLRENV.BAT` onto the card over the network.
**Not attempted.** It writes to a file every cell `CALL`s, and a truncated
transfer breaks every future cell, so it wants the operator's go-ahead and a
size check after.

**What limits the damage today:** `--expect-log` makes the engine attest its own
arm from its log, so a round using it is valid whatever `CLRENV` did. `--forbid`
clears and verifies named levers on top. The stale file is a missing layer of
defence, not an active corruption of rounds that attest.

### Resolved on the card, 2026-08-24

Written over the network rather than by a card swap. `C:\MTCP\GET.BAT` pulls
from the FTP server's `stage/` directory, so the file went
`stage/CLRENV.BAT` -> `C:\DOSKUTSU\CLRENV.BAT` with a `.BAK` taken first.

**Verified three ways, because no single reading on this console is worth
trusting:**

- **Arithmetic.** The `DIR` total reads 21,408, and 13,800 + 7,608 = 21,408
  exactly. A sum that reconciles cannot be produced by a lucky misread of one
  field, which is what makes it the strong witness. Both individual sizes were
  in fact misread (`13,800` as `13,808`, the same `0`->`8` as everywhere else).
- **Content.** `THRASH_CENTRE` reads `count: 2` in the new file and `count: 0`
  in the `.BAK`; `TAS_REPLAY` reads `count: 4`. All three match the repo
  exactly, and the `.BAK` difference proves the swap actually happened rather
  than the file merely looking right.
- **By eye.** The counts were read from the screenshots directly, because OCR
  dropped every one of them -- see 11 below.

**Two things that went wrong and are worth carrying:**

**`GET.BAT` ignored its second argument and overwrote `CLRENV.BAT` in place.**
The plan was to fetch to `.NEW`, verify, then swap, so the live file could
never be truncated. That safety never executed and nobody was told -- the
transfer simply landed on the real filename. **The `.BAK` is the only reason
this was safe**, which is an argument for taking the backup even when the plan
says you will not need it.

**The first "control" was not a control.** `THRASH_ROWS` was chosen as a name
known to be in both files; it is in neither, and returned `count: 0` from a
perfectly healthy read. A control has to be verified present, not assumed
present, or a genuine zero looks like a broken pipeline.

A generated `CLRENV` is owed from the doskutsu side and rides the next
populate. Until it lands, the guards above are the only protection.

---

## 4. Ring dumps are bounded by the ring, not the cell  — MOSTLY FIXED

`GMQ3-glass.avi` shared **18,871 of 19,976 frame packets with `GMQ2`** — 94.5%,
byte-identical. Its window opened twelve seconds after GMQ2's. An analysis
found real game frames in a file named GMQ3 that belonged to MQ2, and produced
a confident wrong refutation.

Cause was the ring overrunning `target_span_s` (480 s asked, 1193 s held)
because eviction ran on bytes and not age. **Fixed** — but the habit still
matters:

- **Record with an explicit seq range** and log `first_seq`/`last_seq` beside
  the file. `vcctrl record --out F.avi <a> <b>`.
- Check `first_seq` against neighbouring cells before treating a recording as
  evidence. One pass over the `.meta` files catches this for a whole archive.
- `written/fps` well under `span_s` means the ring **thinned**, so AVI time is
  not wall time in that file and any t-to-wall arithmetic on it is wrong.
  (A good file looks like `RGA-glass.avi`: 11 repeats in 6,301 frames.)

**BUILT AND WIRED, 2026-08-23.** `buffer_avi(since=, clip=)` refuses a window
that opens before the caller's own start time, naming how many frames are
foreign and by how long; `--clip` takes only your own and reports
`clipped_frames` so a shortened recording says so. Exposed as
`vcctrl record --out F.avi [--since T|now] [--clip]`, refusing with exit 4.

**It shipped undiscoverable.** The daemon had it, the client parsed it, a test
covered it — and the usage text mentioned neither flag, so nobody reading
`vcctrl` help would learn the guard existed. **A guard nobody is told about is
a guard nobody uses**, which is the same family as a check whose output nothing
consumes. Documented 2026-08-23.

**USE IT.** `--since now` at the start of a run, keep the value, pass it to
`record`. Without it a dump is bounded by the ring, not by the run.

---

## 5. A new websocket streams at 20 fps until told otherwise  — BY DESIGN, KNOW IT

A page intending 8 fps still pulls 20 fps of ~35 KB frames on connect.
Harmless on an idle rig; **during a 150 s timed cell it is ~700 KB/s of
unbudgeted work on the machine doing the measuring.**

**Do not attach a streaming client during a cell, and do not leave a KVM tab
open.** An open tab is not only a ring-pin hazard, it is load.

Two clamps are now echoed rather than silent: asked 40 → 30, asked 0.2 → 1.0.

---

## 5b. THE VGA SPLITTER — what it costs, if it is used again

**Fitted and removed 2026-08-21. NOT currently in the path.** Measured
properly while it was in, so the next person does not have to.

Measured on **identical content** — a screen reproduced from a pre-splitter
capture, not whatever happened to be showing. First attempt compared a full
PGSB boot log against a near-empty-prompt baseline and read 55.2 KB/frame
against ~17: an alarming number that meant nothing.

    state            lit px   mean-lit   edge   noise
    no splitter        2063      174.5   96.1    0.07
    WITH splitter      2063      108.9   65.2    0.07
    after removal      2063      174.5   97.9    0.07

**Amplitude and sharpness recovered 100% on removal, and the lit pixel count
is identical at all three points.** The splitter causes it, and it is fully
reversible.

### What it costs

    signal amplitude   -38%   (mean lit brightness 174.5 -> 108.9)
    edge sharpness     -32%   (mean edge step 96.1 -> 65.2)

A passive splitter halves the drive into two loads. **It ATTENUATES rather
than DISTORTS** — identical geometry, no added noise — which is the difference
between a splitter you can live with and one that quietly corrupts what the
rig sees.

### What it does NOT cost — checked, not assumed

    lock                  locked, spawns 1, last_error null
    distinct / n          90 / 90    healthy analog source
    bytes/frame, matched  ~17.0 -> 17.6 KB
    OCR + read_count()    'count: 1' reads as 1 -- ARGUABLY CLEANER
                          (straight quotes where the pre-splitter image
                          produced curly ones)
    decode_errs           no new errors across ~75k post-fit attempts

**The attestation pipeline survives it.** Every arm attestation in every round
depends on OCR reading a `count:` line off the glass, and at -38% amplitude
that was the likeliest thing to break. It does not.

### THE HAZARD: brightness is discontinuous across the boundary

**Round R's game cells read 24.6. The same content reads dimmer with the
splitter in.** A pre-splitter brightness and a with-splitter brightness are
**different instruments wearing the same units**, and nobody reading a table in
three weeks will know the cable changed.

No threshold is at risk — `_is_picture` needs a range of 4 and text still
spans ~109 — but comparing the *numbers* across the boundary is invalid.

**If it goes back in: re-baseline every brightness figure, and say in the
writeup which side of the swap each number came from.**

---

## 6. Volume measures cannot see the signal die  — INSTRUMENT HAZARD

A stress harness logged **a flat 111 fps straight through the target losing
mains.** The stick keeps emitting at 30 fps; the frames merely become uniform
and tiny. Frames per second, socket liveness, spawn count and
`last_frame_age_s` all survive the source dying.

**Use a content measure**: `framestats` distinct-count, per interval, never
cumulative.

    distinct ~= n    a real analog source (90 of 90 on a static DOS prompt --
                     sensor noise makes every frame differ)
    distinct ~= 1    no lock; the stick's own constant, frames still arriving
    n == 0           starved pipe

Measured both arms: target off gives 1 distinct at 14.7 KB/frame; live 640x480
gives distinct ≈ n at ~35 KB/frame. **The volume columns are identical between
those two rows.**

---

## 7. A cell inherits whatever boot profile it finds  — GATED

NET and the sound profile are **indistinguishable from the harness**: same
prompt, same `at_prompt`, same screen. A cell died with
`sdl_init failed: No BLASTER environment variable` because the machine was
still in NET from a hand-run FTP transfer an hour earlier.

`vcctrl-cell` now refuses unless BLASTER is set. **After any hand-run NET
session, reboot to the menu default** — ctrl-alt-del, let the 5 s timeout land
on PGSB.

BLASTER does double duty: boot-profile guard, *and* the known-present probe
that makes every absence check under it non-vacuous.

---

## 8. Mach64 capture regression  — NOT REPRODUCING, CAUSE UNKNOWN

For a period on 2026-08-20 no Mach64 game mode would lock. Since a reboot to
the default profile and a daemon restart it has locked on **every** attempt —
16 of 16 cells plus a 210 s recording at full 30 fps.

**What cleared it is not established.** Candidates: the reboot, the daemon
restart, the ring age-eviction fix. Nothing separated them.

If it returns: **try a reboot first.** That cheap experiment was never run,
because the fault was assumed to be the capture stick latching. See
`PLAN-NEXT.md`, which is marked superseded for exactly this reason.

---

## Cross-cutting rules earned the hard way

- **Never prove an absence on a pipeline that has not just proved a presence.**
  A pipeline carrying only questions whose wrong answer is silent has no way
  to tell you it is broken.
- **A check may not pass on an empty population.** Report the sample size
  beside every verdict.
- **Could-not-look is not a finding.** Keep three states, never two.
- **Attest from the thing that ran it**, not from what it was asked to run.
- **Prefer a check with no units and no glyphs.** A PID comparison caught a
  restart that a timestamped journal query missed entirely — integers carry no
  timezone, no locale, no font.
- **A null result is what comparing a thing with itself produces**, and it
  arrives dressed as a clean measurement.
- **A pre-registered wrong answer is worse than an unplanned one**, because the
  plan supplies the permission not to look again. When writing an outcome
  table, ask what a third state would look like.

---

## 9. The arm attestation polls blind through a mode transition  — FIXED 2026-08-24

**Twice now** an `--expect-log` attestation has refused with `COULD NOT READ`
on a cell that was fine: Phase 0's `D3` and the confirming round's `CX3B`. Both
times the identical `FIND` run by hand seconds later returned `count: 1`.

**Mechanism, from `grab()`'s own docstring:** it returns `(None, None)` for no
picture, and that includes *"a stream that is frozen -- frames arriving but all
byte-identical, which is what DOS text mode 03h produces"*.

**The attestation runs immediately after the game exits**, which is the
640x480-to-text mode transition — the same window both recorded decode errors
came from (11 s and 13 s before a cell exited). If the stick has not re-locked,
every `read_env_text_raw()` returns `None`, all 20 polls fail, and the cell
refuses.

**It fails SAFE** — an unreadable answer never becomes a pass, which is why
this has cost minutes rather than a round. But it is a check polling blind
through the one window where the thing it polls is least reliable.

**Fix, not yet applied:** wait for `video state == locked` before starting the
attestation polls, rather than beginning them the instant the game exits. Do
not simply raise the poll count — that treats a timing problem as a patience
problem, and 30 s is already 20x the transition.

**Do not change `vcctrl-cell` while a round is running on it.** A cell that
straddles a harness change is the same hazard as one that straddles a daemon
restart.

### Fixed, after the confirming round landed

`wait_video_locked()` in `vcctrl_common.py`, called once before the attestation
polls begin. **Three states, deliberately** — `True` locked, `False` asked and
never locked, `None` could not ask at all — because a refusal has to be able to
say whether the instrument or the target was the problem. The two failures look
identical from the read itself, so if the distinction is not made here it does
not exist anywhere.

**Waiting is not polling more.** The retry count is unchanged; what changed is
that the polls now start from a screen known to be readable.

**Not yet exercised against a live transition.** It returns `True` in 60 ms
against the healthy rig, and the unlocked and unreachable paths are covered by
`test_wait_video_locked_reports_which_of_three_things_happened` with invented
rigs — including a well-formed reply carrying no `state` field, which must be
`None` rather than `False`. **The real proof is the next cell that exits a game
into text mode**, and until one has, this is fixed in the sense that the code is
right, not in the sense that the fault has been observed to go away.

## 10. `started_rtc_local` is corrupt — PROVENANCE HAZARD, ANALYSIS ONLY

**The manifest's `started_rtc_local` field cannot be trusted, and every writeup
that cites it for provenance is citing a fabrication.**

Found 2026-08-24 while trying to establish which archived cells shared a
sitting. Two independent proofs from data already on disk:

    CX2B.LOG   started_rtc_local=2026-12-02T18:40:00
    CX2B.LOG   its own log lines read [08:54:56] ... [08:57:04]

The cell ran that morning. The manifest dates it to December. And the field is
not merely offset — it is **incoherent**:

    GPU0B   started_rtc_local=2026-11-28T21:25:03
    GPUAB   started_rtc_local=2026-11-28T21:25:41

**Thirty-eight seconds apart, for two cells that take 164 s each.** They cannot
both be true and they cannot overlap. Some cells carry a plausible date
(`GPU0` reads `2026-08-20`, `CX1A` reads `2026-08-24T08:49:10`, both correct),
so the field is *sometimes* right, which is worse than always wrong: it looks
reliable in exactly the spot-check a reader would perform.

**The in-log `[HH:MM:SS]` prefixes are the good clock.** They are coherent,
monotonic, and spaced like real cells:

    CX1A  08:49:10   CX2B  08:54:56   CX3B  09:00:42   CX4A  09:08:25

**What it cost:** nothing yet, but only by luck. A variance claim was one
message from entering the benchmarking standard, and the archive comparison
that killed it was **only possible after abandoning this field** — grouping
cells by `started_rtc_local` puts same-sitting cells in different months and
would have made the refuting comparison unbuildable.

**Rule:** for anything that depends on WHEN a cell ran — sitting membership,
ordering, drift, elapsed time — read the log-line timestamps. Treat
`started_rtc_local` as unverified until the RTC is understood.

**Not yet diagnosed.** Candidates: a dead CMOS battery resetting on cold boot,
a profile that sets the clock and one that does not, or the field being written
from a different source than the log prefixes. Cheap to investigate — the DOS
`DATE`/`TIME` at a prompt, against the Pi, on a warm and a cold start.

**This is an analysis-side fault, not a measurement one.** No fps figure
depends on it. Every conclusion keyed on ordering does.


## 11. The screen reader drops the line its caller wants  — FIXED 2026-08-24

**Tesseract's default page-segmentation mode omits the `count:` line from
`FIND /C` output, deterministically, 3 captures out of 3.** On the glass:

    C:\>FIND /I /C "THRASH_CENTRE" C:\DOSKUTSU\CLRENV.BAT
    ---------- C:\DOSKUTSU\CLRENV.BAT
    count: 2                        <-- read by a human, dropped by the OCR

The default is PSM 3, which analyses page layout; it reads the command line and
the dashed header, then stops. PSM 6 -- *"a uniform block of text"* -- recovers
it every time. That is not luck: **a DOS console IS a uniform block of
monospaced text**, and PSM 3's newspaper-column heuristics have nothing to work
with.

**Why the existing retry loop could never have saved it.** The attestation
polls twenty times. Twenty identical reads of an identical frame reproduce an
identical omission — **retrying is only a fix for flakiness, and this is not
flaky.** The cell then refuses with `COULD NOT READ` against a screen anyone
could read at a glance. Same shape as 9, different cause, and it would have
been diagnosed as 9.

**Fix:** `read_env_text_raw(psm=...)`, with both polling loops alternating —
even tries use the default, odd tries use PSM 6. **A fallback, not a swap.**
Every read that works today keeps its exact behaviour on the first try;
changing the mode globally under readers that are currently fine is a larger
risk than the bug. Covered by
`test_psm3_drops_the_count_line_and_psm6_recovers_it` against a real capture,
and that test asserts the DEFAULT still fails — if PSM 3 ever starts reading
the fixture, the fixture has stopped demonstrating anything and the test says
so rather than passing on both branches.

**How it was found:** by looking at the screenshot after the OCR returned
`None` for a reading that had a plainly visible answer. The harness has eyes;
the OCR is one instrument and not the only one.
