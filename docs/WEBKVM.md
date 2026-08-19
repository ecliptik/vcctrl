# A web KVM for the g2k, and the daemon merge it implies

**Branch:** `webkvm`. **Status:** plan only, nothing built. Written 2026-08-19.

The goal, in the operator's words: *see the VGA output, use the keyboard and
mouse, send/receive files and power on/off just like the Claude Code harness
does -- so I can see what is going on with automated testing and take over or
interact if needed, or just use the PC as a remote KVM like a JetKVM.*

Target browsers **Firefox and Safari**. Lowest practical latency, accepting
that the path runs through the Pi.

This is a front-end onto the harness that already exists. It adds one genuinely
new mechanism -- a persistent owner of the capture device -- and folds the
result into a single daemon that both the browser and Claude drive as equal
clients (section 2, revised on the operator's direction after the first draft).

---

## 0. Decisions already taken

Answered by the operator before writing this:

| question | answer |
|---|---|
| Capture device during sweeps | **Streamer owns `/dev/video0` always**; `shot`/sweep read through it |
| Video transport | **MJPEG over WebSocket now**, WebRTC later if measurement disappoints |
| Access | **Tailscale only** -- no LAN bind, no password |
| v1 scope | **Video + keyboard + power.** Mouse and files come after |
| Daemon shape | **One daemon named `vcctrl`**, capability modules, browser and Claude as equal clients (sec. 2) |

Added by the operator mid-planning: *having the option to select between video
delivery would be nice, which we can add later, as well as other video tweaks
to help performance. Add as a TODO, let's get an MVP working first.* That is
section 6.3 and it shapes one thing in the MVP -- the frame source is kept
behind an interface so a second transport is an addition, not a rewrite.

Confirmed by the `vcctrl` peer session, which owns the automation side:

- It **wants** the streamer. A per-shot capture costs it ~40 s end to end; its
  sweep monitor polls at 15 s intervals it cannot actually meet, and it has
  twice mis-diagnosed machine state from a frame that was half a minute stale.
- Two invariants must survive the move (section 4.4).
- **mTCP FTP works end to end today**, ~880 KB/s, NIC up. The CF reader is no
  longer the only route.
- **Mouse is unproven against DOS.** The uinput device exists and `vcctrl
  mouse` exists; nothing has ever exercised it against the g2k.
- **Reset GPIO is not wired.** Kasa is the only power control. Cold start to
  prompt ~90 s.

---

## 1. What the rig actually is  **[measured 2026-08-19]**

Facts gathered off the Pi for this plan, because several of them change the
design:

**Pi 3 Model B rev 1.2**, Raspbian 12 bookworm, **armv7l** (32-bit -- USB4VC
requires it, PLAN sec. 7), 4 cores, **920 MB RAM**, `wlan0` only at a
**130 Mbit/s** link rate, signal 59. `eth0` is down. Tailscale is up as
`usb4vc` / `100.64.0.1`.

**Capture stick** -- `0001:ff02` "Fry's Electronics", the MACROSILICON. Offers
**MJPG and YUYV**, 640x480 among many sizes, at 60/50/30/20/10 fps. Its
companion **USB audio capture** is ALSA card 1.

**Hardware video encoders exist**: `/dev/video11` is `bcm2835-codec-encode`,
and ffmpeg has `h264_v4l2m2m`, `h264_omx` and `vp8_v4l2m2m`. Relevant only to
the WebRTC TODO; the MVP encodes nothing.

**Frame sizes, from a real burst the peer session had just taken** (mode 12h,
640x480, the DOS prompt):

    /tmp/vcraw01-03.jpg   2602 bytes each   settle frames, flat black
    /tmp/vcraw04-16.jpg   ~5.5 KB each      real picture

So a 640x480 MJPEG frame of DOS text is about **5.5 KB**. At 30 fps that is
**1.3 Mbit/s** -- two orders of magnitude inside the wifi budget. Game content
will be larger, plausibly 20-30 KB, so ~6 Mbit/s. **Bandwidth is not a
constraint on this rig**, which is the single strongest argument for shipping
raw MJPEG and not encoding anything.

**Python 3.11.2, PIL 9.4.0.** No numpy, no websockets, no aiohttp. apt offers
`python3-websockets` 10.4 and `python3-aiohttp` 3.8.4.

### The measurement that kills the obvious optimisation

The tempting bandwidth win is "hash each frame, don't resend duplicates" --
DOS screens are static most of the time. Checked against the same burst:

    01,02,03  6d79c8a1870cec915f636044a4804912   identical
    04..16    thirteen distinct hashes

The three settle frames are byte-identical. **Every frame of real picture is
distinct** -- VGA is analog, so sampling noise makes consecutive captures of a
provably static screen differ at the byte level. **Exact-hash dedup buys
nothing on live content.** It is still worth keeping for the no-signal case,
where it collapses a stream of identical flat frames to nothing, but it must
not be presented as a general bandwidth strategy. A perceptual measure would
work and costs a decode per frame, which is the expensive thing we are trying
to avoid. Ship without it; revisit under section 6.3 only if a measurement
demands it.

---

## 2. Architecture -- one daemon, capability modules

**Revised on the operator's direction, mid-planning:**

> if/when the kvm is running, could we directly use /dev/video0 and not have to
> go through vcctrl, instead sending the stream to both the remote KVM and
> vcctrl daemon? Ideally at some point having the KVM as the lead daemon (named
> vcctrl) and having the ClaudeCode/API access to it would be better, then we
> could make it like a plugin type architecture.

Yes to all of it, and the first half is already how this works -- worth saying
plainly because the wording suggests a hop that does not exist. **`vcctrld` has
never touched video.** Nothing goes "through" it to reach the capture stick;
today every capture is a fresh `ffmpeg` spawn against `/dev/video0` with no
daemon involved at all. The streamer opens the device directly and fans frames
out to both consumers. There is no relay in the path either way.

The second half is the better idea, and it is worth adopting **now** rather
than after the MVP, because it is nearly free at this stage and expensive to
retrofit. The plan originally called for a second daemon, `vckvmd`. **That is
dropped.** There is one daemon, it is `vcctrld`, and the web KVM is one of its
capabilities.

```
  browser (Firefox/Safari)        Claude Code / API        vcctrl CLI
     |  HTTPS + WSS                  |  HTTP                  |  unix socket
     |  (tailscale serve)            |                        |
     +---------------+---------------+------------------------+
                     |   one API, one state, one arbiter
  ------------------ v --------- Pi (usb4vc) --------------------------------
   vcctrld  -- core: device ownership, arbitration, event bus, transport
     |
     +-- input      uinput kbd + mouse -> USB4VC HAT -> STM32 -> PS/2
     +-- video      owns /dev/video0, JPEG ring, fan-out to every subscriber
     +-- power      Kasa EP10
     +-- leds       PS/2 return channel
     +-- web        static UI, WS video out, WS input in       [new]
     +-- audio      ALSA card 1 off the capture stick          [later]
     +-- reset      GPIO -> opto -> J31                        [when wired]
     +-- files      FTP staging, CF reader                     [later]
                     |
                     v
             g2k (1995 Gateway 2000, DOS 6.22)
```

### Why one daemon is the right call, and it is not about tidiness

**Arbitration is a correctness requirement, and only a single owner can
enforce it.** PLAN sec. 5.1 is blunt that keystrokes mid-sweep corrupt a run --
the harness drives sweeps, not cells, for exactly this reason. The moment a
browser exists, there are two independent things that can type at the g2k: the
operator and the automation. With two daemons and two API surfaces there is
nowhere to put the rule that stops them colliding. With one, there is exactly
one place, and it is the same place that already holds the device lock.

Three further consequences, all of which the split architecture could not give:

- **Claude and the operator see the same state.** The browser and the API read
  the same lock state, the same frame ring, the same LED values, from the same
  process. "See what is going on with automated testing" stops being *watch a
  video of it* and becomes *watch the harness's actual state*.
- **Claude stops paying the ssh tax.** The peer session measured every `vcctrl`
  call at 1.52-2.55 s and attributes four separate bugs today to loops written
  as though calls were free. An HTTP API on the tailnet is ~10 ms. That is a
  bigger win for the automation than for the browser.
- **New hardware lands as a module.** The J31 reset opto, audio, the Pi 5 port
  (PLAN sec. 7) and file transfer each become one capability registering
  commands and, if they want one, a UI panel. None of them touch the core.

### Why it is not a plugin *loader*

The MVP registers modules from a table in the source. No discovery, no dynamic
import, no third-party plugin contract. The value the operator is asking for is
the **shape** -- capabilities that are separable, each owning one device, each
addable without disturbing the others -- and that shape is worth having from
day one. A real loader is a feature to add when something outside this repo
needs to plug in, and nothing does yet.

### The one real cost, and how it is paid

`vcctrld` is currently the most reliable thing in the rig, and the uinput
devices are the reason. USB4VC only discovers input devices on its 0.75 s scan
(PLAN sec. 2), so a daemon crash does not merely restart -- it drops the
devices and silently loses the first keystrokes afterwards. **Folding a web
server, a video pipeline and, later, a file upload handler into that process
widens the blast radius around the single component that must not fail.**

Three rules that pay for it, and they are load-bearing rather than stylistic:

1. **The input path takes no dependency on any other capability.** Not on the
   web module, not on video, not on the HTTP stack. It must remain possible to
   type at the g2k with every other capability broken or unloaded.
2. **A capability that raises is unloaded, not fatal.** The core catches at the
   module boundary, marks the capability failed, reports it in `state`, and
   keeps serving everything else. A dead video pipeline must never cost the
   operator the keyboard.
3. **Anything that can take the process down stays out of process.** ffmpeg is
   already a subprocess and stays one. This is what keeps a native decoder
   segfault from being a keyboard outage, and it is why the video capability is
   a pipe reader rather than a library binding.

If those three hold, the merge is a straight improvement. If they turn out not
to hold in practice, the fallback is to split device ownership back out -- so
keep the module boundaries real enough that splitting stays cheap.

### Arbitration policy -- needs a decision, and not mine to make

The rule itself is a question for the operator and the benchmarking session,
because it is a comparability judgement about the fps matrix, not an
engineering one. The choice:

- **(a) Locked, with break-glass.** While a sweep holds the input lock, browser
  keyboard is refused, with a visible banner naming what holds it. An explicit
  "take control" button breaks the lock and **marks the run tainted** in its
  log. Recommended: it makes the destructive case deliberate and self-
  documenting, and a tainted run is recoverable where a silently corrupted one
  is not.
- **(b) Advisory.** Input always allowed, every injection recorded, runs
  flagged after the fact. Simpler, but it makes the operator responsible for
  remembering that typing during a sweep invalidates it.

Either way, **the event bus is the part that serves the original request.**
Every injected keystroke, power event, lock acquisition and lock release is
published to subscribers, so the browser can show a live activity log beside
the video: not just *the screen changed*, but *the harness typed `RB` at
19:22:04 and took the input lock*. That is what turns watching a sweep from
spectating into supervision.

### The single most important design constraint

The peer session, unprompted:

> **Every `vcctrl` call costs about 1.5 seconds** -- it is an ssh round-trip
> to the Pi, measured 1.52-2.55 s. That number has produced four separate bugs
> in my tooling today. For a browser KVM it is more than a nuisance: typing at
> 1.5 s per keystroke is unusable.

Everything interactive runs **on the Pi**. No ssh is anywhere in the input
path. This is why the whole thing lives on the Pi rather than on the VM
proxying to it, and it is the same reason the repo already puts all timing
there (README, PLAN sec. 1).

**The `vcctrl` CLI contract does not change.** Same commands, same output; it
talks to a richer daemon over the same socket. The peer session's tooling keeps
working through the merge, and that is a hard requirement -- ~157 banked fps
measurements sit behind it.

---

## 3. Access and transport security

Bind to the tailnet only. Two ways, and the second is recommended:

- `vcctrld` listens on `100.64.0.1:8080` directly. Simple, http only.
- **`tailscale serve https / http://127.0.0.1:8080`** -- `vcctrld` listens on
  loopback and Tailscale terminates HTTPS with a real cert for
  `usb4vc.<tailnet>.ts.net`. Recommended, for a reason that is not about
  security:

**A raw-IP `http://` origin is not a secure context.** Pointer Lock still works
there, but `RTCPeerConnection` and the async clipboard API do not, in both
Firefox and Safari. The WebRTC option the operator wants held open (6.3) and
any future clipboard sharing both require HTTPS. Getting that for free now
costs one command and removes a future blocker. Do this in the MVP even though
the MVP needs neither.

No password, per the operator's answer. Tailscale is the authentication
boundary. Worth one sentence in the README so the choice is on the record:
anything on the tailnet can power-cycle the g2k.

---

## 4. `vcctrld` -- the capture streamer

This is the load-bearing new component. Everything else is UI.

### 4.1 Why it must own the device

`/dev/video0` is single-open (PLAN sec. 4). Today nothing holds it and every
capture is a fresh `ffmpeg -frames:v 16` spawn. That has three costs:

1. **~40 s per capture**, per the peer session's measurement -- device open,
   lock acquisition, the settle frames, then scp back to the VM.
2. **Staleness.** By the time a frame is judged it can be half a minute old.
   This has caused real mis-diagnosis.
3. **It makes the operator's actual request impossible.** "See what is going on
   with automated testing" cannot be built on a device that automation opens
   and closes.

A persistent owner fixes all three at once, and the automation gets *faster*,
not merely unblocked. That is the argument to lead with when the change lands
in the peer session's code.

### 4.2 Frame acquisition

    ffmpeg -hide_banner -loglevel error -f v4l2 -input_format mjpeg \
           -video_size 640x480 -framerate 30 -i /dev/video0 \
           -c:v copy -f mjpeg -

Spawned once, at daemon start, and kept alive. `-c:v copy` means the JPEGs the
stick produced are passed through untouched -- **no decode, no encode, no
scaling anywhere on the Pi.** CPU cost is close to a memcpy, which matters on a
Pi 3 already running USB4VC's scan loop.

Frames are split out of the pipe on `FFD8 ... FFD9`. This is safe: inside
entropy-coded data every `0xFF` is byte-stuffed as `FF 00`, and restart markers
are `FFD0`-`FFD7`, so `FFD9` only ever appears as a genuine EOI. Validate
length anyway and drop anything absurd -- one of the peer's existing shot files
(`vcraw07.jpg`) is **zero bytes**, so partial writes are a thing that happens
here.

Reading V4L2 directly via ctypes would shave one buffer copy. Not for the MVP:
ffmpeg is already proven on this exact device and the saving is well under one
frame time. Listed in 6.3.

### 4.3 The ring buffer

Keep the last ~90 frames (3 s at 30 fps) in memory as raw JPEG bytes. At 5.5 KB
that is 500 KB; at a pessimistic 30 KB it is 2.7 MB. Fine in 920 MB, but cap by
**bytes** and not by count so a high-detail mode cannot balloon it.

The ring is what makes `shot` instant: the frames the caller wants have already
been captured before it asks.

### 4.4 The two invariants that must survive

From the peer session, both learned the hard way, both restated in memory and
in PLAN sec. 4.1:

> **Burst-and-pick-brightest, not single-frame.** The capture path emits
> intermittent flat-black frames while correctly locked. A single grab is not
> evidence of what is on screen.

> **The first frames of a burst are flat-black settle frames.** I once reported
> 11% pixel difference on a provably static screen because a diagnostic
> compared the first and last raw frames. Comparing the *selected* frame of
> each burst gives 0.0%. If your API hands out raw frames, say so loudly.

Both are honoured by making the **selection** the default and the raw frames
the thing you have to ask for by name:

| API | returns |
|---|---|
| `shot` | one JPEG, brightest non-flat of the last N frames. **The default.** |
| `burst` | the last N raw frames, explicitly labelled raw in the response |

Note the settle-frame problem is *weaker* here than it is today, because a
long-lived stream is not constantly re-settling -- settle frames appear after a
device open or a mode change, not continuously. But it does not vanish: a mode
change on the DOS side still produces them, and the browser will show them.
The selection logic stays.

Selection cost: decoding ~10 candidate JPEGs with PIL on demand. Only on
demand -- **never decode the streaming path.** The browser decodes its own
frames; the Pi does not need to know what they look like except when asked.

### 4.5 Signal loss, mode changes, and the watchdog

DOS text mode 03h (720x400 @70) never locks; the game's 320x240 and mode 12h
(640x480 @60) do (PLAN sec. 4). So the stream will genuinely stop delivering
frames during normal operation -- at every reboot, at the boot menu, and
between cells on a payload without the `MODE12` line.

`vcctrld` must therefore treat "no frames" as an expected state, not a fault:

- No frame for **2 s** -> publish `{"state":"nosignal"}` to browsers, which
  show a "no signal / not locked" panel rather than a frozen last frame. A
  frozen last frame is exactly the failure mode that makes a KVM lie.
- No frame for **10 s** -> kill and respawn ffmpeg, reopening the device. Cheap
  and it recovers the case where the stick wedges after a mode change.
- Log every transition with a timestamp. When a sweep is being watched, "the
  picture went away at 19:22:04" is diagnostic data.

**Do not report "nosignal" as "the machine is off".** That inference is exactly
the mistake PLAN sec. 4.1 records, one level up.

### 4.6 What automation calls instead

`vcctrld` exposes `/run/vcctrl.sock`, same JSON-lines shape as `vcctrl.sock`:

    {"cmd":"shot"}                    -> {"ok":true,"jpeg":<base64>,"mean":58.9,
                                          "selected_from":10,"age_ms":120,
                                          "state":"locked"}
    {"cmd":"burst","n":16}            -> {"ok":true,"frames":[...],"raw":true}
    {"cmd":"state"}                   -> lock state, fps, last frame age

and over HTTP, which is faster still for the VM because it skips ssh entirely:

    GET /shot.jpg                     -> the selected frame, ~100 ms over tailnet
    GET /state.json

Migration for the peer session's code is one function. Today `grab()` in
`bin/vcctrl-sweep` ssh's, runs ffmpeg, scp's back -- ~40 s. It becomes an HTTP
GET to the Pi -- ~100 ms, returning `(path, mean)` with the same signature, so
callers do not change at all. **Offer them that shim rather than asking them to
edit call sites.**

`vcctrl shot` on the Pi becomes a `/run/vcctrl.sock` client. Same command, same
output, ~1.5 s instead of ~40 s, and that 1.5 s is now entirely the ssh hop.

### 4.7 Fallback when the video capability is down

`vcctrl shot` must not become dependent on the streamer. If the video
capability is unloaded, failed, or the daemon is not running at all, fall back
to today's one-shot `ffmpeg` path. The automation's recovery path is the last
thing that should acquire a new dependency, and the case where you most need a
frame is the case where something is already broken.

Note this is also the honest answer to rule 2 in section 2: a failed video
capability costs the operator the live picture and costs the automation ~40 s
per frame, and costs neither of them anything else.

---

## 5. Input

### 5.1 What the input path cannot do yet

Reading `daemon/vcctrld.py`, three things block interactive use:

**`serve()` is single-threaded and handles one request per connection, to
completion.** `power cycle` blocks it for 15+ s; `ledwait 5` blocks it for 5 s.
Under a web KVM that means the keyboard freezes whenever anything slow runs --
including the operator's own click on the power button. Fix: **thread per
connection.** `Devices.lock` already serialises the actual device writes, so
the emission path is already safe; `power` and `ledwait` touch no device at all
and genuinely want to run concurrently.

**There is no key down / key up.** The surface is `key` (tap), `hold` (press,
dwell, release, blocking) and `combo` (chord). None of them expresses "the
operator is holding W right now and I do not know when they will let go."
Add `keydown` / `keyup` taking the same key names. This maps cleanly onto the
wire: USB4VC forwards the raw evdev `(type, code, value)` triple over SPI --
`make_keyboard_spi_packet` at `usb4vc_usb_scan.py:155` copies `input_data[2:5]`
straight through -- so down and up are already what the transport carries.

**The key table is a typing table, not a keyboard.** `NAMED_KEYS` covers
letters, digits, F-keys, arrows and modifiers; punctuation exists only inside
`CHARMAP`, reachable through `type`, and there is no keypad, no PrintScreen, no
Pause. A KVM needs every physical key addressable by name for down/up. Extend
the table to the full US PS/2 set.

### 5.2 The unknown that has to be measured

The Pi does **not** translate keycodes. It hands raw evdev codes to the STM32
protocol board, and the Linux-to-PS/2 mapping lives in that firmware. So
**which keys actually arrive at the g2k is not derivable from any source on
the Pi** -- only measurable.

This is the same shape as the finding that keeps recurring in this repo: a
label is not a proof, and a fixed sequence driven by assumption is a latent
bug. The right move is to measure it, and the streamer makes that cheap:
inject each key at a DOS prompt, capture, read back what echoed. A keyboard
sweep that would have cost 100 x 40 s of captures costs about a minute once
frames are free. **Do this early -- it is a good first real use of the
streamer, and it produces a coverage table the UI can grey out keys from.**

### 5.3 What the browser cannot capture, in Firefox and Safari

Honest limitation, stated up front because it is the first thing the operator
will hit:

**The Keyboard Lock API (`navigator.keyboard.lock()`) is Chromium-only.** It is
in neither Firefox nor Safari. So keys the host OS or browser reserves cannot
be delivered to the g2k, however the page is written:

- **Safari:** all `Cmd`-based shortcuts are reserved absolutely.
- **Firefox:** `Ctrl-W`, `Ctrl-T`, `Ctrl-N`, `F11`, and the OS's `Alt-Tab` /
  `Ctrl-Alt-Del`.
- `preventDefault()` handles most single keys and many `Ctrl`/`Alt` chords, but
  not the reserved set above.

Mitigations, all of which real KVMs use:

1. **A macro bar** -- on-screen buttons for `Ctrl-Alt-Del`, `Alt-Tab`, `F11`,
   `Esc`, and the DOS-specific ones this rig cares about. These go over the
   wire as `combo`, which already exists and already works.
2. **Sticky modifiers** -- click `Ctrl` to latch it, then press the letter.
   Covers every chord the browser eats, without a macro per combination.
3. **A visible "keys captured" indicator**, so the operator knows whether the
   page has focus and is swallowing input.

Note `Ctrl-Alt-Del` is worth a button but is **not** a reliable reboot on this
machine: PLAN sec. 3 records that it is swallowed while the game is running,
which is why the reset GPIO is planned at all. The UI should not present it as
"reboot".

### 5.4 Autorepeat

On real PS/2 hardware, typematic repeat is generated **by the keyboard**. Here
there is no keyboard -- there is uinput, and whether the STM32 firmware
synthesises repeat from a held-down state is unknown. Two possibilities, and
the measurement in 5.2 will show which:

- If repeat happens downstream, the browser must send exactly one `keydown`
  and suppress `KeyboardEvent.repeat`, or every held key doubles up.
- If it does not, the browser (or `vcctrld`) synthesises it -- 500 ms delay,
  30 Hz -- and must stop instantly on `keyup`.

Get this wrong in the second direction and a stuck key types forever at a DOS
prompt. **Whatever the answer, `vcctrld` releases every held key when a
WebSocket closes.** A dropped wifi connection must never leave a key down.

### 5.5 Pacing

`DEFAULT_PACE_S = 0.012` gives roughly 40 chars/s, which is faster than anyone
types, so interactive typing needs no change. But note that pacing exists
because USB4VC drains **one event per device per loop pass** with a 5 ms idle
sleep (PLAN sec. 2). A browser can generate events faster than that during fast
typing or a mouse drag, so `vcctrld` must **queue and pace**, never forward
straight through. Dropping events on the floor is worse than adding latency
here.

---

## 6. Video delivery

### 6.1 MVP -- MJPEG over WebSocket

Frames go out as binary WebSocket messages, one JPEG per message. The browser
does:

    const bmp = await createImageBitmap(new Blob([evt.data], {type:'image/jpeg'}));
    ctx.drawImage(bmp, 0, 0);

`createImageBitmap` decodes off the main thread and is supported in both target
browsers. One `<canvas>`, no `<img>` churn, no `URL.createObjectURL` leaks.

Why this and not `multipart/x-mixed-replace` in an `<img>`, which is the
classic MJPEG trick: Safari's support for it has been unreliable across
versions, and a WebSocket gives one connection carrying frames out *and* input
in, with a clean disconnect signal -- which section 5.4 needs for the
release-all-keys rule.

Backpressure: if a client's socket buffer is above a threshold when a frame
arrives, **drop the frame** rather than queue it. A KVM that falls behind and
plays catch-up is worse than one that skips. Track drops and show them in the
UI's stats line.

### 6.2 Latency budget  **[estimated -- to be measured]**

| stage | estimate |
|---|---|
| stick internal + USB | 1-2 frames, 16-33 ms |
| ffmpeg passthrough | <1 frame, ~5-15 ms |
| WS over tailnet wifi | 3-10 ms RTT, 5.5 KB serialises in under a ms |
| browser decode + paint | 5-16 ms |
| **total** | **~50-120 ms glass to glass** |

Comparable to a JetKVM, which is the operator's stated reference point. This is
an estimate built from component figures, not a measurement. **Measure it
properly before claiming it**: display a rolling millisecond counter on the g2k
(or just watch a key echo), point a phone camera at both the CRT and the
browser, and count frames. That is the only honest way to get glass-to-glass.

### 6.3 TODO -- selectable delivery and video tweaks

Per the operator: hold the option open, build it later. The MVP requirement
this creates is only that **the frame source and the frame sink sit behind an
interface**, so a second transport is an addition rather than a rewrite.

Candidates, roughly in order of expected value:

- **Selectable transport in the UI** -- MJPEG/WS vs WebRTC, chosen per session.
  Needs HTTPS, which section 3 already sets up.
- **WebRTC over `h264_v4l2m2m`.** Lower latency and far better bandwidth on
  motion, at the cost of an encode on a Pi 3 and a signalling stack. The
  hardware encoder is present and unused.
- **Adjustable framerate and resolution.** The stick offers 60/50/30/20/10 fps
  and sizes up to 1920x1080. A "quality vs latency" slider is cheap once the
  ffmpeg spawn is parameterised.
- **Direct V4L2 via ctypes**, dropping ffmpeg. Saves a copy and a process.
- **Region-of-interest / change detection.** Expensive on a Pi 3 and the
  measurement in section 1 says bandwidth is not the problem, so this is a
  latency play at best. Low priority.
- **Audio.** The stick's ALSA capture works (-30.8 dB vs -65.6 dB floor, PLAN
  sec. 4.2). Opus over the same WebSocket would let the operator *hear* the
  machine. Genuinely useful for QA and not much work; gated on nothing.
- **Screenshot / clip recording** from the ring buffer -- "save the last 3
  seconds" is nearly free once the ring exists, and would be useful evidence
  when a sweep does something odd.

---

## 7. Power, and the readiness trap

`vcctrld` already implements Kasa control, so this is mostly UI: **On**, **Off**
and **Cycle** buttons plus a live state read. Cycle holds the rails down for
`POWER_CYCLE_OFF_S = 15 s` (raised from 6 s after POST measured 26 s cold but
44 s after a short cycle -- the supply had not drained).

Two things the UI must get right:

**Confirm before cutting mains.** This is a 1995 machine with a running sweep
on it. Off and Cycle get a confirmation; On does not. The operator asked for
JetKVM-like behaviour and this is the one place where a stray click is
expensive.

**Readiness is an edge, not a level.** From the peer session:

> `/sys/class/leds` retains the last state the host published, and a
> powered-off host publishes nothing. A level check for "ready" returns true
> instantly on a machine that has not begun to POST. Use edges, not levels,
> across a power cycle. That cost me a wrong readiness report this morning at
> 2.5 seconds after power-on.

So the status panel shows the three PS/2 LEDs live -- they are the non-video
proof that a keystroke landed, and they are genuinely useful to watch -- but
**must not** derive "machine is up" from their level. Derive it from a change
after the cycle, or from frames appearing, and label a stale reading as stale.
Cold start to prompt is ~90 s; show a timer rather than a spinner.

Add a **Reset** button, disabled with a tooltip, when the J31 opto lands
(PLAN sec. 3). Reset and power are not redundant -- reset is the fast primary,
mains is the fallback.

---

## 8. Files -- phase 2

Deferred out of v1 by the operator's scope answer, and it is the right call,
because the transport underneath has a constraint that shapes the whole UI:

**mTCP FTP is a client only.** The DOS side always initiates; nothing can be
pushed to the g2k unsolicited. And it needs the **NET boot profile** -- from any
other profile there is no packet driver, and getting there costs a reboot.

So a browser "send file" is not a push. It is:

1. Upload to `vcctrld`, which stages it on the Pi and hands it to the VM's
   `serve.sh` FTP root (192.0.2.10:2121, `USER`/`PASSWORD_FROM_ENV`).
2. Verify the gates that already exist and must not be dropped: **sha per
   binary, CRLF on every `.BAT`, ASCII-only** (PLAN sec. 5).
3. **Type `GET.BAT` at a DOS prompt** to make the machine pull. `PUT.BAT` for
   the return direction.
4. Watch it land, on the video stream.

The honest UI consequence: **the transfer button is only enabled at a NET
prompt.** Everywhere else it must be greyed with the reason shown, not left
clickable to fail confusingly. That state is knowable -- the harness already
detects the prompt (`at_prompt`, commit 340b16d) and the boot profile is
readable via `SET`.

Throughput measured by the peer session: ~880 KB/s, 10/10 byte-identical over
nine files. The CF reader on the Pi stays as the recovery path (PLAN sec. 5.6)
and is worth exposing as a second, manual route -- it works when the network
does not, which is exactly when you need it.

---

## 9. Mouse -- phase 2, and measure first

The peer session is explicit: `vcctrl mouse move|click` exists, the uinput
device exists, and **it has never been exercised against the g2k**. There is no
finding recording that a cursor moved. `CTMOUSE` loads in `AUTOEXEC` on every
profile except `CLEAN`, so the DOS side is present.

The cheap test, which should happen before any browser work: boot any profile,
`vcctrl mouse move 200 0`, capture, look. One minute.

If it works, the browser side is **Pointer Lock**, because PS/2 mice are
relative-only -- there is no way to say "the cursor is at 320,240". Two
consequences the UI has to own:

- **Drift.** The DOS cursor and the operator's expectation diverge over time.
  Every KVM with a relative mouse has this. Mitigation is a "park cursor"
  button that slams it to a corner with a large negative move, re-establishing
  a known origin.
- **Acceleration.** DOS mouse drivers apply their own ballistics. Raw browser
  deltas will not feel like the deltas the driver expects, so a scaling factor
  needs tuning by hand, per driver.

Neither is hard, but both are why this is not in v1, and why it should not be
built at all until someone has seen a cursor move.

---

## 10. Build order

| step | deliverable | gated on |
|---|---|---|
| 0 | Coordinate with the `vcctrl` session on a window to take `/dev/video0` | it is actively driving the machine |
| 1 | **Core refactor**: capability registry, threaded `serve()`, input path isolated from everything else. No new features | nothing |
| 2 | Video capability: ffmpeg pipe, frame split, ring, `shot`/`burst`/`state` on the existing socket | 0, 1 |
| 3 | `vcctrl shot` reads the ring, with fallback to today's ffmpeg path; `grab()` shim offered to the peer | 2 |
| 4 | HTTP capability + `tailscale serve` HTTPS + `/shot.jpg` + the event bus | 2 |
| 5 | **WS video to the browser.** A page that shows the g2k live. First real milestone | 4 |
| 6 | Input capability: `keydown`/`keyup`, full key table, the input lock | 1 |
| 7 | Keyboard coverage sweep -- measure what the STM32 actually delivers (5.2) | 5, 6 |
| 8 | **Keyboard in the browser**, macro bar, sticky modifiers, release-all-on-disconnect | 7 |
| 9 | Power panel + live LEDs + activity log, with the edge-not-level rule | 4, 6 |
| 10 | **v1 done.** Measure real glass-to-glass latency and write it down | 5, 8, 9 |
| 11 | Mouse capability: hardware test, then Pointer Lock | 10 + a cursor that moved |
| 12 | Files capability: NET-prompt-gated FTP, CF fallback | 10 |
| 13 | Selectable transport, WebRTC, audio, reset GPIO, the rest of 6.3 | 10 |

Step 1 is deliberately a refactor with no user-visible change, so the merge
lands while the surface is still small and the peer session's tooling can be
checked against it in isolation. Steps 2-5 are the interesting half and depend
on nothing but a window on the capture device. Step 6 is independent of all of
it and can go first if the device is busy.

---

## 11. Open questions

1. **Does the STM32 deliver every key?** Unknowable from the Pi; measured in
   step 6. Shapes what the UI can offer.
2. **Is autorepeat generated downstream?** Same measurement. Gets a stuck key
   or a doubled key if guessed wrong (5.4).
3. **Does the stick survive a long-lived open across mode changes?** Every
   capture to date has been a fresh open. A persistent owner is a new condition
   for this device and the watchdog in 4.5 exists because the answer might be
   no. **This is the main technical risk in the plan** -- if the stick needs a
   reopen per mode change, the streamer still works, but "seamless across a
   reboot" becomes "a two-second gap at each mode change."
4. **Does a persistent capture cost enough CPU to perturb a measurement?**
   The fps matrix is the whole point of the rig. Passthrough MJPEG should be
   nearly free, but *nearly free* is an assertion until it is measured against
   a banked anchor. **Ask the benchmarking session before running the streamer
   during a scored sweep**, and default `vcctrld` to idle-when-no-viewers if it
   turns out to matter.
5. **Does the mouse work at all?** (9)
6. Framerate: the stick offers 60 fps at 640x480. Is 60 worth double the
   frames, given the source is a DOS box? Probably 30. Measure.
7. **What happens when the operator types during a sweep?** Section 2 lays out
   locked-with-break-glass versus advisory and recommends the former, but the
   call belongs to the operator and the benchmarking session, because it is a
   judgement about what invalidates a measurement.
8. **Does the merged daemon hold rule 1** -- that the input path keeps working
   with every other capability broken? Worth an explicit test rather than an
   assumption: unload video, kill the web module, confirm `vcctrl type` still
   lands.

---

## 12. What this deliberately does not do

- **No transcoding in the MVP.** The Pi passes bytes through. Every codec
  question is deferred to 6.3, where it belongs.
- **No recording of sweeps by default.** The ring buffer is 3 seconds. Long
  recording is a 6.3 item and wants a decision about where the bytes go.
- **No OCR.** Same reasoning as PLAN sec. 4: logs come back over the network as
  text now.
- **No LAN bind and no auth.** Tailscale is the boundary, per the operator.
- **No attempt to defeat browser keyboard reservation.** It is not defeatable
  in Firefox or Safari; the macro bar is the answer (5.3).
- **No plugin loader.** Capabilities register from a table in the source. The
  separable shape is worth having now; dynamic loading is worth having when
  something outside this repo needs to plug in, and nothing does (sec. 2).
- **No change to the `vcctrl` CLI contract.** ~157 banked fps measurements sit
  behind the peer session's tooling. The daemon grows; the CLI does not move.
