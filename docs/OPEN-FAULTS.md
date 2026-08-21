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
names to timestamps, no `free`). **What is not ruled out:** the websocket
frame streamer, which hands ring bytes to SSL from a thread that is not the
capture reader. Evidence is two dirty windows (32, 28 min, browsers connected)
against one clean 55-minute window and one clean 12.2-minute stress. **Neither
is a controlled test and nobody should present them as one.**

**A cell that straddles a restart still finishes and writes a plausible
number** — one did, 122,485 bytes. `roundR/run.sh` pins the daemon's MainPID
and refuses to continue across a change. Keep that guard in any new runner.

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

## 3. `CLRENV.BAT` clears 205 names; ~97 engine levers are not among them  — OPEN

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

**Not yet built:** a `buffer_avi` refusal when the window starts before the
caller's own start time. That would make provenance structural instead of
careful.

---

## 5. A new websocket streams at 20 fps until told otherwise  — BY DESIGN, KNOW IT

A page intending 8 fps still pulls 20 fps of ~35 KB frames on connect.
Harmless on an idle rig; **during a 150 s timed cell it is ~700 KB/s of
unbudgeted work on the machine doing the measuring.**

**Do not attach a streaming client during a cell, and do not leave a KVM tab
open.** An open tab is not only a ring-pin hazard, it is load.

Two clamps are now echoed rather than silent: asked 40 → 30, asked 0.2 → 1.0.

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
