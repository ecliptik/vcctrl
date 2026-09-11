#!/usr/bin/env python3
"""vcweb_public -- a read-only, isolated mirror of the KVM page for public
viewing.

Deliberately its own file, importing nothing from vcctrld.py or vcweb.py:
this process must be auditable, by grep alone, as incapable of ever sending
a command to the target. It has no `/cmd` route, and it never implements
vcweb.py's `/ws`. That is not caution for its own sake: `/ws` was checked
directly against `_ws_handle_input` (daemon/vcweb.py:1253-1310) and found to
be a genuine second, bidirectional command channel -- it parses client JSON
and dispatches `keydown`/`keyup`/`type`/`combo`/`release_all` -- not just a
video feed. Video reaches viewers here only as `multipart/x-mixed-replace`,
which is architecturally one-directional: there is no code path in an HTTP
response body that can carry a client-to-server message.

`do_POST` EXISTS NOW (added 2026-08-28), and answers exactly one path,
`/telemetry` -- aggregate-only visit/performance/error counters, allowlisted
`kind`, size-capped body, every value validated before it touches shared
state (see `Handler.do_POST`'s own docstring). Every other path, `/cmd`
included, still falls through to a 404 -- this method changed what an
UNKNOWN path returns (501 with no `do_POST` at all, before; 404 from a
`do_POST` that only recognizes one path, now) but not what accepting a
command would look like, because nothing here accepts one. The data this
collects never reaches the target, never reaches vcctrld over a network
call at all -- it is a local file this process writes and
`PublicTelemetryCapability` (daemon/vcctrld.py) reads directly off the same
Pi's filesystem, one-way, no route back.

Everything this process shows comes from ONE background thread family
polling the existing, unmodified private vcweb.py over loopback at a fixed,
low rate -- see the four `_poll_*` functions below. Public viewer count
therefore never changes the load the private daemon sees, regardless of
whether zero or five hundred people are watching; MAX_VIEWERS/POLL_HZ below
are the other half of that story, bounding what THIS process spends on
public connections.

Audio IS relayed, one-way, over at most one upstream connection PER CODEC
that this process opens to itself: it is the WEBSOCKET CLIENT to the private
daemon's `/wsaudio` (raw PCM) and `/wsaudio?codec=opus` (Ogg Opus pages,
~12x smaller -- the default the public page asks for), never the reverse,
and every public listener of a codec gets a copy of the same bytes. Each
upstream connection exists only while it has at least one public listener,
so an idle mirror costs the private daemon nothing and the Opus encoder over
there does not even run. The public-facing half deliberately does not parse
anything a listener sends -- see `Handler._audio_ws` below -- because
`_ws_handle_input` (daemon/vcweb.py:1253-1310) is exactly what a WebSocket
that DOES act on client frames looks like, and this one must never grow into
that by accident. (The one thing the Opus relay reads out of UPSTREAM frames
is each Ogg page's granule field, to know which pages are the stream headers
it must replay to a late-joining listener -- upstream is the trusted private
daemon, and even that read never branches on a public client's bytes.)
"""

import base64
import collections
import hashlib
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------ framing
#
# Copied verbatim from daemon/vcweb.py rather than imported -- see the module
# docstring: this file must be auditable, by grep alone, as importing nothing
# from the control-path modules.

# RFC 6455 section 1.3. See vcweb.py's own comment on this constant: it was
# wrong for hours once, in a way four self-checking tools all agreed with.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_frame(payload, opcode=0x2):
    """Server -> client (i.e. this process -> a public /wsaudio listener).
    Never masked, per RFC 6455."""
    n = len(payload)
    head = bytearray([0x80 | opcode])
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + payload


def ws_read(sock):
    """Read one frame. Handles BOTH directions this file needs: masked
    (a public listener's frames, if it ever sends one) and unmasked (the
    private daemon's own frames, arriving on the upstream client connection
    below) -- the mask bit in the header says which, so one function serves
    both without this file needing to know which side of a connection it is
    reading. Returns (opcode, payload) or None at close."""
    def recvn(n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    hdr = recvn(2)
    if hdr is None:
        return None
    fin, opcode = hdr[0] & 0x80, hdr[0] & 0x0F
    masked, ln = hdr[1] & 0x80, hdr[1] & 0x7F
    if ln == 126:
        b = recvn(2)
        if b is None:
            return None
        ln = struct.unpack(">H", b)[0]
    elif ln == 127:
        b = recvn(8)
        if b is None:
            return None
        ln = struct.unpack(">Q", b)[0]
    if ln > 1 << 20:
        raise ValueError("oversized frame: %d" % ln)
    mask = recvn(4) if masked else None
    if masked and mask is None:
        return None
    data = recvn(ln) if ln else b""
    if data is None:
        return None
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    if not fin and opcode != 0x0:
        raise ValueError("fragmented frame, unsupported")
    return opcode, data

# --------------------------------------------------------------------- config
#
# Env vars, not a YAML load: this is a standalone script with no other state,
# and a second config loader in the same repo is a second place for "what did
# the running process actually resolve" to disagree with a file on disk. See
# examples/vcctrl.example.yaml's `daemon.web_public` block for the documented meaning
# of each of these.

UPSTREAM_PORT = int(os.environ.get("VCCTRL_WEB_PORT", "8080"))
UPSTREAM = "http://127.0.0.1:%d" % UPSTREAM_PORT
BIND = os.environ.get("VCCTRL_PUBLIC_BIND", "127.0.0.1")
PORT = int(os.environ.get("VCCTRL_PUBLIC_PORT", "8091"))
# Empty by default -- a bare port needs no prefix. Set only when fronted by
# `tailscale serve --set-path=/whatever`, which forwards the FULL incoming
# path to the backend unchanged (confirmed against this rig's own /mcp
# mount: `serve status` shows it proxying to .../mcp, not stripped) -- so
# without this, every route below would need its own prefixed twin instead
# of matching once. Stripped in do_GET, before any route comparison.
PREFIX = os.environ.get("VCCTRL_PUBLIC_PREFIX", "").rstrip("/")

# WHERE TELEMETRY PERSISTS ACROSS RESTARTS -- NOT /opt/vcctrl (this process's
# own install dir, root:root 755, `vcctrl-ro` cannot write there by design).
# pi/files/vcctrl-web-public.service grants a StateDirectory= of the same
# name, which systemd creates and chowns to `vcctrl-ro` the same way it
# already does for tailscaled-ro's own dirs -- see that unit's own comment.
STATE_DIR = os.environ.get("VCCTRL_PUBLIC_STATE_DIR",
                           "/var/lib/vcctrl-web-public")
TELEMETRY_PATH = os.path.join(STATE_DIR, "telemetry.json")

# The vendored browser-side Opus decoder (vendor/README.md has the
# provenance), installed beside this file like kvm-ro.html. The version is
# part of the name on purpose -- see the route's own comment.
OPUS_DECODER_JS = "ogg-opus-decoder-1.7.5.min.js"

# The Pi this runs on has limited free memory and no swap (docs/WEBKVM.md
# sec. 3). At the shared, low poll rate below, only outbound BANDWIDTH scales
# with public viewer count -- not Pi CPU or memory, since every viewer reads
# the same cached frame rather than causing a new upstream request. This cap
# is headroom against bandwidth exhaustion and thread pile-up, not a tightly
# reasoned number; raise it once real traffic says it's too low.
MAX_VIEWERS = 40

# Raised from 1.5 -- operator decision, 2026-08-28, after comparing this
# page against the private KVM side by side. The original number was the
# "proof of life" floor from before audio went Opus; with each listener now
# ~135 kbit/s instead of 1.5 Mbit/s, the budget moved to where the eyes
# are. Per viewer this is 0.6-2.8 Mbit/s (15 KB text frames to 70 KB dense
# ones) -- a handful of real viewers is light, and only the full
# MAX_VIEWERS cap on dense content gets heavy. Every extra frame a second
# is still bandwidth spent once per viewer; raise further only against
# telemetry, not taste.
POLL_HZ = 5.0

# BELT-AND-SUSPENDERS against a stuck viewer slot, on top of the TCP
# keepalive in _mjpeg() below -- see that method's own comment for the
# failure this guards against (found 2026-08-28: diagnostic connections
# that vanished without a clean FIN/RST held a MAX_VIEWERS slot each,
# indefinitely, exhausting real viewers' access with nothing actually
# watching). A real viewer reconnects seamlessly on a forced disconnect
# (img.onerror -> restart() in kvm-ro.html); a stuck connection now loses
# its slot within this long at the very worst, even if keepalive somehow
# never fires (a NAT/relay hop that never delivers keepalive probes,
# say). Long enough that no genuine viewer should ever notice it.
MJPEG_MAX_DURATION_S = 1800

# ---------------------------------------------------------------------- data
#
# One lock guards every cache below. Contention is not a concern: readers
# hold it only long enough to copy a tuple/dict reference, never across a
# socket write.


# A SMPTE-STYLE COLOR-BARS TEST CARD, WITH VISIBLE TEXT -- the one frame
# this process is allowed to fabricate, and only as the last resort in
# _mjpeg() below, when NEITHER a live nor a last-known-good frame has ever
# been confirmed by the private daemon. Operator decision, 2026-08-28:
# without it, a target sitting in `frozen` with nothing yet "positively
# picture" (see vcctrld.py's _lastgood) left /stream.mjpg writing zero
# bytes indefinitely -- a browser's "waiting on <host>" status describing
# a connection that will never deliver anything on its own.
#
# NOT a plain black frame, deliberately: this codebase already learned (see
# project memory "Black frames are not black screens") that a flat, unlabelled
# frame is indistinguishable from a genuinely dark target -- exactly the
# confident-but-wrong signal a public viewer must never be handed. A color-bar
# test card with "NO SIGNAL" burned into the pixels cannot be
# mistaken for a capture even with every other piece of context stripped away
# (the veil text, this page's own copy) -- which matters here specifically
# because with no stale frame recorded either, dismissing the veil would
# otherwise uncover this image with nothing else on screen to explain it.
_TEST_PATTERN_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABALDA4MChAODQ4SERATGCgaGBYWGDEjJR0oOjM9PDkz"
    "ODdASFxOQERXRTc4UG1RV19iZ2hnPk1xeXBkeFxlZ2P/2wBDARESEhgVGC8aGi9jQjhCY2NjY2Nj"
    "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2P/wAARCAHgAoADASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDoKKKK"
    "ACiiigAooooAKKKKACiiigAqCf74+lT1BP8AfH0rhzD+CVDcjooorwDYKKKKACiiigAooooAKKKK"
    "AIrj/Ut+H86pVduP9S34fzqlX0uT/wAB+v6I+ezX+MvT9WFFFFeueWFFFFABRRRQAUUUUAFFFFAF"
    "eX/WGmU+X/WGmV8xX/iy9X+ZstgooorEYUUUUAFFFFABRRRQAVDdf6sfWpqhuv8AVj6114H/AHiH"
    "qd2W/wC90/UqUUUV9iffBRRRQAUUUUAFFFFABRRRQAVWf77fWrNVn++31ry8y+CJcBtFFFeMaBRR"
    "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"
    "ABRRRQAUUUUAFFFFAD4v9an+8K0qzYv9an+8K0qACiiigAooooAKKKKACiiigAooooA0LX/j3X8f"
    "51NUNr/x7r+P86moAKKKKACiiigAooooAKKKKACsHxv/AMine/8AbP8A9GLW9WD43/5FO9/7Z/8A"
    "oxaAPHaKKKACiiigAooooAKKKKACiiigAq7Zf6o/71Uqu2X+qP8AvUAWKKKKACiiigAooooAKKKK"
    "ACiiigCW1/4+F/H+VaFZ9r/x8L+P8q0KACiiigAooooAKKKKACiiigAooooA04v9Un+6KdTYv9Un"
    "+6KdQAUUUUAFFFFABRRRQAUUUUAFW7H+P8P61Uq3Y/x/h/WgC3RRRQAUUUUAFFFFABRRRQAUUUUA"
    "Pi/1qf7wrSrNi/1qf7wrSoAKKKKACiiigAooooAKKKKACiiigDQtf+Pdfx/nU1Q2v/Huv4/zqagA"
    "ooooAKKKKACiiigAooooAKwfG/8AyKd7/wBs/wD0Ytb1YPjf/kU73/tn/wCjFoA8dooooAKKKKAC"
    "iiigAooooAKKKKACrtl/qj/vVSq7Zf6o/wC9QBYooooAKKKKACiiigAooooAKKKKAJbX/j4X8f5V"
    "oVn2v/Hwv4/yrQoAKKKKACiiigAooooAKKKKACiiigDTi/1Sf7op1Ni/1Sf7op1ABRRRQAUUUUAF"
    "FFFABRRRQAVbsf4/w/rVSrdj/H+H9aALdFFFABRRRQAUUUUAFFFFABRRRQA+L/Wp/vCtKs2L/Wp/"
    "vCtKgAooooAKKKKACiiigAooooAKKKKANC1/491/H+dTVDa/8e6/j/OpqACiiigAooooAKKKKACi"
    "iigArB8b/wDIp3v/AGz/APRi1vVg+N/+RTvf+2f/AKMWgDx2iiigAooooAKKKKACiiigAooooAKu"
    "2X+qP+9VKrtl/qj/AL1AFiiiigAooooAKKKKACiiigAooooAltf+Phfx/lWhWfa/8fC/j/KtCgAo"
    "oooAKKKKACiiigAooooAKKKKANOL/VJ/uinU2L/VJ/uinUAFFFFABRRRQAUUUUAFFFFABVux/j/D"
    "+tVKt2P8f4f1oAt0UUUAFFFFABRRRQAUUUUAFFFFAD4v9an+8K0qzYv9an+8K0qACiiigAooooAK"
    "KKKACiiigAooooA0LX/j3X8f51NUNr/x7r+P86moAKKKKACiiigAooooAKKKKACsHxv/AMine/8A"
    "bP8A9GLW9WD43/5FO9/7Z/8AoxaAPHaKKKACiiigAooooAKKKKACiiigAq7Zf6o/71Uqu2X+qP8A"
    "vUAWKKKKACiiigAooooAKKKKACiiigCW1/4+F/H+VaFZ9r/x8L+P8q0KACiiigAooooAKKKKACii"
    "igAooooA04v9Un+6KdTYv9Un+6KdQAUUUUAFFFFABRRRQAUUUUAFW7H+P8P61Uq3Y/x/h/WgC3RR"
    "RQAUUUUAFFFFABRRRQAUUUUAPi/1qf7wrSrNi/1qf7wrSoAKKKKACiiigAooooAKKKKACiiigDQt"
    "f+Pdfx/nU1Q2v/Huv4/zqagAooooAKKKKACiiigAooooAKwfG/8AyKd7/wBs/wD0Ytb1YPjf/kU7"
    "3/tn/wCjFoA8dooooAKKKKACiiigAooooAKKKKACrtl/qj/vVSq7Zf6o/wC9QBYooooAKKKKACii"
    "igAooooAKKKKAJbX/j4X8f5VoVn2v/Hwv4/yrQoAKKKKACiiigAooooAKKKKACiiigDTi/1Sf7op"
    "1Ni/1Sf7op1ABRRRQAUUUUAFFFFABRRRQAVbsf4/w/rVSrdj/H+H9aALdFFFABRRRQAUUUUAFFFF"
    "ABRRRQA+L/Wp/vCtKs2L/Wp/vCtKgAooooAKKKKACiiigAooooAKKKKANC1/491/H+dTVDa/8e6/"
    "j/OpqACiiigAooooAKKKKACiiigArB8b/wDIp3v/AGz/APRi1vVg+N/+RTvf+2f/AKMWgDx2iiig"
    "AooooAKKKKACiiigAooooAKu2X+qP+9VKrtl/qj/AL1AFiiiigAooooAKKKKACiiigAooooAltf+"
    "Phfx/lWhWfa/8fC/j/KtCgAooooAKKKKACiiigAooooAKKKKANOL/VJ/uinU2L/VJ/uinUAFFFFA"
    "BRRRQAUUUUAFFFFABVux/j/D+tVKt2P8f4f1oAt0UUUAFFFFABRRRQAUUUUAFFFFAD4v9an+8K0q"
    "zYv9an+8K0qACiiigAooooAKKKKACiiigAooooA0LX/j3X8f51NUNr/x7r+P86moAKKKKACiiigA"
    "ooooAKKKKACsHxv/AMine/8AbP8A9GLW9WD43/5FO9/7Z/8AoxaAPHaKKKACiiigAooooAKKKKAC"
    "iiigAq7Zf6o/71Uqu2X+qP8AvUAWKKKKACiiigAooooAKKKKACiiigCW1/4+F/H+VaFZ9r/x8L+P"
    "8q0KACiiigAooooAKKKKACiiigAooooA04v9Un+6KdTYv9Un+6KdQAUUUUAFFFFABRRRQAUUUUAF"
    "W7H+P8P61Uq3Y/x/h/WgC3RRRQAUUUUAFFFFABRRRQAUUUUAPi/1qf7wrSrNi/1qf7wrSoAKKKKA"
    "CiiigAooooAKKKKACiiigDQtf+Pdfx/nU1Q2v/Huv4/zqagAooooAKKKKACiiigAooooAKwfG/8A"
    "yKd7/wBs/wD0Ytb1YPjf/kU73/tn/wCjFoA//9k="
)
_TEST_PATTERN = base64.b64decode(_TEST_PATTERN_B64)

_lock = threading.Lock()
_live = None          # (jpeg_bytes, fetched_at) or None if never fetched
_stale = None          # same shape, from /lastgood.jpg
_state = {}            # last /public.json body
_state_ts = 0.0
_events = collections.deque(maxlen=200)
_events_seq = 0        # highest event seq already folded into _events

_viewer_sem = threading.Semaphore(MAX_VIEWERS)

# ------------------------------------------------------------------ telemetry
#
# AGGREGATE ONLY, NEVER PER-VISITOR -- no IP, no user-agent, no cookie, no
# session id is ever stored here, by construction: nothing below even reads
# those off the request. This is the operator's own scoping decision
# (2026-08-28): visit counts, first-frame timing, and client-side error
# counts by message, nothing that could identify who looked. This process
# already sees a visitor's source IP at the TCP level for every request --
# nothing here changes that exposure -- but nothing here RETAINS it either.
#
# PRIVATE BY CONSTRUCTION, NOT BY A CHECK ON THE READER: there is no GET
# route that serves this back. The only way it leaves this process is the
# local file it's periodically written to (see _telemetry_save), which
# PublicTelemetryCapability (daemon/vcctrld.py) reads directly off disk --
# the two processes share a filesystem, on the same Pi, not a network route.
_telemetry_lock = threading.Lock()
_telemetry = {
    "started_at": time.time(),
    "visits_total": 0,
    "visits_by_day": {},        # "YYYY-MM-DD" (UTC) -> count
    "first_frame_ms": [],       # rolling window of recent samples
    "errors": {},               # capped/truncated message -> count
}
TELEMETRY_MAX_PERF_SAMPLES = 500
TELEMETRY_MAX_DISTINCT_ERRORS = 100
TELEMETRY_MAX_ERROR_LEN = 160
TELEMETRY_MAX_DAYS = 90
TELEMETRY_SAVE_INTERVAL_S = 60


def _telemetry_load():
    """Best-effort restore from the last save. A deploy restarts this
    process often (every `pi/deploy.sh --public`) -- without this, visit
    history would reset to zero on every redeploy, which is a worse failure
    for a COUNTER than for NoteCapability's deliberately-ephemeral text."""
    try:
        with open(TELEMETRY_PATH) as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    with _telemetry_lock:
        for k in _telemetry:
            if k in saved:
                _telemetry[k] = saved[k]
        _telemetry["started_at"] = time.time()


def _telemetry_save():
    """Atomic write (temp file + rename) so a reader (PublicTelemetryCapability,
    polling from a separate process) never sees a half-written file."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with _telemetry_lock:
            snapshot = json.dumps(_telemetry)
        tmp = TELEMETRY_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(snapshot)
        os.replace(tmp, TELEMETRY_PATH)
    except OSError as exc:
        print("vcweb_public: telemetry save failed: %s: %s"
              % (type(exc).__name__, exc))


def _telemetry_record_visit():
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with _telemetry_lock:
        _telemetry["visits_total"] += 1
        by_day = _telemetry["visits_by_day"]
        by_day[day] = by_day.get(day, 0) + 1
        if len(by_day) > TELEMETRY_MAX_DAYS:
            del by_day[min(by_day)]


def _telemetry_record_perf(ms):
    with _telemetry_lock:
        samples = _telemetry["first_frame_ms"]
        samples.append(ms)
        if len(samples) > TELEMETRY_MAX_PERF_SAMPLES:
            del samples[:len(samples) - TELEMETRY_MAX_PERF_SAMPLES]


def _telemetry_record_error(message):
    # CAPPED LENGTH AND CAPPED DISTINCT COUNT -- this field is the one place
    # a visitor's browser gets to put arbitrary text into this process at
    # all (see Handler.do_POST's own comment on why that's still safe). A
    # long string just gets truncated; a flood of distinct garbage messages
    # collapses into "(other)" once the distinct-message cap is hit, rather
    # than growing this dict without bound.
    message = (message or "")[:TELEMETRY_MAX_ERROR_LEN].strip() or "(empty)"
    with _telemetry_lock:
        errs = _telemetry["errors"]
        if message not in errs and len(errs) >= TELEMETRY_MAX_DISTINCT_ERRORS:
            message = "(other)"
        errs[message] = errs.get(message, 0) + 1


def _get(path, timeout=2.0):
    """GET `path` on the trusted, loopback-only private daemon.

    Returns (status, body_bytes) or (None, None) on ANY failure -- a timeout,
    a connection refused because vcctrld is restarting, a non-2xx/non-4xx
    transport error. This poller has to survive the private daemon being
    briefly unreachable without ever taking the public process's own
    listener down with it, so every failure mode collapses to "no update this
    cycle," not an exception.
    """
    try:
        with urllib.request.urlopen(UPSTREAM + path, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception:
        return None, None


def _poll_loop(name, interval, fn):
    """Runs `fn()` every `interval` seconds, forever, on its own thread.

    `fn` raising is a bug worth seeing in testing (hence no blanket
    try/except around it in the caller) -- but ONE bad iteration must not
    permanently kill the poller, so the loop itself catches and logs rather
    than letting a single exception unwind the thread.
    """
    while True:
        try:
            fn()
        except Exception as exc:
            print("vcweb_public: %s poll failed: %s: %s"
                  % (name, type(exc).__name__, exc))
        time.sleep(interval)


def _poll_shot():
    global _live
    status, body = _get("/shot.jpg")
    if status == 200 and body:
        with _lock:
            _live = (body, time.time())
    # A non-200 (typically 503, "no picture") is not a placeholder to cache --
    # leave _live exactly as it was, same reasoning vcweb.py itself uses for
    # not answering /shot.jpg with a fabricated image.


def _poll_stale():
    global _stale
    status, body = _get("/lastgood.jpg")
    if status == 200 and body:
        with _lock:
            _stale = (body, time.time())


# WHITELIST, NOT A BLACKLIST. The private daemon's /public.json is the FULL
# internal snapshot -- WebCapability.snapshot() -- because every consumer of
# it so far has been trusted (the operator's own browser, over the tailnet).
# This process's public /state.json is not that consumer: a raw `curl` from
# the open internet sees exactly this dict, whatever kvm-ro.html chooses to
# render. Passing the snapshot through unfiltered would publish the smart
# plug's network address and identity (power.host/model -- the exact device
# that mains-cycles the target), the FTP server's host:port and filesystem
# paths (files.*), and the Pi's own host facts (host.model/kernel/mem/disk),
# none of which the public page has any use for. Found in review -- see the
# security audit this responds to -- rather than by anyone exploiting it, but
# the fix belongs here regardless: enumerate exactly what daemon/kvm-ro.html
# reads (cross-checked against every j.<key>/lastState.<key> access in that
# file) and drop everything else, rather than trust every future field the
# private snapshot ever grows to be safe by default.
def _filter_public_state(obj):
    def pick(src, keys):
        src = src or {}
        return {k: src[k] for k in keys if k in src}

    out = {
        "video": pick(obj.get("video"), (
            "state", "last_frame_age_s", "frames", "fast_failures",
            "span_s", "target_span_s", "ring_frames", "ring_bytes",
            "mem_limited")),
        "audio": pick(obj.get("audio"), (
            "state", "rate", "channels", "last_chunk_age_s",
            "device_present")),
        "board": pick(obj.get("board"), (
            "id", "reason", "target", "name", "source", "stale",
            "keyboard")),
        "sysinfo": pick(obj.get("sysinfo"), (
            "fields", "source", "stale", "reason", "age_s")),
        "leds": pick(obj.get("leds"), (
            "available", "why", "reason", "capslock", "numlock",
            "scrolllock", "changed_at", "changes")),
        "input_verified": pick(obj.get("input_verified"), (
            "available", "why", "reason", "ok", "age_s")),
        # `alias` is a human label the operator chose for the plug and is
        # already rendered in the kept Power status row; `host`/`model`
        # (the plug's own network address and hardware identity) are not
        # used by anything in kvm-ro.html and are exactly what F1 flagged.
        "power": pick(obj.get("power"), (
            "on", "reason", "stale", "age_s", "alias")),
        # Only the throttling chip's two fields -- everything else in `host`
        # (model, kernel, mem/disk totals, load) belonged to the Status
        # panel's Host group, which this fork does not have.
        "host": pick(obj.get("host"), ("throttled", "temp_c")),
        "ws": pick(obj.get("ws"), ("sent_frames", "dropped")),
        "lock": pick(obj.get("lock"), ("owner", "held_s")),
        "profile": pick(obj.get("profile"), ("name", "at", "how", "reason")),
        # setInputLamps() reads only caps.input.ok -- the rest of `caps`
        # carries every capability's backend name and settings, which for
        # `power`/`files` is the same host/path detail dropped above.
        "caps": {"input": pick((obj.get("caps") or {}).get("input"), ("ok",))},
        "targets": [pick(t, ("width", "height", "name"))
                    for t in (obj.get("targets") or [])],
    }
    # Already minimal at the source (Activity.report()/Arbiter.status()/the
    # note capability/the /public.json led_log addition) -- passed through
    # rather than re-filtered, so there is exactly one place each shape is
    # defined. `build` (the internal git commit SHA) is deliberately NOT in
    # this set: it is internal repo state with no public purpose (F2).
    for k in ("viewers", "listeners", "inflight", "note", "led_log"):
        if k in obj:
            out[k] = obj[k]
    return out


def _poll_state():
    global _state, _state_ts
    status, body = _get("/public.json")
    if status == 200 and body:
        try:
            obj = json.loads(body)
        except ValueError:
            return
        with _lock:
            _state, _state_ts = _filter_public_state(obj), time.time()


# REDACTED, BY OPERATOR DECISION -- not an oversight. `_summarise()`
# (vcctrld.py) puts the literal typed text in a `type` event's `detail`
# specifically so the OPERATOR can see what a sweep typed; a security audit
# of this feature flagged that the same field, unfiltered, narrates
# passwords/usernames/paths to the open internet the moment they're typed.
# The operator's call: redact it here, keep the one-line "what's happening"
# note (NoteCapability) as the public page's account of current activity --
# that answers "what is it doing" without echoing keystrokes. Every other
# event kind (key names, mouse deltas, power actions) is not free text and
# stays as-is; see the audit's own note that `inflight` carries command
# names only, never arguments, so this is the one place redaction is needed.
def _redact_public_events(evs):
    out = []
    for ev in evs:
        if ev.get("kind") == "cmd" and ev.get("cmd") == "type":
            ev = dict(ev, detail="[redacted]")
        out.append(ev)
    return out


def _poll_events():
    global _events_seq
    status, body = _get("/events?since=%d" % _events_seq)
    if status != 200 or not body:
        return
    try:
        obj = json.loads(body)
    except ValueError:
        return
    evs = _redact_public_events(obj.get("events") or [])
    with _lock:
        _events.extend(evs)
    if obj.get("seq") is not None:
        _events_seq = obj["seq"]


def _start_pollers():
    _telemetry_load()
    for name, interval, fn in (
        # The shot poll bounds what POLL_HZ can deliver: viewers are fed
        # from this cache, so it must refresh at least as fast as _mjpeg
        # resends (0.6 s here once capped the "5 fps" page at an actual
        # 1.7). One GET of /shot.jpg per tick against the loopback daemon,
        # which serves it from the ring without decoding.
        ("shot", 0.2, _poll_shot),
        ("stale", 5.0, _poll_stale),
        ("state", 1.75, _poll_state),
        ("events", 2.5, _poll_events),
        # Not an upstream poll like the four above -- reuses _poll_loop's
        # "run forever, one bad iteration doesn't kill the thread" shape
        # because that's exactly what periodic persistence needs too.
        ("telemetry-save", TELEMETRY_SAVE_INTERVAL_S, _telemetry_save),
    ):
        t = threading.Thread(target=_poll_loop, args=(name, interval, fn),
                             daemon=True, name="poll-%s" % name)
        t.start()


# -------------------------------------------------------------------- audio
#
# ONE upstream connection, shared by every public listener -- the same
# "poll/hold once, fan out to N" shape as the video cache above, not a
# per-viewer proxy. This process is the WEBSOCKET CLIENT here; the private
# daemon is the server, exactly as a browser would see it.

MAX_AUDIO_LISTENERS = MAX_VIEWERS  # same headroom reasoning as video

# A semaphore, not a `len(listeners) >= MAX` check: the latter is a
# check-then-act race between two connecting threads, same reason
# `_viewer_sem` guards /stream.mjpg above rather than counting `_live`'s
# readers by hand. ONE semaphore across both codecs -- the cap is about
# public connection slots, not about which encoding each slot carries.
_audio_sem = threading.Semaphore(MAX_AUDIO_LISTENERS)


class _AudioRelay:
    """One upstream audio connection, fanned out to its public listeners --
    one instance per codec, both running the same `_audio_upstream_loop`.

    `replay_headers` is the whole difference between the two: an Ogg Opus
    stream begins with OpusHead/OpusTags pages a decoder cannot start
    without, so the Opus relay caches the header pages of the CURRENT
    upstream stream and `_audio_ws` sends them to every listener before any
    live page. `epoch` counts upstream (re)connects: a reconnect means a
    fresh encoder stream over there whose pages cannot follow the old
    stream's headers, so on every reconnect the Opus relay drops its
    listeners (kvm-ro.html reconnects on close and gets the new headers by
    the same replay) where the stateless PCM relay just keeps feeding.
    """

    def __init__(self, name, upstream_path, replay_headers):
        self.name = name
        self.upstream_path = upstream_path
        self.replay_headers = replay_headers
        self.lock = threading.Lock()
        self.listeners = set()       # raw sockets currently attached
        self.headers = []            # header-page WS payloads, current epoch
        self.epoch = 0
        # Set by _audio_ws the moment a listener attaches, so the upstream
        # loop's idle and backoff waits end NOW instead of at their next
        # tick. Without this, unmute paid for the lazy connect in real,
        # audible seconds: up to 0.5 s of idle poll on a first listen and
        # up to the full 3 s teardown backoff on an unmute-after-mute --
        # the operator heard the difference against the private KVM before
        # this event existed.
        self.wake = threading.Event()


_PCM_RELAY = _AudioRelay("pcm", "/wsaudio", replay_headers=False)
_OPUS_RELAY = _AudioRelay("opus", "/wsaudio?codec=opus", replay_headers=True)


def _is_ogg_header_page(data):
    """True for the header pages of an Ogg stream: a real Ogg page whose
    granule position is 0 (OpusHead and OpusTags; every audio page carries
    the running sample count instead). Callers only apply this before the
    first audio page of a stream."""
    return (len(data) >= 27 and data[:4] == b"OggS"
            and int.from_bytes(data[6:14], "little", signed=True) == 0)


def _ws_client_handshake(sock, host, path):
    """RFC 6455 opening handshake, client side. Raises on any mismatch --
    a silent bad handshake here would mean silently reading garbage as WS
    frames next.

    Reads the HTTP response ONE BYTE AT A TIME, deliberately, rather than
    through `sock.makefile()`. A buffered reader over-reads from the kernel
    socket buffer opportunistically, and on loopback the server's first audio
    frame can easily arrive in the same TCP segment as its handshake response
    -- `serve_ws_audio` (vcweb.py) starts pushing from the live edge the
    instant the handshake completes, with nothing to wait for. A buffer that
    swallowed those bytes and was then discarded (`.detach()` does not hand
    them back) would desync every `ws_read()` after it: the first "frame"
    read would actually start partway into a real one. One byte at a time
    costs nothing on a handshake this short and cannot over-read.
    """
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET %s HTTP/1.1\r\n"
           "Host: %s\r\n"
           "Upgrade: websocket\r\n"
           "Connection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, key))
    sock.sendall(req.encode())

    def readline():
        buf = bytearray()
        while True:
            b = sock.recv(1)
            if not b:
                raise ConnectionError("upstream closed during handshake")
            buf += b
            if buf.endswith(b"\r\n"):
                return bytes(buf[:-2])

    status_line = readline().decode(errors="replace")
    if " 101 " not in status_line:
        raise ConnectionError("handshake refused: %s" % status_line)
    headers = {}
    while True:
        line = readline().decode(errors="replace")
        if not line:
            break
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    want = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    if headers.get("sec-websocket-accept") != want:
        raise ConnectionError("Sec-WebSocket-Accept mismatch")


def _audio_upstream_loop(relay):
    """Runs forever, one thread per relay: while the relay has at least one
    public listener, hold a client connection to the private daemon's
    matching /wsaudio and copy every binary frame to every listener; with no
    listeners, hold nothing (so the private daemon's "only stream when
    someone is listening" -- and for Opus, "only ENCODE when someone is
    listening" -- extends through this process to the actual public
    audience, instead of the mirror itself counting as a permanent
    listener)."""
    while True:
        # Clear BEFORE checking, so a listener that registers between the
        # check and the wait leaves the event set and the wait returns at
        # once -- the standard order that makes an Event race-free here.
        relay.wake.clear()
        with relay.lock:
            wanted = bool(relay.listeners)
        if not wanted:
            relay.wake.wait(0.5)
            continue
        sock = None
        saw_audio = False
        try:
            sock = socket.create_connection(("127.0.0.1", UPSTREAM_PORT),
                                            timeout=5)
            _ws_client_handshake(sock, "127.0.0.1:%d" % UPSTREAM_PORT,
                                relay.upstream_path)
            sock.settimeout(30)
            while True:
                got = ws_read(sock)
                if got is None:
                    break
                opcode, data = got
                if opcode == 0x8:          # upstream closed
                    break
                if opcode != 0x2:          # only relay binary frames
                    continue
                frame = ws_frame(data, opcode=0x2)
                # SNAPSHOT THE SET, SEND OUTSIDE THE LOCK. `sendall` blocks
                # until the OS accepts the bytes, which a listener that has
                # stopped draining its own socket can stall indefinitely --
                # holding the relay lock across that would freeze every other
                # listener's connect/disconnect for as long as one stuck
                # listener takes. `_audio_ws` bounds each socket's own
                # blocking calls to a few seconds (see its `settimeout`), so
                # the worst case here is bounded too, not unbounded.
                #
                # HEADER CACHING AND THE SNAPSHOT SHARE ONE LOCK HOLD, and
                # that is load-bearing for the Opus relay: a listener joining
                # via _register_with_headers atomically checks "no header I
                # have not sent myself" before attaching, so as long as
                # cache-append and listener-snapshot cannot interleave with
                # that check, a header page reaches each listener exactly
                # once -- from the cache if it attached after the append,
                # live from this loop if it attached before.
                with relay.lock:
                    if (relay.replay_headers and not saw_audio
                            and _is_ogg_header_page(data)):
                        relay.headers.append(data)
                    else:
                        saw_audio = saw_audio or relay.replay_headers
                    listeners = list(relay.listeners)
                dead = []
                for listener in listeners:
                    try:
                        listener.sendall(frame)
                    except Exception:
                        dead.append(listener)
                if dead:
                    with relay.lock:
                        for d in dead:
                            relay.listeners.discard(d)
                    # shutdown(), not just discard: the listener's own
                    # _audio_ws thread is blocked in recv() and holds a
                    # MAX_AUDIO_LISTENERS slot; without this it kept
                    # holding both until the 30-minute cap, fed by
                    # nothing (same stuck-slot failure the 2026-08-28
                    # keepalive work was about, reached by a different
                    # door: a peer that stops READING fails sends here,
                    # while one that vanishes entirely fails the recv).
                    # close() would not wake that blocked recv;
                    # shutdown() does, and the handler closes its own fd.
                    for d in dead:
                        try:
                            d.shutdown(socket.SHUT_RDWR)
                        except Exception:
                            pass
                # All listeners gone mid-stream: drop the upstream too,
                # rather than streaming to nobody until it happens to close.
                if not listeners and not dead:
                    with relay.lock:
                        wanted = bool(relay.listeners)
                    if not wanted:
                        break
        except Exception as exc:
            print("vcweb_public: audio upstream (%s): %s: %s"
                  % (relay.name, type(exc).__name__, exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        # This upstream stream is over. For the Opus relay that has a
        # consequence PCM does not have: the next connection is a NEW Ogg
        # stream (fresh serial, fresh headers, granule restarting), and its
        # pages cannot follow the headers already sent to current listeners.
        # Advance the epoch, forget the dead stream's headers, and close
        # every attached listener -- kvm-ro.html reconnects on close and the
        # replay hands it the new stream's headers. A dropped-and-reconnected
        # listener hears a blip; a listener fed a second stream's pages
        # behind the first stream's headers hears garbage or silence with no
        # error, which is the confident-but-wrong failure this file never
        # accepts.
        with relay.lock:
            relay.epoch += 1
            relay.headers = []
            dropped = list(relay.listeners) if relay.replay_headers else []
        for d in dropped:
            # shutdown(), NOT close(). Each of these sockets has an
            # _audio_ws thread blocked in recv() on it, and close() from
            # this thread neither wakes that recv nor reliably sends the
            # FIN while the in-flight syscall still references the fd --
            # the integration check caught exactly that: "dropped"
            # listeners that never saw the connection end. shutdown()
            # terminates the connection out from under the blocked read
            # (it returns empty, the handler exits and closes its own fd),
            # and the browser sees a clean close to reconnect from.
            try:
                d.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
        # Backoff sized to who is waiting: 3 s idle keeps a dead upstream
        # from being hammered for nobody; 0.5 s when listeners exist keeps
        # a daemon restart from costing them more than a blink. Either way
        # the wake event cuts it short the moment a NEW listener arrives --
        # this wait is where an unmute-after-mute used to sit out the full
        # idle backoff.
        with relay.lock:
            waiting = bool(relay.listeners)
        relay.wake.wait(0.5 if waiting else 3.0)


def _register_with_headers(relay, sock):
    """Attach `sock` to a header-replaying relay: send every cached header
    page of the current upstream stream, then attach atomically -- the
    attach only happens in a lock hold that proves no header page exists
    that this listener has not already been sent (see the fan-out loop's
    own comment for the other half of the exactly-once argument). Returns
    True when attached; False when the upstream stream changed underneath
    (caller should just close -- the client's reconnect lands cleanly).
    Socket errors propagate to the caller like any other send failure."""
    sent = 0
    with relay.lock:
        epoch = relay.epoch
    while True:
        with relay.lock:
            if relay.epoch != epoch:
                return False
            hdrs = relay.headers[sent:]
            if not hdrs:
                relay.listeners.add(sock)
                return True
        for page in hdrs:
            sock.sendall(ws_frame(page, opcode=0x2))
        sent += len(hdrs)


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vcctrl-public"
    # Suppress the "Python/3.x.y" sub-string the stdlib appends to Server (F6):
    # a public-facing process has no reason to advertise its interpreter build.
    sys_version = ""

    # Same reasoning as vcweb.py's own Handler (FINDINGS 19): the default
    # logs every request to stderr/journald, and a stream plus per-request
    # logging is how the harness DoSed journald once already. Silence here is
    # deliberate, doubly so on a process meant to face the open internet.
    def log_message(self, fmt, *args):
        pass

    # -- helpers ------------------------------------------------------------

    def _send(self, code, body, ctype="application/json", extra=None, csp=None,
              cache=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # no-store for everything except what explicitly opts out: live
        # state must never be a stale cache hit, but the one big immutable
        # asset (the versioned Opus decoder) would otherwise be re-sent to
        # every visitor on every visit.
        self.send_header("Cache-Control", cache or "no-store")
        # Hardening headers on every response from the public-facing process
        # (F3/F3-followup). frame-ancestors 'none' alone was the baseline --
        # a full default-src/script-src policy needed serve-time nonce
        # injection, which the one `<script>` in kvm-ro.html now gets (see
        # do_GET's `/` route). ONE header, ONE full policy: two separate
        # Content-Security-Policy headers are enforced as an INTERSECTION
        # (each directive wins from whichever header states it), which reads
        # as "additive" and is not -- passing the whole string through `csp`
        # keeps every route's policy a single, complete, readable value
        # rather than something that only makes sense combined with the
        # default this method would otherwise send.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp or "frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj), "application/json", extra)

    def _serve_file(self, name, ctype, csp=None, cache=None):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                body = f.read()
        except OSError as exc:
            return self._json({"error": str(exc)}, 500)
        return self._send(200, body, ctype, csp=csp, cache=cache)

    # A FRESH NONCE PER RESPONSE, spent once. Reusing one across requests
    # would let a script injected via some OTHER hole (this file has none
    # known, but a CSP's whole point is to survive one appearing) replay a
    # previously-seen nonce; generating one per GET of `/` is what makes the
    # nonce actually mean "the server emitted this specific script tag just
    # now," not "the server knows a password."
    def _serve_html_with_csp(self):
        try:
            with open(os.path.join(HERE, "kvm-ro.html"), "rb") as f:
                body = f.read()
        except OSError as exc:
            return self._json({"error": str(exc)}, 500)
        # A SERVER-SIDE VISIT COUNT, not the client-side "pageload" beacon
        # (kvm-ro.html's own JS, sent to /telemetry once it runs) -- this
        # one fires even for a visitor whose JS never runs at all (blocked,
        # or the same restricted-context class of failure the PAGE SCRIPT
        # ERROR banner exists for), so "how many times was the page served"
        # stays accurate independent of whether the client-side beacon
        # would have. Both feed the same visits_total/visits_by_day -- there
        # is deliberately no attempt to de-duplicate one visit counted
        # twice; this is a traffic gauge, not an analytics platform.
        _telemetry_record_visit()
        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        # ONE nonce'd tag: `<script>` is a literal opening tag with no
        # attributes today (grep confirms exactly one in kvm-ro.html), so a
        # plain byte-replace is exact and does not risk matching inside a
        # JS string/comment the way a regex over the whole file could.
        body = body.replace(b"<script>",
                             ('<script nonce="%s">' % nonce).encode("ascii"),
                             1)
        # OG_PAGE_URL, THE SAME PER-REQUEST SUBSTITUTION AS THE NONCE ABOVE,
        # for the same reason: a link-unfurler (Slack, Signal, iMessage)
        # fetches this markup with no browser to resolve a relative URL
        # against, and no tailnet hostname is ever a literal in the tracked
        # file (kvm-ro.html's own comment explains why). `self.headers["Host"]`
        # is THIS request's own Host header -- always whichever hostname the
        # visitor actually used. Always "https": this process is only ever
        # reached through `tailscale serve`/`funnel`, which terminate TLS
        # before proxying here as plain HTTP, so the PUBLIC url is always
        # https regardless of what this process itself was handed.
        #
        # No OG_IMAGE_URL substitution: this page serves no og:image /
        # twitter:image (see kvm-ro.html's own comment for why).
        origin = "https://" + (self.headers.get("Host") or "")
        body = body.replace(b"OG_PAGE_URL", (origin + "/").encode("ascii"))
        # default-src 'none': every category below is opted in explicitly,
        # so a resource type nobody has thought to write a rule for is
        # refused rather than silently inheriting a permissive default.
        #   script-src   the nonce for the page's one inline script, 'self'
        #                for exactly one same-origin file this process
        #                chooses to route (the vendored Opus decoder,
        #                OPUS_DECODER_JS -- this used to say "no 'self',
        #                this process serves no OTHER .js file", and 'self'
        #                was added in the same change that made that stop
        #                being true), and 'wasm-unsafe-eval' because that
        #                decoder instantiates WebAssembly, which a strict
        #                CSP refuses without it. 'wasm-unsafe-eval' permits
        #                WASM compilation and nothing else -- notably NOT
        #                eval()/Function(), which stay refused. Still no
        #                'unsafe-inline'.
        #   style-src    'self' for /themes.css, 'unsafe-inline' for the one
        #                inline <style> block and this page's dozen inline
        #                style="" attributes. CSS injection cannot execute
        #                script; nonce-per-attribute is impractical here, so
        #                this is the one directive with a real trade-off,
        #                same one F3 already accepted for this page.
        #   img-src      'self' for /shot.jpg,/lastgood.jpg, data: for the
        #                favicon links, blob: for the fetched-then-
        #                createObjectURL'd stale-frame image.
        #   connect-src  'self' for /state.json,/events,/shot.jpg,
        #                /lastgood.jpg's fetch() calls, and (CSP upgrades
        #                the scheme for comparison purposes) the same-origin
        #                /wsaudio websocket -- no separate ws:/wss: entry
        #                needed since nothing here opens one to any other
        #                host or port.
        #   media-src    'self' for the mjpeg <img> stream (browsers file
        #                multipart/x-mixed-replace under img-src OR media-src
        #                depending on engine; both list it to not depend on
        #                which one a given browser chooses).
        #   base-uri/form-action 'none': nothing on this page ever needed
        #                either, so neither should ever silently start being
        #                usable by something injected.
        csp = ("default-src 'none'; "
               "script-src 'nonce-%s' 'self' 'wasm-unsafe-eval'; "
               "style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data: blob:; "
               "connect-src 'self'; "
               "media-src 'self'; "
               "base-uri 'none'; "
               "form-action 'none'; "
               "frame-ancestors 'none'" % nonce)
        return self._send(200, body, "text/html; charset=utf-8", csp=csp)

    # -- routes ---------------------------------------------------------
    #
    # Deliberately absent from this list, not 403'd, not gated: /cmd, /ws,
    # /buffer.avi, /pulled, /keymap.json, /config, /timeline.json. A request
    # for any of them falls through to the 404 at the bottom. `do_POST`
    # exists now (added 2026-08-28, see its own docstring below) but only
    # ever answers ONE path, /telemetry, with aggregate-only counters --
    # every other POST, /cmd included, still falls through to the same 404
    # this method returns for an unknown GET path. See the module docstring
    # for why that one addition doesn't reopen the command-channel question
    # F1/the security audit closed.
    #
    # /wsaudio IS implemented here, and it is the one websocket route this
    # file offers -- worth explaining why that's not a contradiction of "no
    # /ws": /wsaudio is OUTPUT ONLY, mirroring the private daemon's own
    # /wsaudio (confirmed genuinely one-directional, see
    # _audio_upstream_loop's docstring and `Handler._audio_ws` below, which
    # never parses what a listener sends beyond noticing it closed). /ws is
    # the channel that actually carries commands, and it stays absent.

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if PREFIX and path.startswith(PREFIX):
            path = path[len(PREFIX):] or "/"
        try:
            if path == "/":
                return self._serve_html_with_csp()
            if path == "/themes.css":
                return self._serve_file("themes.css", "text/css; charset=utf-8")
            if path == "/" + OPUS_DECODER_JS:
                # The VERSION IS IN THE FILENAME, and that is what makes the
                # immutable cache honest: re-vendoring the decoder changes
                # the name (vendor/README.md), kvm-ro.html's script tag moves
                # with it, and no visitor can be pinned to a stale copy.
                # charset must be explicit -- the decoder's own README
                # requires UTF-8 reads, and a separate file does not inherit
                # the page's <meta charset>. Three layouts, first hit wins:
                # flat beside this file (pi/install.sh install_public()'s
                # --public-only deploy), ./vendor/ (a full install copies
                # the whole vendor tree under PREFIX), ../vendor/ (a source
                # checkout) -- same multi-layout pick as vcctrld's own
                # common/ imports.
                name = OPUS_DECODER_JS
                for rel in (name, os.path.join("vendor", name),
                            os.path.join("..", "vendor", name)):
                    if os.path.exists(os.path.join(HERE, rel)):
                        name = rel
                        break
                return self._serve_file(
                    name, "application/javascript; charset=utf-8",
                    cache="public, max-age=31536000, immutable")
            if path == "/state.json":
                with _lock:
                    state, ts = dict(_state), _state_ts
                if not state:
                    return self._json({"ok": False,
                                       "error": "no state relayed from "
                                                "upstream yet"}, 503)
                # The AGE OF THE RELAY, not the age of any one field inside
                # it -- kvm-ro.html's own per-field staleness math (e.g.
                # sysinfo's age_s, leds' changed_at) still means what it says
                # relative to when the PRIVATE daemon took the reading; this
                # is only "how current is this mirror's own copy."
                state["_relayed_age_s"] = round(time.time() - ts, 1)
                return self._json(state)
            if path == "/events":
                since = 0
                if "?" in self.path:
                    for part in self.path.split("?", 1)[1].split("&"):
                        if part.startswith("since="):
                            since = int(part[len("since="):] or 0)
                with _lock:
                    evs = [e for e in _events if e.get("seq", 0) > since]
                    seq = _events_seq
                return self._json({"ok": True, "events": evs, "seq": seq})
            if path in ("/shot.jpg", "/lastgood.jpg"):
                with _lock:
                    cached = _live if path == "/shot.jpg" else _stale
                if cached is None:
                    # Same 503-not-a-placeholder discipline as vcweb.py: a
                    # public viewer must be able to tell "no picture" from
                    # "a picture", never be handed a plausible-looking blank.
                    return self._json({"picture": False,
                                       "reason": "no frame relayed from "
                                                "upstream yet"}, 503)
                body, ts = cached
                return self._send(200, body, "image/jpeg",
                                  {"X-Frame-Age":
                                       str(round(time.time() - ts, 2))})
            if path == "/stream.mjpg":
                return self._mjpeg()
            if path == "/wsaudio":
                codec = "pcm"
                if "?" in self.path:
                    for part in self.path.split("?", 1)[1].split("&"):
                        if part == "codec=opus":
                            codec = "opus"
                return self._audio_ws(_OPUS_RELAY if codec == "opus"
                                      else _PCM_RELAY)
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            except Exception:
                pass

    TELEMETRY_KINDS = {"first-frame", "error"}
    TELEMETRY_MAX_BODY = 2048

    def do_POST(self):
        """The ONLY thing a POST to this process can ever do: submit one
        aggregate telemetry sample to /telemetry. Every other path --
        including /cmd -- falls through to the same 404 do_GET returns for
        an unknown path, not to some richer handler; adding this method
        does not change what POST /cmd gets back in any way that matters
        (501 with no do_POST at all, 404 with one that only answers
        /telemetry -- neither is "the command was accepted"). See the
        module docstring for the fuller reasoning.

        kvm-ro.html's own JS is the only intended caller, but nothing here
        trusts that: `kind` is allowlisted, the body is size-capped before
        it's even read, and every value pulled out of it is validated and
        clamped before touching shared state -- the same "untrusted input,
        never let it reach an exception past this boundary" posture as
        _get()'s own docstring above."""
        path = self.path.split("?", 1)[0]
        if PREFIX and path.startswith(PREFIX):
            path = path[len(PREFIX):] or "/"
        if path != "/telemetry":
            return self._json({"error": "not found"}, 404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > self.TELEMETRY_MAX_BODY:
                return self._json({"error": "bad request"}, 400)
            body = self.rfile.read(length)
            obj = json.loads(body)
            kind = obj.get("kind")
            if kind not in self.TELEMETRY_KINDS:
                return self._json({"error": "bad request"}, 400)
            if kind == "first-frame":
                ms = obj.get("ms")
                if isinstance(ms, (int, float)) and 0 <= ms < 300000:
                    _telemetry_record_perf(ms)
            elif kind == "error":
                _telemetry_record_error(str(obj.get("message", "")))
            return self._send(204, b"")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, TypeError, OSError):
            try:
                return self._json({"error": "bad request"}, 400)
            except Exception:
                pass
        except Exception as exc:
            try:
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            except Exception:
                pass

    def _mjpeg(self):
        """multipart/x-mixed-replace, fed from the shared `_live`/`_stale`
        caches only.

        Every connected viewer reads the SAME caches at the SAME poll rate --
        this is the load safeguard from the plan's section 4. A viewer that
        cannot keep up just gets a write error and is dropped; nothing is
        buffered per-viewer beyond the OS socket buffer, matching this
        codebase's existing "drop rather than queue" philosophy (see
        vcweb.py's `_ws_pump`).

        SENDS EVERY TICK, not only when the frame changes -- operator
        decision, 2026-08-28: a target sitting in `frozen` (every recent
        frame a byte-identical duplicate) makes the private daemon's own
        `/shot.jpg` refuse outright ("every frame in the window was a
        duplicate"), which used to mean this connection wrote NOTHING for as
        long as that lasted -- an open response with zero bytes ever sent,
        which is exactly what a browser's "waiting on <host>" status
        describes, and it is not wrong to say so. Resending the same JPEG on
        a fixed cadence instead matches how a real analog capture already
        behaves under a genuinely static picture -- the stick keeps emitting
        a frame every cycle whether or not the content changed; see the
        "frozen reads as no signal, and that is not a euphemism" comment in
        kvm.html/kvm-ro.html for the same fact from the other side. Falling
        back to `_stale` (the daemon's own last-known-good) when there is no
        live frame at all keeps the same property one step further out.

        BOTH CACHES EMPTY falls back to `_TEST_PATTERN` -- operator decision,
        2026-08-28, made after the target sat in `frozen` with no `lastgood`
        ever recorded either, which left this loop with nothing at all to
        resend and the browser back to "waiting on <host>" regardless. See
        `_TEST_PATTERN`'s own comment for why it is a labelled color-bar card
        and not a plain black frame.
        """
        if not _viewer_sem.acquire(blocking=False):
            return self._json({"error": "too many viewers, try again "
                                        "shortly"}, 503)
        try:
            boundary = "vcctrlpublicframe"
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=%s"
                             % boundary)
            self.send_header("Cache-Control", "no-store")
            # Same hardening as _send (F3); this path builds its own headers.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            # TCP KEEPALIVE -- guards against a HALF-OPEN connection holding
            # a viewer slot forever. This loop only ever WRITES; nothing
            # here reads from the client to notice it's gone, and a
            # `wfile.write()` to a vanished peer does not fail on its own
            # until the local send buffer actually fills -- which, at one
            # ~15-69KB frame per POLL_HZ tick, can take a long time against
            # a peer that torched the connection without a clean FIN/RST (a
            # killed process, a relay hop that ate the reset). Found the
            # hard way 2026-08-28: several diagnostic connections left
            # exactly like that were still counted as viewers by
            # `_viewer_sem` with nothing on the other end, and exhausted
            # MAX_VIEWERS for a real one. Keepalive makes the OS itself
            # probe for a dead peer and fail the socket in roughly
            # KEEPIDLE + KEEPINTVL*KEEPCNT seconds (~30s here) instead of
            # whatever it would take the send buffer to fill.
            try:
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                self.connection.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
                self.connection.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                self.connection.setsockopt(
                    socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except (AttributeError, OSError):
                # TCP_KEEPIDLE/INTVL/CNT are Linux-specific; SO_KEEPALIVE
                # alone (set above, if that didn't already raise) still
                # gets the OS's own default keepalive schedule, just not
                # tuned this aggressively. Either way, not fatal to the
                # stream itself.
                pass
            started_at = time.monotonic()
            while time.monotonic() - started_at < MJPEG_MAX_DURATION_S:
                with _lock:
                    item = _live or _stale
                body = item[0] if item is not None else _TEST_PATTERN
                self.wfile.write(
                    ("--%s\r\nContent-Type: image/jpeg\r\n"
                     "Content-Length: %d\r\n\r\n"
                     % (boundary, len(body))).encode())
                self.wfile.write(body)
                self.wfile.write(b"\r\n")
                time.sleep(1.0 / POLL_HZ)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            _viewer_sem.release()

    def _audio_ws(self, relay):
        """Server-side handshake, then register this socket to receive
        whatever `_audio_upstream_loop` relays for this codec. This method's
        only job after the handshake is noticing the connection died -- it
        never inspects what a listener sends beyond the opcode needed to
        tell. Same handshake arithmetic as vcweb.py's own `_websocket()`,
        independently implemented here rather than imported (see module
        docstring)."""
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._json({"error": "not a websocket request"}, 400)
        if not _audio_sem.acquire(blocking=False):
            return self._json({"error": "too many listeners, try again "
                                        "shortly"}, 503)
        try:
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            sock = self.connection
            # BOUNDS BOTH DIRECTIONS ON THIS SOCKET: the fan-out thread's
            # `sendall` to a listener that stopped draining its receive
            # buffer, and this loop's own `ws_read` when the listener (as
            # expected -- it never has to send anything) simply says
            # nothing for a while. A plain socket timeout applies to both,
            # so the read loop below must treat a timeout as "nothing yet",
            # not as a close -- see the `except socket.timeout` below.
            sock.settimeout(5.0)
            # TCP KEEPALIVE, same reasoning and same incident as _mjpeg()'s
            # own comment: a `socket.timeout` above is NOT evidence the
            # peer is gone, only that it said nothing for 5s -- which is
            # the expected, normal case here (a listener never sends
            # anything). A half-open connection (peer vanished without a
            # clean FIN/RST) would `continue` on that timeout forever,
            # holding an `_audio_sem` slot with nothing listening. Keepalive
            # makes the OS itself probe for and fail a genuinely dead peer
            # in ~30s instead of its own multi-hour default.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except (AttributeError, OSError):
                pass
            try:
                if relay.replay_headers:
                    # Cached stream headers first, then attach -- see
                    # _register_with_headers. False means the upstream
                    # stream flipped mid-join: close, and the client's own
                    # reconnect gets the new stream cleanly.
                    if not _register_with_headers(relay, sock):
                        return
                else:
                    with relay.lock:
                        relay.listeners.add(sock)
                relay.wake.set()
            except Exception:
                return
            try:
                started_at = time.monotonic()
                while time.monotonic() - started_at < MJPEG_MAX_DURATION_S:
                    # NOT `_ws_handle_input`. The only thing read from this
                    # opcode is "did it close" -- no json.loads, no dispatch,
                    # no branch on payload contents. Actual audio bytes reach
                    # this socket from the OTHER thread
                    # (_audio_upstream_loop), which writes to it directly;
                    # this loop never writes anything.
                    try:
                        got = ws_read(sock)
                    except socket.timeout:
                        continue
                    if got is None or got[0] == 0x8:
                        break
            except Exception:
                pass
            finally:
                with relay.lock:
                    relay.listeners.discard(sock)
        finally:
            _audio_sem.release()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    _start_pollers()
    for relay in (_PCM_RELAY, _OPUS_RELAY):
        threading.Thread(target=_audio_upstream_loop, args=(relay,),
                         daemon=True,
                         name="audio-upstream-%s" % relay.name).start()
    httpd = Server((BIND, PORT), Handler)
    print("vcweb_public: listening on %s:%d, mirroring %s"
          % (BIND, PORT, UPSTREAM))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
