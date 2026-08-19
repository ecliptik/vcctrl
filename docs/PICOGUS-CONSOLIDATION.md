# Collapsing the three PicoGUS boot profiles into one

**Status: proposal. Nothing has been changed.** Drafted 2026-08-19 while the
RB sweep ran; the verification section is deliberately unfinished and gets
appended once RB's result is in hand.

## The question

`PGSB`, `PGADLIB` and `PGGUS` are three of six boot entries. Can they be one
entry plus a `pgusinit` call at the DOS prompt?

## The short answer: yes, technically, and the machine already does it

The three `CONFIG.SYS` blocks are **completely empty**:

    [PGSB]

    [PGADLIB]

    [PGGUS]

Everything that distinguishes them is in `AUTOEXEC.BAT`, and all of it is
settable from a prompt:

| profile | environment | card |
|---|---|---|
| PGSB | `BLASTER=A220 I7 D3 P330 T3`, `..._SB_FORCE_8BIT=1` | `/mode sb` then `/sbenv` |
| PGADLIB | `..._AUDIO_BACKEND=adlib` | `/mode adlib` |
| PGGUS | `..._AUDIO_BACKEND=gus`, `ULTRASND=240,3,3,7,7` | `/mode gus` then `/gusdma 12` |

All three then run `MSCDEX /D:OPTICAL /M:5` identically.

The DOS side is already doing this at runtime: **`PUMP.BAT` switches the card
itself**, running `pgusinit /mode sb` and `/sbenv` mid-sweep, and its usage
line says so. So mid-session mode switching is not speculative; it ships.

`VIBRA` and `NET` are different and must keep their own entries. VIBRA needs
`CDMKE.SYS`, which is a `CONFIG.SYS` device and cannot be loaded from a
prompt. NET's TSRs have no unload path at all.

So the reachable target is **four entries, not one**: `PG`, `VIBRA`, `NET`,
`CLEAN`.

## What it costs, and this is the part that decides it

### 1. The provenance witness stops carrying information

r16 -- pushed to the card this morning -- adds `ECHO config=%config%` to all
nine sweep manifests. `%config%` is chosen at the boot menu and **cannot be
retrofitted onto a run that did not boot that way**, which is exactly what
makes a returned log self-proving rather than self-asserting. The peer's own
comment names the failure it catches: a cell run on the wrong profile, which
"yields no DAC at all and would otherwise look like silent audio with no
record of why".

Collapse the three into one and `config=PG` becomes a constant. It would still
prove the machine is not in VIBRA or NET -- worth something -- but it would no
longer say which sound mode produced the numbers, and that is the distinction
the witness was added for.

This is not a blocker. It is a thing that must be replaced before, not after.
`pgusinit` with no arguments reports the card's current mode, so the manifest
can carry a *read* rather than a *declaration*:

    pgusinit >> LOGS\%QAM%RB.NFO

That is arguably better evidence than `%config%`, because it reports the
hardware rather than the operator's intent. It is also a change to nine files
the peer owns, so it is their call, not mine.

### 2. Environment hygiene stops being free

A fresh boot guarantees that only the chosen profile's variables are set. A
`SETMODE.BAT` must **clear the other two profiles' variables**, not merely set
its own -- `SB_FORCE_8BIT` left over into an AdLib run, or a stale `BLASTER`
into GUS, is exactly the contamination `CLRENV` exists to prevent.

The project has already been bitten here. `DEEP.BAT` carries a comment about
cells that "worked anyway by INHERITING SB mode", and elsewhere about a run
where "no SB DSP answered at 0x220 and both cells died at sdl_init".

A correct `SETMODE.BAT` is perfectly writable. The point is that the reboot is
currently doing this work for free and silently, and after consolidation a
bug in one batch file replaces it.

### 3. An open empirical question

`pgusinit /mode X` **reprograms the card's firmware and reboots it** -- the
boot capture shows `Mode change requested. Rebooting to fw: SB...` followed by
re-detection. Whether a card that has been mode-switched mid-DOS-session is
in an identical state to one switched at boot is *unverified*. It probably is.
"Probably" is not the standard the ~157 banked fps measurements were taken to.

## Recommendation

**Do it, but in three steps, and do not skip the middle one.**

1. **Add `SETMODE.BAT` now, change nothing else.** `SETMODE SB|ADLIB|GUS`:
   CLRENV first, then set that mode's variables and run its `pgusinit` calls.
   Immediately useful for probing -- switching modes to listen to something no
   longer costs a 43 s reboot -- and it is the piece every later step needs.
2. **Prove equivalence.** Run the same sweep twice on one machine: once booted
   into `PGSB`, once booted into `PGGUS` and switched to SB with `SETMODE`.
   Compare fps. If they differ beyond the 0.2 fps noise floor, stop -- the
   consolidation is unsound and the reboot is buying something real.
3. **Then collapse**, together with the manifest change from cost 1, so the
   logs never pass through a state where they cannot say what produced them.

Step 2 is the one that will be tempting to skip because the answer seems
obvious. It is also the only step that produces evidence.

## What consolidation actually buys

Worth being honest that the win is modest:

- One less triplicated block in `AUTOEXEC` -- real, but it is 12 lines.
- Faster iteration between sound modes -- **this is the real win**, and step 1
  delivers all of it without touching the menu at all.
- A shorter menu. Marginal: the menu does not go away, and blind selection
  still has to hit `VIBRA`, `NET` and `CLEAN`.

Which points at something worth saying plainly: **step 1 captures most of the
value and carries almost none of the risk.** If steps 2 and 3 never happen,
the machine is still better off.
