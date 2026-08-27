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

**Landing files into a subdirectory that doesn't exist yet on a fresh
target (a new port with no established directory tree).** `vcctrl_send_file`
takes an optional `dest` (default `C:\XFER\IN`), but everything staged in
one call lands FLAT under that one `dest` -- there is no per-file
subdirectory support, so a payload with its own `graphics/`/`music/`/etc.
layout needs one `send_file` call per destination directory, staged and
sent separately. The target-side batch (`VCGET.BAT`) auto-creates only the
*deepest* level of `dest` with `MD` -- and DOS 6.22's `MD` cannot create a
nested path in one shot, so `dest="C:\NEWPORT\GRAPHICS"` fails silently if
`C:\NEWPORT` doesn't already exist. Measured 2026-08-26, bringing up a new
port's directory tree for the first time: pre-create the full tree by hand
first (`MD C:\NEWPORT`, then `MD C:\NEWPORT\GRAPHICS`, etc., one command at
a time -- see `vcctrl-rig-hazards` on why one at a time), THEN run one
`send_file(dest=...)` per subdirectory. An established port (doskutsu) never
hits this because its directory already exists from the last time.

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

**Fetching a specific known file from somewhere other than the OUT
directory** (a port's own debug log, e.g. `C:\SDLDBG.LOG` or
`C:\<PORT>\SOMELOG.LOG`, not something the port copies into `C:\XFER\OUT`
itself) -- `vcctrl_get_file` and `vcctrl_file_list` both take an optional
`from_dir`, so you do NOT need to hand-type a `COPY` into `C:\XFER\OUT`
first: `vcctrl_get_file(names=["SDLDBG.LOG"], from_dir="C:\\DOSSAGE",
confirm="get")` reads directly from that directory. One thing it can't do:
the daemon's own path guard (`dos_dir_path`, see `docs/MCP-SERVER.md`)
refuses the bare drive root as a directory -- `from_dir="C:\\"` is
refused, so a log that genuinely lives at the root (not in a
subdirectory) still needs the manual `COPY` into a real directory first.
There is no per-port convention yet for *where* a debug log lives -- this
session had to ask a peer session for the path rather than reading it from
anything vcctrl itself knows. Worth raising with a port's own profile
(`profiles/<port>.yaml`) as a field to add, rather than building a new
vcctrl tool for it -- `get_file(from_dir=...)` already does the fetch once
the path is known.

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

## Checking audio

Audio and video are two *separate* capture pipelines (`v4l2-ffmpeg` for
video, `alsa-ffmpeg` for audio, per `vcctrl_caps`), with separate rings.
There is no single tool that gives you both together, and there is
currently no way to get raw audio out of vcctrl at all -- know this before
promising a peer a recording:

- **`vcctrl_audio_verdict(ms=3000)` -- start here for "is music/SFX playing
  right now".** One call, no thresholds to know: it combines `level` and
  `spectrum` into the same NO_SIGNAL / SILENT / AUDIO_PRESENT judgement
  `bin/vcctrl-audio` prints as text (same `common/audio_bands.verdict()`
  function underneath, so the CLI and this tool can't quietly disagree).
  `tone_like: true` means the signal looks more like a steady tone/hum than
  music/SFX; `false` means it looks like real content; `null` means there
  wasn't enough information to judge it either way. This is the automated
  stand-in for a human listening -- reach for it before composing `level`
  and `spectrum` yourself and re-deriving the floor/margin logic by hand.
- `vcctrl_audio_state()` -- device/ring health (rate, channels, bytes
  flowing, `state: capturing`). This tells you the capture PATH is alive.
  It says nothing about whether the target is producing anything worth
  capturing -- a live, healthy ALSA ring with nothing connected to its
  input, or a target that's silent by design, reads identically to a
  target with broken audio. Don't treat `state: capturing` as "audio is
  working."
- `vcctrl_level(ms=3000)` -- mean/peak dBFS and a histogram over the last
  `ms`, computed from the audio ring. This is the amplitude-domain half of
  what `vcctrl_audio_verdict` combines. **It reads from the exact same ring
  the browser-KVM's live audio websocket streams from** (`serve_ws_audio` in
  `daemon/vcweb.py` iterates the same `AudioCapability.ring` `_levels()`
  does) -- there is no separate pipeline, so a `level` reading and what a
  human hears through the KVM describe the same signal, once you account
  for timing (next point).
- `vcctrl_spectrum(ms=3000)` -- per-band energy (8 octave-spaced bands,
  100Hz-12.8kHz) and `active_bands`, the frequency-domain half of the
  verdict. Amplitude alone can't tell music/SFX (energy spread across
  bands) apart from a steady tone or mains hum (concentrated in one) at a
  similar level -- this is the direct measurement of that, not a proxy.
  `active_bands` compares bands to EACH OTHER, so unlike `mean_db`/
  `peak_db` it stays meaningful regardless of the physical volume knob
  (FINDINGS sec 11 / WEBKVM-AUDIO.md sec 4). Reach for this directly (over
  `vcctrl_audio_verdict`) when you want the raw per-band numbers, e.g. to
  compare two readings rather than to get a single yes/no judgement.
- **Timing pitfall, not a tool bug: a `level`/`spectrum`/`verdict` check and
  a human's "I hear it" over async chat are not simultaneous.** Measured
  2026-08-26: calling `vcctrl_level` right after launching a program, then
  separately asking "do you hear anything" and getting an answer a
  message-round-trip later, produced a flat noise-floor reading (~-70dB)
  for a program later confirmed audible. Re-run with the level check fired
  at the SAME moment the human said "I hear it now" (not before, not
  after) read -34dB mean / -22.6dB peak -- clearly real signal. The
  lesson: don't check on a fixed delay after starting something and treat
  a quiet reading as "confirmed silent" -- either check repeatedly over a
  longer window, or synchronize the check to an explicit "now" from
  whoever is listening, the same call-and-check-immediately pattern used
  above.
- **`vcctrl_audio_match(file_path, ms=3000)` -- does the live audio
  resemble a SPECIFIC reference file, not just "is something playing".**
  Control-mode only (`file_path` is local to the control host -- daemon
  mode has nothing to reach it with). Decodes the reference file via
  ffmpeg into the same 8-band signature `vcctrl_spectrum` produces, then
  reports a Jaccard-overlap similarity score against a fresh live reading.
  **Read the score as "does the live signal's frequency balance resemble
  the reference's", never as "is this exact track playing right now"** --
  8 bands cannot distinguish two tracks with similar broad frequency
  balance, and it checks nothing about tempo, melody, or timing. No
  match/no-match threshold is asserted by the tool; judge the number
  yourself. Use it for a QA question about a *specific* cue ("did the
  death jingle play"), not as a replacement for `vcctrl_audio_verdict`'s
  generic liveness check.
- **`vcctrl_record` captures VIDEO ONLY.** Measured 2026-08-26: the AVI it
  writes has exactly one stream (`mjpeg`, `codec_type=video`) -- `ffprobe`
  shows no audio stream at all, even though the daemon is simultaneously
  running an audio capture. If a peer asks for "a recording" to judge
  audio quality/tempo/pitch, `vcctrl_record` will not get it for them --
  there is currently no raw-PCM/WAV export path in the daemon (`_audio`
  only does state/acquire/release; `_level`/`_spectrum` only return
  summary stats, never the samples). Say so rather than sending a
  video-only file and calling it an audio capture.
- For "does it sound right" (tempo, pitch, melody correctness, pops/
  clicks) there is no substitute today for a human actually listening
  live through whatever the KVM's audio output is connected to. `level`/
  `spectrum`/`verdict` readings can rule silence in or out, catch gross
  dropouts, and tell music/SFX apart from a tone/hum, but none of them
  judge musical correctness or verify a specific track's identity.
- Sequencing hazard specific to games with a title/start-gate: a target
  that's silent may simply not have received the keypress that starts
  the state which actually plays audio (e.g. a title screen that mutes
  music until a key advances past it). Confirm the keypress actually
  landed (a visible on-screen change, not just the tool call returning
  `ok`) before concluding a SILENT verdict, or a low `vcctrl_audio_match`
  score, is a bug.

## Checking rig health before acting

Cheap, read-only, ungated -- worth calling before a sequence above rather
than discovering a problem mid-sequence: `vcctrl_status`, `vcctrl_caps`,
`vcctrl_board`, `vcctrl_activity` (what's running and who holds input),
`vcctrl_power_state`, `vcctrl_video_state`, `vcctrl_audio_state`,
`vcctrl_lock_status`.

## Locks, when you want explicit control

Most tools above acquire the input lock automatically on first need and
release it after 300s idle -- but that idle release runs inside *this
session's own* MCP client process, not the daemon (see `vcctrl-mcp-workflows`).
It only fires if this process is still alive to run it; call
`vcctrl_lock_release()` explicitly when you're done with a sequence rather
than counting on the idle timer, especially before a long pause. Call
`vcctrl_lock_acquire()` first if you want the lock held before doing
anything observable (e.g. to keep a peer out for the duration of a
multi-step sequence).

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
