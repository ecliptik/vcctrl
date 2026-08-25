---
name: vcctrl-common-workflows
description: Recipe-level call sequences for vcctrl's most common tasks -- uploading/downloading files, power actions, typing text, pressing individual keys, sending chords, capturing frames, and running harness workflows. Use when about to ACCOMPLISH one of these tasks (not just make one isolated vcctrl_ call); pairs with vcctrl-mcp-workflows for the locking/confirmation discipline that applies across all of them.
---

# vcctrl common workflows

This is the "what to call, in what order" companion to `vcctrl-mcp-workflows`
(the "why the guards exist" skill) and `vcctrl-rig-hazards` (facts that make a
step here necessary). Read those two for the reasoning; this one is the
checklist. Full tool reference: `docs/MCP-SERVER.md`.

## Uploading a file to the target

1. `vcctrl_file_check()` -- confirm the file server is answering *before*
   spending a reboot on a dead one. A post-reboot discovery costs the
   target's whole environment for nothing.
2. `vcctrl_file_name(name)` -- if the DOS 8.3 rename matters, check what the
   name becomes before staging, not after.
3. `vcctrl_stage_file(path, name=None, replace=False)` -- stages a LOCAL file
   (on the machine running the MCP server) for the next transfer. Nothing
   touches the target yet. Repeat for multiple files.
4. `vcctrl_file_queue("list")` -- sanity-check what's actually staged before
   sending; `file_queue` is daemon-global, so `clear` drops what *any*
   caller staged, not just yours.
5. `vcctrl_send_file(mode, confirm="send")` -- `mode="return"` reboots back
   to the measurement-clean menu default afterward; `mode="stay"` leaves the
   machine in NET (no measured run may start from there). Reboots the
   target **twice**. Returns immediately -- it does not block for the
   transfer.
6. `vcctrl_file_status()` -- poll until done. Exit 0 mid-run means "still
   running, nothing failed yet" as well as "fully done and verified" --
   check `running`/the log tail, not just the exit code, to tell those
   apart. Exit 1 means a file failed; exit 2 means it finished but
   `remaining` names work left behind (cancelled or stopped early).

## Downloading files from the target

1. `vcctrl_file_check()` -- same reasoning as upload: confirm the server is
   up before paying for a reboot.
2. `vcctrl_file_list(from_dir=None)` -- what the target's OUT directory held
   **when it was last read**, with the age of that reading. This does not
   touch the target -- if you need a current listing, that's what step 3 or
   `vcctrl_file_refresh` is for.
3. `vcctrl_get_file(names=[...]|fetch_all=True, mode, confirm="get")` -- a
   name not in the last-read listing is refused here, not typed at the
   machine. Reboots the target unless `already_net=True` (which skips the
   reboot but still requires arrival proof -- being wrong about the
   machine's state costs a refusal, not a garbage command). `paranoid=True`
   fetches each file twice and diffs them: proves the transfer path is
   repeatable, **not** that either copy matches what's on the card (DOS 6.22
   has nothing that can hash a file). Nothing is ever deleted from the
   target, so a fetch is always safe to retry. Returns immediately.
4. `vcctrl_file_status()` -- poll until done, same three-way reading as
   upload.
5. `vcctrl_pulled("list")` -- what landed on the *daemon* host.
6. `vcctrl_pulled_save(name)` -- copy one fetched file onto the machine
   actually running this MCP server. Skipping this step leaves the file
   stuck on the daemon host even though the transfer reads as done.

To re-read the OUT directory without fetching anything, use
`vcctrl_file_refresh(mode, confirm="refresh")` instead of a full `get_file`
-- same reboot cost, no files copied, just an updated `file_list`.

## Power actions

1. `vcctrl_power_state()` -- read-only, never gated. Check `board_match` here
   *before* acting if which physical machine gets powered matters to the
   task -- board-scoping is visibility to a human, not a safety guarantee
   (see `vcctrl-rig-hazards`).
2. `vcctrl_power(action, confirm=action)` -- `action` must be `"on"`,
   `"off"`, or `"cycle"`, and `confirm` must equal that exact string (this is
   the same named-confirmation shape as `vcctrl_combo`'s reboot gate --
   a bare `true` does nothing on purpose). `cycle` runs the full
   off/wait/on sequence unconditionally, regardless of what
   `vcctrl_power_state` currently reports -- a wedged machine can report
   "on" while being useless, so don't skip the cycle because the reported
   state already looks right.
   `off_seconds` controls the dwell; the call blocks for the full duration.
   Refused by the daemon itself if the installed board isn't one the
   configured plug is declared to control.

## Typing text and pressing keys

- `vcctrl_type(text)` -- literal text, shifted characters handled. Use for
  anything you'd type as a sentence or filename.
- `vcctrl_key(keys)` -- tap named keys in sequence, e.g. `["enter"]` or
  `["esc","f1"]`. Use `vcctrl_keymap()` first if you're not sure a name is
  valid -- it lists accepted key names and the alias table, though it says
  nothing about which keys actually *reach* the target (that's measured at
  the machine, not declared here).
- `vcctrl_hold(key, ms)` -- press, dwell, release as one call -- the
  primitive for "hold this direction for N ms," not two calls glued
  together.
- `vcctrl_keydown(key)` / `vcctrl_keyup(key)` -- press-and-hold with no
  automatic release, for anything longer or more dynamic than `hold` covers.
  **Always pair these**, and call `vcctrl_release_all()` at the end of a
  sequence (or on any error) so a held key doesn't outlive your intent.
- After anything you depend on landing, verify by a channel other than "the
  call returned without error" -- Caps Lock silently inverts case on
  everything typed, and OCR off a captured frame can't reliably read digits
  (`vcctrl-rig-hazards`). `vcctrl_verify_input()` proves the PS/2 round trip;
  a follow-up `vcctrl_shot` is the visual alternative.

All of the above acquire the shared input lock on first use and **refuse**
(never force) if a human or peer holds it -- check `vcctrl_lock_status()`
rather than retrying blind.

## Sending chords (combos)

1. `vcctrl_keymap()` -- see accepted key names and the alias table before
   constructing a chord, rather than guessing spellings.
2. `vcctrl_combo(keys, confirm=None)` -- modifiers press first regardless of
   the order you list them, release in reverse; the reply echoes the order
   actually sent. If the chord is a superset of Ctrl-Alt-Delete (extra keys
   included -- e.g. Ctrl-Alt-Shift-Del still counts), it is refused unless
   `confirm="reboot"` is passed. This isn't a human-in-the-loop gate -- an
   agent can confirm autonomously when a reboot is genuinely intended -- it
   only exists so a reboot is never the *accidental* shape of a call.

## Capturing what's on screen

- `vcctrl_shot(n=None)` -- one selected/judged frame to a local file, or an
  explicit "no picture." Two-valued: file exists iff the daemon says
  picture.
- `vcctrl_lastgood()` -- the last frame positively judged as picture, aged.
- `vcctrl_burst(n)` -- n raw frames at once, all-or-nothing (no silent holes
  that would look like a complete capture and aren't).
- `vcctrl_timeline()` then `vcctrl_frame(seq)` -- index the scrub buffer
  cheaply, then pull one specific raw frame by sequence number.
- `vcctrl_record(since, from_seq=None, to_seq=None, clip=False)` --
  **`since` is required, not optional**, and must be your own start time (a
  unix timestamp or `"now"`). Without it the resulting AVI is bounded by the
  ring, not by what you asked for -- a real capture on this rig was 94.5%
  the *previous* cell's frames because this was skipped. `clip=True` limits
  it to frames taken under this session's own lock ownership.
- `vcctrl_pin("on"|"off"|"status")` -- stop the ring evicting frames while
  you examine them; auto-releases after 300s regardless, so an interrupted
  session can't wedge it.
- Never judge target state from a single frame -- the capture stick emits
  flat-black frames while locked/re-syncing, indistinguishable in one still
  from a genuinely blank screen (`vcctrl-rig-hazards`).

## Checking rig health before acting

Cheap, read-only, ungated -- worth calling before a sequence above rather
than discovering a problem mid-sequence: `vcctrl_status`, `vcctrl_caps`,
`vcctrl_board`, `vcctrl_activity` (what's running and who holds input),
`vcctrl_power_state`, `vcctrl_video_state`, `vcctrl_audio_state`,
`vcctrl_lock_status`.

## Locks, when you want explicit control

Most tools above acquire the input lock automatically on first need and
release it after 300s idle. Call `vcctrl_lock_acquire()` first if you want
the lock held before doing anything observable (e.g. to keep a peer out for
the duration of a multi-step sequence), and `vcctrl_lock_release()` when
you're done with input for a while and want to let a human or peer back in
without waiting out the idle timer.

## Harness workflows (control mode only: preflight, cells, sweeps, collect)

Not available against `vcctrl-mcp-daemon` -- these are control-host-side
orchestration scripts with nothing correct to do on the daemon host itself.

1. `vcctrl_preflight(no_input=False)` -- one gate for "is this rig fit to
   run": caps, board, power, video, scrub ring, the input lock, and (unless
   `no_input`) the input round trip. Scope is the *harness*, not the target
   -- it says the rig can drive the machine, never that the machine is
   correctly configured for a measured run.
2. `vcctrl_sweep_list()` -- every sweep name from `profiles/doskutsu.yaml`,
   safe and fast, no hardware touched -- check this before picking a name
   for step 3.
3. One of:
   - `vcctrl_run_cell(tag, card, set_vars=[...], forbid=[...], expect_log=[...], confirm="run")`
     -- a single diagnostic cell. `set_vars` are `"VAR=VAL"` strings set
     before launch; `forbid` names are verified **positively absent** (a
     read-back count of 0), which is how you assert a contamination rule
     across a set of cells that must not inherit each other's hint
     variables.
   - `vcctrl_run_sweep(sweep, machine_digit, confirm="run")` -- a full
     unattended QA sweep, 12-23 minutes.
   - `vcctrl_collect(sweep, machine_digit, confirm="run")` (or `tags=[...]`)
     -- bring a sweep's logs back, unattended. Confirmation comes from the
     filesystem (arrival of each log), not the screen.
4. All three launch detached and return a `job_id` immediately. Poll with
   `vcctrl_job_status(job_id)`; list everything this session has launched
   with `vcctrl_job_list()`; `vcctrl_job_cancel(job_id, confirm="terminate")`
   is a last resort (SIGTERM, not graceful -- can land mid-keystroke or
   mid-reboot on the target).

**A returned log is not a valid cell.** `vcctrl-collect`'s own log-arrival
check distinguishes "never written" from "transfer failed," but neither
proves the run itself was sound -- check `expect_log` assertions and the
per-cell profile/lever assertions your run sheet actually calls for.
