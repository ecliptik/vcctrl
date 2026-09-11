# dinspect / sysinfo — the one real measured run

What `vcctrl_file_scan` / the KVM's **Re-scan hardware** button actually
costs, and what a real report from this system looks like. See
`.agents/skills/vcctrl-dinspect-sysinfo` for the how-to; this document is the
measurement, with its conditions, per this repo's own rule that a number
without them is not a number anyone can trust later.

**This is ONE run, in ONE sitting, on ONE machine configuration.** It proves
the pipeline works and gives a working `ScanJob.RUN_WAIT_S`. It is not a
repeatability study — there is no error bar here, only a single data point.
Treat every timing below as "this is what one run cost," not "this is what a
run costs."

## Conditions

- **2026-08-27, 23:55:50–23:58:31 (local)**, first real end-to-end run of
  `FilesCapability.file_scan` since it shipped.
- Board: 1 (Gateway 2000, IBM PC-compatible, PS/2), `board.source:
  status-file`.
- Boot profile the machine was in when the job started: **PGSB**
  (PicoGUS in Sound Blaster emulation mode) — this is what the CONFIG.SYS
  menu's timeout lands on by default on this system, which is what
  `ScanJob._boot_to_default()` relies on and does not itself assert.
- `vendor/dinspect.exe` as staged: 30162 bytes, sha256
  `7e6e089684621ba81ee7a771e2f8d9baa287d35d587dc2fe484d489d035a748f` —
  pushed once, earlier in the same sitting, verified byte-for-byte by the
  existing `send_file` path.
- `ScanJob.RUN_WAIT_S` at the time of this run: **15.0s** (the value
  already in `daemon/vcctrld.py` — this run is what validated it, not a
  value derived from a prior measurement).
- Command typed: `C:\XFER\IN\DINSPECT.EXE -o C:\XFER\OUT\SYSINFO.TXT
  --show-undetected`.

## What the run cost, phase by phase

Timestamps from the job's own log (`file_status`), not reconstructed —
this is what the daemon itself recorded while the run happened.

| from → to | phase | elapsed | what it is |
|---|---|---|---|
| job start → "rebooting to the menu default" | — | 5.1s | queueing/arm overhead before the first log line |
| reboot logged → "the machine booted" | reboot→boot | **38.6s** | Ctrl-Alt-Del edge + POST + AUTOEXEC to a prompt |
| "the machine booted" → "typing the dinspect invocation" | — | 0s | typed immediately, no gap |
| typed → "handing off to the fetch" | scan | **17.6s** | `RUN_WAIT_S` (15.0s) + `type_line()`'s own typing/round-trip overhead for the ~90-character command (~2.6s) |
| "handing off" → "rebooting into the NET profile" | — | 0s | `PullJob` starts immediately |
| reboot logged → "selecting NET, blind" | reboot | 26.6s | Ctrl-Alt-Del edge for the NET-bound reboot |
| "selecting NET" → "proving NET" | select→attest | 18.3s | AUTOEXEC to a NET-booted prompt (packet driver, mTCP) |
| "proving NET" → "NET confirmed" | attest | 5.5s | `VCCHK.BAT`/FTP round trip that proves NET is up |
| "NET confirmed" → "C:\XFER\OUT holds 22 files" | list | 5.3s | `VCLIST.BAT`: `DIR` piped to a file, FTP'd back |
| listing → "SYSINFO.TXT came back the size the card says it is" | fetch→verify | 5.8s | `VCGET`/FTP fetch of the 567-byte report |
| verified → "the machine booted" (return leg) | return | **38.1s** | the return-to-menu-default reboot — same order of magnitude as the first, reinforcing "~40s to boot" as this system's real figure, not a one-off |

**Total: 160.98s (≈ 2m 41s), job start to finish, both reboots included.**

**What this table does NOT establish**: how long `dinspect.exe` itself
took to run and write its report. The 17.6s "scan" phase includes the
15.0s blind wait *and* whatever `dinspect` actually took, and nothing in
the log distinguishes the two — only that the whole thing (typing +
running + writing) finished inside the 15s window, because the file was
there, correctly sized, when `PullJob` looked for it a NET-reboot later.
15.0s is a proven-sufficient value on this hardware for this command, not
a measured `dinspect` runtime with a known margin on top of it. dinspect's
own README documents a "Runtime" field meant for exactly this kind of
external calibration, but it does not appear in this build's `-o` output
(confirmed against the real report below — no such field is present), so
that path isn't available yet either.

## The real report

`C:\XFER\OUT\SYSINFO.TXT`, 567 bytes, sha256
`87b88d511b585b195793909d37fedf574442005e9c31ec70ca3bea413ec37e1d`, fetched
and verified `size` (DOS 6.22 cannot hash a file; see `FILE-TRANSFER.md` on
why a pull is one step weaker than a push):

```
OS: MS DOS 6.22
Shell: C:\DOS\COMMAND.COM
CPU: Intel 486DX2
CPU Speed: ~45 MHz
CPU Features: FPU
Floating Point Unit: YES
L1 Cache: UNKNOWN
L2 Cache: UNKNOWN
Base Memory: 640 KB
Ext. Memory: 48128 KB
Video: ATI MACH64 (VBE 1.2)
Video Memory: 2 MB
Sound BLASTER: A220 I7 D3 P330 T3 (Sound Blaster 2.0)
Sound OPL: OPL2/3
Sound SB DSP: DSP v2.1
Sound MPU-401: YES
PicoGUS: SB mode (PicoGUS 2, protocol v4)
Network Packet Driver: not detected
Network IP Config: not configured (MTCPCFG not set)
Floppy drives: 1
Disk C: 1263040/1967296 KB (36% free)
```

Cross-checks against what the daemon already knew independently, both of
which held:

- **`Sound BLASTER`** (`A220 I7 D3 P330 T3`) matches
  `BoardCapability.BLASTER_PROFILES["PGSB"]` exactly
  (`daemon/vcctrld.py`) — the profile the boot menu's default actually
  landed on, confirmed from the *target's own* environment rather than
  asserted.
- **Network fields read "not detected"/"not configured"**, correct for a
  PGSB boot, which loads no packet driver — the same fact `PROFILE`'s
  BLASTER-based reasoning already relies on (a profile that sets no
  BLASTER cannot be told apart from NET/CLEAN/PGADLIB/PGGUS by that
  signal alone; this run adds an independent, DOS-side confirmation that
  the network stack genuinely was not loaded).

`daemon/vcsysinfo.parse_dinspect_report()` on this exact file returns
every one of the 20 lines above under a known label in `fields`, and
`Disk C` (the only line not in `KNOWN_FIELDS`) under `other` — confirmed
by running the parser against this file directly, not inferred from the
code.

## Open

- No repeated runs, so no repeatability figure for the 161s total or any
  of its phases exists yet.
- `RUN_WAIT_S` has never failed on this hardware, but it has also only
  been asked to succeed once. A margin narrower than the noise is exactly
  the failure this repo has been bitten by before (see `README.md`'s
  retractions section) — a second and third run, ideally on a slower
  machine than a 486DX2, would turn "proven sufficient once" into an
  actual margin.
- The DOS-System panel's gate (`board.keyboard === 'pc-at-101'`) has not
  been exercised against a real Mac Plus board swap — only reasoned about
  and code-reviewed. `docs/BOARD-IDENTITY.md` is the place a real swap
  test of it belongs.
