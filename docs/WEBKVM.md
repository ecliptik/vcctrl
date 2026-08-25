# A web KVM for the g2k, and the daemon merge it implies

**Branch:** `webkvm`. **Status:** plan only, nothing built. Written 2026-08-19.

The goal, in the operator's words: *see the VGA output, use the keyboard and
mouse, send/receive files and power on/off just like the Claude Code harness
does -- so I can see what is going on with automated testing and take over or
interact if needed, or just use the PC as a remote KVM like a JetKVM.*

Target browsers **Firefox and Safari**. Lowest practical latency, accepting
that the path runs through the Pi.

**And the sharper statement of why, which arrived after the first draft:**

> this is why I want the kvm too btw, so I can actively see if something is
> stuck if you get stuck in some loop or wait state

That is not the same requirement as remote convenience, and it changes what
this thing has to be good at. **The KVM's value is being a channel the
automation does not control.** When the harness says RUNNING and the screen
says a DOS prompt, the screen wins, and the operator does not have to take a
Claude session's word for the state of his own machine.

Three failures on 2026-08-19 make it concrete, all from the harness session,
all within a few hours:

- The Pi went unreachable for ~30 minutes. `vcctrl` calls blocked with no
  timeout, which is **indistinguishable from a long-running cell**. Thirteen
  minutes passed before anything was suspected.
- A `pgrep -f netstep` wait loop matched its own wrapper shell and span
  forever, with elapsed time reported as evidence of progress for twelve
  minutes. The operator asking "shouldn't we have a result by now" is what
  surfaced it.
- Four cells reported "done in 152s" and had **never run** -- a 150 s floor
  elapsing at an idle prompt, then a probe finding the prompt it had never
  left.

Every one is answered by a live screen in one glance. Two consequences run
through this whole document: **observation is never gated** (sec. 2), and **a
correct still beats smooth video** (sec. 6.0).

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
| v1 scope | **Video + keyboard + power + activity log.** Mouse and files come after |
| Arbitration | **Locked with break-glass** -- input refused while a sweep holds the lock, explicit override marks the run tainted |
| Daemon shape | **One daemon named `vcctrl`**, capability modules, browser and Claude as equal clients (sec. 2) |
| Sequencing | Core built in the `webkvm` worktree, then **handed to the `vcctrl` session to finish and layer into testing** (sec. 13) |
| Purpose | **Independent verification**, not remote convenience -- observation is never gated, and a correct still beats smooth video |

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
  call at 1.52-2.55 s, since improved to ~0.21 s with ssh ControlMaster, and
  attributes four separate bugs to loops written as though calls were free. An
  HTTP API on the tailnet is ~10 ms. That is a bigger win for the automation
  than for the browser.
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

1. **The input path takes no *logical* dependency on any other capability, and
   is scheduled ahead of all of them.** Not on the web module, not on video,
   not on the HTTP stack. It must remain possible to type at the g2k with every
   other capability broken or unloaded.

   **Restated after review, because the first wording was wrong.** The peer
   session's objection: "achievable as a logical statement and false as a
   resource one, because it is one Python process." Correct. A keystroke that
   does not *depend* on video can still queue behind it under the GIL, and
   "input still works" is not the same claim as "input still works with the
   timing the STM32 expects." So the rule has to name scheduling, not just
   dependency: the input capability gets a **dedicated thread that never
   touches the async loop**, and holds `Devices.lock` across a whole logical
   operation (13.3).

   One refinement in the other direction, because the GIL exposure is narrower
   than the objection assumed: **the per-frame hot path does no base64 and no
   decode.** WebSocket frames are binary, so fan-out is a socket write of the
   JPEG bytes the stick already produced, and the syscall releases the GIL.
   Base64 appears only in the JSON socket API, where a `shot` is a one-off
   request rather than a 30 Hz loop. The exposure that remains is any
   pure-Python framing or copying in the fan-out, which is exactly what must be
   kept out of the hot path.

   **This is settled by measurement, not by argument** -- see 13.2.
2. **A capability that raises is unloaded, not fatal.** The core catches at the
   module boundary, marks the capability failed, reports it in `state`, and
   keeps serving everything else. A dead video pipeline must never cost the
   operator the keyboard.
3. **Anything that can take the process down stays out of process.** ffmpeg is
   already a subprocess and stays one. This is what keeps a native decoder
   segfault from being a keyboard outage, and it is why the video capability is
   a pipe reader rather than a library binding.

   **Rule 3 does not catch memory**, which the review caught: an OOM from an
   unbounded frame ring kills the process exactly as dead as a segfault, and it
   is in-process by construction. Hence the ring is capped in **bytes** (4.3).
   Frames is the wrong unit the moment anyone raises resolution or framerate --
   which is item one on the 6.3 TODO list, so it will happen.

If those three hold, the merge is a straight improvement. If they turn out not
to hold in practice, the fallback is to split device ownership back out -- so
keep the module boundaries real enough that splitting stays cheap.

### Arbitration policy -- needs a decision, and not mine to make

**First, the part that is not a decision.** The lock gates **input only, and
never observation.** The viewer must be able to watch at all times, including
while a sweep holds the lock -- especially then, because that is the moment the
operator most needs to know whether the harness is working or only thinks it
is. A design where the browser goes dark because automation is running would
remove exactly the capability being asked for, at exactly the moment it
matters. This plan already reads that way, and it is written down here as an
invariant so a later "the sweep owns the session" simplification cannot quietly
take it out.

With that fixed, the remaining rule is a question for the operator and the
benchmarking session, because it is a comparability judgement about the fps
matrix rather than an engineering one. The choice:

**Decided by the operator: (a), locked with break-glass.** While a sweep holds
the input lock, browser keyboard is refused with a banner naming what holds it.
An explicit "take control" button breaks the lock and **marks the run tainted**
in its log. It makes the destructive case deliberate and self-documenting, and
a tainted run is recoverable where a silently corrupted one is not. The
rejected alternative was advisory -- input always allowed, runs flagged
afterwards -- which puts the operator in charge of remembering that typing
during a sweep invalidates the comparability of ~157 banked measurements.

**Compatibility rule that makes this safe to land now: the lock is unheld by
default, and with no lock held every input command behaves exactly as it does
today.** Nothing in the existing tooling acquires it, so nothing changes until
something opts in. The gate applies to input only -- `leds`, `power`, `status`,
`caps`, `events` and `activity` are never gated, `power` deliberately so, since
cutting mains is how you rescue a sweep that has wedged past the point where
input helps.

Either way, **the event bus is the part that serves the original request, and
it does more work than its size suggests.** Every injected keystroke, power
event, lock acquisition and lock release is published to subscribers, so the
browser shows a live activity log beside the video: not just *the screen
changed*, but *the harness typed `RB` at 19:22:04 and took the input lock*.

That is the difference between **the automation is working** and **the
automation thinks it is working** -- and all three failures quoted at the top
of this document would have been visible in an activity log *even with no video
at all*. Two cheap additions make it answer the operator's question directly:

- **Show the current operation and its age.** The daemon knows when a command
  arrived. "harness has been in `ledwait` for 14 minutes" is the whole ask,
  rendered in one line.
- **Show the last event's age even when nothing is happening.** Silence and
  wedged look identical unless the clock is on screen.

**In v1, by the operator's decision**, on the strength of the requirement
above -- it was proposed rather than assumed, since v1 was scoped before that
requirement was stated.

### The single most important design constraint

The peer session, unprompted:

> **Every `vcctrl` call costs about 1.5 seconds** -- it is an ssh round-trip
> to the Pi, measured 1.52-2.55 s. That number has produced four separate bugs
> in my tooling today. For a browser KVM it is more than a nuisance: typing at
> 1.5 s per keystroke is unusable.

**Updated the same day: 1.5 s is now ~0.21 s.** The harness was opening an ssh
session per keystroke and DoSing journald; ControlMaster fixed it (FINDINGS 19).
**The conclusion is unchanged and the margin is still overwhelming** -- 0.21 s
per keystroke is four characters a second, and the in-process path is three
orders of magnitude below it. Recorded rather than quietly restated, because a
7x improvement is exactly the kind of thing that invites reopening a settled
decision, and here it does not come close.

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

**DONE 2026-08-19. `https://vcctrl-pi.example.ts.net/` is live**, via
`tailscale serve --bg --https=443`, proxying to the daemon on the tailnet
address. Verified: TLS 1.3, cert verified by a default trust store, and the
WebSocket upgrade works through the proxy at 8.5 Mbit/s. The page already
chooses `wss` from `location.protocol`, so it needed no change. Set up in
`pi/install.sh`, idempotently, and the config lives in tailscaled's state so it
survives reboots.

**Plain HTTP is off, by the operator's decision.** The daemon binds `127.0.0.1`
only, so nothing answers on the tailnet address at all and the sole route in is
through the TLS proxy. That is a stronger guarantee than binding to the
tailscale address was -- that still served unencrypted requests to anything on
the tailnet.

The cost is that every tool talking to the daemon over HTTP had to move to the
HTTPS name: `bin/vcctrl-sweep` and `bin/vcctrl-audio` both did. Measured from
the VM, `urllib` over TLS costs about a quarter second per call more than plain
http with no connection reuse -- 0.55 s against 0.29 s for a frame, which is
still two orders of magnitude below the 40 s it replaced.

**The recovery path does not route through the web server.** `vcctrl` speaks to
the unix socket over ssh, so if TLS, `serve` or the cert ever fails, input and
power still work and the machine is not stranded.

### Considered and not taken: Caddy with the Tailscale plugin

Proposed by the operator, on the strength of a working Caddyfile from another
stack, for automatic certificate renewal. **Checked rather than assumed, and
the premise does not hold here: `tailscale serve` already renews
automatically.** `tailscaled` is itself the ACME client --
`/var/lib/tailscale/certs/` holds `acme-account.key.pem` alongside the
certificate, and the live cert is a 90-day Let's Encrypt one it obtained and
will replace on its own. Caddy's Tailscale integration would obtain certs
through the same mechanism, so it would add a layer without adding the
property it was proposed for.

Against that, one real cost: another process on a box with **680 MB free and no
swap**, where `tailscaled` alone is already 96 MB. Nothing here needs what
Caddy is good at -- there is one backend, the daemon serves its own content,
and compression is pointless on JPEG and PCM.

**Caddy would be the right answer the moment this Pi serves a second thing.**
One config with several backends beats several `serve` mappings, and its access
log would be genuinely useful (this daemon deliberately logs no requests, since
per-request logging into journald is what took the Pi off the network for 30
minutes -- FINDINGS 19). Revisit then; not before.

**Certificate renewal runs weekly and on boot**, as a systemd timer rather than
a cron entry. `tailscale serve` renews on its own, so the timer is not the
mechanism -- but it is not merely belt and braces either. **Tailscale renews
lazily, when something asks it to serve TLS.** A KVM that nobody opens for
three months is exactly the case where no handshake triggers a renewal, and it
is also exactly the case where you next open it because something has gone
wrong. The timer's job is to guarantee a trigger that does not depend on
someone happening to visit. The reason it is a timer:
cron's `@reboot` fires before tailscaled has connected, so an on-boot renewal
would run against a down control plane and fail silently. A timer can say
`After=tailscaled.service` and settle for three minutes first, and its result
lands in journald with everything else.

One consequence worth stating rather than burying: a publicly-trusted
certificate means the machine's MagicDNS name appears in public Certificate
Transparency logs. That is inherent to the cert, not to this design, and the
operator approved it.

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
that is 500 KB; at a pessimistic 30 KB it is 2.7 MB. Fine in 920 MB, but **cap
in bytes and not in frames.** This is not tidiness: the ring is in-process, so
an unbounded one is an OOM, and an OOM takes the uinput devices down with it --
the one failure mode rule 3 does not cover (sec. 2). Frames is the wrong unit
the moment resolution or framerate goes up, which is the first item on the 6.3
TODO list.

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
| `shot` | one JPEG by the selection below, **or an explicit "no picture"**. The default. |
| `burst` | the last N raw frames, explicitly labelled raw in the response |

**The selection algorithm changed under this plan on 2026-08-19** and the new
one is load-bearing. It is no longer "brightest non-flat". `grab()` in
`bin/vcctrl-sweep` now:

1. hashes every frame in the burst;
2. **drops any frame whose hash appears more than once** -- settle frames are
   bit-identical, real picture never repeats (sec. 1);
3. picks the brightest of what survives;
4. **returns `(None, None)` if every frame was a duplicate.**

Step 4 is the point, and it is why this is not a detail. **Brightness alone
cannot distinguish a flat-black no-lock from a genuinely dark screen** -- it
returns the least-black frame either way and reports a number as though it
meant something. `bin/vcctrl-uvconfig` now refuses to start on `(None, None)`,
because every check it makes is read off the screen.

So `shot` must implement duplicate-hash rejection **and be able to answer "no
picture"** rather than always returning a frame. A `shot` that hands back a
plausible dark frame during a no-lock would reintroduce a bug the peer session
removed the same morning this was written.

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

The `vcctrl` session mapped where a persistent open gets stressed hardest, and
it is worse than "the occasional reboot":

- **Every cell boundary in a `DKTCAP` sweep.** The BAT flips back to mode 12h
  after each cell, so a four-cell sweep is **eight mode transitions in about
  twelve minutes.** That is a soak test that arrives free with any real run.
- **A genuine unlockable state, not merely a transition** -- anything running
  `VGACAP\MODE03`, or a profile booting without the mode-12h line. This can
  persist indefinitely and is *correct*.
- **POST**, where the signal disappears and comes back with different timing.
- **A card on which `nosignal` is the correct steady state, indefinitely.**
  PLAN sec. 4 predicts the Mach64 under UniVBE runs 512x384 at ~77 Hz, which
  cannot be double-scanned and sits outside the stick's 60 Hz-only firmware
  table. If that prediction holds, then with that card installed the streamer
  never receives a frame **and nothing is wrong.** This is the strongest
  argument for the liveness rule below: a timer-driven respawn would churn the
  device forever against a correct state, for as long as that card is in the
  machine. The `vcctrl` session is measuring it now with
  `bin/vcctrl-capcheck`.

So "no frames" must be treated as **possibly correct and indefinite**:

- No frame for **2 s** -> publish `{"state":"nosignal"}`. Browsers show a "no
  signal / not locked" panel rather than a frozen last frame. A frozen last
  frame is exactly the failure mode that makes a KVM lie.
- **Whether to respawn is decided by ffmpeg's liveness, not by a clock.** This
  replaces the 10/30/60 backoff an earlier draft had, which the peer session
  correctly called out as still churning -- one respawn a minute forever
  against text mode 03h is slower churn, not an absence of churn.

  | frames stopped, and... | meaning | action |
  |---|---|---|
  | process **alive** | the stick is not locking; the input is absent | publish `nosignal`, **do nothing else, indefinitely** |
  | process **exited, or unresponsive to signal 0** | wedge | respawn |

  That collapses the policy to a check the system can actually answer, with no
  guessed interval to tune. Keep a slow ceiling -- at most one respawn a minute
  -- purely as a backstop against a wedge that somehow keeps the process alive,
  but it should be the rare path and not the normal one.

  This is the same principle as everything else on this rig: **ask the system
  what is true rather than inferring it from a clock.** Respawning cannot
  manufacture a signal that is not arriving at the stick.
- Never escalate a no-lock into an error state or a notification. It is a
  reading, not a failure.
- Log every transition with a timestamp. "The picture went away at 19:22:04" is
  diagnostic data when a sweep is being watched.

There is a cheap improvement available once the daemon is merged (sec. 2): the
input capability knows when a keystroke that changes video mode was injected,
so it can hint the video capability that a transition is expected. Worth doing,
but **the watchdog must be correct without the hint** -- the operator can change
mode at the physical keyboard, and a sweep changes it without anyone typing.

**Do not report "nosignal" as "the machine is off".** That inference is exactly
the mistake PLAN sec. 4.1 records, one level up.

**Acceptance test, offered by the peer session:** `MODE12` / `MODE03` / `MODE12`
at a DOS prompt gives lock, unlock, relock in about ten seconds, without
burning a sweep. That is the gate for step 2 -- run it against a build before
trusting the streamer across a real run.

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
edit call sites.** Signature confirmed correct by the peer session.

**The shim must propagate "no picture", and this is the part to get right.**
`grab()` returns `(None, None)` when every frame in the burst was a duplicate,
and `bin/vcctrl-uvconfig` refuses to start on that value. So:

    state != "locked"   ->   the shim returns (None, None)

and **never** a frame. A shim that hands back a plausible dark frame during a
no-lock silently reintroduces the ambiguity between no-lock and dark-screen
that step 4 of the selection algorithm exists to remove (4.4). This is the one
place where "the migration is transparent" could be true of the signature and
false of the semantics.

`vcctrl shot` on the Pi becomes a `/run/vcctrl.sock` client. Same command, same
output, ~0.2 s instead of ~40 s, and that 0.2 s is now entirely the ssh hop.

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

### 5.1a Where the on-screen keyboard stands, 2026-08-25

The panel is a **whole keyboard** now — F-row, the alphanumeric block in
QWERTY with real key-unit widths, Space, the Ins/Home/PgUp · Del/End/PgDn
cluster, the arrows, the locks, and a row of DOS chords — and it is **drawn
from a layout table rather than typed into the markup**, because the rig has
two protocol boards and an IBM PC board and a Lisa/Mac/ADB board are not the
same keyboard with a few keys missing.

- The layout descriptors live in `LAYOUTS` in `kvm.html`. A layout is a fact
  about a class of machine, so it is data in the page.
- **The key tables are the daemon's**, fetched once from `/keymap.json`: the
  alias map, the modifier order, the reboot chord, and every name the daemon
  accepts. `vcctrl keymap` prints the same object from the same command. The
  page carries no transcription of any of it, and **fails closed** — with no
  table it cannot tell a reboot from a chord, so chords are held rather than
  sent unwarned.
- **Chords are ordered in the daemon**, not the browser. See
  `docs/CLI-PARITY.md` sec. 5a: the page used to sort modifiers to the front
  and the CLI did not, so `vcctrl combo delete ctrl alt` was quietly a
  different command from the same chord built in the page.
- **Which layout is the daemon's answer**, published as `board.keyboard` in
  `/state.json` and bound by a `keyboard:` word in `targets:`. See
  `docs/BOARD-IDENTITY.md` sec. 4.1. The page maps no board id to anything.
- `null` draws **no keyboard** and says which of the three absences it is: no
  board identified, no layout configured for this board, or a layout id this
  page does not have. It never falls back to the PC.
- The phone keeps a compact rail; the alphanumeric block is rendered and
  hidden there. Tapping the picture already raises the system keyboard through
  `#ghost`, and a 24px QWERTY would be the third way to type the same letter.

**The geometry was measured in an engine, and the numbers are here because
they are the evidence for decisions somebody will otherwise re-tune by eye:**

| | |
|---|---|
| desktop panel | 559 × 442, every block row **540.7 px** |
| phone, a true 390 px viewport | 374 × 586, no row overflowing, 46 px keys |
| the block on a phone | `display:none` — rendered, not omitted |

Three of those are load-bearing rather than decorative:

- **Equal row widths are what make it a keyboard.** Key units, not flex-grow:
  a 2u key must span two 1u keys *and the gap between them*, or every row
  drifts left by a gap per wide key and the columns stop lining up.
  `test_keyboard_chords_in_a_browser` asserts the rows are equal, so the
  number above is the evidence and the test is the guard.
- **The phone padding is 9 px, and that is a measurement not a preference.**
  13 px put the F-row on three lines and the whole panel at 639 px of an
  844 px phone — a control surface that has eaten the picture it controls.
  9 px is two lines and 586 px, with the 46 px touch target untouched.
- **Rendered-and-hidden, not omitted**, so there is one DOM and one code path
  and dragging a desktop window down to phone width needs no re-render.

Taken inside an **iframe**: headless chromium clamps a top-level viewport to
500 px however `--window-size` is set, so every "390 px phone" figure measured
directly is a 500 px figure wearing a phone's name.

**Two live defects were found writing it, and both are fixed.** They are
recorded because each is a shape that recurs here:

1. The sticky-modifier code added a class called `sticky` and no stylesheet
   selected it. A latched Ctrl looked exactly like an unlatched one — the one
   piece of invisible state in the panel, invisible. The class the page styles
   is `on`.
2. **`lctrl,lalt,delete` was not recognised as Ctrl-Alt-Del.** The page keyed
   its confirmation on the literal string `'ctrl,alt,delete'`, and `_combo` in
   the daemon tested `{"ctrl","alt"} <= keys` — while the panel's modifier
   buttons send `lctrl` and `lalt`. So a Ctrl-Alt-Del assembled from those
   buttons would have rebooted the machine with no dialog **and** left
   `PROFILE` holding a reading from the boot before it. It was unreachable
   only because the panel had no Delete key to finish the chord with. It has
   one now. Both sides match on the canonical key SET (`is_reboot_combo` /
   `isReboot`), a superset test, and every route to the chord — a button, a
   sticky Send, a latched modifier plus a tap — goes through one function.

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

**5.2 IS STILL OWED, AND STEP 8 SHIPPED WITHOUT IT.** The pacing table below
gates step 8 on step 7, and that is not what happened: the browser keyboard
went in first and the sweep was never run. The rail got away with it because
ten F-keys, four arrows and Esc are keys anybody would notice failing within a
day. Seventy is a different bet — `pause`, `sysrq`, `102nd` and the right-hand
modifiers are the ones most likely to be missing from a firmware map and the
ones nobody presses often enough to notice.

So the panel **carries the unknown rather than settling it**: every key is
drawn alike, none is greyed, and one line above them says none of them have
been measured. Greying would assert they do not work; drawing them silently
asserts they do. Neither has been measured, and the line says so until the
sweep replaces it.


### 5.2b The coverage record, agreed before the sweep rather than after

Design, not implementation. **Nothing below is built**, deliberately: an empty
coverage table invites being filled with assumptions, and the greying rule it
feeds is a UI that looks authoritative. Written now because deciding a schema
with a powered machine waiting is how the cheaper option wins — the `vcctrl`
session's point, and it is the right one.

**IT IS 105 KEYS, NOT 129.** `NAMED_KEYS` has 129 entries and 105 distinct
keycodes: 22 codes carry more than one name (`ctrl`/`lctrl`, `enter`/`return`,
`del`/`delete`, `esc`/`escape`, `period`/`dot`, `prtsc`/`printscreen`, and the
printed-keycap aliases `-`/`minus`, `;`/`semicolon` …). The sweep measures
PHYSICAL KEYS. Counted, not estimated:

| group | n | witness |
|---|---|---|
| letters, digits, punctuation | **47** | echoes a character. Direct and unambiguous. |
| the keypad | **16** | echoes — but `kp5` and row `5` echo the SAME character |
| caps / num / scroll | **3** | the PS/2 LED channel — non-video, and the one round trip here that has never lied |
| F1–F12, Enter, Esc, Tab, Backspace, Space | **17** | a BEHAVIOUR, not a character. Each needs its own designed observation. |
| 8 modifiers, 10 nav + arrows, `sysrq`, `pause`, `menu`, `102nd` | **22** | **nothing at a bare prompt** |
| | **105** | accounted, none left over |

**And the record must be keyed by KEYCODE, not by name, or it can contradict
itself.** A table with a row per name has 129 rows for 105 facts: `ctrl` and
`lctrl` are one physical key, and nothing stops one row saying `arrives: true`
and the other `arrives: false` for it.

**The sharpest case is `printscreen` = `prtsc` = `sysrq` — one keycode, three
names, and they do not even sit in the same row of the table above.** `sysrq`
is in the twenty-two that produce nothing at a bare prompt; anyone sweeping by
name would reach for `printscreen` expecting something to happen and record a
different verdict for the same physical key. The greying rule would then grey
one name and not the other, on one key, from one run. (`.` = `dot` = `period`
is the only other three-name code.) Found by the `vcctrl` session checking the
alias table rather than the example.

Key the record by one canonical name per keycode and resolve aliases into it —
the page draws by name and looks the name up. A table with more rows than
facts is a table that will eventually disagree with itself, and this one greys
keys in a UI.

Two consequences that decide the shape:

- For group four, "no echo" means both *the firmware did not map it* and *it
  arrived and did nothing here*. One value, two facts — the shape this repo
  keeps finding, and worse than usual here because the output greys keys in a
  UI.
- For the keypad, echo proves **delivery** and cannot prove **identity**, and
  identity is the entire reason the keypad matters: DOS software reads it
  distinctly from the number row.

So the sweep wants a witness that reads SCAN CODES rather than characters.
With one it is a single loop over most of the 105 and the marginal cost of the
boring keys is near zero. Without one, the classes "most likely to differ" are
exactly the classes the witness cannot see, so that ordering produces the least
trustworthy rows first — and they are the rows nobody can check by eye
afterwards.

**The instrument is `INT 16h AH=11h`/`AH=10h`, NOT an INT 9 hook**, and the
change is the `vcctrl` session's. The extended keyboard read returns AH = BIOS
scan code, AL = ASCII, from ordinary foreground code. What that buys:

- **It takes the instrument out of the seam.** No hook means no second reader
  of port 0x60, and none of the `0xFA`-phantom mechanism `rdypulse.asm`
  documents — on a path whose 8042 is an STM32 emulation, which is the very
  thing under test. The argument against the hook was the strongest argument
  for it: the note warning about leftover ACKs was warning about the
  instrument I had proposed.
- **No DOS reentrancy problem.** DOS is not reentrant, so an ISR cannot write
  a file; a hook needs a RAM buffer plus a foreground flush — more moving
  parts inside the one component the whole table's correctness rests on.
- **It settles the keypad**, which was the sharpest case here: row `5` and
  `kp5` carry different scan codes even though they produce the same ASCII, so
  identity comes free. *(The two codes differing is itself something this
  sweep measures. If they come back identical, the instrument cannot separate
  them and the 16 keypad rows are `no-witness`, not `arrives`.)*
- **It collapses most of group five.** The 10 nav and arrow keys return
  distinct extended scan codes, so they stop being "nothing at a bare prompt"
  and join the directly-witnessed set.

**The trade, stated rather than buried.** `INT 16h` reports what the BIOS
PRODUCED, not what the protocol board put on the wire — so a silent key still
conflates *the board sent nothing* with *the board sent something the BIOS
discarded*. A hook would separate those. It is not worth the seam risk here,
because the question this table exists to answer is "does pressing this on the
page reach DOS as this key", and `INT 16h` is measured at exactly that end.
**The hook is the follow-up diagnostic for keys that come back silent**, run
against those specific keys to learn whether it was the board or the BIOS —
not the primary instrument for all 105.

**So `arrives` is not one claim, and the record must say which claim it is.**
Three witnesses answer three different questions: `INT 16h` says the BIOS
produced this key for DOS; the LED channel says the keyboard controller
received it; BDA shift flags say the BIOS believes a modifier is held. A
consumer must never compare two rows with different `how` values as though
they measured the same thing, which is why `how` is mandatory and not
decorative — the same discipline as `why` in `LedsCapability`.

The residue still needs a witness each, named rather than hidden under one
instrument: the **8 modifiers** by BDA shift flags at `0040:0017`/`0018` (and
`0040:0496` for the left/right ctrl+alt distinction), held with `keydown`,
sampled, released; the **3 lock keys** by the LED channel, separately, as
above; and `sysrq`, `pause`, `menu`, `102nd` genuinely awkward and likely
`no-witness`, honestly labelled — which is what the schema is built to say.
`102nd` in particular wants `unsupported` rather than `arrives: false`: a key
a US layout does not have is a fact about the keyboard, not a measurement of
the wire.

**The negative control needs `AH=11h`, not `AH=10h`.** A blocking read cannot
report "nothing happened"; it waits. So the no-key-pressed control polls with
the non-blocking check for a bounded interval and asserts zero events — and it
runs FIRST, before any positive control, so the witness has to report nothing
before it is allowed to report something.

**The witness must not paint the screen.** Scancodes are hex digits, and this
rig has a standing finding that OCR does not read digits off this glass
(`OPEN-FAULTS.md`: counts read by size and by eye for exactly this reason; and
at −38% amplitude it is worse). A misread nibble is a **wrong identity rather
than a missing one**, which fills the table instead of leaving a hole. So the
probe appends to a file on the card and the file is fetched over `--from` and
compared as bytes — the leg walked 2026-08-24 against two independent
transports three days apart, agreeing on every byte. No OCR, no glass, no
amplitude dependence, and the run becomes unattended, re-runnable and
diffable when firmware changes. Raised by `vcctrl`. It is the
remove-the-dependency move rather than the tune-the-probe one, applied to an
instrument that had not been written yet — which is the cheapest moment to
apply it.

#### The artifact: `KEYWIT03`, and it emits EVENTS, never verdicts

Settled with the `vcctrl` session 2026-08-25 and tested in emulation before
anything touched the rig. `dos/keywit.asm` writes `C:\XFER\OUT\KEYWIT.LOG`,
CREATE/TRUNCATE — never append, because a stale file from an earlier run read
as current is a failure this rig has already had.

    KEYWIT03<CR><LF>                 10 bytes
    SSSS AA CC LL TTTT<CR><LF>       20 bytes, fixed, one per EVENT
    END NNNN<CR><LF>                 10 bytes

    SSSS  sequence, hex
    AA    BIOS scan code, INT 16h AH=10h (AH)
    CC    AL -- ASCII, or E0 marking an extended key. NOT named "ASCII":
          a reader who trusts that header decodes E0 as a character.
    LL    the raw BDA keyboard-flags byte at 0040:0017, AT THIS RECORD
    TTTT  low word of the BIOS tick at 0040:006C. Wraps every ~65 min; a
          DECREASE is a wrap, not an error.

`LL` bits: 0 right shift · 1 left shift · 2 ctrl · 3 alt · 4 Scroll Lock ·
5 Num Lock · 6 Caps Lock · 7 Insert. A non-zero low nibble in record 0 means
something was held down when the run began — a finding there would otherwise
be no way to see.

**`LL` is per-record and NOT in the header**, and both halves of that are
deliberate. Per-record because a header value asserts the lock state was
constant across the run, and this sweep falsifies that the moment it presses
NumLock — header-only is correct solely under a scheduling discipline, and a
discipline is a premise that stops holding the day somebody reorders the
sweep, silently, with the header still claiming otherwise. Measured:

    0001 4C 00 00 4BAE     kp_5, NumLock off
    0004 4C 35 20 4BCF     kp_5, NumLock ON -- AL moved, scan did not

Not in the header as well, because that is two copies of one fact and two
copies drift. Record 0 carries the start-of-run state.

**Truncation check: `(size - 20) / 20 == NNNN`.** It can only run if there is
a well-formed `END` to read `NNNN` from, so **a missing or malformed `END` is
truncated, full stop, before the arithmetic is reached** — that is the case
the trailer exists for. A crash before any record gives 10 bytes and a
negative result, void by the same rule.

**KEYWIT EMITS EVENTS AND NEVER VERDICTS**, and the verdict is formed in the
analysis where the static key-class table lives. That is what keeps the
instrument free of a classification it could get wrong, and it is why all 105
keys are injected including the 11 below: a modifier that DID produce a
keystroke is a surprise worth seeing, and only an instrument that reports
events can show it to you.

#### The sentinel frame, and why counting the unknowns cannot work

The first design reconciled expected against recorded count and voided the
run on a mismatch. **That voids exactly the finding the sweep exists to
record:** send 105, record 104 because one key genuinely does not arrive, and
the run destroys itself. It reconciles the count of the things whose arrival
is the open question — and a control has to be something you already know.

Worse, correlation was POSITIONAL — record N is key N — so a drop in the
middle shifts every row after it. Key 3's scan code filed under key 2, all the
way down: rows of **wrong identity**, self-consistent, and invisible to the
truncation check. The OCR argument arriving through the protocol instead of
the instrument.

**So a sentinel — a key already PROVEN to arrive — goes between every key
under test:**

    S k1 S k2 S k3 ... S k105 S      106 sentinels + 105 keys = 211 injections

`a` is the sentinel: scan `1E`, and scan is invariant under Caps Lock so it
holds however the lock state drifts. Verdicts become per-slot rather than
positional — nothing between two sentinels means that key did not arrive, and
the frame re-establishes at every sentinel so no row can shift. Reconciliation
moves to the **sentinel count**, where it can carry weight. Measured, with two
true negatives in it:

    a lshift a b a lctrl a  ->  7 injections, 5 records, sentinel count 4/4,
                                RUN VALID, two `arrives: false` preserved

Two corners: when `k_i` IS the sentinel, use a different sentinel for that
slot and record which; a dropped sentinel merges two slots and shows as a slot
holding two non-sentinel records, so it is detectable rather than silent.

**THE SENTINEL DID NOT FIX THE DROPPED-KEY PROBLEM AND IT MADE IT QUIETER.**
Under the count rule a drop announced itself as a broken run. Under this one a
drop is an empty slot — a well-formed `arrives: false` in a run that passes
every check here. So a single run still cannot separate *does not arrive* from
*the injection dropped it*, and **two independent runs that must agree is the
only discriminator there is.** It survived the redesign; it did not become
redundant. Disagreement is `why: "unstable"`, with both verdicts in `reason`.

This is not hypothetical: one emulation run sent six keys and recorded three,
never reproduced in six attempts, and **the file was internally consistent** —
three records, `END 0003`. The instrument did not lie about what it had. Had
the send count not been known, that run would have produced three confident
negatives.

#### Eleven keys INT 16h cannot see, and they must not get a verdict

**8 modifiers and 3 lock keys — 11 of 105, 10.5% — never enqueue an INT 16h
keystroke at all.** They set BDA flags or toggle state. An empty slot for one
of them says *nothing whatever* about whether the board delivered it.

    lshift rshift lctrl rctrl lalt ralt leftmeta rightmeta
    capslock numlock scrolllock

`how` alone is not enough here. A row reading `arrives: false, how: "int16"`
for `numlock` is a **true statement about the wrong question**, and the reader
is a greying rule — a label is a thing that can be skipped. So the analysis
emits no verdict for these at all: empty slot plus can-produce-a-keystroke is
`arrives: false`; empty slot plus structurally-invisible is `why:
"no-witness"` naming the mechanism. **Better that the artifact never contains
the wrong verdict than that it contains one correctly labelled.**

Which one applies is a static property of the key, known before the run — so
it belongs in the protocol, not in the reader.

*(This was found in the demonstration run above: `lshift` and `lctrl` were
labelled "does not arrive" two paragraphs before the same session diagnosed
the identical error for `numlock`. Having named a hazard produces the feeling
of coverage that replaces the check.)*

#### Sweep order, pinned rather than assumed

The lock keys go **last** and their states are pinned and recorded. Two
reasons, and only the first was previously written down: `FINDINGS.md` sec. 3
says never send a lock key mid-sweep because they are the harness's own
signalling; and, measured above, NumLock moves the AL column of all 16 keypad
keys. The per-record `LL` makes a reordering visible rather than silently
wrong, but the order is still the right one to keep.

#### The record

Modelled on `LedsCapability.snapshot()`, which solved this exact problem, and
its docstring is the design review nobody has to repeat:

    "a":      {"arrives": true,  "how": "scancode", "at": 178...}
    "kp5":    {"arrives": true,  "how": "scancode", "at": 178...}
    "102nd":  {"arrives": false, "how": "scancode", "at": 178...}
    "sysrq":  {"why": "no-witness", "reason": "no INT 16h event, and no
               witness was run that could see it"}
    "pause":  {"why": "not-swept", "reason": "..."}

Four properties, three of them stolen wholesale:

1. **`why` names a distinct reason and is not several falsy values wearing one
   flag.** Split `no-witness` by WHICH witness was unavailable — "the screen
   cannot see this key" and "that witness was not run" want different actions.
2. **When there is no verdict the verdict key is ABSENT, never `false`.** A
   consumer that forgets to check reads absent as undefined and draws a dash;
   it reads `false` as *measured, does not work* and greys the key. Half a
   schema is worse than none, and this is the half that gets skipped.
3. **The set of `why` values is OPEN.** That must be said in the same breath
   as the values, because the LED docstring said CLOSED SET while emitting
   five for two hours and a consumer was written against three in good faith.
   The greying rule is a consumer of exactly this kind: it must degrade on a
   value it has never heard of, and adding one is a seam event announced to
   consumers rather than left to be discovered by reading.
4. **The daemon has two opposite absence rules and it is four to one, not a
   pair — a coverage table needs both at once.** Counted rather than assumed:

       every key always present, null where unknown
           PowerCapability, BoardCapability, TargetProfile, FilesCapability
       value keys ABSENT when unavailable, never zero
           LedsCapability, and only LedsCapability

   Both are correct and the line between them is **descriptive versus
   measured**. A status field keeps its key and carries null, because null is
   a real answer somebody wants and a consumer that branches on which keys
   EXIST re-encodes the daemon's internal states. A SAMPLED MEASUREMENT loses
   its key, because a plausible default is indistinguishable from data —
   `{"capslock": 0}` on a channel never read says *the lamp is off* to anyone
   who forgot to check `available`. Leds is the exception because it is the
   only capability publishing sampled values at all.

   This matters here because the LEDs shape is the natural one to copy for a
   coverage table and **copying it alone would be half wrong**. A coverage
   table needs both at once and gets both rather than choosing: **every key in
   the layout has an entry** (a consumer can enumerate, and a missing entry is
   a bug), **and an entry with no verdict carries no `arrives` field.**

   The fork is now recorded in `Capability`'s own docstring in `vcctrld.py`,
   because the next person to hit it will be writing a `snapshot()` rather
   than reading this section.

#### What the page does with it, and what it will not do

`arrives: false` greys the key. **Nothing else greys anything** — not a
missing entry, not any `why`, not a value the page has never heard of. Those
draw normally and count toward the "unmeasured" sentence the panel already
carries. Greying on absence would assert a negative from an instrument that
could not see.

#### Two hazards that are not optional

**The lock keys are measured deliberately and separately, never swept
inline.** `FINDINGS.md` sec. 3, and on this rig they are the harness's own
signalling: Caps is the reboot detector, Scroll is `RDYPULSE`. A sweep that
injects them in sequence fights the instrumentation while looking like a
keyboard fault.

**The instrument sits in the seam it measures.** `rdypulse.asm` says the
mechanism out loud — a leftover `0xFA` ACK in the output buffer is taken for a
scancode by INT 9, "which in this rig means a phantom keystroke landing in
whatever the harness types next". That mechanism is why the witness is INT 16h
rather than an INT 9 hook -- but the follow-up hook, if it is ever run against
keys that came back silent, lives in exactly that seam. So a
**control is mandatory, not advisable**: sweep a key with independent evidence
and confirm the witness agrees before trusting it on a key without. The
control set already exists — the three lock keys have the LED channel, and
`TIMING-FIXES.md` bug 1 closed the loop through `key 5 enter` against
`PKTTOOL`.

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

### 6.0 A correct still beats smooth video

**The tie-break rule for every decision in this section**, and it comes from
the only person who has spent a day diagnosing this machine from captured
frames:

> The most valuable frames today were of a DOS prompt, not of the game. On the
> Mach64 the game's 512x384 mode does not lock at all, so mid-cell is
> invisible -- but every diagnosis I made was from a still frame of a console:
> a `DIR` listing showing stray files, an FTP transcript showing `is not a
> file`, a `[DKTCAP=1]` banner confirming a sweep launched correctly. Static
> text screens, read once.

**If latency and getting *a* correct frame ever conflict, take the frame.**
This does not contradict the low-latency goal -- interactive typing still needs
it -- but it settles the cases where they pull apart, and those are the cases
that matter for the stuck-detection requirement.

Two concrete consequences:

- **Keep a "last good frame" with its age, always.** The brightest non-flat,
  duplicate-rejected frame from the ring, held with a timestamp and shown
  when live frames stop. **This is not the frozen-last-frame failure of 6.1** --
  the difference is entirely in the labelling. A silently frozen frame makes a
  KVM lie; a frame captioned *last locked picture, 4 m 12 s ago* is the single
  most useful thing on the page during a no-lock, and on the Mach64 it may be
  the only picture available for a whole cell.
- **Dropping frames under load is correct; dropping the still is not.** The
  backpressure rule in 6.1 discards frames when a client falls behind. The last
  good frame is exempt: it is state, not stream.

**This also rescues the video capability on the Mach64.** If that card never
locks (4.5), the live stream is empty for the whole run -- but the prompts
before and after each cell are mode 12h and *do* lock, and those are where the
diagnoses actually came from. A card that makes mid-cell invisible does not
make the KVM useless; it makes it a very good still camera pointed at the
prompt.

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
detects the prompt (`at_prompt`, commit 853c02d) and the boot profile is
readable via `SET`.

Throughput measured by the peer session: ~880 KB/s, 10/10 byte-identical over
nine files. **Do not promise that figure with viewers attached** -- the Pi's
stream and the g2k's transfer share the wifi, and this is the one place where a
viewer measurably costs the harness something (sec. 11, q4). The CF reader on the Pi stays as the recovery path (PLAN sec. 5.6)
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
| 1 | **Core refactor** (sec. 13): threaded `serve()`, capability registry, existing behaviour moved into modules, no new features. Built here, handed over for integration | nothing |
| 2 | Video capability: ffmpeg pipe, frame split, ring, `shot`/`burst`/`state` on the existing socket | 0, 1 |
| 3 | `vcctrl shot` reads the ring, with fallback to today's ffmpeg path; `grab()` shim offered to the peer | 2 |
| 4 | HTTP capability + `tailscale serve` HTTPS + `/shot.jpg` + the event bus | 2 |
| 5 | **WS video to the browser.** A page that shows the g2k live. First real milestone | 4 |
| 6 | Input capability: `keydown`/`keyup`, full key table, the input lock | 1 |
| 7 | Keyboard coverage sweep -- measure what the STM32 actually delivers (5.2). **STILL OWED** -- step 8 shipped without it; see 5.2 | 5, 6 |
| 8 | **Keyboard in the browser**, macro bar, sticky modifiers, release-all-on-disconnect. **Done** (5.1a), out of order | 7 |
| 9 | Power panel + live LEDs + **activity log and current-operation age** (sec. 2), **in v1** | 4, 6 |
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
3. **Does the stick survive a long-lived open across mode changes?** Still
   unknown -- every capture on this rig has been a fresh open, and the peer
   session cannot answer it either. **This remains the main technical risk in
   the plan.** Section 4.5 has the stress cases and the ten-second acceptance
   test. If the stick needs a reopen per transition the streamer still works,
   but "seamless across a reboot" degrades to "a gap at each mode change."
4. **Can a viewer perturb a scored measurement?** ~~Open.~~ **Structurally, no
   -- and the framing was wrong.** From the peer session:

   > A viewer cannot perturb a scored measurement, because there is no shared
   > resource between the Pi and the g2k. The capture stick is a passive tap on
   > the VGA line with passthrough to the monitor. Whatever load it presents to
   > the g2k's RAMDAC is a property of the cable being plugged in, and is
   > identical whether the Pi is decoding frames, encoding them, or sitting
   > idle with the device closed. The only wire between Pi and g2k is the PS/2
   > keyboard, and nothing in the streamer touches it.

   That is right, and it relocates the question: the `-c:v copy` argument is
   about **Pi CPU**, which is a "does the Pi keep up" problem and not a matrix
   problem. Two things keep it from being closed outright:

   - **It assumes the stick is a passive tap, and nobody has opened it.** It has
     monitor passthrough and carries audio, which is consistent with passive,
     but that is inference from behaviour. Recorded here rather than buried,
     per this repo's own rule that a label is not a proof: if the stick turns
     out to do anything active on the VGA side, the argument collapses.
   - **One second-order path the argument does not cover:** Pi and g2k share the
     **network**. A viewer streaming over wifi contends with an mTCP transfer to
     the g2k. This cannot touch a scored cell -- those are network-TSR-free by
     design (PLAN sec. 5) -- but it can slow a file transfer, so section 8
     should not promise 880 KB/s with three browsers attached.

   The peer session is running the empirical check anyway -- RB with a viewer
   attached versus RB without, ~25 minutes -- and raising it with the
   benchmarking session itself. Correct call: it should come from the session
   that executes it.
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

## 11.1 Two failure families, and the design rules that pre-empt them

Added at the peer session's suggestion, and it is the most portable thing in
this document. Between two sessions in one day this rig produced **six** timing
bugs, and every one of them belongs to one of two families:

**1. A tuned interval standing in for a fact the system could be asked for.**
The watchdog is the example this document produced twice: a flat 10 s ffmpeg
respawn, then a 10/30/60 backoff, both of them guessing at "is the capture
wedged" when the process's liveness answers it exactly (4.5). Both fixes
*removed* an interval rather than retuning one, and that is the tell.

> **Rule: prefer a liveness check to a timer.** If you are choosing a number,
> ask first whether something can be interrogated instead. A retuned interval
> is the same bug at a different frequency.

**2. A signal read as something adjacent to what it means.** The LED level
attests "the host published a state at some point", and was read as "the host
is up" -- true only across an edge, and wrong at 2.5 s after power-on. Frame
brightness attests "these pixels are dark", and was read as "the screen is
dark", when a flat-black no-lock produces the same reading (4.4). The USB
descriptor's `0x0602` attests "the vendor's firmware says digital audio", and
was read as "there is no analog input" -- wrong, and it stopped an
investigation for a while.

> **Rule: ask what a signal attests, not what it correlates with.** When the
> two differ, the gap is where the bug lives. Where a reading cannot
> distinguish two states, say so and return "unknown" -- `grab()` returning
> `(None, None)` rather than the least-black frame is this rule applied.

**The sharper form of family 2, found across two sessions in one day: the
problem is not that proxies are wrong, it is that they drift from the thing
they stand for *silently, by construction*.** Four instances, four different
proxies, none of which announced anything:

| the proxy | what it was read as | what it actually attested |
|---|---|---|
| `/sys/class/leds` level | the machine is up | the host published a state at *some* point |
| frames arriving | the picture is live | the USB device is producing bytes |
| focus on a hidden input | the keyboard is captured | that element has focus |
| Caps Lock LED toggling | DOS is at a prompt | the BIOS INT 9 handler is intact |

The last is the `vcctrl` session's, and it is the most instructive because it
had the strongest track record. `at_prompt()` toggles Caps Lock and watches the
LED -- but **Caps Lock is serviced by the BIOS keyboard ISR, not by DOS**, so
the LED flips whether or not `COMMAND.COM` is reading input. It appeared
reliable for months because the one case it does catch is the game, which hooks
INT 9. It cannot tell "at a prompt" from "`FTP.EXE` is running", which is
exactly how 41 characters got typed into a 15-key buffer while a BAT was still
finishing.

**A proxy that is right about the case you keep testing is the most dangerous
kind**, because the track record is real and is evidence for the wrong claim.

### Consequence for this plan, section 8

The file-transfer UI was specced to enable its button only "at a NET prompt",
with `at_prompt` named as the way to know. **That gate rests on the proxy
above and is therefore not sound as written.** It needs the DOS-level probe the
`vcctrl` session is building -- type a sentinel, look for its echo in a
captured frame, `Esc` it away -- which is affordable only because a grab is now
0.2 s rather than 40 s. Section 8 should not be built until that lands.

Neither rule would have been derived from the individual bugs; both were
visible only once the bugs were lined up. **The next one will not look like
either of these**, which is the argument for writing the families down rather
than the instances.

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

---

## 13. Handoff -- what goes to the `vcctrl` session, and when

**Operator's direction:** build the core in this worktree, then hand it to the
`vcctrl` session to finish out and layer into their testing. So this is not a
work order handed over up front -- it is the contract the work is built
against, and the checklist it is handed over with.

The rename item raised earlier is **dropped**. There was never a file to move:
`daemon/vcctrld.py`, `vcctrld.service`, `/run/vcctrl.sock` and both `vcctrl`
CLI paths all keep their names. Section 2's merge is a change to what the
daemon *owns*, not to what it is called, and no path moving is the point --
the peer session's tooling crosses it untouched.

### 13.1 The work, in landable order

Built here, in `webkvm`. Each item independently committable and revertable.

1. **Thread `serve()`.** One thread per connection. `Devices.lock` already
   serialises the actual device writes, so the emission path is safe as-is;
   `power` and `ledwait` touch no device and genuinely want concurrency. This
   is the item that unblocks everything interactive, and it is small.
2. **Capability registry.** A table in the source -- name, init, command map,
   shutdown. No discovery, no dynamic import (sec. 2). The core dispatches by
   command name and **catches at the module boundary**: a capability that
   raises is marked failed and reported in `status`, never fatal.
3. **Move existing behaviour into modules** -- `input`, `power`, `leds` -- with
   **no behaviour change at all.** This is the risky-looking step that must be
   provably boring; see 13.2.
4. **Add `keydown` / `keyup` and the full US PS/2 key table** (sec. 5.1). New
   surface, no existing surface touched.

Items 1-4 need **no access to the capture stick**. That is deliberate and it is
what makes this safe to start now: it proceeds while the machine is busy with
PicoGUS consolidation, the Vibra fit, the video-card swap and Round P, without
anyone giving up `/dev/video0`. The video capability (step 2 of sec. 10) is
where the device window is needed, and it comes after.

### 13.2 Acceptance criteria -- the handover checklist

All testable, all worth running rather than reasoning about. These are what
make the handover reviewable rather than a request to take it on trust:

- `vcctrl status | type | key | hold | combo | mouse | leds | ledwait | power`
  produce **byte-identical output** before and after the refactor, **including
  error paths**. The peer session's tooling parses the JSON and branches on
  `ok`; a refactor that changes an error shape breaks callers that never see a
  success, and a success-only comparison would pass anyway.
- A `power cycle` in flight (15 s of rails-down) does **not** block a
  concurrent `vcctrl type`. This is the one that proves item 1.
- **Two concurrent `vcctrl type` calls produce two intact strings.** Added on
  review, and it is the most important test on this list, because it is the
  only place the refactor can introduce a *new* class of corruption. Threading
  `serve()` makes two clients typing at once newly possible, and the failure
  mode is not a crash -- it is `CD \DOSKUTSU` and `QA 1` arriving as
  `CQDA  \1DOSKUTSU`. That corrupts a sweep launch silently and looks like a
  DOS quirk. §13.3's rule about holding `Devices.lock` across a logical
  operation is the fix; this is the test that proves it.

  **Run it repeatedly with long homogeneous strings, not once** -- forty `a`s
  against forty `b`s, many trials, asserting each arrival is homogeneous. A
  race of this shape passes most single trials even with the lock entirely
  absent, and a green check on the one item that matters would be the worst
  possible acceptance result. **Include a control** that proves the test can
  detect the failure it claims to: run two unlocked writers and assert the
  same check fails. Implemented in `tests/test_core.py`.
- **p99 of `vcctrl key` with three viewers attached is not materially worse
  than with none.** This is how rule 1 in section 2 gets settled -- by
  measurement rather than by argument. If it moves, rule 1 is not holding, and
  the point of measuring early is to know before it matters.
- With the video capability deliberately faulted or unloaded, `vcctrl type`
  still lands at the g2k. This is the rule-1 test from sec. 2, and it is open
  question 8 -- do not assume it, run it.
- After a daemon restart, `usb4vc_holds_us()` reports both devices held. A
  restart is not free (13.3) and this is how you confirm it recovered.
- `vcctrl-sweep`, `vcctrl-collect`, `vcctrl-uvconfig` and `vcctrl-capcheck` run
  unmodified against the new daemon. (`vcctrl-cardid` reads log files and never
  touches the daemon, so it is out of scope. All of these landed after this
  plan's first draft; check the current contents of `bin/` rather than this
  list, which will go stale again.)

The last one cannot be verified from this side alone -- it needs the peer
session's actual tooling against the actual machine. **That is the natural
handover point:** core built and self-tested here, then handed over for the
integration that only they can run.

### 13.3 Constraints that must not be broken

All already load-bearing in the current daemon, recorded here so a restructure
does not quietly drop one:

- **The uinput devices stay open for the process lifetime.** USB4VC only
  discovers input devices on its 0.75 s scan (`usb4vc_usb_scan.py:946`), so a
  device created per-request is invisible for up to 0.75 s and the first
  keystrokes are silently lost. This is why a daemon crash is worse than a
  restart, and why item 3 is the step to be careful with.
- **Do not disturb USB4VC's device classification.** Name must not contain
  "motion" (`:896`); the keyboard needs `KEY_ENTER` and `KEY_Y` (`:913`); the
  mouse needs `BTN_LEFT` and `EV_REL` (`:911`); neither may declare gamepad
  buttons (`:877`). Extending the key table for item 4 must not trip these.
- **`ctrl-alt-del.target` stays masked.** The virtual keyboard is a keyboard to
  the Pi as well as to the g2k; without the mask, `vcctrl combo ctrl alt delete`
  reboots the Pi. Found the hard way -- see `docs/FINDINGS.md`.
- **Keep the pacing semantics.** `DEFAULT_PACE_S = 0.012` exists because USB4VC
  drains one event per device per loop pass with a 5 ms idle sleep (`:772`,
  `:766`). Threading `serve()` must not let two clients interleave events on
  one device faster than that -- `Devices.lock` is what prevents it, so hold it
  across a whole logical operation and not per-event.
- **The `vcctrl` CLI contract is frozen.** ~157 banked fps measurements sit
  behind the peer session's tooling.
- **`status` and `caps` stay separate commands.** Not because a caller would
  break today, but because merging them makes `status` a dict whose truthiness
  varies with unrelated subsystem health -- and then `all(status.values())`,
  which is the obvious thing to write, becomes a preflight that refuses to run
  a sweep because the web UI is down. Confirmed by the peer session, whose
  preflight would not have noticed the change and would have inherited the
  trap.

### 13.4 Verified on hardware -- 2026-08-19

Deployed to the Pi and run against the live g2k (Mach64, PGSB profile, mode 12h
console, capture locked). Results against the 13.2 checklist:

| criterion | result |
|---|---|
| `leds` byte-identical | **identical** |
| `power state` byte-identical | identical but for `on_time_s`, a live counter |
| error path byte-identical | **identical** -- `error: ValueError: unknown key: nosuchkey` |
| `status` byte-identical | differs in one field, and **not because of the refactor** -- see below |
| USB4VC holds both devices after restart | **yes**, keyboard and mouse both true |
| two concurrent `type` calls stay intact | **yes, on real hardware** -- see below |
| observation ungated while locked | **yes** -- `leds`, `activity`, `status`, `caps`, `events` all answered |
| break-glass publishes a taint | **yes** -- `lock.broken {broke: sweep-test, by: operator-browser, taint: true}` |

**The `status` difference is `led_paths`, and it was worth checking rather than
explaining away.** The paths moved from `input6::capslock` to `input8::capslock`
across the deploy, which looks exactly like a refactor regression. It is not:
restarting the daemon again moved them to `input10`, so the index increments on
every restart because each restart creates fresh uinput devices. It would have
differed identically across a restart of the old daemon. **Tested, not
asserted** -- the hypothesis was cheap to falsify and stating it without the
second restart would have been a guess wearing a conclusion's clothes.

**The concurrent-`type` result is the one that mattered**, and hardware gave
better evidence than the unit test could:

    C:\>aaaaaaaaaaaaaaaaaaaaaaaaaaaaaabbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

Two intact runs of 30, read off the screen through the full uinput -> USB4VC ->
STM32 -> PS/2 -> DOS path. The event log carries the corroborating measurement:
the two calls took **727.8 ms and 1456.3 ms** -- the second is almost exactly
twice the first, because it waited on `Devices.lock` rather than interleaving
with it. The screen shows the outcome; the timing shows the mechanism.

Still outstanding from 13.2, because they need the peer session's tooling
rather than this side: `vcctrl-sweep`, `vcctrl-collect`, `vcctrl-uvconfig`,
`vcctrl-cell` and `vcctrl-capcheck` running unmodified, and p99 of `vcctrl key`
under viewer load, which cannot be measured before viewers exist.

**One CLI difference that is not byte-identical and should not be missed:** the
usage text printed for an unrecognised command has grown, because `keydown`,
`keyup`, `release-all`, `caps`, `events`, `activity` and `lock` were added to
it. No caller should be matching on usage text, but "byte-identical output" was
the promise and this is the one place it does not hold.

### 13.5 What is not in this

Video, web, HTTP, WebSocket, browser, mouse-over-Pointer-Lock, files. Those are
sections 4 through 9 and steps 2 onward. **This is the core refactor and
nothing else** -- the reason to keep it separate is that it lands while the
surface is still small enough to verify against the existing tooling in
isolation.

---

## 14. Built and measured -- 2026-08-19

v1 is running on the Pi. `http://100.64.0.1:8080/`, tailnet only.

| | |
|---|---|
| Video | MJPEG passthrough, no decode/encode/base64 on the per-frame path |
| Rate | 30 fps source; **17.0 Mbit/s** at 30 fps on a detailed screen, **7.1** at 12 |
| Frame size | ~15 KB text console, ~70 KB a full `DIR` listing |
| `shot` | **0.29 s**, down from ~40 s |
| Transports | WebSocket preferred; `multipart/x-mixed-replace` fallback |
| Input | keydown/keyup, full US PS/2 table, click-to-capture, Ctrl+Alt+Shift to release |

### What measurement changed, which is most of it

**The 5.5 KB frame figure in section 1 was wrong** -- it measured frames
ffmpeg had *re-encoded* to JPEG files, not the stick's own output. Passthrough
frames are ~15 KB for a text console and ~70 KB for a dense screen, so the
bandwidth estimates here were low by 3x. The conclusion survives (still far
inside the link) but the number was measuring the wrong thing.

**Duplicate-hash rejection holds at 30 fps.** The claim was measured on 8 fps
bursts; rechecked on the live stream, a static mode-12h console gives 90 frames
and 90 distinct hashes. Analog noise really does differ every frame.

**"Text mode 03h stops the stream" is wrong for a persistent open.** Every
capture before this opened the device fresh. A long-lived stream keeps
receiving 30 fps of **bit-identical flat black** frames instead -- 30 frames,
one distinct hash. Perfect separation from live picture, but it means the
watchdog's "no frames" condition may never fire for a mode change, and the real
signal is *frames that never change*. Freshness is judged on content now:
identical hashes attest that the source is repeating; frame arrival attests
only that the USB device is producing bytes.

**And what it repeats is flat black, not the last real screen** -- so a viewer
in that state has nothing to look at unless the last live frame was kept. It is
kept, and shown dimmed behind the notice with its age (6.0).

### Four bugs worth keeping, because they share a shape

1. **A reader thread died on an AttributeError** (`read1` on a raw `FileIO`)
   while `video state` reported `owned=true, frames=0`.
2. **`_select` returned a bare `None` for three different causes** and reported
   all of them as "every frame was a duplicate". The real cause was a
   `NameError` on `io.BytesIO`.
3. **Two writers for one piece of state**: `_push` set `locked` on every frame,
   30 times a second, against the watchdog's twice-a-second classification. It
   flapped 138 times while *every* poll returned `locked` -- invisible to
   sampling, visible only in the event log.
4. **No backpressure**: `sendall` on a client that cannot drink 17 Mbit/s
   blocks, so frames pile into the kernel buffer and the stream becomes a
   backlog being replayed. This is very likely why an iPhone kept dropping the
   socket while the handshake was provably fine.

1, 2 and 3 are all the same failure: **a component reporting confidently about
itself while being wrong**, which is precisely what section 11.1 family 2
describes and precisely what this tool was built to catch in *other* software.
An `except` that discards the reason turns a bug into a lie. 4 is a case of
implementing the happy path of a rule the plan already stated.

### What it unblocked, which is not what was planned for

The plan justified this tool by *watching* -- see a sweep, see a wedge, take
over. The first thing it actually unblocked was different, and the `vcctrl`
session put it better than the plan does:

> An interactive DOS configurator is exactly the class of tool that is unusable
> over a blind harness and trivial over a screen. My tool's caution was correct
> given it could not see; the answer was never a braver tool, it was a visible
> one.

`UVCONFIG.EXE` detects the card, you save, you exit. Three keystrokes into a
full-screen menu. `vcctrl-uvconfig` was written to write the config files and
then **refuse to press any keys**, because a harness driving a screen it cannot
read is how you corrupt a machine -- the same rule as sec. 11.1, applied
correctly and at cost. The consequence was a half-configured machine that stood
for six hours while the fault was investigated rather than finished.

The general form is worth keeping, because it predicts what else this unblocks:
**wherever the harness declined to act because it could not see, the KVM
converts a refusal into an operation.** Boot menus, configurators, anything
modal. The blind harness was right to stop; it just had no way to look.

### Still not done

- Mouse (9), file transfer (8), audio, WebRTC, selectable transport (6.3).
- HTTPS via `tailscale serve` (3) -- plain HTTP works, but WebRTC and the
  clipboard API both need a secure context, so this blocks 6.3.
- p99 of `vcctrl key` under viewer load (11 q3) -- measurable now that viewers
  exist.
- The J31 reset opto is still unwired, so mains remains the only recovery.
