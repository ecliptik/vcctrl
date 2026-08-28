#!/usr/bin/env python3
"""vcweb_public -- a read-only, isolated mirror of the KVM page for public
viewing.

Deliberately its own file, importing nothing from vcctrld.py or vcweb.py:
this process must be auditable, by grep alone, as incapable of ever sending
a command to the target. It has no `/cmd` route -- `do_POST` is not defined
anywhere in this file, so `BaseHTTPRequestHandler`'s stock handler answers
any POST with 501 -- and it never implements vcweb.py's `/ws`. That is not
caution for its own sake: `/ws` was checked directly against
`_ws_handle_input` (daemon/vcweb.py:1253-1310) and found to be a genuine
second, bidirectional command channel -- it parses client JSON and
dispatches `keydown`/`keyup`/`type`/`combo`/`release_all` -- not just a video
feed. Video reaches viewers here only as `multipart/x-mixed-replace`, which
is architecturally one-directional: there is no code path in an HTTP
response body that can carry a client-to-server message.

Everything this process shows comes from ONE background thread family
polling the existing, unmodified private vcweb.py over loopback at a fixed,
low rate -- see the four `_poll_*` functions below. Public viewer count
therefore never changes the load the private daemon sees, regardless of
whether zero or five hundred people are watching; MAX_VIEWERS/POLL_HZ below
are the other half of that story, bounding what THIS process spends on
public connections.

Audio IS relayed, one-way, over a single upstream connection this process
opens to itself: it is the WEBSOCKET CLIENT to the private daemon's
`/wsaudio` (never the reverse), and every public listener on `/wsaudio` gets a
copy of the same bytes. The public-facing half deliberately does not parse
anything a listener sends -- see `Handler._audio_ws` below -- because
`_ws_handle_input` (daemon/vcweb.py:1253-1310) is exactly what a WebSocket
that DOES act on client frames looks like, and this one must never grow into
that by accident.
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
# vcctrl.example.yaml's `daemon.web_public` block for the documented meaning
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

# The Pi this runs on has limited free memory and no swap (docs/WEBKVM.md
# sec. 3). At the shared, low poll rate below, only outbound BANDWIDTH scales
# with public viewer count -- not Pi CPU or memory, since every viewer reads
# the same cached frame rather than causing a new upstream request. This cap
# is headroom against bandwidth exhaustion and thread pile-up, not a tightly
# reasoned number; raise it once real traffic says it's too low.
MAX_VIEWERS = 40

# This is a "watch it work" feed, not a remote-desktop session -- the low end
# of what a human eye needs to see the machine is alive. Every extra frame a
# second here is bandwidth spent once per viewer, with no cap on how many
# viewers there can be.
POLL_HZ = 1.5

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
    for name, interval, fn in (
        ("shot", 0.6, _poll_shot),
        ("stale", 5.0, _poll_stale),
        ("state", 1.75, _poll_state),
        ("events", 2.5, _poll_events),
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

_audio_lock = threading.Lock()
_audio_listeners = set()          # raw sockets currently on /wsaudio
# A semaphore, not a `len(_audio_listeners) >= MAX` check: the latter is a
# check-then-act race between two connecting threads, same reason
# `_viewer_sem` guards /stream.mjpg above rather than counting `_live`'s
# readers by hand.
_audio_sem = threading.Semaphore(MAX_AUDIO_LISTENERS)


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


def _audio_upstream_loop():
    """Runs forever: connect to the private daemon's /wsaudio, relay every
    binary frame to whichever public listeners are on /wsaudio, reconnect on
    any failure. One thread, one upstream socket, regardless of how many
    public listeners there are -- the audio equivalent of the video pollers
    above."""
    while True:
        sock = None
        try:
            sock = socket.create_connection(("127.0.0.1", UPSTREAM_PORT),
                                            timeout=5)
            _ws_client_handshake(sock, "127.0.0.1:%d" % UPSTREAM_PORT,
                                "/wsaudio")
            sock.settimeout(30)
            while True:
                got = ws_read(sock)
                if got is None:
                    break
                opcode, data = got
                if opcode == 0x8:          # upstream closed
                    break
                if opcode != 0x2:          # only relay binary PCM chunks
                    continue
                frame = ws_frame(data, opcode=0x2)
                # SNAPSHOT THE SET, SEND OUTSIDE THE LOCK. `sendall` blocks
                # until the OS accepts the bytes, which a listener that has
                # stopped draining its own socket can stall indefinitely --
                # holding `_audio_lock` across that would freeze every other
                # listener's connect/disconnect for as long as one stuck
                # listener takes. `_audio_ws` bounds each socket's own
                # blocking calls to a few seconds (see its `settimeout`), so
                # the worst case here is bounded too, not unbounded.
                with _audio_lock:
                    listeners = list(_audio_listeners)
                dead = []
                for listener in listeners:
                    try:
                        listener.sendall(frame)
                    except Exception:
                        dead.append(listener)
                if dead:
                    with _audio_lock:
                        for d in dead:
                            _audio_listeners.discard(d)
        except Exception as exc:
            print("vcweb_public: audio upstream: %s: %s"
                  % (type(exc).__name__, exc))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        # No listeners, no rush; a listener connecting mid-backoff just waits
        # for the next attempt rather than forcing an immediate reconnect.
        time.sleep(3.0)


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

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Hardening headers on every response from the public-facing process
        # (F3). frame-ancestors 'none' blocks the page being embedded to
        # impersonate the feed; it is a CSP directive unaffected by the page's
        # inline <script>/<style>, so it does not need nonces. A full
        # default-src/script-src CSP is deliberately NOT set here -- the
        # inline-heavy page would break without serve-time nonce injection.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj), "application/json", extra)

    def _serve_file(self, name, ctype):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                body = f.read()
        except OSError as exc:
            return self._json({"error": str(exc)}, 500)
        return self._send(200, body, ctype)

    # -- routes ---------------------------------------------------------
    #
    # Deliberately absent from this list, not 403'd, not gated: /cmd, /ws,
    # /buffer.avi, /pulled, /keymap.json, /config, /timeline.json. A request
    # for any of them falls through to the 404 at the bottom. There is no
    # `do_POST` on this class at all -- see the module docstring.
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
                return self._serve_file("kvm-ro.html",
                                        "text/html; charset=utf-8")
            if path == "/themes.css":
                return self._serve_file("themes.css", "text/css; charset=utf-8")
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
                return self._audio_ws()
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
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
            while True:
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

    def _audio_ws(self):
        """Server-side handshake, then register this socket to receive
        whatever `_audio_upstream_loop` relays. This method's only job after
        the handshake is noticing the connection died -- it never inspects
        what a listener sends beyond the opcode needed to tell. Same
        handshake arithmetic as vcweb.py's own `_websocket()`, independently
        implemented here rather than imported (see module docstring)."""
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
            with _audio_lock:
                _audio_listeners.add(sock)
            try:
                while True:
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
                with _audio_lock:
                    _audio_listeners.discard(sock)
        finally:
            _audio_sem.release()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    _start_pollers()
    threading.Thread(target=_audio_upstream_loop, daemon=True,
                     name="audio-upstream").start()
    httpd = Server((BIND, PORT), Handler)
    print("vcweb_public: listening on %s:%d, mirroring %s"
          % (BIND, PORT, UPSTREAM))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
