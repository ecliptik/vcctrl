---
name: vcctrl-camera
description: The second, discretionary UVC webcam (CameraCapability in daemon/vcctrld.py) pointed at the physical rig itself, not the DOS target's video signal -- how to read it via vcctrl_camera_state/vcctrl_camera_shot, why it has no ring/judgement, its mount orientation history (upside down until 2026-08-30, right-side up since), and why it's out-of-band from the primary capture stick. Use when the primary VGA capture is frozen/no-signal and you need an independent witness of physical machine state, or when asked about the "second camera"/hardware camera/room camera.
---

# vcctrl camera (the second, hardware-facing webcam)

`CameraCapability` (`daemon/vcctrld.py`) owns an Innomaker U20CAM-1080p (or
whatever UVC device `capabilities.camera.settings.device` names) pointed at
the physical rig -- the machine, its lights, the monitor from outside --
not a video-signal capture. Added 2026-08-28 (`312846c`) as a toggleable
overlay in `kvm.html` only; see "Not in kvm-ro.html" below.

**Off by default.** A rig with `capabilities.camera.backend: none` (the
tracked template's default) reports `device_present: false` and
`state: "starting"` forever -- an absent `capabilities.camera` block still
resolves to "no backend", not silently disabled-but-configured. Check
`vcctrl_camera_state()` before assuming the tool will return anything.

## Why it's a separate, simpler path from the main video capture

`VideoCapability` (the analog capture stick) carries a ring buffer, frozen-
frame/no-lock detection, pin/scrub state -- all calibrated to the ANALOG
stick's own failure modes (a relock that emits flat black, a text mode that
legitimately never changes; see `vcctrl-rig-hazards`). None of that applies
to a UVC webcam: it either delivers frames or the ffmpeg process exits,
closer to `AudioCapability`'s "no data is a fault" watchdog than to
`VideoCapability`'s "no data can be correct" one. So `CameraCapability`
keeps only the single latest frame -- no ring, no picture/no-lock
judgement, nothing to scrub or pin.

**This is exactly why it's worth reaching for when the primary capture is
in doubt.** The two devices are physically and pipeline-independent: a
frozen/no-signal `vcctrl_video_state` (capture stick re-syncing, or
genuinely stuck) says nothing about whether the machine or monitor is
actually doing anything, and a black/duplicate frame from `vcctrl_shot`
can't tell you that either (`vcctrl-rig-hazards`' "black frame is not a
black screen"). A camera frame is an independent physical witness -- is the
monitor lit, are there POST beeps' LEDs on, is anything on screen at all --
that doesn't share whatever's wrong with the capture stick's own signal
path. It answers "is the capture stick lying to me" that nothing else can.

## Tools

- `vcctrl_camera_state()` -- `state` (`starting`/`capturing`/`nosignal`/
  `unavailable`), `owned`, `frames`, `spawns`, `fast_failures`,
  `last_error`, `device_present`, `device`, `last_frame_age_s`. Not gated
  by the input lock, cheap, never touches hardware beyond a status read.
- `vcctrl_camera_shot()` -- the single latest frame, written to a local
  file, or an explicit no-frame-yet (two-valued, same file-exists-iff-
  frame-exists contract as `vcctrl_shot`/`vcctrl_frame`). **RAW, not
  judged** -- there is no `picture`/`considered`/`live`/`mean` field to
  read, because there is no judgement pipeline behind it (see above). CLI
  equivalent: `vcctrl camera shot --out FILE` (`vcctrl camera state` for
  the state read).

## Right-side up as of 2026-08-30 -- was upside down, check before trusting either way

The camera was physically mounted upside down, and `kvm.html`'s browser
view corrected that with a CSS `transform:rotate(180deg)` on the client
side only (see `#camimg`'s own comment in `daemon/kvm.html`) -- nothing in
`CameraCapability`, the daemon, or these tools ever rotated the actual
JPEG bytes. The operator re-mounted the camera right-side up on
2026-08-30 and the CSS flip was removed to match, so a frame from
`vcctrl_camera_shot()` and what a human sees in the KVM are now the SAME
orientation, right-side up, neither rotated. If you are reading this
significantly later than that date and the frames look upside down again,
the mount may have moved (or moved back) without this doc catching up --
compare a fresh `vcctrl_camera_shot()` frame against what the KVM page
shows before trusting either one's orientation, rather than assuming this
paragraph is still current.

## The other, HTTP-only path: `/cam.mjpg` -- USE THIS ROUTINELY, not just as a last resort

`kvm.html`'s live browser overlay does NOT go through these tools or
through a WebSocket -- `daemon/vcweb.py`'s `/cam.mjpg` route is the camera
feed's ONLY transport (see that route's own comment), an MJPEG multipart
stream served straight from `CameraCapability._latest()`. Before
`vcctrl_camera_shot()` existed, the only way to pull one frame as an agent
was to fetch a chunk of `https://<web-base>/cam.mjpg` directly with
curl and split a JPEG out with ffmpeg:

    curl -sk "$WEB/cam.mjpg" -o chunk.mjpg &   # let it run a couple seconds, then kill it
    # or: timeout 8 curl -sk "$WEB/cam.mjpg" -o chunk.mjpg
    ffmpeg -y -i chunk.mjpg -frames:v 1 -update 1 frame.jpg

Prefer the MCP tools (`vcctrl_camera_state`/`vcctrl_camera_shot`) when they
work: they go through the same locking/attribution/observability path as
every other vcctrl_ call (`vcctrl-mcp-workflows`'s "nothing here is a side
channel"), a raw `curl` does not. **But `/cam.mjpg` is not merely a fallback
for when the MCP tools are "unavailable" in the abstract -- check it live,
every session, before assuming the tools work**: found 2026-08-31, this
control-mode session's `vcctrl_camera_shot` failed (`--out` rejected for
`camera`) even though the KVM's own live camera overlay was working fine and
the repo's checked-out `bin/vcctrl-client` already had the fix (`--out`
allowed for `camera` at the arg-validation stage, and the generic
`if out is not None: return write_frame(resp, out)` dispatch needs no
per-command change to cover it) -- the gap was purely that the fix hadn't
been pushed to the Pi's DEPLOYED CLI (`pi/deploy.sh --mcp`/`install.sh`) yet.
Symptom: the MCP tool errors while the KVM camera overlay (which goes
through `/cam.mjpg`, not this CLI, at all) works perfectly -- that specific
split (web overlay fine, MCP tool broken) is the tell that it's a deploy
lag, not a real camera fault, and `/cam.mjpg` sidesteps it entirely because
it never goes through the CLI/socket-protocol path these tools share.

**Reach for a camera frame in tandem with `vcctrl_video_state`/`vcctrl_burst`
as routine practice, not only once the primary capture already looks
broken.** It costs one `curl`+`ffmpeg` call and is a genuinely independent
witness -- different device, different mode/refresh constraints, not
subject to the analog stick's lock-loss behavior at all. Live case,
2026-08-31: running `UVCONFIG.EXE` (see `docs/lab/VIDEO-SWAP.md`) made the VGA
capture stick lose lock mid-configuration (`vcctrl_video_state` read
`state: "frozen"`, flat black, for 20+ seconds) -- indistinguishable, from
the analog stick alone, between "stuck on an interactive menu neither the
harness nor a human can see" (the exact six-hour-incident failure mode
`bin/vcctrl-uvconfig`'s hard refusal exists to prevent) and "just changed to
a video mode the capture stick can't lock, monitor's fine." A `/cam.mjpg`
frame resolved it immediately and completely: UniVBE's own completion
banner, already back at a clean prompt -- the reconfiguration had succeeded
non-interactively, same as every other tested case, and the VGA capture's
blind spot was never a real problem. Without the camera, the only honest
move at that point would have been to stop and refuse to guess a keystroke
(which is what happened first, before reaching for it) -- the camera is what
turned "stop and ask a human to check the monitor" into "confirmed working,
proceed," from the same chat, with no travel time.

## Not in kvm-ro.html or the public mirror

Deliberately, per `312846c`'s own commit message: "a physical room is more
sensitive than the emulated screen." The camera sees the operator's actual
space, not just the DOS target's screen -- kvm-ro.html is served publicly
(see `vcctrl-webkvm-copy` and `docs/SECURITY-AUDIT-2026-08-28-webkvm.md`),
and nothing about the camera feed, `/cam.mjpg`, or these tools should be
wired into that page or its deploy path. If a future change adds ANY
camera surface to kvm-ro.html, that is a deliberate reversal of an explicit
privacy decision, not a routine sync -- flag it for the operator rather
than porting it over silently the way a normal kvm.html -> kvm-ro.html
theme/layout change would be.
