# Scrub buffer and recording

**Branch:** `webkvm`. **Status:** plan only. Written 2026-08-19.
Companion to [WEBKVM.md](./WEBKVM.md) and
[WEBKVM-AUDIO.md](./WEBKVM-AUDIO.md); their rules apply unchanged.

Two related features, from the operator:

> a playback buffer, where we can record/keep the last 30 seconds of streaming
> and we can scrub back through frame-by-frame, this way we can pick out a
> specific issue or problem. Then we can save, copy it, or send it to the
> harness for analysis

> augment the plan so we can do a full video/audio capture recording if we need
> to, give it some additional control dialog or a start/stop recording and save
> (to local system web browser is on)

They are different things and should not be built as one. **The scrub buffer is
always on and costs RAM; the recording is explicit and costs disk.** The first
answers "what just happened", the second answers "keep this".

### Decisions taken

| question | answer |
|---|---|
| Recording written where | **Pi disk while running**, browser downloads on stop |
| Codec | **H.264, hardware encoded** (`h264_v4l2m2m`) |
| Duration | **Minutes** -- a cell or a boot, up to ~10 min |
| Event log | **Markers on the timeline**, from the existing event bus |

---

## 1. The budget, which decides the design  **[measured 2026-08-19]**

    Pi 3B:  920 MB total, 680 MB available, NO SWAP
    Card:   21 GB free
    Frames: ~15 KB a text console, ~70 KB a dense screen (measured)
    Audio:  192 KB/s raw

**No swap is the constraint that matters.** There is nothing to absorb an
overshoot: an OOM does not slow the Pi down, it kills the daemon, and killing
the daemon drops the uinput devices, which is the one failure this whole design
has been arranged to avoid (WEBKVM sec. 2, rule 3 -- which explicitly does not
cover memory).

30 s at 30 fps is 900 frames. That is **13.5 MB of text console or 63 MB of
dense screen** -- a 4.7x spread depending on what happens to be on screen,
which means a buffer sized in *seconds* has no fixed cost and a buffer sized in
*bytes* has no fixed duration. This is the same units problem the `vcctrl`
session hit today with keystrokes and attempts: **bound the resource in the
units the resource is measured in**, then report the other number rather than
promising it.

So: **the buffer is capped in bytes, and the UI states how many seconds that
currently buys.** Proposed budget **48 MB video + 8 MB audio**, roughly 8% of
available RAM, leaving the rest of the headroom alone.

### Adaptive thinning, so 30 s means 30 s

A fixed byte cap on a dense screen would hold ~11 s and silently stop being a
30-second buffer exactly when something interesting is happening. Instead, when
the buffer is over budget, **thin the oldest third by dropping alternate
frames** rather than evicting it.

That trades temporal resolution for wall-clock span, in the region where you
need it least: recent frames stay at full 30 fps for frame-by-frame work, and
the older end degrades to 15 fps, then 7.5. Thirty seconds of history is
preserved in every case; only the granularity of the old end moves.

The UI must **say which frames are thinned**, because a scrub timeline that
looks uniform while stepping 1/30 s in one place and 1/7.5 s in another is a
lying interface. Ticks on the timeline, denser where the frames are.

---

## 2. Scrub buffer

### 2.1 Server-side, not in the browser

The frames originate on the Pi, and three of the four things the operator asked
for -- save, copy, **send to the harness for analysis** -- want them there. A
browser-side buffer would have to upload them back to be analysed, and would
lose everything on a reload, which is the moment you most want it.

It also means one buffer serves every viewer and the harness at once, and that
`vcctrl` can ask "what did the screen look like 12 seconds ago" without a
browser being open at all. That is a genuine capability the live stream does
not provide.

### 2.2 What has to change in the ring

- **Frames need sequence numbers.** The video ring stores `(t, frame)`; the
  audio ring already stores `(t, seq, chunk)`. Scrubbing needs stable
  addressing that survives eviction, so video gets the same.
- **Pinning.** The moment you scrub back, the live stream keeps writing and the
  ring keeps evicting -- so the frame you are looking at can be freed while you
  look at it. Entering scrub mode **pins** the buffer: eviction stops and new
  frames are dropped instead, with the UI saying so. Leaving scrub unpins and
  the buffer refills. Without this the feature is subtly broken in exactly the
  case it exists for: a long look at an interesting moment.
- **A memory guard.** Before growing the ring, read `MemAvailable` from
  `/proc/meminfo` and refuse to exceed a floor (say 200 MB). No swap means the
  budget is not advisory.

### 2.3 API

    GET /timeline.json?from=&to=   frame index: seq, t, bytes, thinned flag,
                                   plus event markers in the same time base
    GET /frame.jpg?seq=N           one exact frame by sequence number
    GET /clip.mkv?from=&to=        muxed clip of a scrub selection
    POST /cmd {"cmd":"pin"}        stop eviction while examining
    POST /cmd {"cmd":"unpin"}

The timeline and the event markers share one time base because they already do
-- the bus stamps `t` from the same clock the frame ring does. That is what
makes "the harness typed `RB` at this exact frame" a lookup rather than a
correlation exercise.

### 2.4 The UI

- A **timeline strip** under the video: position, ticks showing frame density,
  and **event markers** from the bus -- keystrokes with their literal text,
  power events, lock changes, `video.frozen` transitions.
- **Frame stepping**: `←`/`→` for one frame, `Shift` for ten, `,`/`.` as
  aliases. Space to play/pause. A **Live** button to return to the edge, which
  must be obvious, because a KVM showing 20-second-old footage while looking
  live is the worst failure this tool can have (WEBKVM sec. 6.0).
- **While scrubbing, the picture is captioned with its age and the veil rules
  still apply.** The existing `frozen`/`nosignal` logic is about the live
  stream; scrub mode is a separate, explicit state and must be labelled as one.
- **Jump to previous/next event marker** -- the fastest way to find the moment
  something happened is to move between the things the harness did.

### 2.5 Save, copy, send

- **Save frame** -- downloads the JPEG. Works today.
- **Copy frame** -- `navigator.clipboard.write()` is **secure-context only**,
  so it does not work on the plain-http URL. Third feature to need HTTPS, after
  WebRTC and AudioWorklet (WEBKVM-AUDIO sec. 5). At some point `tailscale
  serve` stops being optional.
- **Save clip** -- a scrub selection muxed to a file. **MJPEG frames copied
  without re-encoding**, so a clip of a defect is bit-exact with the frames
  that were captured; this is the pixel-level QA path and must not be lossy.
- **Send to the harness** -- write the clip into a directory the VM already
  reaches, and return its path. The harness is a peer process on a machine
  30 ms away, so "send" is "write it where they can read it and say where".

---

## 3. Recording

Separate from the scrub buffer: explicit start/stop, written to disk, encoded.

### 3.1 Pipeline

    MJPEG ring ─┐
                ├─> ffmpeg ─> h264_v4l2m2m + AAC ─> /var/lib/vcctrl/rec/*.mkv
    PCM ring   ─┘

The daemon already owns both streams, so recording is a *third consumer* of
what it holds rather than a new capture. ffmpeg takes MJPEG on stdin and PCM
from a FIFO, muxing both. Rule 3 holds: the encoder is a subprocess, and if it
dies the recording stops and nothing else notices.

**Hardware H.264 at 640x480 is well within a Pi 3**, and the encoder
(`/dev/video11`, `h264_v4l2m2m`) is currently unused. Expect roughly
7-15 MB/min against MJPEG's 27-126, so ten minutes is ~150 MB rather than
possibly a gigabyte.

### 3.2 The risk that will actually bite: A/V sync

An MJPEG stream carries no timestamps, and this capture path is **not** a
reliable 30 fps -- it drops frames when the source is idle and stops entirely
when the stick is not locking (WEBKVM sec. 4.5, and on the Mach64 a whole game
run produces nothing). ffmpeg told to assume constant frame rate will drift
audio against video, and over ten minutes that drift is not subtle.

So: `-use_wallclock_as_timestamps 1` on both inputs, variable frame rate
output, and **verify against a clap test** -- something that makes a sound and
a visible change at the same instant, checked at the start and end of a ten
minute recording. This is the item most likely to need a second attempt, and
the one where a plausible-looking file is worst, because it looks fine until
you rely on it.

### 3.3 What happens when the picture stops

A recording running across a mode change gets a video stream that simply
**stops delivering** while audio continues. That is correct behaviour on this
rig and the file must represent it honestly: a gap, not a freeze-frame silently
padded to look continuous. Wallclock timestamps give that for free, and it is
worth asserting in a test, because "the picture froze for two minutes" and "the
recorder padded two minutes of the last frame" are indistinguishable
afterwards and mean completely different things.

### 3.4 Controls and lifecycle

- **A record panel**: start/stop, elapsed time, current size, and free space on
  the card.
- **A hard cap** -- stop automatically at 15 minutes or 500 MB, whichever
  first, and say why it stopped. "Minutes" was the stated need; an unbounded
  recorder on a 21 GB card with no swap is a slow-motion outage.
- **Recordings list**: name, duration, size, download, delete.
- **Retention**: cap the directory at ~2 GB, delete oldest first, and log it.
  Silent deletion of something the operator recorded deliberately is not
  acceptable; it goes in the activity log.
- **Download on stop**, with `Content-Disposition: attachment`. The file is on
  the Pi first, so a dropped connection, a locked phone or a backgrounded tab
  costs nothing -- which is the whole reason for choosing that path.
- **The recording survives the browser leaving.** It is a daemon-side process;
  closing the tab must not stop it, and the UI must show a recording that is
  already running when a page loads.

### 3.5 Event sidecar

Markers on the timeline were the chosen option. Writing a sidecar `.events.txt`
next to each recording was offered and not taken -- but it is nearly free once
the markers exist, and a clip sent for analysis with no record of what the
harness was doing is much less useful. **Proposed anyway as a one-line
addition**, to be dropped if unwanted.

---

## 4. Build order

| step | deliverable | gated on |
|---|---|---|
| 1 | Sequence numbers on video frames; byte-capped ring with adaptive thinning; `/proc/meminfo` guard | nothing |
| 2 | `pin`/`unpin`, `/timeline.json`, `/frame.jpg?seq=` | 1 |
| 3 | Scrub UI: timeline, stepping, Live button, age caption | 2 |
| 4 | Event markers on the timeline | 3 |
| 5 | Save frame, save clip (MJPEG copy), send-to-harness path | 2 |
| 6 | Recorder: FIFO plumbing, `h264_v4l2m2m`, start/stop/status | audio landing first |
| 7 | Recordings list, download, retention, caps | 6 |
| 8 | A/V sync verification and the gap test (3.2, 3.3) | 6 |
| 9 | HTTPS, which unblocks copy-to-clipboard | -- |

Steps 1-5 need no new device and no daemon restart beyond deploying, so they
can land alongside the harness's work. Step 6 needs the audio capability, which
needs `hw:1,0`.

---

## 5. Open questions

1. **Does `h264_v4l2m2m` actually work on this Pi's 32-bit userland at
   640x480?** The encoder node exists and ffmpeg lists it. That is a label, not
   a measurement, and this rig has been bitten by exactly that distinction
   before. Test before building the UI on top of it.
2. **How much does encoding cost while a sweep runs?** The structural argument
   that the Pi cannot perturb the g2k (WEBKVM sec. 11 q4) still holds -- no
   shared resource. But recording is the first feature that puts sustained load
   on the Pi, and if the harness's own timing degrades, that is a Pi-side
   problem worth knowing about.
3. **Should the scrub buffer keep audio too?** 30 s of PCM is 5.8 MB and makes
   a scrub selection exportable with sound. Assumed yes at 8 MB; cheap to drop.
4. **Does pinning need a timeout?** A pinned buffer stops accepting new frames.
   If someone pins and walks away, the live view is stale until they return.
   Proposed: pin expires after 5 minutes with a visible countdown, because the
   failure mode of a silently stale KVM is the one this tool exists to prevent.
