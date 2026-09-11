# docs — what is here and which of it is current

29 files, written over a campaign rather than designed. This says what each
is for, and — more importantly — **which numbers in them have been retracted**,
because several documents carry a live figure and a withdrawn one in the same
section.

**Layout, since 2026-09-11:** most files sit directly in `docs/`. `lab/`
holds six documents specific to measuring this project's own physical
system — findings, open faults, hardware migrations and swaps — kept public
because they're the most honest record of what real hardware does, but
clearly separated from target-agnostic reference. `history/` holds
superseded planning documents, kept verbatim for the record. This index
still names every file by its bare filename below; check `lab/` first if a
name doesn't turn up in `docs/` directly.

---

## Start here

**`KNOWN-LIMITATIONS.md`** — what does NOT yet adapt to a different system's
shape, separately from `OPEN-FAULTS.md`'s bugs-on-this-hardware register.
**Read this if you're bringing your own hardware** — it's the honest list
of what's still a literal in the source rather than a config choice.

**`FILE-TRANSFER.md`** — how a file gets onto the CF card and how one comes
back off it, and why the improvised reboot-and-`GET.BAT` is not it. `GET.BAT`
is the bootstrap and the recovery path; `VCGET.BAT` is the routine one.
**The two directions do not carry the same proof** and the document says which
is which.

**`OPEN-FAULTS.md`** — the forward-looking register: what is still broken, what
is worked around rather than fixed, and what to check before trusting a result.
**Read this before running anything measured.** `FINDINGS.md` records what
happened; this one records what will bite you.

**`PROFILES.md`** — one `vcctrld` process, multiple targets (`gateway2000`,
`modernpc`, and how to scaffold the next one). Written 2026-09-02 at the end
of the session that built it; §21 of `OPEN-FAULTS.md` is its own
forward-looking half.

---

## The Mach64 30-fps campaign

Moved 2026-09-11 to doskutsu's own repo, at
`qa-results/2026-08-21-mach64-tilemap/README.md` — it decomposes and
optimizes the engine's own render path (`Renderer.cpp`, `main.cpp`), which
is doskutsu's concern, not this harness's. `bin/vcctrl-score-pump` moved
with it, to `tools/score-pump.py`.

---

## The harness, and how it has been wrong

**`FINDINGS.md`** — 55 sections, measured rather than reasoned. The recurring
shape is worth knowing before you read any of it:

> A check that returns the reassuring answer when it is broken.

Sections 31, 35, 36, 37, 38, 39, 41 are all that family: absence read as a
value; a check passing on an empty population; a dump bounded by the ring
rather than the cell; Caps Lock inverting every typed character; a journal
query nine hours in the future returning "No entries"; eight cells silently
comparing a condition with itself; `verify_input` reporting a keystroke
reached a target that had no power to receive it.

- **`CLI-PARITY.md`** — the `vcctrl` verb surface against the web KVM's,
  trimmed 2026-09-11 to what shipped and one lasting lesson: parity is not
  "the CLI can reach it", it is "both sides get the same thing when they
  do". A chord ordered in the browser and not in the CLI was two different
  commands with one name, and the verb count could not see it.
- **`MOUSE.md`**, **`TIMING-FIXES.md`** — input path and settling races.
- **`MSD.md`** — USB mass storage: mounting a disk image onto a
  `hdmi-usb`-kind target over the same gadget as its keyboard/mouse.
- **`PI5-MIGRATION.md`** — the Pi 3 → Pi 5 move, closed.

## The standard

**`HARNESS-STANDARD.md`** — the contract for running measured tests on
constrained hardware when an agent, not a person, is reading the run sheet.
Target-agnostic: it states requirements, and the project-specific parameters
live in a profile rather than in the standard. Imported 2026-08-23 from the
doskutsu campaign, where it was written; vcctrl is its reference harness
implementation. **Read it before adding a measurement to anything here.**

## Driving the system from an agent directly

**`SKILLS.md`** — the `.agents/skills/` layout: portable system/repo knowledge
as skill files any coding agent can discover, and the multi-harness
pointer scheme that keeps one canonical copy.

**`MCP-SERVER.md`** — `agent/vcctrl_mcp.py`, 59 tools over MCP wrapping
`bin/vcctrl` and the harness scripts, so Claude Code, Codex or any other
MCP client can drive the system without a person typing commands. Setup for
both clients, the safety model (shared lock, named confirmation, board-
scoped power), and a status table of what's been proven live versus
unit-tested only. PC/DOS only so far, by operator direction.

## The KVM (webkvm session's component)

`WEBKVM.md` is the main one. `WEBKVM-DESIGN.md`, `WEBKVM-AUDIO.md`,
`WEBKVM-BOARDS.md`, `WEBKVM-SCRUB.md` and `WEBKVM-A11Y.md` are its
subsystems.

**`SECURITY-AUDIT-2026-08-28-webkvm.md`** — the pre-release audit of the
public mirror: what held under live probing, and six findings (F1–F6), all
implemented the same day. The findings' fixes are commented at their sites
in `daemon/vcweb_public.py` with the F-numbers this doc defines.

**Sec. 5.1a — the on-screen keyboard — carries a live warning.** It is a whole
keyboard now, drawn from a layout the daemon names per protocol board, and
**not one of its keys has been measured at the target.** Sec. 5.2's coverage
sweep gates step 8 in the pacing table and step 8 shipped without it. The panel
says so itself rather than the fact living only here.

**`DINSPECT-SYSINFO.md`** — the Status panel's DOS System section, grounded
in a real `dinspect` run: one full scan (both reboots) cost **161s**, and
its actual report is in there verbatim. **One run, no repeatability figure
yet** — `ScanJob.RUN_WAIT_S` is proven sufficient once, not measured with a
margin.

## Hardware facts

`BOARD-IDENTITY.md`, `VIDEO-SWAP.md`, `SOUND-PROFILES.md`,
`PICOGUS-CONSOLIDATION.md`.

## History

**`ORIGINAL-PLAN.md`** (in `docs/history/`) — the pre-build design document,
moved here 2026-09-11 and kept verbatim except for two identifier fixes. It
predates
the Pi 3→Pi 5 migration, the multi-profile architecture, the web KVM and
the MCP server; read it as what was planned, not as current fact.

**`PLAN-NEXT.md`** (in `docs/history/`) — moved here 2026-09-11. Items 2 and
3 are done or half-done; item 1 (the capture regression) is **marked
superseded**, because it stopped reproducing and its evidence table had a
row that reported an instrument's state as the target's. See
`lab/OPEN-FAULTS.md` for what is currently broken.

**`HISTORY-MAP.txt`** (in `docs/history/`, not a `.md` file, so not in the
index above) — the full old-to-new commit hash map, for anything citing a
commit from before 2026-09-11 outside this repository.

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

## Commit hashes changed on 2026-09-11

A second history rewrite (public-release identifier scrub, on top of
2026-08-24's phase 7) gave every commit a new hash. Every citation in
this directory was updated to match; `docs/history/HISTORY-MAP.txt` is the
full old-to-new map, for anything citing a commit from before that date
outside this repository.

# Planning docs live in internal/, which is gitignored. See CONTRIBUTING.md.
