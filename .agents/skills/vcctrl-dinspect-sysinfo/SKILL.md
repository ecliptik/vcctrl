---
name: vcctrl-dinspect-sysinfo
description: Recipe-level steps for keeping the DOS target's real hardware inventory (CPU, memory, video, sound) grounded in the web KVM -- updating the vendored dinspect.exe binary, staging it on the card, and re-scanning after a hardware change. Use when asked to refresh, check, or explain the KVM's "DOS System (dinspect)" panel, or after any physical swap of the target's board, card or peripherals.
---

# vcctrl dinspect / sysinfo

`vendor/dinspect.exe` is a real-mode DOS program (built in the sibling
`dosfetch` repo, checked into `vcctrl` as a binary) that inventories the
machine it runs on and writes a plain-text report. `SysinfoCapability`
(`daemon/vcctrld.py`) reads the last report FilesCapability has pulled off
the card and serves it as `vcctrl_sysinfo` / the KVM's **DOS System
(dinspect)** panel. It is a READING with an age, never a live poll — see
`vcctrl-rig-hazards` for why nothing here can be trusted as current without
checking that age.

Pairs with `vcctrl-mcp-workflows` (locking/confirmation discipline) and
`vcctrl-common-workflows` (the plain file-upload recipe this borrows step
1 from).

## Re-scanning (the common case: "get a fresh hardware reading")

Do this after any physical change to the target — a board swap, a sound
card change, a different video adapter — since **the daemon cannot detect
any of those on its own** (it can only see a USB4VC board swap, via
`board.changed`). There is no automatic re-scan; this is the whole
mechanism by which "hardware changed" becomes a grounded fact instead of
an assumption.

**Precondition, and it is not checked for you:** the target must be free
to reboot twice and DINSPECT.EXE must already be staged at `C:\XFER\IN` on
the card (see "Updating the vendored binary" below if you are not sure it
is). If it is not staged, this still reboots the machine twice and comes
back `not-listed` rather than silently doing nothing.

1. `vcctrl_file_scan(mode="return", confirm="scan")` — reboots to the
   target's menu default (typed blind, nothing here can confirm it wasn't
   mid-something first — see ScanJob's docstring in `daemon/vcctrld.py`),
   runs `DINSPECT.EXE -o C:\XFER\OUT\SYSINFO.TXT --show-undetected` there,
   then reboots into NET to fetch the report. Several minutes; returns at
   once.
2. Poll `vcctrl_file_status()` until `job.running` is false.
3. `vcctrl_sysinfo()` for the parsed reading — `fields` (every known label,
   `None` where not detected), `age_s`, `source`, `stale` (see the tool's
   own docstring for what `stale` does and does not mean).

In the KVM: the **Download from target** popover's **Re-scan hardware**
button does the same three steps, and the reading appears in the **State**
panel under **DOS System (dinspect)**.

## Updating the vendored binary (rare — after a `dosfetch` change)

Only needed when `dosfetch`'s `dinspect.exe` itself changes (a new
detected field, a bug fix). The Pi that runs the daemon has no checkout of
`dosfetch` — the binary is vendored specifically so the daemon never needs
one.

1. Build in `/home/claude/git/dosfetch` (Open Watcom — see that repo's own
   `make`).
2. Copy the built `dinspect.exe` to `vcctrl/vendor/dinspect.exe`, replacing
   the checked-in copy.
3. **If the field list changed**, update `KNOWN_FIELDS` in
   `daemon/vcsysinfo.py` by hand against a real report from the new
   build — it is not read from `dosfetch` at runtime or at build time, see
   that file's own header comment for why.
4. Push the new binary to the card over the standard upload recipe (see
   `vcctrl-common-workflows`' "Uploading a file to the target"):
   `vcctrl_file_check()` → `vcctrl_stage_file("vendor/dinspect.exe",
   name="DINSPECT.EXE")` → `vcctrl_file_queue("list")` →
   `vcctrl_send_file(mode="return", confirm="send")` → poll
   `vcctrl_file_status()`. Lands at `C:\XFER\IN\DINSPECT.EXE`, which
   `file_scan` assumes.
5. Re-scan (above) once, to confirm the new build runs and its report
   still parses.
