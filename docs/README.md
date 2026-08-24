# docs — what is here and which of it is current

Twenty files, written over a campaign rather than designed. This says what each
is for, and — more importantly — **which numbers in them have been retracted**,
because several documents carry a live figure and a withdrawn one in the same
section.

---

## Start here

**`OPEN-FAULTS.md`** — the forward-looking register: what is still broken, what
is worked around rather than fixed, and what to check before trusting a result.
**Read this before running anything measured.** `FINDINGS.md` records what
happened; this one records what will bite you.

---

## The Mach64 30-fps campaign, in reading order

Each answers the question the previous one raised.

| doc | question | headline |
|---|---|---|
| `MACH64-PHASE0-RESULTS.md` | what is a frame made of? | the flip is **17.8%** of it; VRAM write 4.77 ms |
| `MACH64-PHASE0B-RESULTS.md` | what is the other 82%? | the tilemap is **49.8%** — half the frame |
| ↳ same file, TAUD + yield pair | where does audio go? | **2.35 ms**, and 61% of it lands in `fb_yield` |
| `T1-K-PREDICTION.md` | pinned before the data | direction right, magnitude wrong 5x |
| `T1-TILEMAP-RESULTS.md` | do the tilemap levers help? | +1.00, +0.50, and **−9.35** |
| `T1-CONFIRM-RESULTS.md` | do they reproduce, and do they add? | dword **ships**; asm **unpinned**; and the band was never capable |

**Current perf baseline: 27.6 fps** (Round R, eight cells, A-to-A spread 0.10).

### RETRACTIONS — do not quote these

- **"±0.1 fps within-session repeatability"** — quoted all week, used as the
  T1 confirming round's acceptance band, and it is **integer truncation in the
  metric** (`main.cpp:1530`), not measurement noise. The rig's real same-config
  pair spread is **0 to 16 flips**, and 0.1 fps is 10.3 flips — **narrower than
  the repeatability it was gating.** See `T1-CONFIRM-RESULTS.md` and
  `HARNESS-STANDARD.md` 10.0e. Express a band in counted units and check it
  against the archive before running.
  **And do not quote the 16 as the rig's repeatability either** — it is a
  *pair* spread. The accumulated stock population, nine cells over three
  sittings, spans **31 flips (0.30 fps)**, `ACC1` 2827 to `FRS1` 2858. Pair
  spread and population range are the two bands of `HARNESS-STANDARD.md` 10.0;
  say which one you are reading against. `PI5-MIGRATION.md` sec. 6 carries the
  amendment.
- **`ASM_BLIT` "+0.50"** (`T1-TILEMAP-RESULTS.md`). It did not reproduce: the
  confirming round gives **+0.389** against T1's **+0.486**. The lever is real
  (2.5x the noise floor) but its **magnitude is unpinned**, and it adds at most
  a fifth of that on top of `TILE_DWORD_COPY`.
- **`started_rtc_local`** — corrupt in the manifest; never use it for ordering
  or sitting membership. `OPEN-FAULTS.md` §10.

- **"30.2 fps with audio off"** (`MACH64-PHASE0B-RESULTS.md`). A *diag* number.
  The transferable quantity is the **2.35 ms delta**, which applied to the
  clean 27.6 gives **29.5 fps — short of the target.**
- **`stationary_frac` mean 0.11** (`T1-TILEMAP-RESULTS.md`). A **block-size
  artifact**: the same reel at ~10 ticks/block gives 0.004 against ~285
  ticks/block giving 0.11. **The median 0.00 is the defensible statement.** The
  ~0.26 fps adaptive-gate estimate built on it goes too.
- **"no usable Mach64 glass since MQ2"** (`FINDINGS.md` sec. 36). MQ3's file was
  94.5% the *previous* cell's frames. Corrected in place.

**Every one of these was live in a pushed document before being caught.** That
is why this section exists.

---

## The harness, and how it has been wrong

**`FINDINGS.md`** — 39 sections, measured rather than reasoned. The recurring
shape is worth knowing before you read any of it:

> A check that returns the reassuring answer when it is broken.

Sections 31, 35, 36, 37, 38, 39 are all that family: absence read as a value; a
check passing on an empty population; a dump bounded by the ring rather than
the cell; Caps Lock inverting every typed character; a journal query nine hours
in the future returning "No entries"; eight cells silently comparing a
condition with itself.

- **`CLI-PARITY.md`** — the `vcctrl` verb surface and why each verb refuses.
- **`MOUSE.md`**, **`TIMING-FIXES.md`** — input path and settling races.
- **`PI5-MIGRATION.md`** — the Pi 3 → Pi 5 move, closed.

## The standard

**`HARNESS-STANDARD.md`** — the contract for running measured tests on
constrained hardware when an agent, not a person, is reading the run sheet.
Target-agnostic: it states requirements, and the project-specific parameters
live in a profile rather than in the standard. Imported 2026-08-23 from the
doskutsu campaign, where it was written; vcctrl is its reference harness
implementation. **Read it before adding a measurement to anything here.**

## The KVM (webkvm session's component)

`WEBKVM.md` is the main one. `WEBKVM-DESIGN.md`, `WEBKVM-AUDIO.md`,
`WEBKVM-BOARDS.md` and `WEBKVM-SCRUB.md` are its subsystems.

## Hardware facts

`BOARD-IDENTITY.md`, `VIDEO-SWAP.md`, `SOUND-PROFILES.md`,
`PICOGUS-CONSOLIDATION.md`.

## Plans

**`PLAN-NEXT.md`** — items 2 and 3 are now done or half-done; item 1 (the
capture regression) is **marked superseded**, because it stopped reproducing
and its evidence table had a row that reported an instrument's state as the
target's.

---

## Three rules that came out of the campaign

**Never prove an absence on a pipeline that has not just proved a presence.**
A pipeline carrying only questions whose wrong answer is silent has no way to
tell you it is broken.

**Attest from the thing that ran it, not from what it was asked to run.** An
engine that states its own configuration is a witness; an environment is a
request. This caught an eight-cell round that had silently compared arm B with
arm B.

**A large bucket licenses a characterisation, not an optimisation.** Milliseconds
cannot distinguish bandwidth-bound from overhead-bound, which is how a cleanly
measured 4.77 ms left a decision unmade.

# Planning docs live in internal/, which is gitignored. See CLAUDE.md.
