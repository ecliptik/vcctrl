# Video card swaps: ViRGE, Mach64, Cirrus

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
6. **Run `vcctrl-uvconfig`.** It backs up the current driver first, runs
   uvconfig, and stops rather than guessing if a screen appears. It reboots
   afterwards, because uvconfig generates a driver that only loads at
   TSR-load time. Rehearsed end to end on the ViRGE.

   If it stops with a screen showing, **that is the swap path working as
   intended**, not a failure: drive it by hand from the captured frame. A
   wrong key here does not crash -- UniVBE's config pages include mode tables,
   so it boots fine and drives the monitor slightly wrong, which is the kind
   of wrong that gets measured.
7. **Reboot if required** (pending the answer above), then confirm the prompt
   via RDYPULSE.
8. **Run RB as the anchor**, with `--collect`. Not because it should match --
   it should not, see above -- but because RB is the sweep with the most
   banked history, so its shape is the most interpretable.
9. **Do not bank the numbers against the ViRGE matrix.** A swap starts a new
   column. Say so explicitly when handing results to the analysis session, or
   the comparison will be made by default.

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

**640x480 is the default for this card. 512x384 is an opt-in the user can
select when wanted.**

His reasoning: 512x384 is slightly faster and worth keeping rather than
removing, but observability is the better default and ~3% is not worth giving
up seeing the screen on every run.

    default   pin stood down    DOSKUTSU_PIN_NATIVE_MODE=0    640x480
    opt-in    pin left engaged  (variable unset)              512x384

Since there are no banked Mach64 figures older than today, nothing forces the
older mode for compatibility.

**Note the asymmetry, which is deliberate rather than unnoticed:** patch 0317
is default-OFF, so the *faster* mode is the one needing no flag and the
*default* mode is the one you have to ask for. Supporting the opt-in therefore
costs nothing -- it is unmodified behaviour. If that inversion ever becomes a
nuisance the fix is on the DOSKUTSU side, either flipping 0317's polarity or
setting the variable in this machine's AUTOEXEC. Not proposed now.

### Not yet settled

**27.7 is one cell, not a pair.** It sits inside the spread of the four RB
cells, so 3% is an estimate rather than a measurement. A paired run would
settle it and costs one sweep.

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
