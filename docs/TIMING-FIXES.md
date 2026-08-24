# The three timing bugs: what is fixed, what is proven, what is still open

Written 2026-08-19, after measuring the LED channel exposed three bugs in
`vcctrl-collect`. Code fixes landed in `6394fd9`. **Landing a fix and proving
it are different things**, and for this class of bug the difference matters
more than usual: all three returned the correct answer in the first case
anyone would try. A fix that is merely plausible is not obviously better than
the bug.

This document is the plan to get each from "fixed" to "proven".

## The shared root cause

**Every `vcctrl` call is an ssh round-trip costing about 1.5 s** (measured
1.52-2.55 s). The PS/2 LED round-trip is much faster than that. So the harness
is dominated by its own instrumentation cost, and any loop written as though
the calls were free is wrong by roughly a factor of three.

An apparent "3.14 s LED latency" was two ssh calls of polling overhead being
misread as device behaviour. That misreading is the same shape as the frame
comparison bug in FINDINGS sec. 15 -- an instrument's own cost mistaken for
the signal -- and it is worth expecting a third instance somewhere.

---

## Bug 1 -- menu selection cadence

**Was:** digit and Enter sent as two calls with an LED poll between them.
About 5 s per attempt, against a boot menu that times out in 5 s. It would
have worked about half the time, and the failure looks like a healthy machine
on the wrong profile.

**Fix:** one call (`vcctrl key 5 enter`), no LED polling inside the window.

**Status: PROVEN.** Measured 1.76 s apart, ~2.8 attempts inside a 5 s window.
Exercised on hardware 2026-08-19: NET selected blind from a cold menu, and
confirmed *by reading the machine* -- `PKTTOOL scan` reported ODIPKT at 0x7E
with MAC 00:00:5E:00:53:02 -- not by assuming the keystroke landed.

**Residual issue, NOT yet fixed:** the spam loop overran its budget badly --
26 attempts across 71 s against a 40 s window. Individual calls blocked far
longer during POST than the 1.76 s measured at idle. Consequences: the machine
takes keystrokes long past the menu, which is what produced the burst of PC
speaker beeps the operator heard (buffer full, one beep per rejected key), and
the "Bad command or file name" flood at the prompt.

  - [x] Bound the loop by attempt count as well as wall clock. Now capped at
        12 attempts AND 20 s, down from an unbounded 40 s window. The 20 s
        comes from the measured shape of a reboot: caps-clear edge, then
        RDYPULSE about 12 s later, with the 5 s menu somewhere inside.
  - [ ] ~~Stop early once RDYPULSE has fired~~ **Rejected.** Reading the LEDs
        between attempts costs another 1.5 s call and pushes the cadence back
        out toward the 5 s window it must beat -- reintroducing bug 1 to save
        a few seconds. The attempt cap solves the overrun without touching
        the property that made the fix work.
  - [ ] Measure how long a `key` call actually takes *during* POST. Still
        unknown, and every timeout in the harness is guessed against it.

## Bug 2 -- `arm_leds()` slept instead of waiting

**Was:** toggle a lock key, `time.sleep(0.5)`, re-read. The re-read is its own
ssh round-trip, so the whole thing raced. It reported failure on a machine
that had toggled correctly a moment later -- and because `arm_leds()` gates
the reboot, that failure aborts collection on a perfectly healthy machine.

**Fix:** wait for the toggle to come back (`wait_led`, 15 s bound) instead of
sleeping a guessed interval.

**Status: SMOKE-TESTED ONLY.** Called once directly, returned True, caps went
0 -> 1. That exercises the path where the LED was *already* wrong and needed
one toggle. It does not exercise:

  - [ ] The adverse starting state -- caps=0 AND scroll=1, so both keys need
        toggling. Force it by hand and confirm arming succeeds. **Queued for
        after RB.**
  - [ ] The genuine-failure path -- a machine that will not toggle at all
        should fail the 15 s wait and return False, not hang. Test by arming
        against a machine that is at a *game* rather than a prompt, where
        SDL3 owns INT 09h and the LEDs will not move. **Queued for after RB.**
  - [ ] That 15 s is enough. It is three to ten calls' worth, which looks
        generous, but nothing has measured the worst case.

## Bug 3 -- stale LED levels across a power cycle

**Was:** `/sys/class/leds` on the Pi retains the last state the host
published, and a powered-off host publishes nothing. After `vcctrl power on`
the LEDs still read the pre-shutdown values -- so a level check for
"scrolllock is 1" **returned ready 2.5 s after power-on**, on a machine that
had not begun to POST.

This is the worst of the three. It is correct whenever the previous run ended
any way other than at a ready prompt, so it survives most tests worth writing.

**Fix written:** `wait_cold_boot()` waits for POST to *clear* the LED first,
proving the reading belongs to this boot, and only then for RDYPULSE to set
it. An edge pair cannot be fooled by a stale level.

**Status: NOT WIRED IN, THEREFORE NOT FIXED.** `wait_cold_boot()` is defined
and called from nowhere. Both tools simply refuse when the machine is off and
tell the operator to run `vcctrl power on` themselves -- which is precisely
the improvised path where this bug bit, and it bit the harness author rather
than a user. Writing the guard and leaving it unreachable is not a fix; it
just moves the bug out of the file you are reading.

  - [x] Add `--power-on` to `vcctrl-sweep` and `vcctrl-collect`: power on,
        then `wait_cold_boot()`, instead of refusing. Landed as
        `vcctrl_common.ensure_powered()`; the cold path is now code that is
        reviewed and reused rather than retyped at a prompt each time.
  - [ ] Prove it against the exact trap: leave the machine powered off with
        scrolllock latched at 1 (which is the *normal* state after a clean
        shutdown, since RDYPULSE is the last thing that runs), then power on
        and confirm the wait does not return until the machine is genuinely
        at a prompt. This is the one test that distinguishes the fix from the
        bug; without it there is no evidence either way.
  - [ ] Consider having `vcctrld` expose LED *staleness* -- a timestamp of
        the last host update -- so the level can be self-describing rather
        than needing an edge dance to be trustworthy. This is the real fix;
        the edge pair is a workaround for an instrument that cannot say "I do
        not know".

## Bug 4 -- `at_prompt()`, found by the audit this document called for

Writing the rule below and then grepping for it immediately turned up a fourth
instance, in **both** tools:

    before = leds()
    vc("key", "capslock")
    time.sleep(1.0)          # <-- racing an ssh round-trip
    after  = leds()

Identical in shape to bug 2. It has been passing -- 4/4 in direct testing --
but only because the two `leds()` calls bracketing the sleep add about 3 s of
real time on their own. The sleep was never doing the work; the instrumentation
overhead was, by accident. Change the transport to something faster and this
starts failing.

That matters because `at_prompt()` gates everything: a false negative refuses a
sweep, and refuses collection, on a machine that is perfectly healthy.

**Fixed** in both tools -- toggle, then wait for the flip with an 8 s bound,
restore, wait again. Faster in the success case too, since it returns as soon
as the LED moves rather than always paying the full sleep.

**Status: not yet exercised.** It is on the same list as bug 2:

  - [ ] Confirm it still returns True at a live prompt. The running RB sweep
        exercises the OLD copy -- it loaded before the fix -- so this needs a
        run of its own.
  - [ ] Confirm it returns False, without hanging, against a machine in-game
        where SDL3 owns INT 09h and the LEDs cannot move.

---

## The general rule this session earned

Three separate bugs, one mistake: **a fixed sleep where a closed loop
belonged.** The harness already knew this -- "the harness has eyes, so it
should look rather than count" (FINDINGS sec. 9) -- and the rule was applied
to the screen while the LED channel kept getting fixed delays.

Anything in this codebase that sleeps a constant and then reads is a latent
instance of the same bug. The grep for `time.sleep(` followed by a read is
what produced bug 4 above, within a minute of writing this line down. The
remaining hits are all in the launch sequence (`CD \DOSKUTSU`, `QA n`,
`SET DKTCAP=1`), where the sleeps pace typing into DOS rather than wait for an
observable state -- those are legitimate, but they are also unverified, and
each one is a place where a slow machine desynchronises the harness silently.


---

## Bug 5, in a sense -- the helpers existed twice

The reason bug 4 was in two files is that `vc`, `vc_json`, `leds`, `wait_led`,
`at_prompt` and `arm_leds` had all been written once and copied. A fix to one
copy left the other wrong, indefinitely, because the wrong one kept working.

All six now live in `bin/vcctrl_common.py` and both tools import them. Each is
defined exactly once, verified by grep. That is the actual fix for bug 4 --
closing the loop in `at_prompt` twice would have left the same trap set for
whoever writes the third tool.

Landed alongside: `--power-on` on both tools, a `BrokenPipeError` guard (line
buffering made `| head` produce a traceback), and a `KeyboardInterrupt` guard
that says the target was left untouched, since the natural worry on Ctrl-C
mid-sweep is what state the machine was abandoned in.


---

## Bug 6 -- arrival is not readiness  [found on the first real collection run]

The first end-to-end `vcctrl-collect` run brought all four RB log pairs back
correctly and then printed:

    == return to the menu default ==
      WARNING: could not arm LEDs; skipped the return reboot.

**leaving the machine sitting in NET** -- the one profile a measured run must
never start from. `arm_leds()` reproduced fine a minute later (1.5 s), so it
was not broken; it was called at the wrong moment.

The cause is a conflation in `put_tag()`. It confirms a transfer by watching
the file appear on the FTP server, which is the right signal *for the
transfer* and was a deliberate choice over reading the screen. But at the
instant the file lands, the DOS side is still **inside `FTP.EXE`** -- which
has yet to print its summary, quit, return to `PUT.BAT`, echo, and drop back
to a prompt. The next keystroke therefore went into the BIOS buffer rather
than being processed, and the arming wait timed out on a healthy machine.

**Arrival proves the transfer. It says nothing about readiness.** Two
different questions; one check was being asked to answer both.

Fixed with `wait_for_prompt()` -- distinct from `at_prompt()`, which asks once,
because here the machine is *known* to be busy and the job is to wait it out.
The collector now waits for the prompt before the return reboot, and if it
never comes, says explicitly that the machine is in NET and must not be used
for a measured run.

Worth noting what this bug is not: it is not the fixed-sleep family. A settle
delay would have papered over it. It is the same family as the LED level
check -- **a signal that means something adjacent to what it is being read as**
-- which is now two instances, and probably the more dangerous pattern of the
two, because the reading is always plausible.


## Bug 6a -- RDYPULSE has a tail

Immediately after the bug 6 fix, rebooting the machine out of NET produced:

    reboot edge t+27.7s
    RDYPULSE t+12.3s
    at_prompt: False

but a screen capture showed the machine sitting at `C:\>` under
`[PGSB] ready.`. The probe was a **false negative**.

Same root cause one layer down. **RDYPULSE means AUTOEXEC finished, not that
COMMAND.COM is reading input.** There is a short tail -- the remaining AUTOEXEC
lines and the return to the shell -- and a probe fired inside it gets its
keystroke buffered rather than processed.

`reboot_into_net()` had been covering this with `time.sleep(SETTLE_S)`, which
is the guess-an-interval mistake from bugs 2 and 4 wearing a different name: it
happened to be long enough there and was not long enough in the ad-hoc path.
Replaced with `wait_for_prompt()`. That was the last fixed sleep in the
readiness path; the only `time.sleep` left in either tool is the poll interval
on a *local* filesystem check, which costs nothing and is not racing anything.

**A second defect, found while explaining the first:** `at_prompt()` did not
restore Caps Lock on its failure path. But the failure path is usually a
BUFFERED keystroke, not a lost one -- DOS processes it a moment later and the
LED flips after the probe has given up. So a failed probe left the LED
inverted, silently corrupting the state the next probe reads. It now restores
either way. Verified: three consecutive probes return True in 7.8 s each and
leave the LEDs exactly as found.

### The tally

Six bugs, three families:

  * **guess an interval** -- bugs 1, 2, 4, 6a. Fixed sleeps and cadences
    racing an ssh round-trip nobody had measured.
  * **read a signal as something adjacent to what it means** -- bug 3 (a
    stale level read as current), bug 6 (arrival read as readiness), 6a
    (AUTOEXEC-done read as prompt-ready). The dangerous family: the reading
    is always plausible and often correct.
  * **the same code in two places** -- bug 5, which is why bug 4 existed
    twice.

All six were found by measuring the instrument rather than by a failure in
the field. Only bug 6 cost anything real, and what it cost was a machine left
in the wrong profile -- caught by its own warning.
