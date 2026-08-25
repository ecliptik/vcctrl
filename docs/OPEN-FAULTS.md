# Open faults, and what to do about them

`FINDINGS.md` records what happened. **This file is the forward-looking half:
what is still broken, what is worked around rather than fixed, and what to
check before trusting a result.** If you are picking this up cold, read this
before running anything measured.

Last reviewed 2026-08-24.

---

## 1. `vcctrld` aborts with a glibc double-free  — OPEN, UNEXPLAINED

    double free or corruption (top)     23:09, after 32 min / 1m13s CPU
    double free or corruption (!prev)   23:43, after 28 min / 3m10s CPU

Two aborts in 62 minutes on 2026-08-20, both SIGABRT, both auto-restarted by
systemd in ~3 s. `PIL._imaging` is loaded; **no thread was inside PIL at the
abort** — glibc raises on whoever calls `free()` and finds the arena damaged,
so the corrupting write happened earlier on another thread.

**If it fires, do this FIRST, before anything else touches the box:**

    pi/core.sh              # newest vcctrld core, `thread apply all bt`
    sudo journalctl -u vcctrld --since '-45 min'   # faulthandler stacks

Cores are armed and `libc6-dbg` + debuginfod are installed, so Pillow's frames
resolve. **Verify on the RUNNING PROCESS, not the unit** — the unit reports the
*hard* limit and Debian's soft limit is 0:

    grep 'Max core' /proc/$(systemctl show vcctrld -p MainPID --value)/limits

Both columns must read `unlimited`. This is why the first abort left no core
despite the unit looking configured.

**What is ruled out:** not a deploy, not the ring-eviction change (a dict of
names to timestamps, no `free`).

**The daemon lifetimes, from the journal:**

    136204  22:36:51 -> 23:09:03   32.2 min   ABORT (top)     cells + browsers
    143721  23:15:03 -> 23:43:15   28.2 min   ABORT (!prev)   cells + browsers
    167771  23:43:18 -> 00:56:52   73.6 min   clean           12 cells, 2 collects,
                                                              a 216 MB AVI mux,
                                                              NO browsers

**The clean window is 73.6 minutes, not the 55 quoted earlier**, and it
carried more harness load than either aborting window: twelve cells, two
NET-boot collects, and a 216 MB mux, including a period with the ring raised
to 335 MB. It beats both abort intervals by better than 2x.

**What is not ruled out:** the websocket frame streamer, which hands ring
bytes to SSL from a thread that is not the capture reader.

**The combination present at BOTH aborts has never been tested**: cells
running AND streamers connected. vckvm's 12.2-minute stress had streamers and
no cells; the 73.6-minute clean window had cells and no streamers. Neither
arm reproduces it alone, and nobody has run both together. **None of this is a
controlled test and it should not be presented as one.**

**A cell that straddles a restart still finishes and writes a plausible
number** — one did, 122,485 bytes. `roundR/run.sh` pins the daemon's MainPID
and refuses to continue across a change. Keep that guard in any new runner.

### The combination run of 2026-08-21 19:16 — CEILING WRITTEN BEFORE THE RESULT

Three arms together for the first time, daemon 519329:

    real-frame decode stress   ~2,900 decodes/s, ~790/s FAILING on the
                               GENUINE short frame the hardware emitted,
                               interleaved with good frames at the rig's ratio
    inputload                  4 threads, uinput + LED reads
    2 browser-shaped tabs      video + audio ws, /state.json, /shot.jpg,
                               reviewing tab on /timeline.json

**What this arm has that no previous one did:**

- **Real byte churn.** 52.0 KB/frame, `distinct 90/90` — ABOVE the game's
  ~44 KB and far above console's ~17. Age eviction is freeing 52 KB objects
  thirty times a second. This is the axis the morning's ceiling said was
  "not the combination at reduced byte churn", and it is finally open.
- **The genuine artifact**, not constructed mutants.
- **All three load types at once**, which neither abort window ever lacked.

**What it still does NOT have, stated in advance:**

- **`inputload` is running at 27-29 ops/s, not the 57 it managed alone** — the
  Pi is saturated by the decode stress. **A null from this arm is a null at
  half the input throughput previously measured.**
- **No cells are running**, so there are no mode transitions. Every real
  malformed frame ever observed came from a cell boundary; here they are
  supplied artificially instead.
- **~790 failures/s against roughly 2 per cell naturally** — about five orders
  of magnitude above the natural rate. That is the point of a stress, but it
  means the *allocator pattern* is not the service pattern however well the
  good/bad ratio is matched.

**If it runs 55 minutes clean:** thousands of synthetic mutants did not abort,
and roughly 2.5 million real-frame failures did not either. **That is about as
strong a negative as this mechanism can be given** — and it is still a bound,
not an exoneration.

### RESULT: it ran clean. 2.69 million real-frame failures, no abort.

    daemon 519329, NRestarts 0, spawns 1, up 2h16m, zero abort lines

    real-frame arm   55.0 min   2,687,682 GENUINE malformed-frame decode
                                failures (814/s), interleaved with
                                7,215,513 good decodes
    input arm        53.6 min   92,009 ops, 0 errors
    browser arm      55.1 min   72,207 video frames, 329,976 audio chunks,
                                1,029 shots, 1,029 lastgood, 253 timelines

Load average north of 7 on a 4-core box for the whole window.

**THE MALFORMED-FRAME HYPOTHESIS IS AS CLOSE TO SETTLED AS IT CAN GET WITHOUT
A CORE.** Thousands of synthetic mutants, then 2.69 million real ones, plus two
genuine truncated frames that reached Pillow through the live system during
actual cells. **A decoder overrunning on a malformed frame is not what aborted
that daemon.**

> **READ IN HINDSIGHT — 2026-08-24, once the SSL_free race was found.** The
> hypothesis was not weak. It was well-formed, cheaply falsifiable, and
> hammered harder than anything else in this file. It was aimed at the wrong
> subsystem. **"We tested it hard and it never reproduced" reads as an
> exoneration of the code under test, when it is equally evidence that the code
> under test was never involved** — and those two want completely different
> next moves. The first says look harder here; the second says look somewhere
> else. Everyone examined the data; the fault was a close racing a read in the
> plumbing, in a path nobody had put an arm on. **The faulting path had no test
> arm, no counter and no stress harness; the exonerated one had all three —
> instrumentation attracts the search to the instrumented side, so the
> best-lit component absorbs the effort while a dark one holds the fault.**
> Where a negative is this comprehensive, spend the next hour widening the
> search rather than deepening it: ask which subsystems have no arm on them at
> all. (Framing from the benchmarking session, now its harness standard 11.1.)

**AND THE COMBINATION IS FINALLY TESTED** — cells-shaped input load, browser
clients and heavy decode simultaneously, which is what was present at both
aborts and had never been run together. **Both aborts happened at 32 and 28
minutes under a load LIGHTER than this one in every dimension we can measure**,
which makes "it is load-related" harder to hold, not easier.

**The model was wrong too, not just the mechanism.** `decode_errs` stayed at 1
for the entire 55 minutes — no new malformed frames, because **nothing changed
video mode**. Both frames ever observed came from a cell teardown, 11 and 13
seconds before exit. **That is a mode-transition artefact on a schedule, not a
rate.** "Malformed frames arrive at some frequency" was never the right model.

**What remains is the armed core.** It fires once and names the frame, and it
is worth more than any further stress either session can design. We have run
out of hypotheses that are cheap to test.

### The hunt of 2026-08-21: four arms, no reproduction

Run against one daemon (214909, `NRestarts 0`, target powered OFF):

    B  well-formed decode   ~3,800 decodes/s          85 min   4.1M decodes
    A  browser-shaped tabs  video + audio ws,
                            /state.json, /shot.jpg,
                            reviewing tab             50 min   65,700 video frames
                                                              299,983 audio chunks
                                                              3,868 polls
    C  input path           56.8 ops/s uinput
                            writes + LED reads      48.6 min   166,021 ops, 0 errs
    D  malformed decode     ~1,500 attempts/s,
                            ~700/s failing,
                            grow-dominant SOF         35 min

Track A logged **966 `http-err` over 50 minutes and every one is the
`/shot.jpg` 503** described above. Expected, not a fault -- recorded here so
nobody later reads the count as one.

**Not reproduced under any of them, alone or combined. This is a bound, not an
exoneration.** What it does NOT cover:

- **A live source.** The ring held the stick's uniform 14.7 KB no-lock
  constant, against 35-54 KB of real game screen. Age eviction was freeing
  roughly a third of the real churn, thirty times a second.
- **`/shot.jpg`**, which returns 503 on an all-duplicate window and
  short-circuits *before* any decode. Never exercised.
- **A real cell**, as opposed to cell-shaped input load: no mid-cell `shot` on
  a live frame, no OCR over real screen content.

### The leading hypothesis, and the measurement that will settle it

A glibc double-free is heap **metadata** damage — characteristically a write
past an allocation. The only third-party C extensions are Pillow's and
evdev's. **The daemon's entire frame validation is SOI + EOI + 128 bytes**, so
a garbled frame off the capture stick reaches `Image.open` in four places, and
**every decode failure is swallowed by a bare `except` with no counter.**

Frames pulled from a recorded AVI are **well-formed by selection** — they
survived being muxed — so a stress built on them cannot test this however fast
it runs. Two measurements that shape the fuzzing:

- **Pillow refuses extreme SOF growth** (`DecompressionBombError` above
  ~178.9M pixels), so those mutants never reach the decoder.
- **Shrinking a SOF makes the decoder read FEWER MCUs and stop early** — safe.
  **Growing it moderately (2x-8x) makes it fabricate output while its input
  runs dry**: a rewrite to 2560x1920 produced 4.9M pixels from entropy data
  for 307,200. That is the direction worth hammering.

`video state` now carries **`decode_errs`** and **`decode_last`**.

**THE PREMISE IS CONFIRMED AND THE MECHANISM IS LOOKING WRONG, 2026-08-21.**

Two malformed frames have now reached the decoder through the real system, and
both were caught:

    T1 / S2B    17:29:12   11 s before the cell exited
    YIELD / YA  18:57:16   13 s before the cell exited

**Both at the same point in a cell: the game tearing down 640x480 and DOS
returning to text.** Not gameplay, not the prompt. A VGA mode change alters
pixel clock, sync polarity and line count mid-capture.

**The artifact is SHORT, not corrupt.** Every marker valid and in order, SOF
correctly declaring 640x480, 23,636 bytes of entropy data, ending `ff d6` RST6
then a genuine `ff d9` EOI. Exactly what a capture device produces when it
flushes a partial frame at a mode change and terminates it cleanly.

**So the daemon's filter can never catch these.** SOI, EOI and >=128 bytes are
all satisfied by a frame missing half its picture. The gate is not wrong; it
checks properties this failure preserves.

**AND THE DAEMON DID NOT FALL OVER — EITHER TIME.** It decoded as far as it
could, raised, counted it, kept the bytes and carried on. The second time,
`/timeline.json` — the site that received it — was under continuous load from
two browser tabs. Together with a fuzz corpus of thousands of far nastier
mutants surviving the same call sequences without an abort:

**The malformed-frame hypothesis has a confirmed premise and a mechanism that
is looking wrong.** Malformed frames reach Pillow in normal operation, roughly
twice per cell, and nothing has ever come apart. **Do not read the confirmed
premise as support for the crash theory** — it is the opposite.

**FIRST LIVE-SOURCE READING, 2026-08-21.** Snapshotted before a restart
destroyed it:

    daemon 388273, up 22,419 s, state locked
    frames 672,372 total, ~37,000 of them live 640x480
    decode_errs 0    decode_last null    spawns 1    no abort

**Zero decode failures across ~37,000 real frames of live game content**, with
age eviction freeing 40-60 KB objects thirty times a second throughout — the
byte-churn condition present at both aborts, sustained for 22 minutes without
incident. Three measurement cells ran inside that window.

**This is a real null on the exact input the hypothesis is about, and the
hypothesis is looking weak.** It is not a week and it does not close the
question, but it is no longer "no data".

**A zero reading only counts with a LIVE SOURCE.** With the target off the
stick emits well-formed JPEG for its no-lock constant, so there is nothing
malformed to count and a clean counter means nothing. **Zero after a week of
real sessions kills the hypothesis; zero on a dark rig is an artifact.**

### The abort of 2026-08-24 had a mechanism, and the fix for it was inert

    free(): invalid next size    09:39:57, raised from SSL_free

A different signature from the two above and the first one with a **named
mechanism**. `SSL_free` reached through a Python attribute rebind is an
`SSLSocket` being deallocated. On the TLS port one OpenSSL `SSL*` is shared by
two threads — the frame pump in `_ws_frames` and the input reader in
`_ws_input` — and `serve_ws`'s `finally` closed that socket while the reader
was sitting in `SSL_read` on it. A close racing a read frees the object out
from under a thread that is still inside it.

`wlock` never covered this. It serialises WRITERS against each other; the
reader is neither a writer nor joined.

**a2b318a wrote the right fix into the wrong function and nobody ran it.**
The teardown — stop, join, then close — landed in `TLSServer.get_request`,
which has no `reader` and no `self.lock`, and it removed the `sock.close()`
that was there. So for three days:

- **`serve_ws` still closed the socket under a live reader.** The double-free
  was never actually fixed, while the commit message said it was.
- **Every failed TLS handshake raised `NameError`.** socketserver's
  `_handle_request_noblock` wraps `get_request()` in `except OSError` and
  nothing wider, so that escaped `serve_forever` and killed the accept
  thread — with `tls_up` still True in `/state.json`. **One stray probe on
  8443 takes the KVM off the air while it reports itself up.** That is a
  worse fault than the one being fixed, and it was introduced by the fix.

The commit was never deployed (eight cells were uncollected), which is the
only reason the listener bug was not also observed on the rig.

**Both halves are now in the right place, and both are covered by tests that
were checked against the broken code first.** `test_core.py` asserts the
teardown ORDER against a real socketpair rather than the presence of a
`join` — a source-text check for "join" passes against a2b318a, because the
join was in the file the whole time.

**The join closed the teardown race and left the larger one open** —
concurrent `SSL_read` and `SSL_write` on one `SSL*`, which was happening every
connection, all the time, and which no lock in that design touched. `wlock`
serialised the two WRITERS against each other and nothing else. That is fixed
now; see the rewrite below.

**What this does NOT explain:** the two aborts of 2026-08-20. Those were
`double free or corruption`, not `invalid next size`, and the 73.6-minute
clean window carried browsers' predecessors but no streamers. Do not fold
them into this diagnosis because a nearby mechanism was found — that is the
same move that made the malformed-frame hypothesis look strong for a week.

### The rewrite: one thread owns the socket — 2026-08-24, DEPLOYED 19:06

`serve_ws` no longer starts a reader thread. `_ws_frames` and `_ws_input` are
merged into `_ws_pump` plus `_ws_handle_input`, and one thread owns the socket
for the connection's whole life with `select` driving both directions. The
wait between frames is a `select` on the socket with the time until the next
frame as its timeout, so client input still wakes it immediately rather than
waiting out a sleep.

**This removes the hazard rather than serialising it.** There is no second
thread, so there is no read racing a write on one `SSL*`, no teardown race,
and no join. `wlock` and `_NULLLOCK` are gone — they only ever covered
writer-against-writer.

**`ws.reader_stuck` is gone from `/state.json`.** It counted a teardown that
left a descriptor open because a reader would not exit in 2 s. There is no
reader, so it could never increment again, and **a counter that cannot move
reads as "checked, and fine"** — which is worse than not being there. If you
have a check watching that field it will now read as ABSENT, and absent is the
correct answer. Watch `ws.last_error` and `ws.dropped` instead.

**Kept deliberately, and they are load-bearing:** the drop-rather-than-queue
rule, the ten-second continuous-unwritability stall test that closes an
abandoned socket, and the applied-rate echo. The client protocol did not
change; `kvm.html` needed no edit.

**The test asserts ownership, not the presence of a `select`.** Every touch of
the socket records its thread id and there must be exactly one, with a control
first that the connection really carried both directions — a socket nothing
touched trivially has one owner. Against the two-thread version it reports
"2 threads, 13 touches" and fails.

**DEPLOYED 2026-08-24 19:06:44**, committed first as `07785e9` so the Pi is
running code that exists in a commit.

**VERIFIED ON HARDWARE, and the handshake one is the fix demonstrated rather
than argued.** Three stray plaintext probes at :8443 make `wrap_socket` raise
inside `TLSServer.get_request` — the exact path that raised `NameError` before.
`/state.json` returned 200 before and after all three. Under the previous code
the FIRST probe kills the accept thread while `tls_up` goes on reporting True.

    :8443 /state.json          200 before and after 3 failed handshakes
    ws.reader_stuck            ABSENT from the payload, not zero
    rate echo                  asked 40 -> told 30.0; asked 2 -> told 2.0
    ping/pong                  correct payload, 0 ms (the pong changed threads)
    pacing, idle box           asked 2.0 fps, got 2.00; 41/41 distinct
    a keypress                 typed at the prompt, READ OFF THE SCREEN,
                               then backspaced away

**Under adversarial load it is indistinguishable from an idle box.** A
14-file transfer job ran alongside a watched stream — 78 s of driving input
(14 typed lines, LED polls at 2 Hz, the target running FTP.EXE) between two
reboots. Alignment is not fitted: the job's own boundaries at +0 s and +136.4 s
land on independently logged `frozen` transitions at monitor t=69.4 and
t=205.8.

    frames        4-6/s against an asked 5.0, never sagged
    distinct      equal to the frame count every second
    worst gap     0.25 s, against 0.24 s on the quiet baseline
    ping RTT      0 ms; over 118 pings the MAXIMUM was 1 ms
    after         ws opened 4 / closed 4, last_error null, decode_errs 0

**What that is worth, stated plainly: it is one 78-second window with 14
transitions.** It bounds the starvation question — frames stalling while input
is driven, or input queueing behind frames — on the one shape that was run. It
does not prove the race gone and was never going to. **The argument for the
race is constructive: there is no second thread, so there is no read racing a
write.** The run's real job was catching what the rewrite BROKE, and it caught
nothing.

### Coordination hazard: a change-detector cannot tell an upgrade from a fault

`pi/inputload.py` and `roundR/run.sh` both halt on a MainPID change and print
"run pi/core.sh before anything else touches the box". **A deliberate restart
fires that banner verbatim**, and it lands in the log as a false reproduction
that outlives everyone's memory of the evening. Stop PID-watching runners
before any planned restart, and confirm they are down first.

### Ruled out

- **Not a bad deploy.** 167771 is byte-identical to 143721 and ran 73.6 min
  clean. The code did not change between an aborting run and a clean one.
- **Not `_read_pcm`/audio, on stack evidence.** There is only ONE stack dump —
  faulthandler was armed after the first abort — and the PCM reader is a
  permanent thread that sits in a healthy daemon's stack right now. The audio
  *streaming* path is still untested; the stack is simply not evidence for it.
- **Not `self._chg`.** Concurrent `/timeline.json?change=1` could raise
  `RuntimeError: dictionary changed size during iteration`. Real bug, now
  locked — but it raises, it does not corrupt.

---

## 2. `type_text` cannot produce a requested case  — FIXED 2026-08-24

`vcctrl type` makes uppercase by holding SHIFT; **Caps Lock inverts SHIFT**;
and `at_prompt()` toggled Caps Lock as its probe. The case of everything the
harness typed depended on a bit the prompt detector was flipping. It cost a
transfer: a verification copy asked for as `HELLO.TXT.CHK` arrived as
`hello.txt.chk`, and the wait then timed out on a transfer the server log
showed completing.

**THE FIX: `at_prompt()` PROBES WITH NUM LOCK.** The daemon's character map
contains no keypad codes at all, so `type` is immune to Num Lock state, while
Caps Lock inverts every letter. INT 09h services all three lock keys
identically, so the probe keeps the only real signal it has — detecting a
program that has hooked the vector. Caps Lock and Scroll Lock were both spoken
for: `arm_leds()` arms Caps Lock HIGH so POST clearing it is the reboot edge,
and RDYPULSE sets Scroll Lock as readiness. Num Lock was the free bit.

**The cost, and it is the only one:** anything that SENDS a keypad key becomes
sensitive to Num Lock where it was not before. Checked in both directions
before landing — no path in this repo sends a `kp*` key, and the boot-menu
digit, the one keystroke that decides which hardware profile a measurement
runs under, resolves to `KEY_1`..`KEY_9` on the number row. Both are asserted
by tests so a future keypad user finds out from a red suite.

**`FIND /I` REMAINS MANDATORY.** The harness has stopped being *a* cause of
case corruption; it has not become the only possible one. The operator can
press Caps Lock at the KVM and a DOS program can set it, and a case mismatch
still returns "not found", which is indistinguishable from a real absence. Do
not rely on the case of a `SET` *value* either.

> **THE FIX THIS FILE USED TO PRESCRIBE WOULD HAVE MADE THINGS WORSE, and
> that is worth more to you than a corrected sentence.** It said: *"the daemon
> should read the LED and invert the shift for letters."* **That fix was built
> on the phantom below.** It would have inverted the case of everything typed
> on the strength of a value that may never have been a reading of the target
> at all — silently, confidently, on a fresh daemon, in exactly the conditions
> where that value is least trustworthy. A prescription can be wrong in a way
> that is invisible until you find the thing underneath it, and this one sat
> here looking sensible for as long as the file has existed. The fix that
> landed removes the dependency instead: nothing has to read a lock-key state
> to know what case it typed.

### The Caps Lock reading is a phantom — GATE FIXED, ACCURACY STILL OPEN

**`vcctrl leds` reported `capslock: 1` while the target's Caps Lock was OFF.**
Found by prediction rather than by noticing: before typing at the prompt the
prediction was written down — unshifted `vcctrl6` should render `VCCTRL6` if
caps is on — and it came back **lowercase** on the glass.

The tell was in the same payload all along: **`changes: 0` and
`changed_at: null`.** The daemon had witnessed ZERO lock-key transitions that
lifetime. The byte only arrives on a change, so with no change ever seen that
`1` is the Pi's virtual keyboard device default and **has never been a reading
of the target at all.** Scroll Lock reads 1 the same way.

**AND SOMETHING ACTS ON IT.** `type_line()` on the transfer path reads that LED
and TOGGLES Caps Lock if it reads True. So a phantom 1 means every transfer
has been sending a real Caps Lock keystroke to the target before its first
command. Harmless in DOS and invisible on screen — but it is **an input the
machine receives**, not a number sitting in a dict, so do not write it up as
cosmetic. (That half is the vckvm session's finding, on its own code.)

**The rule that follows, and it is broader than the case question:** the LED
value has never been witnessed to change this daemon lifetime, so **nothing
that reads it should be treated as reading the target until somebody proves an
edge.** Same discipline that fixed the return reboot on the same day — arm the
bit first, so that clearing it is an edge rather than a level that was already
set.

**IT IS NOT STALENESS, AND IT IS NOT THE PROBE MUTATING ITS CHANNEL.** Those
are the two failures already on the books — a reading from the wrong epoch, and
`at_prompt()` toggling the bit whose value it is reporting. This is a third and
it is worse than either: **a value that belongs to NO epoch.** Nothing ever
wrote it. It is the virtual keyboard's initial state, published in the same
shape as a measurement, and no amount of re-reading it will improve it.
(Distinction from the benchmarking session, which has since written it into
the harness standard as 7.2.2f, `7a8037f`.)

**So the requirement, for anything that maintains state by observing changes:**
publish the transition count and the last-changed time BESIDE the value, and
**treat zero observed transitions as UNKNOWN rather than as the initial
value.** The daemon already does the first half, which is the only reason this
was catchable — `changes: 0` was sitting next to the phantom the whole time.
It is the second half that was missing, here and in every consumer.

**So `FIND /I` stays mandatory, for a different reason than section 2 gives.**
Not only because Caps Lock inverts SHIFT, but because the harness's belief
about Caps Lock may be unrelated to the target's. **A reading nothing has ever
witnessed still prints as a number, and gets quoted later as though somebody
looked.** The typed character on the screen is the only direct evidence.

#### The root cause: one line outside one `if`

`_sample()` guarded the change RECORD with "the first sample of a daemon's
life is not a transition" and did **not** guard the PROOF FLAG three lines
above it:

    if values != prev:
        LedsCapability._seen_values = dict(values)
        LedsCapability._proven_epoch = epoch     <-- set by the FIRST sample
        if prev is not None:
            ...the change record...              <-- correctly guarded

The first sample always differs from `None`, so **every daemon proved its own
channel by reading it once.** The field's name said proven; its value meant
"a sample differed from the one before it". That is why `changes: 0` could sit
beside `available: true` — two facts from the same deque, disagreeing, three
lines apart.

**IT TOOK BOTH HALVES AND EITHER ALONE WAS A NO-OP.** Measured against four
builds, on a device whose lock LEDs never move:

    neither fix          available=True   capslock=1   changes=0
    move the line only   available=True   capslock=1   changes=0
    fix the gate only    available=True   capslock=1   changes=0
    both                 available=False  why=unproven values ABSENT

Moving the line makes `_proven_epoch` stay `None` — and the old gate skipped
its check entirely when it was `None`. Fixing the gate alone leaves the first
sample setting a real epoch, which matches and passes. **A half-fix reproduces
the phantom's exact signature**, so anyone verifying this by observing that
the symptom is gone can be looking at an unchanged rig. Check both halves.

**The invariant that now makes them unable to disagree: `available: true`
implies `changes >= 1`.** It is asserted directly, with a positive control, so
a gate that refused everything for ever would not score full marks either.

**Two consumers were reading the phantom and acting on it**, which is what
made it dangerous rather than merely wrong:

- `type_line()` reads the LED and PRESSES Caps Lock if it reads set, so a
  phantom sent a real keystroke to the target before every transfer's first
  command.
- `arm()` SKIPPED its press when a retained value said "already set", so POST
  cleared a bit that had never been armed, no edge occurred, and the run
  refused `no-reset` — "the machine never reset" about a machine that rebooted
  perfectly.

**And `arm_leds()` classified the honest new answer in the wrong bucket.** It
mapped `unproven` to `False`, which its own docstring reserves for "the LED
would not take the state" — a fault in the machine — rather than to `None`,
"could not look". Left alone it would have reported a healthy Gateway as an
arming failure and refused every boot-profile selection on it. **A gate made
honest upstream will be misread downstream by whatever was written against the
dishonest version.**

**`unproven` must also be answered AFTER `unknown`.** Put first it swallowed
it, and an unidentified board reported "nothing has been observed to move on
this channel" — a stronger claim than the daemon can make, because it asserts
the channel exists. `unknown` sends a reader to look at the board; `unproven`
sends them looking for something to press.

#### AND PROOF OF LIVENESS IS NOT PROOF OF ACCURACY — measured 2026-08-24

**The gate above is necessary and it is NOT sufficient.** Measured on the live
rig, with the target booted and the channel showing `changes: 8` — proven,
available, publishing:

    nodes (caps,num,scroll)   target caps, READ OFF THE SCREEN
    1, 0, 1                   OFF    (typed `vcctrl6` -> vcctrl6)
    1, 1, 1                   ON     (typed `vcctrl6` -> VCCTRL6)  num moved
    0, 1, 1                   OFF    (typed `vcctrl6` -> vcctrl6)  caps moved

**The target's Caps Lock followed the presses exactly: OFF, ON, OFF. The node
named `capslock` did not** — it read 1 while caps was off, and only moved on
the second press.

**READ THE WORD, NOT THE BITS, AND IT IS NOT ERRATIC — IT IS STALE.** The first
write-up of this said "which node moves was not consistent between two
identical keypresses", which invites the alarming conclusion that a keystroke
has side effects on unrelated bits. **It does not, and the data never said so.**
Decompose the same readings against a true word of `{caps 0, num 1, scroll 1}`:

    nodes {1,0,1}   caps OFF     caps and num are STALE; scroll is right
    press caps ->   caps ON      true word caps=1; node already 1, so no
    nodes {1,1,1}                change there — and num 0->1 is the stale
                                 bit catching up, not a side effect
    press caps ->   caps OFF     true word caps=0; node 1->0; num already
    nodes {0,1,1}                right; scroll never moved

Every reading is explained, deterministically, by: **the node word was stale,
and each Set-LEDs wrote the truth.** What varied between the two presses was
which bits were already correct — which is not the same thing as inconsistency.
The distinction matters because the two readings have very different
consequences: *a press moves unrelated bits* means the keystroke path is unsafe
and everything is in question; *a stale word is refreshed by a round trip*
means only reads taken BEFORE the first round trip are untrusted. **It is the
second.** (Decomposition from the vckvm session, correcting this file's first
account.)

**So `changes >= 1` proves the channel carries traffic; it does not prove the
CURRENT VALUE IS CURRENT.** A channel can be demonstrably alive and still be
publishing a word from before the last thing that changed it. That is weaker
than the gate's wording implies: the gate stops the worst case, where nothing
was ever witnessed at all. It does not make the values fresh.

**Mechanism, still a hypothesis:** a PS/2 Set-LEDs command carries all three
bits at once, so the node set is refreshed wholesale whenever the target sends
one, and the first press of a session is what corrects it. It fits every
reading here and is not proved by them.

**Num Lock behaved correctly under the same test** — two presses moved the
`numlock` node 1 -> 0 -> 1 cleanly, with nothing else moving. That matters
because the prompt probe now uses Num Lock; it is a reason to keep watching
that bit, not a proof that it is immune.

**THE SCREEN IS THE ONLY DIRECT WITNESS OF THE TARGET'S LOCK STATE.** Everything
else on this rig is an inference from a node two things can write.

**AND A ROUND TRIP APPEARS TO REFRESH THE VALUES, NOT ONLY PROVE THEM.**
Independently reproduced after the 20:48 deploy. `verify_input` reported

    before {caps 1, num 0, scroll 1}  ->  after {caps 0, num 1, scroll 1}

from a press and its restore — **net zero at the target, and the word still
moved, because the word was WRONG BEFORE IT.** Identical start word and
identical correction to the 19:5x readings. The values then AGREED with the
screen: the channel said `capslock: 0` and unshifted text came back lowercase,
where the identical prediction had failed an hour earlier on a channel that was
equally "proven".

So the practical rule, offered as the hypothesis it is: **`verify_input` is
worth running not only to open the gate but because the values are least stale
immediately after it.** One agreement does not establish that, and it does not
tell you how long the refresh lasts — which is the question the instrumented
session should answer.

#### The sitting that would settle this has four tenants, and an order

Four separate questions now wait on the same thing — the PS/2 traffic
instrumented between the board and the target. **Four is past the point where
accumulating is cheaper than scheduling.**

    1  the accuracy fault above: what writes those nodes, and when
    2  how long a refresh holds after a round trip
    3  `wait_prompt`'s exposure -- a single toggle-and-look, the one consumer
       that cannot arm an edge, so it is exposed to exactly (1)
    4  the `--from` fetch path, which has never run on hardware

**ORDER MATTERS AND IT IS NOT THE OBVIOUS ONE.** Items 1-3 want the machine
QUIET and observed; item 4 needs it POWERED and driven hard. Instrument first
against a quiet target, then let the transfer path drive it — the other way
round contaminates the very traffic the instrument is there to read. (Point
from the vckvm session, whose path item 4 is.)

**To open the channel: `vcctrl verify_input`.** It proves by round trip and
reads the nodes directly, so it works while the gate is refusing — otherwise
there would be no way out of a gate that has closed. It now records what it
proved through `_sample()`, the one place that decides what counts as a
change, rather than observing a transition off a raw read and telling nobody.

---

## 3. `CLRENV.BAT` — the generator exists and the CARD never got it  — FIXED 2026-08-24

Including `SHOT_TICKS`, `BACKDROP_CACHE`, `BG_SUBREGION_BLIT`,
`PIN_NATIVE_MODE` and every `TAS_*`. **Any cell that runs without a reboot in
front of it inherits every lever any earlier cell set.** This invalidated a
whole eight-cell round: all eight ran with the patch under test stood down,
including the four meant to be stock, and every summary statistic looked
healthy because the comparison was empty.

**A refusal is not a rollback** — a cell that sets its variables and then
refuses at a later gate has still set them.

**Do not rely on enumerating levers.** The two guards that work:

    --forbid VAR        clears the name AND verifies it absent, positively,
                        after BLASTER has proven the pipeline
    --expect-log STR    requires the ENGINE to have reported this arm in its
                        own log, read off the card after the cell

`--expect-log` is the one that survives a lever nobody enumerated, because the
engine's log is downstream of the environment, the config file, the defaults
and every killswitch. **Give the control arm the same rigour as the treatment
arm** — forbid the *other* arm's lever, not just the ones that spoil the class
of measurement.

**MEASURED ON THE CARD 2026-08-24, and this is the live half of the fault.**
`tests/qa/gen-clrenv.sh` was written, `CLRENV.BAT` in the repo was regenerated
to **189 names / 13,800 bytes**, and `--check` reports it up to date. **The card
still has the old one.**

    repo  tests/qa/CLRENV.BAT   13,800 bytes   189 names
    card  C:\DOSKUTSU\CLRENV.BAT  7,608 bytes  ~104 names, dated 8-20-26

Read by size rather than by name, deliberately — sizes OCR reliably on this
console font and names do not. The arithmetic corroborates it independently:
at ~73 bytes per name (two spellings, CRLF) 7,608 bytes is ~104 names, against
the documented ~102 of the stale file.

**The repo being fixed says nothing about the machine being fixed**, and the
generated file's own header says so: *"this file only reaches the card on a
populate"*. `install-qa.sh` populates, and it runs on the operator's laptop with
the CF physically mounted.

**There may be a path that avoids a card swap.** `C:\MTCP\FTP.EXE` is driven by
a response file (`PUT.RSP`), and mTCP's client supports `get` as well as `put` —
so a `GET.RSP` could pull the new `CLRENV.BAT` onto the card over the network.
**Not attempted.** It writes to a file every cell `CALL`s, and a truncated
transfer breaks every future cell, so it wants the operator's go-ahead and a
size check after.

**What limits the damage today:** `--expect-log` makes the engine attest its own
arm from its log, so a round using it is valid whatever `CLRENV` did. `--forbid`
clears and verifies named levers on top. The stale file is a missing layer of
defence, not an active corruption of rounds that attest.

### Resolved on the card, 2026-08-24

**Done by hand, and that sequence is now superseded — see
`FILE-TRANSFER.md`.** A future `CLRENV.BAT` update should go through
`vcctrl file-stage` + `send-file`, which renames before the bytes move,
refuses to serve a partial file, and proves arrival by round-trip sha256
rather than by reconciling a `DIR` total off the screen. **The hand-run
version below worked, which is exactly why it is worth replacing: it worked
while skipping checks the standard path makes unskippable.**

Written over the network rather than by a card swap. `C:\MTCP\GET.BAT` pulls
from the FTP server's `stage/` directory, so the file went
`stage/CLRENV.BAT` -> `C:\DOSKUTSU\CLRENV.BAT` with a `.BAK` taken first.

**Verified three ways, because no single reading on this console is worth
trusting:**

- **Arithmetic.** The `DIR` total reads 21,408, and 13,800 + 7,608 = 21,408
  exactly. A sum that reconciles cannot be produced by a lucky misread of one
  field, which is what makes it the strong witness. Both individual sizes were
  in fact misread (`13,800` as `13,808`, the same `0`->`8` as everywhere else).
- **Content.** `THRASH_CENTRE` reads `count: 2` in the new file and `count: 0`
  in the `.BAK`; `TAS_REPLAY` reads `count: 4`. All three match the repo
  exactly, and the `.BAK` difference proves the swap actually happened rather
  than the file merely looking right.
- **By eye.** The counts were read from the screenshots directly, because OCR
  dropped every one of them -- see 11 below.

**Two things that went wrong and are worth carrying:**

**`GET.BAT` takes ONE argument, and I passed it two.** CORRECTED 2026-08-24
after reading the source at `g2k:MTCP/GET.BAT` instead of inferring from
behaviour: its interface is `GET [name]`, and it builds
`get %GETF% C:\DOSKUTSU\%GETF%` — **destination always equals source name, by
design.** There is no second parameter; `%2` is never referenced. So it did not
"ignore" anything and nothing was defective — I invented an interface and then
recorded its absence as a fault.
The plan was to fetch to `.NEW`, verify, then swap, so the live file could
never be truncated. That safety never executed and nobody was told -- the
transfer simply landed on the real filename. **The `.BAK` is the only reason
this was safe**, which is an argument for taking the backup even when the plan
says you will not need it.

**The first "control" was not a control.** `THRASH_ROWS` was chosen as a name
known to be in both files; it is in neither, and returned `count: 0` from a
perfectly healthy read. A control has to be verified present, not assumed
present, or a genuine zero looks like a broken pipeline.

A generated `CLRENV` is owed from the doskutsu side and rides the next
populate. Until it lands, the guards above are the only protection.

---

## 4. Ring dumps are bounded by the ring, not the cell  — MOSTLY FIXED

`GMQ3-glass.avi` shared **18,871 of 19,976 frame packets with `GMQ2`** — 94.5%,
byte-identical. Its window opened twelve seconds after GMQ2's. An analysis
found real game frames in a file named GMQ3 that belonged to MQ2, and produced
a confident wrong refutation.

Cause was the ring overrunning `target_span_s` (480 s asked, 1193 s held)
because eviction ran on bytes and not age. **Fixed** — but the habit still
matters:

- **Record with an explicit seq range** and log `first_seq`/`last_seq` beside
  the file. `vcctrl record --out F.avi <a> <b>`.
- Check `first_seq` against neighbouring cells before treating a recording as
  evidence. One pass over the `.meta` files catches this for a whole archive.
- `written/fps` well under `span_s` means the ring **thinned**, so AVI time is
  not wall time in that file and any t-to-wall arithmetic on it is wrong.
  (A good file looks like `RGA-glass.avi`: 11 repeats in 6,301 frames.)

**BUILT AND WIRED, 2026-08-23.** `buffer_avi(since=, clip=)` refuses a window
that opens before the caller's own start time, naming how many frames are
foreign and by how long; `--clip` takes only your own and reports
`clipped_frames` so a shortened recording says so. Exposed as
`vcctrl record --out F.avi [--since T|now] [--clip]`, refusing with exit 4.

**It shipped undiscoverable.** The daemon had it, the client parsed it, a test
covered it — and the usage text mentioned neither flag, so nobody reading
`vcctrl` help would learn the guard existed. **A guard nobody is told about is
a guard nobody uses**, which is the same family as a check whose output nothing
consumes. Documented 2026-08-23.

**USE IT.** `--since now` at the start of a run, keep the value, pass it to
`record`. Without it a dump is bounded by the ring, not by the run.

---

## 5. A new websocket streams at 20 fps until told otherwise  — BY DESIGN, KNOW IT

A page intending 8 fps still pulls 20 fps of ~35 KB frames on connect.
Harmless on an idle rig; **during a 150 s timed cell it is ~700 KB/s of
unbudgeted work on the machine doing the measuring.**

**Do not attach a streaming client during a cell, and do not leave a KVM tab
open.** An open tab is not only a ring-pin hazard, it is load.

Two clamps are now echoed rather than silent: asked 40 → 30, asked 0.2 → 1.0.

---

## 5b. THE VGA SPLITTER — what it costs, if it is used again

**Fitted and removed 2026-08-21. NOT currently in the path.** Measured
properly while it was in, so the next person does not have to.

Measured on **identical content** — a screen reproduced from a pre-splitter
capture, not whatever happened to be showing. First attempt compared a full
PGSB boot log against a near-empty-prompt baseline and read 55.2 KB/frame
against ~17: an alarming number that meant nothing.

    state            lit px   mean-lit   edge   noise
    no splitter        2063      174.5   96.1    0.07
    WITH splitter      2063      108.9   65.2    0.07
    after removal      2063      174.5   97.9    0.07

**Amplitude and sharpness recovered 100% on removal, and the lit pixel count
is identical at all three points.** The splitter causes it, and it is fully
reversible.

### What it costs

    signal amplitude   -38%   (mean lit brightness 174.5 -> 108.9)
    edge sharpness     -32%   (mean edge step 96.1 -> 65.2)

A passive splitter halves the drive into two loads. **It ATTENUATES rather
than DISTORTS** — identical geometry, no added noise — which is the difference
between a splitter you can live with and one that quietly corrupts what the
rig sees.

### What it does NOT cost — checked, not assumed

    lock                  locked, spawns 1, last_error null
    distinct / n          90 / 90    healthy analog source
    bytes/frame, matched  ~17.0 -> 17.6 KB
    OCR + read_count()    'count: 1' reads as 1 -- ARGUABLY CLEANER
                          (straight quotes where the pre-splitter image
                          produced curly ones)
    decode_errs           no new errors across ~75k post-fit attempts

**The attestation pipeline survives it.** Every arm attestation in every round
depends on OCR reading a `count:` line off the glass, and at -38% amplitude
that was the likeliest thing to break. It does not.

### THE HAZARD: brightness is discontinuous across the boundary

**Round R's game cells read 24.6. The same content reads dimmer with the
splitter in.** A pre-splitter brightness and a with-splitter brightness are
**different instruments wearing the same units**, and nobody reading a table in
three weeks will know the cable changed.

No threshold is at risk — `_is_picture` needs a range of 4 and text still
spans ~109 — but comparing the *numbers* across the boundary is invalid.

**If it goes back in: re-baseline every brightness figure, and say in the
writeup which side of the swap each number came from.**

---

## 6. Volume measures cannot see the signal die  — INSTRUMENT HAZARD

A stress harness logged **a flat 111 fps straight through the target losing
mains.** The stick keeps emitting at 30 fps; the frames merely become uniform
and tiny. Frames per second, socket liveness, spawn count and
`last_frame_age_s` all survive the source dying.

**Use a content measure**: `framestats` distinct-count, per interval, never
cumulative.

    distinct ~= n    a real analog source (90 of 90 on a static DOS prompt --
                     sensor noise makes every frame differ)
    distinct ~= 1    no lock; the stick's own constant, frames still arriving
    n == 0           starved pipe

Measured both arms: target off gives 1 distinct at 14.7 KB/frame; live 640x480
gives distinct ≈ n at ~35 KB/frame. **The volume columns are identical between
those two rows.**

---

## 7. A cell inherits whatever boot profile it finds  — GATED

NET and the sound profile are **indistinguishable from the harness**: same
prompt, same `at_prompt`, same screen. A cell died with
`sdl_init failed: No BLASTER environment variable` because the machine was
still in NET from a hand-run FTP transfer an hour earlier.

`vcctrl-cell` now refuses unless BLASTER is set. **After any hand-run NET
session, reboot to the menu default** — ctrl-alt-del, let the 5 s timeout land
on PGSB.

BLASTER does double duty: boot-profile guard, *and* the known-present probe
that makes every absence check under it non-vacuous.

---

## 8. Mach64 capture regression  — NOT REPRODUCING, CAUSE UNKNOWN

For a period on 2026-08-20 no Mach64 game mode would lock. Since a reboot to
the default profile and a daemon restart it has locked on **every** attempt —
16 of 16 cells plus a 210 s recording at full 30 fps.

**What cleared it is not established.** Candidates: the reboot, the daemon
restart, the ring age-eviction fix. Nothing separated them.

If it returns: **try a reboot first.** That cheap experiment was never run,
because the fault was assumed to be the capture stick latching. See
`PLAN-NEXT.md`, which is marked superseded for exactly this reason.

---

## Cross-cutting rules earned the hard way

- **Never prove an absence on a pipeline that has not just proved a presence.**
  A pipeline carrying only questions whose wrong answer is silent has no way
  to tell you it is broken.
- **A check may not pass on an empty population.** Report the sample size
  beside every verdict.
- **Could-not-look is not a finding.** Keep three states, never two.
- **Attest from the thing that ran it**, not from what it was asked to run.
- **Prefer a check with no units and no glyphs.** A PID comparison caught a
  restart that a timestamped journal query missed entirely — integers carry no
  timezone, no locale, no font.
- **A null result is what comparing a thing with itself produces**, and it
  arrives dressed as a clean measurement.
- **A pre-registered wrong answer is worse than an unplanned one**, because the
  plan supplies the permission not to look again. When writing an outcome
  table, ask what a third state would look like.

---

## 9. The arm attestation polls blind through a mode transition  — FIXED 2026-08-24

**Twice now** an `--expect-log` attestation has refused with `COULD NOT READ`
on a cell that was fine: Phase 0's `D3` and the confirming round's `CX3B`. Both
times the identical `FIND` run by hand seconds later returned `count: 1`.

**Mechanism, from `grab()`'s own docstring:** it returns `(None, None)` for no
picture, and that includes *"a stream that is frozen -- frames arriving but all
byte-identical, which is what DOS text mode 03h produces"*.

**The attestation runs immediately after the game exits**, which is the
640x480-to-text mode transition — the same window both recorded decode errors
came from (11 s and 13 s before a cell exited). If the stick has not re-locked,
every `read_env_text_raw()` returns `None`, all 20 polls fail, and the cell
refuses.

**It fails SAFE** — an unreadable answer never becomes a pass, which is why
this has cost minutes rather than a round. But it is a check polling blind
through the one window where the thing it polls is least reliable.

**Fix, not yet applied:** wait for `video state == locked` before starting the
attestation polls, rather than beginning them the instant the game exits. Do
not simply raise the poll count — that treats a timing problem as a patience
problem, and 30 s is already 20x the transition.

**Do not change `vcctrl-cell` while a round is running on it.** A cell that
straddles a harness change is the same hazard as one that straddles a daemon
restart.

### Fixed, after the confirming round landed

`wait_video_locked()` in `vcctrl_common.py`, called once before the attestation
polls begin. **Three states, deliberately** — `True` locked, `False` asked and
never locked, `None` could not ask at all — because a refusal has to be able to
say whether the instrument or the target was the problem. The two failures look
identical from the read itself, so if the distinction is not made here it does
not exist anywhere.

**Waiting is not polling more.** The retry count is unchanged; what changed is
that the polls now start from a screen known to be readable.

**Not yet exercised against a live transition.** It returns `True` in 60 ms
against the healthy rig, and the unlocked and unreachable paths are covered by
`test_wait_video_locked_reports_which_of_three_things_happened` with invented
rigs — including a well-formed reply carrying no `state` field, which must be
`None` rather than `False`. **The real proof is the next cell that exits a game
into text mode**, and until one has, this is fixed in the sense that the code is
right, not in the sense that the fault has been observed to go away.

## 10. `started_rtc_local` is corrupt — PROVENANCE HAZARD, ANALYSIS ONLY

**The manifest's `started_rtc_local` field cannot be trusted, and every writeup
that cites it for provenance is citing a fabrication.**

Found 2026-08-24 while trying to establish which archived cells shared a
sitting. Two independent proofs from data already on disk:

    CX2B.LOG   started_rtc_local=2026-12-02T18:40:00
    CX2B.LOG   its own log lines read [08:54:56] ... [08:57:04]

The cell ran that morning. The manifest dates it to December. And the field is
not merely offset — it is **incoherent**:

    GPU0B   started_rtc_local=2026-11-28T21:25:03
    GPUAB   started_rtc_local=2026-11-28T21:25:41

**Thirty-eight seconds apart, for two cells that take 164 s each.** They cannot
both be true and they cannot overlap. Some cells carry a plausible date
(`GPU0` reads `2026-08-20`, `CX1A` reads `2026-08-24T08:49:10`, both correct),
so the field is *sometimes* right, which is worse than always wrong: it looks
reliable in exactly the spot-check a reader would perform.

**The in-log `[HH:MM:SS]` prefixes are the good clock.** They are coherent,
monotonic, and spaced like real cells:

    CX1A  08:49:10   CX2B  08:54:56   CX3B  09:00:42   CX4A  09:08:25

**What it cost:** nothing yet, but only by luck. A variance claim was one
message from entering the benchmarking standard, and the archive comparison
that killed it was **only possible after abandoning this field** — grouping
cells by `started_rtc_local` puts same-sitting cells in different months and
would have made the refuting comparison unbuildable.

**Rule:** for anything that depends on WHEN a cell ran — sitting membership,
ordering, drift, elapsed time — read the log-line timestamps. Treat
`started_rtc_local` as unverified until the RTC is understood.

**Not yet diagnosed.** Candidates: a dead CMOS battery resetting on cold boot,
a profile that sets the clock and one that does not, or the field being written
from a different source than the log prefixes. Cheap to investigate — the DOS
`DATE`/`TIME` at a prompt, against the Pi, on a warm and a cold start.

**This is an analysis-side fault, not a measurement one.** No fps figure
depends on it. Every conclusion keyed on ordering does.


## 11. The screen reader drops the line its caller wants  — FIXED 2026-08-24

**Tesseract's default page-segmentation mode omits the `count:` line from
`FIND /C` output, deterministically, 3 captures out of 3.** On the glass:

    C:\>FIND /I /C "THRASH_CENTRE" C:\DOSKUTSU\CLRENV.BAT
    ---------- C:\DOSKUTSU\CLRENV.BAT
    count: 2                        <-- read by a human, dropped by the OCR

The default is PSM 3, which analyses page layout; it reads the command line and
the dashed header, then stops. PSM 6 -- *"a uniform block of text"* -- recovers
it every time. That is not luck: **a DOS console IS a uniform block of
monospaced text**, and PSM 3's newspaper-column heuristics have nothing to work
with.

**Why the existing retry loop could never have saved it.** The attestation
polls twenty times. Twenty identical reads of an identical frame reproduce an
identical omission — **retrying is only a fix for flakiness, and this is not
flaky.** The cell then refuses with `COULD NOT READ` against a screen anyone
could read at a glance. Same shape as 9, different cause, and it would have
been diagnosed as 9.

**Fix:** `read_env_text_raw(psm=...)`, with both polling loops alternating —
even tries use the default, odd tries use PSM 6. **A fallback, not a swap.**
Every read that works today keeps its exact behaviour on the first try;
changing the mode globally under readers that are currently fine is a larger
risk than the bug. Covered by
`test_psm3_drops_the_count_line_and_psm6_recovers_it` against a real capture,
and that test asserts the DEFAULT still fails — if PSM 3 ever starts reading
the fixture, the fixture has stopped demonstrating anything and the test says
so rather than passing on both branches.

**How it was found:** by looking at the screenshot after the OCR returned
`None` for a reading that had a plainly visible answer. The harness has eyes;
the OCR is one instrument and not the only one.


## 12. The frame dumps go flat — the binary predates patch 0322  — OPEN

**The binary every measurement has been taken on, `a674802dcab4`, does not
contain patch 0322.** It writes frame dumps to a flat `LOGS\S<tick>.PPM`, which
is 0318's behaviour and exactly the collision hazard 0322 was written to fix.

Attested by the engine itself, cell `DMPA`, 2026-08-24:

    [shot-dump] ARMED n=4 first=1000 last=4000
    [shot-dump] want=1000 got=1000 skew=0 WROTE LOGS\S01000.PPM bytes=230415

**No mkdir warning anywhere in the log**, and 0322 emits one on a failed mkdir
before falling back to flat. So this is absence of the patch, not failure of it.
The working tree HAS 0322 at `Renderer.cpp:5146`; the shipped binary does not.

### What it cost, and it was not nothing

`vcctrl-cfclean` was scoped to `LOGS\<TAG>\` directories as its FIRST design
decision, specifically so a directory-scoped delete could never reach the flat
backlog. **On this binary there are no tag directories, so the tool had no
targets at all** — it would have reported "nothing on the card" indefinitely
while the backlog grew. **A guard aimed at a layout that does not exist is
indistinguishable from a guard that works, right up until you measure it.**

Found only because a cell was run specifically to produce the directory the
tool was built around, and it did not appear.

### Resolved on the harness side, not the engine side

Attribution now comes from **the engine's own log** — `dumps_from_log()` parses
the `WROTE <file> bytes=<n>` lines, which name each file individually with its
size, arrive as exact bytes over FTP, and **remove OCR from the attribution
entirely**. That is strictly stronger than the directory listing it replaced,
and it works on the flat layout.

### Still open on the engine side

**Two cells dumping at the same ticks still overwrite each other on the card**,
which is the hazard 0322 exists to remove. Not hypothetical: `GMQ0` overwrote
`GVS2`'s `S02400.PPM` on 2026-08-20. The fix is a rebuild carrying 0322 — which
changes the binary, so it does not happen mid-campaign without a new baseline.

**Until then, dump-producing cells must not reuse tick values**, and the flat
backlog must be pulled between them. `DMPA` was safe by luck rather than
design: the flat total went 2,304,150 -> 3,225,810, a delta of exactly
921,660 = 4 x 230,415, proving four NEW files and no overwrite. **Had it
collided the delta would simply have been smaller, with nothing announcing it.**


## 13. Caps Lock is harness-controlled state, and short commands do not know it

**Two sessions disagreed about Caps Lock on 2026-08-24 and both were right.**
One saw typed commands arriving inverted; the other read `capslock: 0` from the
LED channel with `available: true`. Neither channel was lying, and the
resolution is that **Caps Lock is deliberately driven by the harness itself.**

Three places move it, all on purpose:

    at_prompt()      TOGGLES it as the liveness probe, and its own docstring
                     says it "does not always restore it"
    arm_leds()       SETS it high -- POST clears it, so caps 1 -> 0 IS the
                     reboot edge
    type_command()   FORCES it high before typing, because mode 12h lowercase
                     OCRs as "he L Lowor Ld" and the echo check then rejects
                     commands that arrived perfectly

**The trap is the asymmetry.** `type_command()` only runs for commands longer
than `SHORT_CMD_CHARS`; anything shorter goes straight to `vc("type", ...)`,
which does not touch Caps Lock and does not know what the last long command
left behind. **So a short command arrives in whatever case the previous long
one happened to set** — and the case a command arrives in is a function of
history, not of the caller.

Observed exactly this: a `DIR ... | FIND "bytes"` typed after a cell arrived as
`dir ... | find "BYTES"`. The unquoted half did not matter. **The quoted
pattern did**, and without `/I` it would have searched for `BYTES` against DOS
output that says `bytes`, matched nothing, and returned a well-formed zero.

The disagreement itself resolved the same way: the LED read `0` later because
two reboots had happened in between, and POST clears it.

### The rule

**Always `FIND /I`. Never let a typed pattern's case carry meaning.** This is
already the convention in `vcctrl-cell`; it is written down here so the reason
survives, because the failure it prevents produces a zero rather than an error.

**And a Caps Lock reading is a reading of the harness, not of the target.**
Comparing it between sessions says nothing unless both know which of the three
writers ran last.


## 14. `git bundle verify` passes on a bundle that cannot be restored

**Measured 2026-08-24.** A 1.4 MB bundle, truncated and each fragment tested:

    95% of the file:  verify=PASS  clone=BROKEN
    75% of the file:  verify=PASS  clone=BROKEN
    50% of the file:  verify=PASS  clone=BROKEN
    25% of the file:  verify=PASS  clone=BROKEN
    10% of the file:  verify=PASS  clone=BROKEN

**A bundle missing ninety percent of its bytes still reports "The bundle
records a complete history."** `verify` reads the header and checks that the
prerequisite commits exist in the LOCAL repo. **It never validates the
packfile.** Only a clone touches the objects.

This matters because it is the check anyone reaches for before an irreversible
operation. **It is not weaker than a restore — it passes on files that cannot be
restored**, which is worse than having no check at all, because it manufactures
confidence at exactly the moment confidence is expensive.

**Acceptance criterion for any bundle relied on before a history rewrite:**

1. `git clone` from the bundle into a scratch directory.
2. Confirm HEAD, commit count and tags match the source.
3. Resolve every SHA cited in docs **inside the clone**.
4. Run the test suite **from the clone**.

**A second trap, found afterwards and worse.** The citation guard read `docs/`
off the FILESYSTEM, so an untracked 41 KB draft supplied 15 of its 22
citations. The same guard therefore reported **19 across 7 docs** in the
working tree and **7 across 6** in a clone of the same commit — and two
sessions produced two different wrong explanations for the gap before either
looked at the instrument.

**A bundle cannot hold untracked files.** So a citation guard run over the
working tree partly validates prose that no backup contains and no clone will
ever see — which is precisely the number somebody would quote to argue a
restore is sound. Fixed to use `git ls-files`, which its sibling
`test_the_docs_index_cannot_rot_silently` already did; the lesson had been
learned once and applied to only one of the two.

**One trap while measuring this:** `git bundle verify` must run from inside a
git repo. Run anywhere else it errors, and `verify | grep -c "complete history"`
then returns 0 — which reads as *failed* rather than as *never ran*. Same shape
as every other well-formed zero in this document.


## 15. The profile witness labels more than it measures  — OPEN

Every cell log carries this line, and it has been read as provenance all week:

    -- profile witness --
      BLASTER                          set (profile is PGSB)

**`PGSB` is a hardcoded string in `harness/vcctrl-cell`, not a reading.** The
check is:

    SET | FIND /I "BLASTER" | FIND /C "="

**Presence, one or zero.** It proves *a* profile that sets `BLASTER` is loaded.

**CORRECTED 2026-08-24, from `g2k:AUTOEXEC.BAT` rather than from a screen
read.** The first version of this section said four of the six profiles set
`BLASTER`. **Only TWO do**, and their strings differ:

    :VIBRA    SET BLASTER=A220 I5 D1 H5 T6 P330
    :PGSB     SET BLASTER=A220 I7 D3 P330 T3
    :PGADLIB  :PGGUS  :NET  :CLEAN   -- none

**That is a correction in the SAFE direction and it narrows the fault
sharply.** A cell booted into `PGADLIB` or `PGGUS` does not silently report
`PGSB` — it gets `count: 0` and the guard REFUSES, correctly. **The only
profile that can masquerade as `PGSB` is `VIBRA`**, and the two are
distinguishable by the value the check throws away.

I reached "four of six" by reading two `SET BLASTER` lines off a `FIND` on the
screen and assuming the sound profiles each had one. **The file was in a git
repo the whole time** (`g2k:AUTOEXEC.BAT`), exact and greppable, and reading it
took one command.

**The information to do better is already on screen and thrown away.**
`AUTOEXEC.BAT` carries exactly two `BLASTER` strings and they are distinct, so
the VALUE names the profile UNIQUELY where the presence cannot. This is now
strong enough to build on rather than merely suggestive. Reading the value instead
of counting it would turn a label into a measurement, at no extra round trip.

**Why it has not bitten yet:** the menu default is `PGSB` (`menudefault=PGSB,5`,
read off the card), and nothing has deliberately selected another sound profile
during a measured run. **So the label has been accidentally true, which is the
worst way for a claim to survive** — it is indistinguishable from a checked one
right up until somebody boots `PGADLIB` and gets a cell that says `PGSB`.

**Not yet fixed and not yet urgent**, but every fps figure this week rests on a
profile assertion that was never made. **Which BLASTER string belongs to which
block needs a boot of each profile to establish** — that is real rig time and
has not been spent.
