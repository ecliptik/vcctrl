# Sound profiles

> This document is one project's record of its own physical system -- specific hardware, specific findings, not a general reference. See `docs/HARNESS-STANDARD.md` for the target-agnostic contract this system implements.

> ## OPERATOR DECISION 2026-08-19 (late): THE VIBRA COMES OUT
>
> **The standing hardware configuration is PicoGUS + video card + NIC. The
> Vibra16S is fitted only while it is actively being tested, and removed
> afterwards.**
>
> Everything below was written while the Vibra was permanent, and the
> constraints it describes are real *when the card is in*. With the Vibra out
> they mostly evaporate, which is most of the reason for the decision:
>
> - **AdLib stops being blocked.** Sec. "AdLib requires REMOVING THE VIBRA"
>   exists because a configured PnP Vibra owns 0x388 and cannot be silenced by
>   software. With the card out, the PicoGUS OPL can sit at 0x388 and AdLib is
>   reachable as an ordinary profile rather than a hardware operation.
> - **The port-relocation work becomes unnecessary** for the default
>   configuration. Keep it recorded for the periods when the Vibra is back in.
> - **`Plug & Play O/S: Yes` is only MANDATORY while the Vibra is fitted**
>   (g2k README, BIOS notes). With the card out that constraint is inactive --
>   but do not change the setting, because it must be Yes again the moment the
>   card returns and a forgotten BIOS change is a POST hang.
>
> **Why:** fewer cards is fewer contacts. Reseating every card on 2026-08-19
> removed a new POST chirp that had appeared that evening, alongside an Intel
> NIC that dropped off the PCI bus for an hour with no software cause ever
> found. Intermittent contact explains both, and it is invisible to every
> diagnostic this system has -- no log, no capture and no LED channel can see a
> card that is electrically half-present.
>
> **Applies to:** anything that assumes the Vibra is present. `vcctrl-cell`
> defaults, the VIBRA boot profile, and the SOUND profile below all still work
> when it is fitted; they are simply not the standing configuration.


# Collapsing the sound boot profiles

Written 2026-08-19, after the Vibra16S was fitted alongside the PicoGUS and
both were confirmed working. Supersedes the guesswork in
`PICOGUS-CONSOLIDATION.md` sec. "Amendment" -- that document predates knowing
what `pgusinit` can actually move.

**Operator decision: the Vibra stays stock. The PicoGUS is the card that
adapts**, because it is fully software-configurable and is already running a
non-standard IRQ/DMA anyway.

## What the hardware actually allows  [read off the machine 2026-08-19]

`PGUSINIT.EXE /?`, retrieved over FTP from the running system:

    /sbport x    - set the SB base port. Default: 220
    /oplport x   - set the base port of the OPL2. Default: 388, 0 to disable
    /mpuport x   - set the base port of the MPU-401. Default: 330, 0 to disable
    /cdport x    - set base port of CD interface. Default: 250, 0 to disable

**Every address the two cards contest is movable on the PicoGUS side.** That is
the fact the whole plan rests on, and it was worth reading rather than
assuming -- an earlier version of this analysis concluded the OPL conflict was
unresolvable.

### The asymmetry that shapes everything

The Vibra16S is a **PnP ISA card**. Once the BIOS configures it at POST it
answers at 0x220, 0x388 and 0x330 **regardless of which profile booted**. No
`AUTOEXEC` line makes it stop; not running UNISOUND leaves its OPL
uninitialised but does not stop it decoding.

So while the Vibra is fitted, it **owns** those three addresses and the
PicoGUS yields them. This is not a configuration choice, it is what "stock
Vibra" means.

## The resulting address map

| | Vibra (stock) | PicoGUS (adapted) |
|---|---|---|
| SB DSP | 0x220  IRQ 5  DMA 1 | **0x240**  IRQ 7  DMA 3 |
| OPL | 0x388 | **disabled** (`/oplport 0`) |
| MPU-401 | 0x330 | **disabled** (`/mpuport 0`) |
| GUS | -- | 0x240 (already) |
| CD emulation | -- | 0x250 |

Nothing collides. Both cards are addressable simultaneously.

## Profiles: six become four

**`SOUND`** -- the everyday profile, replacing `PGSB`, `PGGUS` and `VIBRA`.
Loads `CDMKE.SYS`; both cards live; PicoGUS on 0x240 with OPL and MPU stood
down. Switching between Vibra digital audio, PicoGUS SB, and PicoGUS GUS is
then `SET BLASTER=...` plus a `pgusinit` call -- **no reboot**.

**`NET`**, **`CLEAN`** -- unchanged, for reasons unrelated to sound.

**`ADLIB`** -- see below. It is not a fourth profile so much as a fourth
*hardware* configuration.

## AdLib requires REMOVING THE VIBRA. It is not a boot profile.

This is the part that is easy to get wrong, and this document got it wrong
once already.

Games expect an OPL at **0x388** and treat that address as fixed. To test the
**PicoGUS** as an AdLib, the PicoGUS OPL must be at 0x388 -- and the Vibra is
already there, permanently, because it is PnP.

A separate boot profile does **not** fix this. There is no `AUTOEXEC` line
that silences a configured PnP card. **The Vibra has to come out of the
machine.**

Not currently a problem: AdLib is not being tested much any more. Recorded so
that when it is, nobody spends an afternoon writing a boot profile that cannot
work.

  * Unexplored escape hatch: Creative's `CTCU.EXE` in `C:\CTCM\` may be able
    to relocate or disable the Vibra's OPL. If it can, AdLib becomes reachable
    without opening the case. Nobody has tried.

## What this costs, and it is not nothing

**`config=%config%` gets less informative.** Today it distinguishes PGSB from
PGGUS from VIBRA. After the merge it says `SOUND` for all three, and the
manifest needs `pgusinit` output to record what the card was actually doing.
The benchmarking session already has that change queued; **it should land
before the profiles merge, not after.**

**Runtime switching cannot be attested by `%config%` at all** -- that variable
is fixed at the boot menu. A cell that switches mode mid-session and reports
`config=SOUND` is telling the truth and saying nothing. This is the same
trap the Mach64 pin work hit from the other direction.

## Order of work

1. Land the `pgusinit`-output-in-manifest change (benchmarking session).
2. Prove the address moves on hardware: `/sbport 240 /oplport 0 /mpuport 0`,
   then confirm **both** cards still make sound, independently, in one boot.
3. Only then merge the profiles.

Step 2 is the one that would be tempting to skip because the help text says it
works. The help text says the options exist; it does not say what happens when
a PnP Vibra and a relocated PicoGUS share a bus.
