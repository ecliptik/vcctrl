# Video card swaps: ViRGE, Mach64, Cirrus

> This document is one project's record of its own physical system -- specific hardware, specific findings, not a general reference. See `docs/HARNESS-STANDARD.md` for the target-agnostic contract this system implements.

Written 2026-08-19. **Current card: S3 ViRGE/DX**, which is what every banked
fps anchor was measured on. Mach64 and the onboard Cirrus are both in the
parts pile and both untested under capture.

Split of labour, per the operator: **they do the physical swap and power-on;
the harness drives everything after that**, closed-loop. That line is drawn
where the harness physically cannot cross it, which is the right place.

## Why this is not just "swap and carry on"

The video card is not a peripheral in this workload. Round N accounted for the
POD's 32.8 ms frame as tilemap 21.05 + flip 4.55 + sim 2.39 + HUD/post 0.94 --
so **roughly 78% of the frame is the video path**. A card swap is not expected
to preserve fps; it is expected to move it, and by a lot. Every comparison
across a swap is a different experiment, not a repeat of the old one.

## The uvconfig workflow

Operator's current manual sequence:

    swap card -> boot DOS -> CD \UNIVBE -> UVCONFIG -> space -> space

### Rehearsed on the ViRGE 2026-08-19, and the premise was wrong

The whole procedure was run end to end on the card already fitted, before any
swap, so that a failure would be attributable to the tooling rather than to
new hardware. Result:

    == uvconfig ==
      back at a DOS prompt -- uvconfig completed without asking anything.
    == reboot ==
      reboot edge at t+28.2s ... prompt live ... capture still locked

**UVCONFIG IS NOT INTERACTIVE ON AN UNCHANGED CARD.** It ran silently,
rewrote `UNIVBE.DRV` and `UVCONFIG.DAT` in about two seconds, and returned
straight to the prompt. The remembered "press space, press space again (I
think)" did not reproduce at all.

The likeliest reading -- and it is a reading, not a measurement -- is that
those prompts appear only when the DETECTED card differs from the stored
config. That is exactly the case a swap creates and exactly the case a
same-card rehearsal cannot produce. The operator's "(I think)" was the honest
signal that the sequence was uncertain; it turns out to be *conditional*
rather than fixed, which is worse than uncertain for anything driven blind.

So `bin/vcctrl-uvconfig` sends **no key sequence at all**. It runs uvconfig
and then looks: back at a prompt means nothing was asked and it continues; a
screen still showing means STOP, capture, and hand it to a human.

**UVCONFIG IS CONDITIONALLY INTERACTIVE, WHICH MAKES IT THE WORST KIND OF
THING TO AUTOMATE BLIND.** The harness's own rule (FINDINGS sec. 9) is that anything
driven by a fixed number of keypresses is a latent bug -- menus wrap, a hold
can advance two rows, and a greyed entry may or may not be skipped. "Press
space, then I think space again" is exactly that shape, and the operator's own
"(I think)" is the tell.

So: **capture between every keystroke, confirm the screen, then press.** Never
send the pair as a sequence. The cost is a few 40-second captures per swap,
against a step performed maybe three times.

### Does uvconfig need a reboot to take effect?

**Answer: assume yes, a reboot is required.** The evidence is the directory
listing, pulled off the card 2026-08-19:

    UNICENTR EXE  227,428  08-23-02      original 2002 binaries
    UNIVBE   EXE  112,830  08-23-02
    UVCONFIG EXE  457,724  08-23-02
    UVCONFIG DAT    9,392  08-17-26   <- written by the last uvconfig run
    UNIVBE   DRV   18,661  08-17-26   <- written by the last uvconfig run

**`uvconfig` does not configure a running driver. It GENERATES one** --
`UNIVBE.DRV`, plus its own `UVCONFIG.DAT` state. `UNIVBE.EXE` is the TSR
loader and reads that `.DRV` **when it loads**, from `AUTOEXEC`. A TSR already
resident is running the previous driver, and nothing about writing a new file
on disk changes that.

What is NOT established: whether `UNIVBE.EXE` in this build can unload and
reload in place, which would avoid the reboot. `UNIVBE.EXE -?` printed nothing
usable, and the bundled `README.TXT` is a supported-chipset list, not a
manual. Probing further means running the TSR's uninstall switch on a live
machine, which is not worth doing speculatively.

**So the workflow should reboot, and not because the reboot is expensive --
because the failure mode of skipping it is silent.** A sweep run against a
stale video driver produces numbers that look entirely normal and are wrong,
on the subsystem that is ~78% of the frame. 43 seconds via Ctrl-Alt-Del buys
certainty. If someone later proves an in-place reload works, that is a nice
optimisation for probing; it should not be trusted for a measured run without
the equivalence test the PicoGUS plan asks for.

## The provenance hole, which is worse than the reboot question

`RB.BAT` line 58:

    ECHO video=S3 ViRGE (match the banked anchors) >> LOGS\%QAM%RB.NFO

**Hardcoded.** Swap in the Mach64 and RB's manifest still says ViRGE --
confidently, in the file whose entire job is to say what produced the numbers.
This is the same defect class the `config=%config%` witness was added this
morning to close, sitting three lines below it.

The other sweeps say `video=whatever is in the box`, which is honest but
carries no information.

**Fix, and it should land BEFORE the first swap, not after:** record what
UniVBE *detected* rather than what someone typed. UniVBE prints the chipset it
identifies at load. Capture that into the manifest the same way the pgusinit
proposal captures card mode:

    C:\UNIVBE\UNIVBE.EXE >> LOGS\%QAM%RB.NFO

A detected chipset attests hardware. A hardcoded string attests only that
nobody edited the file since the last swap -- and after three swaps, nobody
will have.

This is the peer's file. Flagged to them, not edited.

## Per-card unknowns

Nothing below is knowledge; it is the list of things a swap has to establish.

**S3 ViRGE/DX** -- current, baseline, capture locks on mode 12h and on the
game's 320x240. This is the only card with any capture evidence at all.

**ATI Mach64** -- the card with the actual defect worth chasing: it produces a
**512x384 mode that has never been characterised**. Unknown whether the
capture stick locks onto it; the stick is 60 Hz-only firmware, and 512x384 at
an unknown refresh is exactly the case that returned flat black for text mode
03h. If capture does not lock, that mode can only be investigated with a human
watching the monitor -- worth knowing before planning a session around it.

**Cirrus (onboard)** -- never brought up under capture. Additional unknown: how
it is enabled. Fitting a PCI card usually disables onboard video via BIOS or a
jumper, so reaching the Cirrus may require removing the PCI card entirely
rather than just selecting it. That is a different physical operation from a
swap between two PCI cards, and the operator should confirm which it is.

## Swap procedure

Steps 1-2 are the operator's. Everything from 3 is the harness's.

1. `vcctrl power off`, swap the card.
2. Power on at the wall, or tell the harness to.
3. **Cold boot, LED edge pair not level.** After a power cycle the Pi's
   `/sys/class/leds` still holds the pre-shutdown values, so a level check
   reports ready before the machine has POSTed (TIMING-FIXES bug 3). Use
   `vcctrl-collect --power-on`, which does the edge pair.
4. **Confirm capture still locks, before anything else.** Grab a frame at the
   DOS prompt. A new video BIOS is the single most likely thing to break the
   capture path, and every later step is judged through it. If the stick
   returns flat black, stop -- the harness is blind and should say so rather
   than proceed on wall-clock alone.
5. **Identify the card from behaviour**, with `bin/vcctrl-cardid` against a
   sweep's SDL log. Do NOT ask UniVBE -- it shims the VBE identity and answers
   "SciTech Software" on every card. `cardid` reads the capability shape
   instead (mode list, `lfb_addr`, VRAM, the engine's detect probes) and says
   UNKNOWN rather than guessing when the log is thin. If its verdict disagrees
   with what was fitted, that is the finding.

   **This read is provisional until step 6 has run -- a stale driver can make
   it genuinely ambiguous, not just imprecise.** Live case, 2026-09-03,
   Cirrus->ViRGE: before UVCONFIG was re-run, `cardid` came back LOW
   CONFIDENCE with `total_vram=1024 KB` (the *previous* card's figure) and
   its own top signature match was "Cirrus (onboard)" (4/5=80%) narrowly
   ahead of ViRGE (6/8=75%) -- both `s3_probe` and `cirrus_bug` fired at
   once. A fresh `dinspect` read agreed with the physical swap even at this
   stage (direct chipset probe, not through UniVBE), so the two witnesses
   can disagree here, and neither should be trusted alone before step 6.
   **Re-run cardid after step 6/7**, against a fresh cell's log -- the same
   run that came back ambiguous pre-UVCONFIG read 8/8 ViRGE, Cirrus 0/5,
   `total_vram=4096 KB`, immediately after.
6. **Run UVCONFIG. `bin/vcctrl-uvconfig` no longer does this itself, and
   that is by design, not a regression to route around** (SUPERSEDED
   2026-09-03; text below described an earlier, more automated version).
   UVCONFIG.EXE only renders correctly in text mode (`MODE03`); this system's
   VGA capture stick only locks onto mode 12h. The instant the machine
   switches to text mode, the harness is completely blind to the screen for
   the whole interactive portion -- exactly the six-hour-incident shape this
   procedure exists to prevent, so the tool refuses to start it rather than
   drive it blind. **This step needs a human physically at the machine:**

       C:\VGACAP\MODE03          (text mode -- capture goes blind, expected)
       C:\UNIVBE\UVCONFIG.EXE    (read what it prints: chip detected, any
                                   withheld modes -- the Mach64-CT case
                                   below is the reason this line matters)
       C:\VGACAP\MODE12          (restores capture)
       Ctrl-Alt-Delete            (the harness can do this part --
                                   AUTOEXEC loads the new config on boot)

   Afterward, `vcctrl-uvconfig --verify` prints `FIND`-based greps to run
   against a fresh cell's SDL log (`oem_string`, the expected mode ID,
   `LFB-decision`, `total_vram`) rather than checking anything itself --
   `total_vram` there identifies the card *through the shim* (2048 KB
   Mach64, 4096 KB ViRGE), a different number and a different attestation
   layer from `cardid`'s own direct `vram=` reading in step 5, and both are
   worth checking, not just one.

   The paragraph below (pre-2026-09-03 behaviour) still applies to what the
   *operator* watches for while running UVCONFIG by hand:

   If it stops with a screen showing, **that is the swap path working as
   intended**, not a failure: drive it by hand from the captured frame. A
   wrong key here does not crash -- UniVBE's config pages include mode tables,
   so it boots fine and drives the monitor slightly wrong, which is the kind
   of wrong that gets measured.

   **If the VGA capture stick loses lock (`state: "frozen"`, flat black)
   partway through, that is NOT the same thing as "a screen is showing that
   nobody can read."** Pull a frame from the hardware camera
   (`vcctrl_camera_shot`, or the `/cam.mjpg` curl+ffmpeg fallback if that
   tool isn't deployed yet -- see the `vcctrl-camera` skill) before concluding
   it's stuck on an interactive menu. Live case, 2026-08-31, the Cirrus
   Logic GD-5434 swap below: the capture stick lost lock for 20+ seconds
   mid-configuration -- indistinguishable from the analog feed alone between
   "invisible interactive menu" (the six-hour-incident failure mode this
   procedure exists to avoid) and "changed to a mode the stick can't lock,
   monitor's fine." A camera frame resolved it instantly: UniVBE's own
   completion banner, already back at a clean prompt -- non-interactive, same
   as the ViRGE and Mach64 cases below. The camera is a genuinely independent
   witness (different device, not subject to the analog stick's lock/refresh
   constraints at all) -- reach for it before assuming a lost VGA lock means
   a stuck menu.
7. **Reboot if required** (pending the answer above), then confirm the prompt
   via RDYPULSE.
8. ~~Run RB as the anchor, with `--collect`.~~ **SUPERSEDED 2026-09-03: not a
   default step any more** -- see the operator decision below. Identity
   witnesses (dinspect + cardid, both fresh, both after step 6) are the
   card-swap validation; a full sweep is now something a *benchmark*
   decides to run, not something a *swap* requires.
9. **Do not bank the numbers against the ViRGE matrix.** A swap starts a new
   column. Say so explicitly when handing results to the analysis session, or
   the comparison will be made by default.

### Card swaps are the expensive step; CPU swaps are not -- OPERATOR DECISION 2026-09-03

A card swap needs a physical operator (steps 1 and 4's UVCONFIG run --
`bin/vcctrl-uvconfig` refuses to drive UVCONFIG itself, see that tool's own
docstring and `docs/DINSPECT-SYSINFO.md`'s sibling skill for why) and a full
identity re-confirmation. A CPU swap is a plain hardware swap with no driver
state to regenerate -- nothing downstream of it needs UVCONFIG re-run.

**So: running a full sweep (RB or any other) on every card swap by default
was over-testing.** *"Card swaps should be relatively lightweight, not full
regression tests every time"* -- the operator's own words, 2026-09-03,
after a full RB anchor sweep ran to validate a ViRGE swap that a fresh
dinspect + cardid read (see step 3's note above -- re-run both AFTER step 6,
not before) had already validated on its own. Step 8 above is retired as a
default; run a sweep when a *benchmark* calls for one, not as a swap
formality.

**Structure a multi-CPU campaign around the card, not the other way round.**
A card swap is the outer, expensive loop (one UVCONFIG run, one identity
re-check); a CPU swap is the cheap, inner loop. For a campaign that needs
several CPUs times several cards, swap the card ONCE, confirm identity ONCE,
then run every CPU's cell/sweep against that same card before touching the
card again -- e.g. ViRGE + {POD-83, Am5x86-133, DX2-66, DX2-50}, then swap to
Mach64 + the same four CPUs, then Cirrus + the same four. That is one
UVCONFIG run per card (three total) instead of one per card/CPU pair
(twelve), for the same coverage.

## Open questions for the operator

- **Cirrus access**: does fitting a PCI card disable it in BIOS, or is there a
  jumper? Determines whether "swap to Cirrus" means removing the PCI card.
- **Mach64 monitor check**: if capture does not lock on 512x384, are you
  willing to sit at the monitor for that one mode? It is the only card with a
  known-unexplained behaviour, so it may be worth the manual session.
- **Swap frequency**: if cards get swapped often, the BIOS `Video shadow`
  setting (currently Enabled, shadowing C000-C7FF) is per-card and worth
  re-checking after each swap. If rarely, ignore it.


---

## Mach64 result: both modes are usable, and the choice is observability vs 3%

Measured 2026-08-19 with patch 0317 installed (build 31ff9cef5336).

### The card offers two working configurations

| | mode | capture during gameplay | fps |
|---|---|---|---|
| default | 512x384 (`0x01F3`) | **no lock** | 28.6 28.5 26.5 30.4 |
| `DOSKUTSU_PIN_NATIVE_MODE=0` | 640x480 (`0x0101`) | **works** | 27.7 (one cell) |

The pin suppresses VBE mode-sets that change VRAM dimensions after the first.
With it engaged, mode-set #3's request for 640x480 is refused and the card
stays at 512x384 -- a mode outside the capture stick's 60 Hz range. Stood down,
#3 executes and the card lands at 640x480, which the stick locks natively:

    pin ENGAGED     #1 512x384   (#3 suppressed)
    pin STOOD DOWN  #1 512x384   #3 640x480   <- ran

### The two modes are measurement-comparable

This is the part that makes the choice cheap. The game does **not** scale to
fill either mode -- `center-oversized` centres the 320x240 logical screen 1:1
and clears the margins once. So both configurations flush the same 76800 bytes
per frame, do the same drawcalls, and run the same present path
(`direct-vesa: presents=0` in both, because centring forces a partial present
either way).

By the analysis session's rule -- **the discriminator is drawn area and
drawcall count, not display mode** -- these are the same workload. The ~3%
gap is a real but small cost, not a different experiment.

### Operating guidance -- OPERATOR DECISION 2026-08-19

**SUPERSEDED 2026-08-19 (later the same day): 640x480 is the ONLY permitted
mode on this card. The 512x384 opt-in is withdrawn.**

    required  pin stood down    DOSKUTSU_PIN_NATIVE_MODE=0    640x480
    withdrawn pin left engaged  (variable unset)              512x384

The original decision kept 512x384 as a selectable option for the ~3% it is
faster. The operator withdrew it: *"stop running the mach64 in 512x384, it
won't display in our harness, it must be 640x480."*

The reasoning that changed is not about speed, it is about what a blind cell
costs once you have been bitten by one. 512x384 is outside the capture stick's
60 Hz range, so a cell in that mode produces no frame at all for its whole
duration -- no mid-cell capture, no wedge diagnosis, no screenshot to hand
over. Every failure in such a cell is indistinguishable from every other.

`vcctrl-cell` now REFUSES rather than warns, and refuses a `--set` override
too. Enforcement rather than documentation is deliberate: **leaving the
variable unset IS 512x384**, because it is the card's own closest match to
320x240. The blind mode is what you get by doing nothing, which is precisely
the kind of default that reasserts itself the moment nobody is watching.

Since there are no banked Mach64 figures older than today, nothing forces the
older mode for compatibility.

**The asymmetry now matters more than it did.** Patch 0317 is default-OFF, so
the *blind* mode is the one needing no flag and the *required* mode is the one
you have to ask for. While 512x384 was a supported opt-in that was merely
untidy; now that it is forbidden, it means the forbidden state is the one the
system falls into unaided.

The harness refusal covers every path that goes through `vcctrl-cell`. It does
NOT cover a cell launched by hand at the DOS prompt. The durable fix belongs on
the DOSKUTSU side -- flip 0317's polarity -- or in this machine's AUTOEXEC, so
the safe mode is the one that needs no action. Worth doing before the next
campaign rather than after.

### Not yet settled

**27.7 is one cell, not a pair.** It sits inside the spread of the four RB
cells, so 3% is an estimate rather than a measurement. A paired run would
settle it and costs one sweep.

---

## Cirrus result: GD-5434 (discrete PCI card, not the motherboard's onboard chip), UVCONFIG silent again

Measured 2026-08-31, dossage project (a separate SDL3-DOS port, same physical
system as doskutsu). Card swapped in from the S3 ViRGE -- **not** "the onboard
Cirrus" this doc's "Per-card unknowns" section originally flagged as unknown
access; this is a distinct discrete GD-5434 PCI card, so the onboard-access
question below remains genuinely open.

**Identity, from UniVBE's own detection (not asked of the shimmed VBE
identity -- see step 5's warning above):**

    Graphics Chip: Cirrus Logic CL-GD5434 PCI with 1 MB
    RAM DAC:       Cirrus Logic 24 bit DAC
    Clock Chip:    Cirrus 5434/36 Internal Clock

**UVCONFIG ran fully non-interactively** -- third data point (ViRGE
unchanged, ViRGE->Mach64 swap, now ViRGE->Cirrus swap) all silent, none
showing the remembered "press space, press space again" screen. The
"conditional on detected-card-differs-from-stored-config" theory from the
2026-08-19 rehearsal section above has now failed to reproduce on an actual
swap twice. Configured VBE 2.0 and 3.0 extensions plus a linear framebuffer;
dinspect confirms the result:

    Video: Cirrus Logic GD-54xx VGA (VBE 1.2)         <- before (bare card ROM)
    Video: Universal VESA VBE 6.70 (VBE 3.0)          <- after uvconfig + reboot

No `UNIVBE.DRV` existed on the card before this run (only `UVCONFIG.DAT`,
stale from 2026-08-28) -- worth checking for on any swap, since "no driver
file at all" and "a driver file for the wrong card" both need the same fix
but read differently in a `DIR`.

**The VGA capture stick lost lock mid-run** (`state: "frozen"`, ~20s) --
see the camera-assisted note on step 6 above. Resolved via a hardware-camera
frame (`/cam.mjpg`), not by guessing a keystroke.

**Provenance-hole files found on the card, left untouched:** `C:\UNIVBE\`
carries `UNIVBE.BAK`/`UVCONFIG.BAK` (08-19-26 2:27p) and `UNIVBE.NEW`/
`UVCONFIG.NEW`/`HANG.OLD` (08-19-26 2:47p, `HANG.OLD` is 0 bytes) -- almost
certainly forensic evidence from the six-hour incident `bin/vcctrl-uvconfig`'s
own refusal is built around. Not touched or cleaned up; flag before deleting
if a future swap wants that directory tidy.

### The in-code comment this refutes

`patches/SDL/0012`'s rationale says of hardware without native 320x240:

> mode-set #1 already picks the 640x480 fallback so the suppression never
> triggers. Harmless on that target.

Both halves are false on this card. Mode-set #1 picks **512x384**, because
closest-match to 320x240 prefers it over 640x480 -- so the suppression does
fire. And standing it down is safe: the upper-left rendering bug the pin
guards against does not reappear, because `center-oversized` post-dates the
pin and handles the oversized surface correctly. Verified by looking at the
frame, not by inferring it.
