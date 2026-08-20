# CLI/KVM parity: why, and what is left

Written 2026-08-20. The operator's requirement: **vcctrl should be able to do
everything the web KVM can**, so the benchmark and test harness can use it from
scripts rather than a person driving a page.

## 1. The gap, measured rather than assumed

The webkvm session found it; this is the independent count. Parsing every
`commands()` map in the daemon and every verb in `bin/vcctrl-client`:

    26 capability commands in the daemon
     6 with no CLI verb

    buffer  burst  frame  pin  timeline  verify_input

`mouse_click` and `mouse_move` look missing to a naive diff and are not —
they are reachable as `mouse move` / `mouse click`. Five registry-level verbs
(`status`, `caps`, `events`, `activity`, `lock`) the CLI already had.

**Five of the six are the scrub buffer.** It was built daemon-side, correctly,
and the only thing that could drive it was `kvm.html`. A sweep could not save
the seconds before a crash, step back through what the screen did, or pin the
ring while it looked. That is the wrong way round: **the CLI is the thing that
runs unattended, and unattended is exactly when nobody is watching the screen
at the moment it matters.**

The sixth is worse in a quieter way. `verify_input` is the round trip that
proves a keystroke reached the target — the only check that says anything
about the far end of the wire, since every other input status describes the
Pi's own end. A headless sweep could not assert its own input path before
typing. It could only type and hope. After a day spent on a bug where every
diagnostic was green and nothing was driven (FINDINGS sec. 29), that is the
one to close first.

## 2. Phase 0, which had to come first: `--out` means the caller's disk

`bin/vcctrl` forwards to the Pi over ssh, so `--out` was a path **there**.
Every frame-returning verb would have written its output onto the wrong
machine, which makes the whole exercise useless to a VM-side harness.

The failing case was merely confusing — an ENOENT that reads like a local
permissions problem. **The succeeding case is the dangerous one**: a path that
exists on both machines writes on the Pi and returns 0, and the caller reads
whatever its own copy holds, possibly a frame from an earlier run. That is
precisely the stale-frame failure `--out`'s two-valued contract was written to
prevent, reappearing one host over where the contract cannot see it.

Now: run to a temp path on the Pi, copy back, remove the remote copy, and
rewrite the reported path so the JSON names where the file actually is.

**The obvious cleaner design is deliberately not used.** Streaming bytes on
stdout and letting the shell redirect is simpler, but the shell creates the
target file *before* the command runs, so a failure leaves a zero-byte file
behind. A file that looks like evidence and is not is the thing this rig keeps
producing.

## 3. Exit codes carry the reading

Settled with the operator. `shot --out` already worked this way, so this
extends a precedent rather than inventing one.

    0  the target answered
    1  it did not          -- a real fault
    2  could not look      -- no LED channel on this board, or unreadable
    3  the tool itself failed

`vcctrl verify-input || abort` is the intended use, and **2 is the reason the
scheme needs four codes rather than two**. On a Macintosh over ADB there is no
LED return channel at all; if that exited 1, every harness guarded that way
would abort on working hardware. Same three-state discipline as the rest of
the rig.

Note `--out` misapplied to a command now exits 3 rather than 2, so that 2
means could-not-look unambiguously. Nothing branched on the old value.

## 4. What shipped

All six verbs, plus `pi/deploy.sh --client`.

That deploy mode matters more than it looks. A full deploy runs `install.sh`,
which restarts `vcctrld` and drops both uinput devices — a sweep cannot
survive it and is not resumable. The client is a fresh process per invocation,
so replacing the file needs no restart at all, for exactly the reason `--page`
does not. **Making the safe path available is what stops the unsafe path being
used out of impatience.** It syntax-checks before and after copying, because a
client that cannot parse takes out every verb at once and would do so on the
next call rather than at deploy time — so the deploy would look like it worked.

Proven against the live rig: `timeline` (622 frames, 31.9 s span), `buffer`,
`pin on`/`off`, `frame <seq> --out` landing locally as a valid 640x480 frame,
`burst 5 --out-dir` writing five, and **`verify-input` returning 0 against the
Gateway** — the input path proven from a script for the first time.

## 5. Phase B, still open

`vcctrl record --out f.avi [--since S] [--until S]`, waiting on the webkvm
session's `avi_mjpeg()` muxer. Two requirements agreed in advance:

- **It takes the pin itself.** The ring keeps rolling while you copy it, and
  the page's version was broken in exactly that way: the save path walked the
  ring frame by frame without ever pinning, so when the signal came back
  eviction destroyed frames mid-copy and the download silently produced almost
  nothing.
- **Two-valued, like everything else here.** Either the file is this run's
  buffer and the status is 0, or the file does not exist. Never a partial AVI
  left on disk from a copy that lost its frames halfway.

Task-shaped verbs come after, built on the thin ones — `save-around <seq>
--seconds 10` is the shape the harness will actually reach for. Deliberately
not invented ahead of seeing which ones get used.
