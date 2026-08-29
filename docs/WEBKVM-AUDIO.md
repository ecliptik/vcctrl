# Audio for the web KVM

**Branch:** `webkvm`. **Status:** plan only. Written 2026-08-19.
Companion to [WEBKVM.md](./WEBKVM.md); its section numbers are referenced
throughout and its rules apply unchanged.

Goal, from the operator: *it's being captured and we have verified it's there,
would be nice to hear music/sfx.*

So this is a **listening** feature, not a measuring one. That distinction is
load-bearing and section 4 explains why it cannot be quietly widened.

---

## 1. What the hardware actually is  **[measured 2026-08-19]**

Read off the Pi for this plan:

    card 1: U0x010xff02 [USB Device 0x01:0xff02], device 0
    hw:1,0   Format S16_LE   Channels 2 (FL FR)   Rates: 48000   Bits 16
    Endpoint 0x82 (2 IN) (ASYNC), packet interval 1000 us

**One rate, and it is the right one.** 48 kHz is what a browser `AudioContext`
runs at by default on desktop, so **no resampling is needed anywhere** -- not
on the Pi, not in the page. That removes the single most annoying class of bug
in streaming audio, and it happens by luck rather than design, so it is worth
writing down before someone "improves" the capture settings.

**Raw PCM is affordable here.** 48000 x 2ch x 2 bytes = **1.536 Mbit/s**.
Against video's measured 7-17 Mbit/s that is noise, and it means the MVP needs
no encoder on the Pi and no decoder in the browser. Mono halves it to
768 kbit/s if a phone ever needs that.

**Live sample taken while writing this**, during a Mach64 game cell:

    mean -68.8 dB   peak -47.9 dB   spread 20.9 dB across 8 buckets
    VERDICT: AUDIO PRESENT -- spread is wide, which is what music looks like

So there is real music on the wire right now. The feature is not speculative.

---

## 2. The constraint that shapes everything: what holds the device

ALSA `hw:` devices are exclusive. The daemon taking `hw:1,0` for its lifetime
is the same move it made for `/dev/video0` (WEBKVM sec. 4.1) and it has the
same consequence -- **`bin/vcctrl-audio` would get EBUSY** -- but the stakes
are higher, because of what that tool is. From its own header:

> audio comes off the capture stick's ALSA device. Sampling it touches neither
> the keyboard nor `/dev/video0`, so unlike every other liveness check in this
> harness it is safe to run **DURING a cell**. That makes it the only
> observation channel that cannot perturb a measured run.

**Breaking that would remove the harness's only mid-cell observation channel.**
So the shim is not a follow-up here; it ships in the same commit as the daemon
taking the device, exactly as `grab()` did.

The good news is the same as it was for video: it gets *better*, not merely
unbroken. With the daemon holding a rolling PCM ring, `vcctrl-audio` computes
its levels from memory instead of spawning a 3-second ffmpeg capture. The
during-a-cell guarantee strengthens too -- today the tool opens a device
mid-cell, and after this it touches no hardware at all.

**The alternative considered and rejected:** ALSA's `dsnoop` plugin lets
several readers share one capture device, which would avoid the shim
altogether. Rejected because it puts the sharing policy in a config file
nobody on this rig has ever edited, and because the ring buffer is wanted
regardless -- it is what makes both the level query and the "save the last N
seconds" idea (sec. 7) cheap. One owner, one policy, same shape as video.

---

## 3. Transport: PCM over a second WebSocket

    ffmpeg -f alsa -i hw:1,0 -acodec pcm_s16le -ac 2 -ar 48000 -f s16le -

A subprocess, per rule 3 (WEBKVM sec. 2). Raw interleaved S16LE on stdout,
chunked into ~20 ms frames (3840 bytes) and pushed to subscribers. No encode,
no decode, nothing on the per-chunk path but a memcpy -- the same discipline as
the video fan-out, and for the same reason.

**A separate socket at `/wsaudio`, not a channel on the video socket.** Video
frames are currently raw JPEG bytes in binary WebSocket frames; adding audio to
that socket means a type prefix on every frame, which touches the video path to
add a feature that is optional and off by default. A second socket costs
nothing, has an independent lifecycle, and makes "audio only streams when
someone is listening" fall out for free rather than needing a flag.

**Nothing streams until a viewer asks.** Silence at 1.5 Mbit/s is still
1.5 Mbit/s.

### Why not Opus, or WebRTC, or an `<audio>` tag

- **Opus** would cut bandwidth ~20x, but decoding it in-page needs WebCodecs
  `AudioDecoder`, which is recent in both target browsers and is exactly the
  kind of dependency that produced a blank screen on an iPhone last night.
  Bandwidth is not the constraint here; compatibility is.
- **WebRTC** is the right long-term answer for both A/V and it is already on
  the 6.3 TODO. It needs a secure context, which is section 5.
- **`<audio src="/stream.wav">`** is the tempting one-liner: browsers buffer it
  aggressively, giving multi-second latency and no way to flush. Fine for a
  radio station, useless for hearing whether a sound effect fired when
  something happened on screen.

---

## 4. What this must not be allowed to become

`vcctrl-audio` is emphatic, and it is right:

> **ABSOLUTE LEVELS ARE NOT COMPARABLE ACROSS SESSIONS.** The signal path is
> speakers-headphone-out into the stick, so a physical volume knob sits in the
> middle. A level that means "music" today can mean "silence" tomorrow with no
> change to the machine.

The page will show a level meter (sec. 6), because it is nearly free and it
answers "is there sound at all" when you cannot hear -- a muted phone, a
meeting, an iPhone ringer switch. **It must be labelled as relative and must
never be recorded into anything a comparison is built on.** Digital silence is
the one unambiguous reading: mean == max at the floor means no signal on the
wire rather than a low knob setting.

The rule from WEBKVM sec. 11.1 applies directly: *ask what a signal attests,
not what it correlates with*. This meter attests "the stick is receiving a
varying signal". It does not attest volume, correctness, or that the right
notes played.

---

## 5. The blocker nobody will expect: AudioWorklet needs HTTPS

**`AudioWorklet` is a secure-context API.** On `http://<tailnet-ip>:8080` it
does not exist, in either target browser. That is the modern, low-jitter way to
play a PCM stream and it is simply unavailable on the URL this tool is served
from today.

Two responses, and the plan takes both:

1. **MVP uses scheduled `AudioBufferSourceNode`s**, which need no secure
   context. Decode nothing; wrap each arriving chunk in an `AudioBuffer` and
   schedule it at a running cursor a fixed lead ahead of `currentTime`. This is
   how everyone did it before worklets, it is not deprecated (unlike
   `ScriptProcessorNode`), and it works today on plain HTTP in both browsers.
2. **Audio makes HTTPS worth doing now.** WEBKVM sec. 3 already recommended
   `tailscale serve` and noted that WebRTC and the clipboard API need a secure
   context; audio adds a third, and it is the first one the operator will
   actually notice. One command, a real cert for the `ts.net` name, and the
   AudioWorklet path becomes available as a straight upgrade.

**Do not discover this at implementation time.** It is the kind of constraint
that reads as "audio is broken in Safari" when it is really "this URL is not a
secure context".

---

## 6. The page

- **A `🔊 Sound` button, off by default.** Both target browsers block audio
  until a user gesture, so this is required, not a preference. It also means
  the stream is not running for viewers who did not ask.
- **A level meter** beside it: peak and mean in dBFS, updated a few times a
  second from the same samples. Labelled *relative*, per section 4.
- **A jitter buffer of ~100-150 ms**, adaptive within bounds. If the cursor
  falls behind `currentTime` the buffer underran; skip forward rather than
  accumulating delay. Same principle as the video backpressure rule: for
  monitoring, **late audio is worth less than current audio**.
- **Explicit state, like everything else here.** `audio: streaming | silent |
  no device | not requested`. Silence must be distinguishable from a dead
  capture path -- that is section 4's one unambiguous reading, and it is the
  same discipline that made the video path report `frozen` rather than
  `locked`.

### Known browser traps, worth stating before they cost a debugging session

- **The iPhone ringer switch mutes Web Audio.** A muted phone will show a
  moving level meter and play nothing, and there is no reliable way to detect
  or override it in Safari. The meter is what makes that diagnosable.
- **`AudioContext` starts suspended** until a gesture; `resume()` must be
  called from inside the click handler, not after an `await`.
- **The context's sample rate may not be 48000** -- iOS has historically used
  44100. Request `new AudioContext({sampleRate: 48000})`, and if the context
  reports something else, resample linearly in the chunk builder. For
  monitoring, linear interpolation is inaudible and a resampler is not worth
  the code.
- **A backgrounded tab suspends the context.** The video path already recovers
  on `visibilitychange` (WEBKVM sec. 6.1); audio hooks the same handler.

---

## 7. Build order

| step | deliverable | gated on |
|---|---|---|
| 1 | `AudioCapability`: ffmpeg subprocess, byte-capped PCM ring, `audio state`, dBFS from the ring | nothing |
| 2 | **Shim `bin/vcctrl-audio` onto the daemon**, with the direct-ffmpeg fallback gated on the daemon being down. Ships **with** step 1, not after | 1 |
| 3 | `/wsaudio` fan-out, subscriber-gated | 1 |
| 4 | Page: Sound button, scheduled `AudioBufferSourceNode` playback, jitter buffer | 3 |
| 5 | Level meter + explicit audio state | 3 |
| 6 | `tailscale serve` HTTPS, then AudioWorklet as an upgrade | 4 |
| 7 | Mono / lower-rate option for constrained clients | 4 |
| 8 | "Save the last N seconds" from the ring, as QA evidence | 1 |

Steps 1-2 need the ALSA device but **not** `/dev/video0` and **not** the
keyboard, so they can land while a sweep is running -- which is the same
property that makes `vcctrl-audio` special in the first place.

---

## 8. Open questions

1. **Does a persistent ALSA reader perturb a measured run?** The structural
   argument from WEBKVM sec. 11 q4 applies unchanged -- the Pi and the g2k
   share no resource, and the capture stick is a passive tap -- and it is
   stronger here, because the audio path is already documented as safe to
   sample during a cell. But "sampling for 3 seconds" and "holding the device
   open for days" are different claims, and the benchmarking session owns that
   judgement, not this document.
2. **Does holding both the video and audio interfaces of the same USB device
   change either?** Watch the **distinct-hash count**, not the frame count --
   the `vcctrl` session's sharpening, and it is the right one. The frame count
   is exactly what fooled both of us over text mode 03h: frames kept arriving
   at 30 fps and the stream was dead. That stick has now surprised us twice, in
   the same direction each time, by continuing to do something when we
   predicted it would stop. They are separate interfaces on one stick sharing one USB
   bus. Video peaks around 2 MB/s and audio adds 192 KB/s against a bus with
   far more headroom, so this should be nothing -- but "should be nothing" is
   what was said about the frame rate before it turned out the stick repeats
   flat black rather than stopping. Watch `framestats` after audio goes live.
3. **Is A/V sync good enough to be useful?** Video lands ~100 ms out, audio
   ~150-250 ms with the jitter buffer, so audio will lag picture slightly.
   Nothing here needs lip-sync, but "did the sound effect fire when the thing
   happened on screen" is a real QA question and the answer may need the
   numbers rather than an opinion.
4. **What is the ring worth in seconds?** 1.5 Mbit/s means 10 s costs ~1.9 MB.
   Cheap, and it makes step 8 nearly free -- but it is byte-capped like the
   frame ring, for the reason rule 3 does not cover memory.

---

## 9. Built, not yet deployed  **[2026-08-19]**

Everything below is written and unit-tested against synthetic signals. It has
**not** run against the rig: taking `hw:1,0` needs a daemon restart, and that
drops the uinput devices for USB4VC's 0.75 s rescan, so it waits for a window.

- `AudioCapability` — owns `hw:1,0`, byte-capped PCM ring, liveness watchdog
  with the fast-failure backoff, `audio state|acquire|release`, `level`.
- `bin/vcctrl-audio` shimmed onto the daemon, with the same
  fallback-only-if-the-daemon-is-actually-down rule that `grab()` ended up
  with.
- `/wsaudio` fan-out, subscriber-gated, starting at the live edge.
- Page: Sound button, scheduled `AudioBufferSourceNode` playback, level meter.

### The scale had to be proved, not assumed

Every reference level in FINDINGS -- the -30.8 dB working figure, the -65.6 dB
floor -- was read off `ffmpeg -af volumedetect`, and `vcctrl-audio`'s verdicts
are tuned to those numbers. Computing levels a different way and calling them
dB would have invalidated all of it silently. So the implementation was
measured against ffmpeg on the same synthetic signals:

| signal | ffmpeg mean/peak/buckets | this |
|---|---|---|
| sine -20 dBFS | -23.0 / -20.0 / 1 | -23.01 / -20.00 / 1 |
| sine -40 dBFS | -43.0 / -40.0 / 1 | -43.05 / -40.02 / 1 |
| sine -60 dBFS | -63.4 / -60.2 / 1 | -63.41 / -60.21 / 1 |
| digital silence | -91.0 / -91.0 / 1 | -91.00 / -91.00 / 1 |
| dither floor | -87.3 / -84.3 / 1 | -87.28 / -84.29 / 1 |

Worst deviation **0.05 dB**. Two things had to be fixed to get there, and both
are the kind of error that would have shifted a scale quietly:

**`audioop.rms` returns an integer.** At the noise floor an RMS of 1.41
truncates to 1, which reads -90.3 dB instead of -87.3 -- a 3 dB error sitting
exactly where "connected but silent" is told apart from "nothing on the wire",
which is the one judgement this tool makes that nothing else can. RMS is
computed in floating point over a strided subsample instead, which is also
immune to `audioop`'s removal in Python 3.13.

**ffmpeg does not print every non-empty histogram bucket.** It walks down from
the loudest and stops once the printed buckets cover 0.1% of samples, so the
count means "how many dB of headroom hold the loudest 0.1%" -- a crest-factor
measure. A naive distinct-count gave 36 where ffmpeg gave 1. `vcctrl-audio`
prints `len(hist)`, so the daemon returns the truncated histogram rather than
the full one.

### And the tests were wrong twice before the code was right once

The first version asserted a -60 dBFS sine should read -60.00. It reads -60.21,
because the amplitude quantises to integer 32 -- and ffmpeg says -60.2 too. The
second asserted RMS should sit 3.01 dB below peak, which is the identity for a
*continuous* sine; at 32 integer steps the real RMS is 0.19 dB off it, and
again ffmpeg agreed with the measurement rather than the identity.

Both failures blamed the measurement for the generator's rounding. The tests
now compare against the exact RMS of the samples that were actually generated.
**A test asserting an ideal is testing arithmetic, not code**, and when it
disagrees with an independent implementation it is the more likely one to be
wrong.

---

## 10. The Opus side-stream  **[built 2026-08-29, spike-verified]**

Raw PCM costs every public listener 1.536 Mbit/s over the funnel (sec. 1).
The mirror's page now asks for `/wsaudio?codec=opus` instead — Ogg Opus at
128 kbit/s (`capabilities.audio.settings.opus_bitrate`), ~12× less — and
falls back to the unchanged PCM stream if the decoder cannot start. One
encoder in vcctrld, fed from the PCM ring (the ALSA device stays
single-open, sec. 2), spawned on the first Opus listener and killed on the
last; one whole Ogg page per WebSocket frame.

**Decode is WASM in the page, not WebCodecs, and that is a compatibility
decision**: as of 2026-08, WebCodecs audio is absent from Firefox for
Android entirely and from Safari before 26, while the vendored
`ogg-opus-decoder` (vendor/README.md) runs wherever the page itself does.

**The spike that gates the design** (conditions: ogg-opus-decoder 1.7.5,
headless Chromium on the control host, Debian 13, 2026-08-28; input a 10 s
440 Hz stereo tone encoded by ffmpeg 7.1.5 with the exact production flags
`-c:a libopus -b:a 128k -frame_duration 20 -f ogg -page_duration 20000`;
stream split into its 503 pages and fed one page per `decode()` call):

| scenario | pages fed | samples out | decode errors |
|---|---|---|---|
| full stream from its true start | 503 | 479,688 (10 s − 312 preskip) | 0 |
| headers replayed + join at 50% | 253 | 239,688 (≈ the 5 s fed) | 0 |
| headers + a 50-page (~1 s) hole | 453 | 431,688 (≈ the 9 s fed) | 0 |

So the two behaviors the whole design leans on — **header replay for a
late-joining listener, and page-granularity skip-ahead for a slow one** —
hold in the real decoder with zero errors. Decode ran ~70× realtime in
that environment; phones are slower, but a 20 ms page budget leaves two
orders of magnitude of headroom. Not yet measured: the same matrix on
real iOS Safari and Firefox-for-Android devices (the operator's device
test), and the delivered bytes/s over the funnel before/after.

Two facts found the hard way, so they are written where the next person
will look:

- **`decode()` returns a Promise in the 1.7.5 dist build** although the
  package's own `types.d.ts` declares it synchronous. An unawaited call
  "succeeds" with zero samples and no error — the first spike run reported
  exactly that for a pristine stream, and the bug was in the caller.
- **ffmpeg's Ogg muxer defaults to one-second pages.** Without
  `-page_duration 20000` the stream is valid, decodes perfectly, and
  carries a hidden second of latency that no error will ever point at.
- **ffmpeg analyzes even a fully-specified raw input for ~5 seconds.**
  Found live 2026-08-28, operator-reported as "audio takes a few seconds
  after unmute": a fresh encoder emitted its first Ogg page ~4.5 s after
  spawn, because `analyzeduration` (default 5 s) buffers input before the
  muxer initializes -- and input arrives at capture pace, so analysis time
  is real time. `-probesize 32 -analyzeduration 0` took first-page latency
  from 4.5 s to 0.09 s (measured on the control host, realtime-paced 20 ms
  chunks into the exact production command; the encoder test now asserts
  the bound so the flags cannot be quietly dropped). `-flush_packets` was
  tested and is NOT the fix -- output already flows per-page once the
  muxer is up. End-to-end after deploying this plus the mirror's wake
  event (conditions: WSS listener through the funnel from the control
  host, tap-to-third-audio-frame): **0.21 s cold encoder, 0.18 s
  immediate re-unmute, 0.23 s settled** -- against ~4.3 s for all warm
  cases before.

The page-size overhead of 20 ms pages is real but small: the 10 s / 128k
test stream weighed 180,630 bytes ≈ 144 kbit/s on the wire, container
included.

**Deployed and measured live 2026-08-28** (conditions: the real rig
capturing its idle input, one WSS listener opened through the Tailscale
funnel from the control host, first 8 frames examined): OpusHead, then
OpusTags, then audio pages with advancing granules, averaging **338 bytes
per 20 ms page ≈ 135 kbit/s on the wire** against raw PCM's 1,536 —
an 11× cut at this bitrate on this content. The daemon's encoder spawned
on that listener's attach and was gone (`audio.opus.running: false`,
zero listeners) within seconds of the disconnect, so idle still costs
nothing. The same deploy confirmed, from the box itself, what the
security audit could only infer from DNS: the mirror IS served over
Tailscale Funnel. Still owed: the real-device matrix (iOS Safari,
Firefox Android) — the operator's device test.
