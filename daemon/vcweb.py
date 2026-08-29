#!/usr/bin/env python3
"""vcweb -- the browser-facing capability of vcctrld.

Serves the KVM page, the frame stream, and input, over the tailnet.

Why a hand-rolled WebSocket rather than `websockets` or `aiohttp`: both are
asyncio, and the daemon is threaded. Rule 1 (docs/WEBKVM.md sec. 2) says the
input path must be scheduled ahead of everything else and must not sit behind
another capability's work -- an event loop shared with the frame fan-out is
precisely the shape that breaks that. The framing this needs is server-binary
out, small-text in, which is about a hundred lines. It also keeps the Pi
dependency-free, which matters on a 32-bit Raspbian image nobody wants to
disturb.

Nothing in the per-frame path decodes, encodes or base64s: a frame goes out as
the JPEG bytes the capture stick produced, in a binary WebSocket frame. The
only per-frame work is a header write and a socket send.
"""

import base64
import collections
import hashlib
import json
import os
import select
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# RFC 6455 section 1.3. Verified against the RFC's own test vector rather than
# transcribed: key "dGhlIHNhbXBsZSBub25jZQ==" must yield accept
# "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", and tests/test_core.py asserts exactly that.
#
# This was wrong for hours. The final group was written 5AB0DC85B11D instead of
# C5AB0DC85B11 -- the same twelve characters rotated by one. Every probe I
# wrote to test the handshake imported this constant, so all four of them
# computed the same wrong accept, agreed with the server, and reported the
# WebSocket healthy. Only Firefox, which has its own copy, ever disagreed.
#
# A measurement tool that shares a constant with the thing it measures cannot
# find a bug in that constant.
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# A self-contained WebSocket measurement. Opens a socket against this same
# origin, counts binary frames for six seconds, and reports the result through
# the daemon's own event bus using the `as` field -- which needs no new route
# and no CORS, and lands somewhere readable from another machine.
WSPROBE = """<!doctype html><meta charset="utf-8"><title>ws probe</title>
<body style="font:16px monospace;padding:16px"><div id="r">running…</div>
<script>
let n=0, bytes=0, opened=false, code='', clean='', err='', t0=Date.now();
let ws;
try { ws = new WebSocket((location.protocol==='https:'?'wss':'ws')
                          + '://' + location.host + '/ws'); }
catch (e) { err = 'ctor:' + e; }
if (ws) {
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { opened = true; };
  ws.onerror = () => { err += ' onerror'; };
  ws.onclose = e => { code = e.code; clean = e.wasClean; };
  ws.onmessage = e => {
    if (typeof e.data !== 'string') { n++; bytes += e.data.byteLength; }
  };
}
setTimeout(() => {
  const tag = 'WSPROBE ua=' + (navigator.userAgent.match(/Firefox|Chrome|Safari/)||['?'])[0]
    + ' proto=' + (performance.getEntriesByType('navigation')[0]||{}).nextHopProtocol
    + ' open=' + opened + ' frames=' + n + ' bytes=' + bytes
    + ' code=' + code + ' clean=' + clean + ' ' + err;
  document.getElementById('r').textContent = tag;
  fetch('/cmd', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd:'lock', action:'status', as: tag})});
}, 6000);
</script>""".encode("utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))

# A SMPTE-STYLE COLOR-BARS TEST CARD, WITH VISIBLE TEXT -- the one frame
# _mjpeg() (below) is allowed to fabricate, and only when the ring is
# genuinely EMPTY (no frame has ever been captured at all -- the device
# never opened, or produced nothing since). Every other case -- including
# `frozen` (frames arriving, all duplicates) -- already has a real frame
# to send; `vid.ring[-1]` does no duplicate rejection, unlike `/shot.jpg`.
#
# NOT a plain black frame, deliberately: this codebase already learned (see
# project memory "Black frames are not black screens") that a flat,
# unlabelled frame is indistinguishable from a genuinely dark target. A
# color-bar test card with "NO SIGNAL" burned into the pixels cannot be
# mistaken for a capture even with every other piece of context stripped
# away. Same image daemon/vcweb_public.py uses for its own, narrower
# version of this fallback -- one asset, not two that could drift.
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



# ------------------------------------------------------------------ framing

def ws_frame(payload, opcode=0x2):
    """Server -> client. Never masked, per RFC 6455."""
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
    """Client -> server. Returns (opcode, payload) or None at close.

    Client frames are always masked. Continuation frames are not handled: the
    client only ever sends small JSON control messages, and a browser will not
    fragment those. If that ever changes this must grow, rather than silently
    mis-parse -- so an unexpected continuation raises instead of guessing.
    """
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
        raise ValueError("oversized client frame: %d" % ln)
    mask = recvn(4) if masked else None
    if masked and mask is None:
        return None
    data = recvn(ln) if ln else b""
    if data is None:
        return None
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    if not fin and opcode != 0x0:
        raise ValueError("fragmented client frame, unsupported")
    return opcode, data


# ------------------------------------------------------------------ handler

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vcctrld"

    # The default logs every request to stderr, which on this box means
    # journald -- and a 30 fps stream plus per-request logging is how the
    # harness DoSed journald and took the Pi off the network for 30 minutes
    # earlier today (FINDINGS 19). Silence is deliberate.
    def log_message(self, fmt, *args):
        pass

    @property
    def cap(self):
        return self.server.web

    # -- helpers ------------------------------------------------------------

    def _send(self, code, body, ctype="application/json", extra=None, csp=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Hardening headers, matching vcweb_public.py's own (added there for
        # F3 of the public-mirror security audit; ported here afterward for
        # consistency -- this page is tailnet-only, so Tailscale is the real
        # authentication boundary and these are defense-in-depth, not closing
        # an actual gap the way they were for the public mirror). ONE
        # Content-Security-Policy header, one full policy, same reasoning as
        # vcweb_public.py's own _send: two separate CSP headers are enforced
        # as an INTERSECTION, not a union, so passing the whole string
        # through `csp` keeps every route's policy self-contained.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp or "frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj), "application/json", extra)

    # A FRESH NONCE PER RESPONSE, spent once -- same reasoning as
    # vcweb_public.py's own _serve_html_with_csp: a nonce that could be
    # replayed across requests would not actually prove "the server emitted
    # this specific script tag just now."
    def _serve_page_with_csp(self):
        body = self.cap.page()
        nonce = base64.b64encode(os.urandom(16)).decode("ascii")
        # ONE nonce'd tag: kvm.html has exactly one <script>, no attributes
        # (grep confirms it), so a plain byte-replace is exact and cannot
        # match inside a JS string/comment the way a regex over the whole
        # file could.
        body = body.replace(b"<script>",
                             ('<script nonce="%s">' % nonce).encode("ascii"),
                             1)
        # Same shape as vcweb_public.py's policy, widened only where this
        # page genuinely needs more:
        #   connect-src: 'self' already covers ws:/wss: to the SAME host and
        #                port as this response (CSP upgrades the scheme for
        #                comparison purposes -- an https page's 'self' does
        #                match a same-host wss: connection). What it does
        #                NOT cover is this page's own :443->:tls_port
        #                reachability probe (see wsURL()/checkPort() in
        #                kvm.html, and F1's own comment on _state_cors): a
        #                same-host, DIFFERENT-port fetch() and WebSocket,
        #                which CSP treats as a different origin. That's the
        #                one thing added beyond 'self', host-wildcarded
        #                rather than naming the tailnet hostname so the
        #                policy carries no rig identifier.
        #   default-src/script-src/style-src/img-src/media-src/base-uri/
        #                form-action/frame-ancestors: identical to the
        #                public mirror's policy -- kvm.html's own inline
        #                <style> block, dozen-plus style="" attributes,
        #                /themes.css link, mjpeg <img>, and fetched-then-
        #                createObjectURL'd frames all match that page's
        #                shape exactly.
        tls_port = getattr(self.cap, "tls_port", 0)
        connect_extra = (" https://*:%d wss://*:%d" % (tls_port, tls_port)
                          if tls_port else "")
        csp = ("default-src 'none'; "
               "script-src 'nonce-%s'; "
               "style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data: blob:; "
               "connect-src 'self'%s; "
               "media-src 'self'; "
               "base-uri 'none'; "
               "form-action 'none'; "
               "frame-ancestors 'none'" % (nonce, connect_extra))
        return self._send(200, body, "text/html; charset=utf-8", csp=csp)

    def _state_cors(self):
        """CORS headers for /state.json, reflecting the request's Origin ONLY
        when it is the same host as this request arrived on (any port).

        The one legitimate cross-origin caller is the page itself: served from
        :443, it probes whether :8443 is reachable from THIS device before
        opening a WebSocket there. Cross-PORT is cross-origin, so that probe
        needs CORS -- but only for an Origin whose HOST matches the Host this
        request came in on. A third-party site opened in a tailnet browser has
        a different Origin host and gets no ACAO, so it cannot fetch() this and
        read the LAN topology (F1)."""
        origin = self.headers.get("Origin")
        if not origin:
            return None
        try:
            o_host = urllib.parse.urlsplit(origin).hostname
            req_host = urllib.parse.urlsplit(
                "//" + (self.headers.get("Host") or "")).hostname
        except ValueError:
            return None
        if o_host and req_host and o_host.lower() == req_host.lower():
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        return None

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                return self._serve_page_with_csp()
            if path == "/themes.css":
                with open(os.path.join(HERE, "themes.css"), "rb") as f:
                    return self._send(200, f.read(), "text/css; charset=utf-8")
            if path == "/state.json":
                # CORS on this endpoint only, and NARROWED (F1). The page is
                # served from :443 and must be able to ask whether :8443 is
                # reachable from THIS device before trying to open a socket
                # there -- otherwise an unreachable port and a broken WebSocket
                # are the same 1006. That probe is same-host, different port,
                # so we reflect the request's Origin only when its host matches
                # the host this request arrived on, and add Vary: Origin;
                # otherwise no ACAO header at all. It was `*`, which let ANY
                # origin's JS in a tailnet browser fetch() this read-only-but-
                # LAN-revealing body cross-origin. Still read-only, tailnet-
                # only, no secrets -- but no longer readable by third-party JS.
                return self._json(self.cap.snapshot(),
                                  extra=self._state_cors())
            if path in ("/shot.jpg", "/lastgood.jpg"):
                cmd = "shot" if path == "/shot.jpg" else "lastgood"
                args = {}
                if "?" in self.path:
                    for part in self.path.split("?", 1)[1].split("&"):
                        if part.startswith("n="):
                            try:
                                args["n"] = max(1, min(120, int(part[2:])))
                            except ValueError:
                                pass
                r = self.cap.call(cmd, args)
                if not r.get("picture"):
                    # 503 rather than a placeholder image. A KVM that answers a
                    # request for the screen with *a* picture, when it cannot
                    # tell picture from no-lock, is the failure this whole
                    # design exists to avoid.
                    return self._json({"picture": False,
                                       "reason": r.get("reason"),
                                       "state": r.get("state")}, 503)
                return self._send(200, base64.b64decode(r["jpeg"]), "image/jpeg",
                                  {"X-Frame-Age": str(r.get("age_s")),
                                   "X-Frame-Mean": str(r.get("mean"))})
            if path == "/keymap.json":
                # ONE COPY OF THE KEY TABLES, and this is where the page gets
                # them. It has to decide whether a chord is the reboot BEFORE
                # it sends it -- that is what the confirmation is -- and it
                # used to do that from its own transcription of the daemon's
                # alias table, kept honest by a test comparing the two files.
                #
                # Its own endpoint rather than a field on /state.json for the
                # same reason /wslog.json is: this is a CONSTANT, and every
                # open tab polls state every 1.5 s.
                return self._json(self.cap.call("keymap", {}))
            if path == "/wslog.json":
                # Its own endpoint rather than a field on /state.json: every
                # open tab polls state every 1.5 s and none of them wants 200
                # connection records with it. This is read once, afterwards,
                # by somebody asking what was connected during a window.
                with self.cap.lock:
                    rows = list(self.cap.ws_log)
                return self._json({"ok": True, "now": round(time.time(), 3),
                                   "count": len(rows), "log": rows})
            if path == "/timeline.json":
                return self._json(self.cap.call("timeline", {}))
            if path == "/public.json":
                # Everything the public read-only mirror's poller needs, in
                # one loopback request -- same shape as /state.json, plus the
                # LED change history, which /state.json does not carry
                # because it lives behind the POST-only /cmd path today. The
                # public process must never issue anything but GET, so that
                # one extra field is folded in here rather than making it do
                # a POST anywhere. Not exposed beyond loopback by anything in
                # this file -- it is trusted exactly as much as /timeline.json
                # already is, no more.
                snap = self.cap.snapshot()
                snap["led_log"] = self.cap.call("led_changes", {"n": 6})
                return self._json(snap)
            if path == "/buffer.avi":
                # ONE REQUEST FOR THE WHOLE BUFFER. The page used to fetch
                # every frame separately -- five hundred round trips, each
                # base64'd through the JSON command path -- which was slow
                # enough to lose a race with the ring's own eviction. This
                # goes straight to the capability and returns raw bytes.
                a = {}
                if "?" in self.path:
                    for part in self.path.split("?", 1)[1].split("&"):
                        k, _, v = part.partition("=")
                        if k in ("from", "to") and v.isdigit():
                            a[k] = int(v)
                        # The caller's own start time, so a recording can be
                        # refused when its window opens before the run it
                        # claims to be of. Float, because it is an epoch.
                        elif k == "since":
                            try:
                                a["since"] = float(v)
                            except ValueError:
                                pass
                        elif k == "clip" and v in ("1", "true", "yes"):
                            a["clip"] = True
                vid = self.cap.registry.caps.get("video")
                if vid is None or not hasattr(vid, "buffer_avi"):
                    return self._json({"ok": False,
                                       "error": "no video capability"}, 503)
                blob, meta = vid.buffer_avi(a.get("from"), a.get("to"),
                                            since=a.get("since"),
                                            clip=a.get("clip", False))
                if blob is None:
                    # A REFUSAL IS NOT A SERVER FAULT. 409 rather than 503:
                    # the daemon is fine and the request is answerable, it is
                    # the window that is wrong, and the whole meta goes back so
                    # the caller can see by how much rather than guess.
                    code = 409 if meta.get("refused") else 503
                    body = {"ok": False, "error": meta.get("error")}
                    body.update({k: v for k, v in meta.items() if k != "error"})
                    return self._json(body, code)
                name = "vcctrl-buffer-%s.avi" % time.strftime("%Y%m%dT%H%M%S")
                return self._send(200, blob, "video/x-msvideo", {
                    "Content-Disposition": 'attachment; filename="%s"' % name,
                    "X-Buffer-Meta": json.dumps(meta),
                })
            if path == "/pulled":
                # A REAL DOWNLOAD, NOT BASE64 THROUGH JSON. The page can
                # already read these bytes over /cmd, and doing it that way
                # means holding a 10 MB file in a string in a phone browser
                # and rebuilding it into a Blob. A Content-Disposition lets
                # the browser do what browsers do.
                #
                # THE NAME IS NOT A PATH AND IS NOT TREATED AS ONE. It goes
                # through the capability's own 8.3 conversion -- which is the
                # path-traversal guard, stated as such on dos_filename -- and
                # the bytes are read by the capability rather than by joining
                # anything here.
                name = ""
                if "?" in self.path:
                    for part in self.path.split("?", 1)[1].split("&"):
                        k, _, v = part.partition("=")
                        if k == "name":
                            name = urllib.parse.unquote_plus(v)
                files = self.cap.registry.caps.get("files")
                if files is None:
                    return self._json({"ok": False,
                                       "error": "no files capability"}, 503)
                blob, r = b"", {}
                while True:
                    r = files._file_pulled({"action": "read", "name": name,
                                            "offset": len(blob)})
                    if not r.get("ok"):
                        # 404 for "there is no such file", 500 for a read that
                        # broke -- a browser retrying the first would be
                        # wasting its time, and the second may well work.
                        return self._json(r, 404 if r.get("why") in
                                          ("not-here", "bad-name") else 500)
                    blob += base64.b64decode(r.get("data") or "")
                    if r.get("eof"):
                        break
                    if not r.get("len"):
                        return self._json({"ok": False, "error":
                                           "the file stopped short at %d of "
                                           "%d bytes" % (len(blob),
                                                         r.get("total"))}, 500)
                return self._send(200, blob, "application/octet-stream", {
                    "Content-Disposition":
                        'attachment; filename="%s"' % r.get("name", "file"),
                })
            if path == "/frame.jpg":
                seq = 0
                if "?" in self.path:
                    for part in self.path.split("?", 1)[1].split("&"):
                        if part.startswith("seq="):
                            try:
                                seq = int(part[4:])
                            except ValueError:
                                pass
                r = self.cap.call("frame", {"seq": seq})
                if not r.get("ok"):
                    return self._json(r, 404)
                return self._send(200, base64.b64decode(r["jpeg"]), "image/jpeg",
                                  {"X-Frame-Age": str(r.get("age_s")),
                                   "X-Frame-Seq": str(r.get("seq"))})
            if path == "/events":
                since = 0
                if "?" in self.path:
                    q = self.path.split("?", 1)[1]
                    for part in q.split("&"):
                        if part.startswith("since="):
                            since = int(part[6:] or 0)
                return self._json(self.cap.call("events", {"since": since}))
            if path == "/wsprobe":
                # Served from the daemon's OWN origin so the result can be
                # posted back without CORS. A file:// test page could open the
                # socket but never report, which is why the WebSocket question
                # went unanswered for an hour: the measurement kept failing for
                # a reason unrelated to what was being measured.
                return self._send(200, WSPROBE, "text/html; charset=utf-8")
            if path == "/stream.mjpg":
                return self._mjpeg()
            if path == "/ws":
                return self._websocket()
            if path == "/wsaudio":
                return self._websocket(kind="audio")
            return self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            try:
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        if self.path.split("?", 1)[0] != "/cmd":
            return self._json({"error": "not found"}, 404)
        cmd = req.pop("cmd", None)
        if cmd not in self.cap.ALLOWED:
            return self._json({"ok": False,
                               "error": "command not exposed: %r" % cmd}, 403)
        return self._json(self.cap.call(cmd, req))

    def _mjpeg(self):
        """multipart/x-mixed-replace -- the fallback that needs no JavaScript.

        An <img src="/stream.mjpg"> animates on its own: the browser does the
        decoding, the compositing and the pacing. No WebSocket, no canvas, no
        createImageBitmap. That makes it the transport most likely to survive a
        browser this code has never run against -- which is the whole reason it
        exists, after the WebSocket path came up blank on an iPhone and the
        server-side handshake was provably fine.

        Slower to first frame than the socket and it cannot carry input, so the
        page uses it only when the socket has not delivered.

        RING EMPTY falls back to `_TEST_PATTERN`, throttled to ~1 fps rather
        than this loop's own `fps` -- operator decision, 2026-08-28, made
        after the public mirror's narrower version of this same fallback
        (see daemon/vcweb_public.py) proved out the fix for the symptom this
        addresses: a genuinely empty ring wrote nothing at all, forever, and
        a browser's "waiting on <host>" status was describing a connection
        that could never deliver anything on its own. `frozen` (frames
        arriving, all duplicates) already had a real frame to send here --
        `vid.ring[-1]` does no duplicate rejection, unlike `/shot.jpg` -- so
        this only fires when the device has never produced one at all.
        """
        vid = self.cap.video()
        if vid is None:
            return self._json({"error": "no video capability"}, 503)
        # Paced lower than the socket by default. A detailed screen is ~70 KB
        # a frame, so 30 fps is ~17 Mbit/s -- fine on the LAN, unkind to a
        # phone on cellular going through the tailnet. The socket path stays at
        # full rate; this one is the compatibility route, not the good one.
        fps = 15.0
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if part.startswith("fps="):
                    try:
                        fps = max(1.0, min(30.0, float(part[4:])))
                    except ValueError:
                        pass
        boundary = "vcctrlframe"
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=%s" % boundary)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        with self.cap.lock:
            self.cap.clients += 1
        last_t = 0.0
        last_test_pattern = 0.0
        try:
            while True:
                with vid.lock:
                    item = vid.ring[-1] if vid.ring else None
                now = time.time()
                if item is not None and item[0] > last_t:
                    last_t = item[0]
                    body = item[2]
                    self.wfile.write(
                        ("--%s\r\nContent-Type: image/jpeg\r\n"
                         "Content-Length: %d\r\n\r\n"
                         % (boundary, len(body))).encode())
                    self.wfile.write(body)
                    self.wfile.write(b"\r\n")
                elif item is None and now - last_test_pattern >= 1.0:
                    # Static and synthetic -- no reason to resend it at the
                    # real stream's own rate.
                    last_test_pattern = now
                    self.wfile.write(
                        ("--%s\r\nContent-Type: image/jpeg\r\n"
                         "Content-Length: %d\r\n\r\n"
                         % (boundary, len(_TEST_PATTERN))).encode())
                    self.wfile.write(_TEST_PATTERN)
                    self.wfile.write(b"\r\n")
                time.sleep(1.0 / fps)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.cap.lock:
                self.cap.clients -= 1

    # -- websocket ----------------------------------------------------------

    def _websocket(self, kind="video"):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._json({"error": "not a websocket request"}, 400)
        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        # Firefox rejects this value with "Sec-WebSocket-Accept check failed"
        # while an independent implementation computes the same thing and
        # agrees. Record exactly what arrived and what went back, because the
        # disagreement has to be in the input, not the arithmetic.
        try:
            raw = self.headers.get_all("Sec-WebSocket-Key") or []
        except Exception:
            raw = []
        self.cap.ws_hs = {"key": repr(key), "keys_seen": [repr(r) for r in raw],
                          "accept": accept}
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        fps = 20.0
        if "?" in self.path:
            for part in self.path.split("?", 1)[1].split("&"):
                if part.startswith("fps="):
                    try:
                        fps = max(1.0, min(30.0, float(part[4:])))
                    except ValueError:
                        pass
        if kind == "audio":
            codec = "pcm"
            if "?" in self.path:
                for part in self.path.split("?", 1)[1].split("&"):
                    if part == "codec=opus":
                        codec = "opus"
            return self.cap.serve_ws_audio(self.connection, codec=codec)
        self.cap.serve_ws(self.connection,
                          agent=self.headers.get("User-Agent"), fps=fps)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class TLSServer(Server):
    """HTTPS served by the daemon itself, so browsers speak HTTP/1.1 to it.

    WHY THIS EXISTS. `tailscale serve` terminates TLS on 443 and negotiates
    HTTP/2 with browsers. WebSocket over HTTP/2 needs RFC 8441 Extended
    CONNECT, and through this proxy it does not survive: measured, the daemon
    wrote 5 frames / 174 KB into Firefox's socket and the connection then
    errored, while an HTTP/1.1 client through the SAME proxy received 60
    frames / 2.04 MB intact. Same server, same code, same TLS -- the only
    difference is the protocol the client negotiated.

    Python's http.server speaks HTTP/1.1 and nothing else, so terminating TLS
    here removes the h2 hop entirely. `tailscale serve --tcp` forwards the port
    as raw TCP, so this certificate is presented directly to the browser and
    the connection stays tailnet-only.

    The certificate is the one the weekly timer already renews. The context is
    rebuilt when the file changes, so a renewal does not need a restart -- an
    hour of downtime three months from now is exactly the kind of thing nobody
    would connect back to this line.
    """

    # Paths come from the daemon, which holds the resolved config. vcweb does
    # not load configuration itself: two loaders means two answers, and the
    # question "which cert is this process actually serving" must have one.
    CERT = "/var/lib/vcctrl/tls.crt"
    KEY = "/var/lib/vcctrl/tls.key"

    def __init__(self, *a, **kw):
        self._ctx = None
        self._cert_mtime = 0
        Server.__init__(self, *a, **kw)

    def _context(self):
        try:
            mtime = os.path.getmtime(self.CERT)
        except OSError:
            return None
        if self._ctx is None or mtime != self._cert_mtime:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.CERT, self.KEY)
            # Do NOT advertise h2. Advertising a protocol this server cannot
            # speak is how the 443 path breaks WebSocket in the first place.
            ctx.set_alpn_protocols(["http/1.1"])
            self._ctx, self._cert_mtime = ctx, mtime
        return self._ctx

    def get_request(self):
        sock, addr = self.socket.accept()
        ctx = self._context()
        if ctx is None:
            sock.close()
            raise OSError("no certificate available")
        try:
            return ctx.wrap_socket(sock, server_side=True), addr
        except Exception as exc:
            # A TLS handshake failure is one client's problem, not the
            # server's; without this the accept loop dies on the first probe.
            # There is no reader thread here and nothing to join -- this
            # socket has never been handed to anyone. It is closed, and the
            # failure is re-raised for the accept loop to skip.
            try:
                sock.close()
            except Exception:
                pass
            # AND IT MUST LEAVE AS AN OSError. socketserver's
            # `_handle_request_noblock` wraps `get_request()` in `except
            # OSError` and nothing wider, so anything else escapes
            # `serve_forever` and kills the accept thread outright -- leaving
            # `tls_up` True in /state.json with nothing listening on the port.
            # `ssl.SSLError` is an OSError, so the ordinary handshake failure
            # was always fine; this is about the paths that are not it. The
            # shape is worth refusing outright rather than trusting whatever
            # `wrap_socket` happens to raise next, because that is exactly how
            # this went wrong once already: a misplaced hunk left a NameError
            # on this line and one bad probe took the HTTPS listener down.
            if isinstance(exc, OSError):
                raise
            raise OSError("TLS handshake failed: %s: %s"
                          % (type(exc).__name__, exc)) from exc


# --------------------------------------------------------------- capability

def _verification_is_void(verified_at):
    """Has the target been power-cycled since this verification?

    Imported lazily from the daemon module rather than held as a reference,
    because vcweb does not import vcctrld at module scope and adding that
    coupling for one field is not worth it. Returns False on any doubt: a
    verification wrongly voided is a nuisance, but wrongly KEPT is the bug
    this exists to fix -- so doubt resolves toward keeping only when we
    genuinely cannot tell, and the caller still sees `age_s`.
    """
    try:
        import sys as _s
        mod = _s.modules.get("vcctrld") or _s.modules.get("__main__")
        target = getattr(mod, "TARGET", None)
        if target is None:
            return False
        _epoch, _powered, changed_at = target.state()
    except Exception:
        return False
    return bool(changed_at and verified_at and verified_at < changed_at)


class WebCapability(object):
    """Registered by vcctrld. Talks to the other capabilities via the registry.

    Deliberately holds no device of its own: it is a view onto input, video,
    power and leds. That is what lets rule 2 apply cleanly -- if this raises,
    the browser goes away and nothing else notices.
    """

    name = "web"

    # Commands a browser may invoke. An allowlist rather than a passthrough:
    # the socket is on the tailnet with no auth, and "whatever the daemon
    # accepts" is a larger surface than a web page needs.
    ALLOWED = frozenset([
        "key", "type", "hold", "combo", "keydown", "keyup", "release_all",
        "mouse_move", "mouse_click", "power", "leds", "status", "caps",
        "events", "activity", "lock", "shot", "lastgood", "video",
        "framestats", "verify_input", "buffer",
        # The page holds the ring while there is no picture, so the seconds
        # that explain an outage are not overwritten by the signal returning.
        "pin", "timeline",
        # Read-only. `level` is needed by both the page meter and by
        # bin/vcctrl-audio, which reaches the daemon over HTTPS now that plain
        # http is off -- it was missing here and the tool got a 403.
        # `spectrum` is level's frequency-domain sibling, used by the same
        # tool the same way, and missed here for the exact same reason on
        # its first pass -- caught by actually running vcctrl-audio against
        # the live rig rather than trusting the daemon-side unit tests alone.
        "level", "spectrum", "powerlog",
        # scrub
        "pin", "timeline", "frame",
        # The change record. vcctrl-94 shipped this with a CLI verb and no
        # web allowlist entry -- the same parity gap it spent the evening
        # closing, in the other consumer. The KVM is where an intermittent
        # would actually be noticed.
        "led_changes",
        # READ-ONLY, ALL THREE. `files` is the availability answer that decides
        # whether the transfer entry in the File menu is offered at all;
        # `file_name` dry-runs the 8.3 rename so a person sees what their file
        # will be called before any byte moves; `file_check` is the expensive
        # server probe, run after the confirmation and before the reboot.
        # Nothing here moves data or touches the target -- the commands that
        # do are deliberately NOT exposed yet.
        "files", "file_name", "file_check",
        # Staging WRITES, and is exposed anyway because the browser upload is
        # the whole point of the feature. What bounds it: the caller sends a
        # NAME and never a path, the name is forced to DOS 8.3, the size
        # policy applies to the file and to the queue total, and the
        # destination directory is decided here. It writes to the daemon
        # host's staging directory and touches the target not at all.
        "file_stage", "file_queue",
        # The transfer itself. It REBOOTS the target, which is the same class
        # of action as the power buttons already in this strip and behind the
        # same kind of confirmation. `file_send` returns at once and
        # `file_status` is how the page follows it, so no request is held open
        # across two reboots.
        "file_send", "file_status", "file_cancel",
        # THE OTHER DIRECTION. `file_pull` reboots exactly as `file_send`
        # does and is behind the same confirmation; `file_listing` is
        # read-only and touches nothing at all -- it is the last reading of
        # the target's outgoing directory, which is what the picker is built
        # from. `file_pulled` reads bytes that are already on this host, and
        # the browser downloads them through /pulled rather than through
        # base64 in JSON.
        "file_pull", "file_listing", "file_pulled",
        # The page names the boot profile in its title, so it needs to read
        # the reading. Read-only from here: `set`, `clear` and `blaster` are
        # reachable over the socket and from the CLI, but a browser must not
        # be able to assert what the target is running.
        "profile",
        # READ-ONLY. What `vcctrl config show` asks the daemon for -- the
        # resolved configuration, not the file on disk. Secrets are never
        # literals in this format (a `*_env` key names an environment
        # variable; `CFG.as_dict()` never reads it), so nothing here needs
        # redacting before a browser sees it.
        "config",
        # A WRITE, exposed anyway, for the same reason file_stage/file_queue
        # are: it is the point of the feature. Unlike those, it touches
        # neither the target nor the daemon's staging area -- it is one
        # short string held in memory, the same class of harmless daemon-side
        # bookkeeping as `pin` or `profile`'s own state. `note` is the read.
        "note", "note_set",
    ])

    def __init__(self, registry, bind, port, tls_port=0):
        self.registry = registry
        self.bind = bind
        self.port = port
        self.tls_port = tls_port
        self.tls_up = False
        self.httpd = None
        self.tlsd = None
        self.clients = 0
        self.ws_opened = 0
        self.ws_closed = 0
        self.ws_last_error = None
        self.ws_last_agent = None
        self.ws_dropped = 0
        # WHO WAS CONNECTED, AND WHEN. Durable, because the question is always
        # asked afterwards.
        #
        # vcctrl-94 tried to bound a browser tab's window from the daemon's
        # event log and could not: it holds 200 events and a running cell's
        # LED polling floods it, so the entire log spanned SEVENTEEN SECONDS.
        # "Was anything else connected while I was measuring" is unanswerable
        # from it, and that is exactly the question you have after a
        # measurement rather than before it.
        #
        # Connections are rare where LED polls are not -- a few an hour
        # against several a second -- so the same 200 entries cover days
        # instead of seconds. Different lifetime, different log; putting them
        # in one was what made the short one useless.
        self.ws_log = collections.deque(maxlen=200)
        # Per-connection accounting. The WebSocket question could not be
        # settled from either end alone: the browser reports what it received,
        # the daemon reported only that it wrote without error. If the daemon
        # says it wrote 200 frames and the page says it saw none, the frames
        # were lost between them -- which is the whole question, and needs no
        # browser scripting to answer.
        self.ws_sent_frames = 0
        self.ws_sent_bytes = 0
        self.ws_last = None
        self.ws_client_ops = []
        self.ws_hs = None
        self.listeners = 0
        self.lock = threading.Lock()

    def start(self):
        self.httpd = Server((self.bind, self.port), Handler)
        self.httpd.web = self
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        if self.tls_port:
            try:
                self.tlsd = TLSServer((self.bind, self.tls_port), Handler)
                self.tlsd.web = self
                if self.tlsd._context() is None:
                    raise OSError("certificate not present at %s"
                                  % TLSServer.CERT)
                threading.Thread(target=self.tlsd.serve_forever,
                                 daemon=True).start()
                self.tls_up = True
            except Exception as exc:
                # Non-fatal: the 443 path still serves the page, only the
                # WebSocket transport is unavailable without this.
                sys.stderr.write("direct TLS listener unavailable: %s\n" % exc)
                self.tlsd = None

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()

    # -- bridge into the rest of the daemon ---------------------------------

    def call(self, cmd, req):
        # execute(), not dispatch(): status/caps/events/activity/lock are
        # handled outside the capability routes, and dispatch() alone reports
        # them as unknown commands.
        req = dict(req)
        req["cmd"] = cmd
        req.setdefault("as", "browser")
        return self.registry.execute(req)

    def video(self):
        return self.registry.caps.get("video")

    def audio(self):
        return self.registry.caps.get("audio")

    def snapshot(self):
        """Everything the page needs to answer "is it stuck", in one request."""
        vid = self.video()
        act = self.call("activity", {})
        # The PS/2 LEDs are the page's signature indicator and its only
        # non-video evidence that a keystroke reached the target, so they ride
        # in the same poll as everything else rather than needing a second one.
        leds_cap = self.registry.caps.get("leds")
        if leds_cap is None:
            leds = {"available": False, "why": "error",
                    "reason": "leds capability failed to start"}
        else:
            try:
                leds = leds_cap.snapshot()
            except Exception as exc:
                # Bounded inline rather than via vcctrld.errstr: this module
                # does not import that one, and reaching for a name that is
                # not there would raise a NameError on the ERROR PATH -- the
                # one place nothing else is going right either. Same shape as
                # the missing `import sys` that took out the TLS fallback in
                # this file earlier today.
                r = "leds snapshot raised: %s: %s" % (type(exc).__name__, exc)
                leds = {"available": False, "why": "error",
                        "reason": r if len(r) <= 240 else r[:239] + "\u2026"}

        # input_verified carries the SAME three-state shape, for the same
        # reason. On ADB the round trip is not a failed check, it is a check
        # that does not apply -- and a Macintosh must not render like a
        # Gateway with a dead PS/2 lead.
        if leds_cap is None:
            verified = {"available": False, "why": "error",
                        "reason": "leds capability failed to start"}
        elif leds.get("why") == "unsupported":
            verified = {"available": False, "why": "unsupported",
                        "reason": leds.get("reason")}
        elif leds_cap.verified_at is None:
            verified = {"available": False, "why": "unknown",
                        "reason": "the input path has not been verified yet"}
        elif _verification_is_void(leds_cap.verified_at):
            # A proof of the input path is a statement about a moment. The
            # target has been through a power transition since this one, so
            # it proves something about a machine that no longer exists in
            # that state -- and it must not keep rendering as a live green
            # tick. FINDINGS sec. 33.
            verified = {"available": False, "why": "unproven",
                        "reason": ("the target's power changed after this was "
                                   "proven, so it is about the previous "
                                   "epoch -- run verify-input again")}
        else:
            verified = {"available": True, "why": None, "reason": None,
                        "ok": leds_cap.verified_ok,
                        "age_s": round(time.time() - leds_cap.verified_at, 1)}

        return {"video": vid._state() if vid else {"state": "unavailable"},
                "leds": leds,
                # Every other input status describes the Pi's own end of the
                # wire. This is the only field that means the target answered.
                "input_verified": verified,
                "inflight": act.get("inflight"),
                "lock": act.get("lock"),
                "last_event_age_s": act.get("last_event_age_s"),
                "seq": act.get("seq"),
                "build": self.build_id(),
                # The page uses this to open its WebSocket against the port
                # that speaks HTTP/1.1, rather than the h2 proxy on 443.
                "tls_port": self.tls_port if self.tls_up else 0,
                # THE CONFIGURED TARGETS, so the page has no table of its own.
                #
                # The page used to name both machines and their native
                # resolutions in a tooltip. Two tables that must agree is one
                # table too many: the daemon knows which boards map to which
                # machines and at what geometry, and the page asking is
                # strictly better than the page remembering. Empty list is a
                # real answer -- a rig that configures no targets gets a
                # tooltip that says so rather than one naming somebody else's
                # hardware.
                "targets": self.registry.configured_targets(),
                # Plug identity so a consumer can say WHICH plug it is about
                # -- "power: on" is not actionable when the rig has one plug
                # that serves whichever machine is currently connected to it.
                # Cached, never a live query: see PowerCapability.snapshot().
                "power": (self.registry.caps["power"].snapshot()
                          if "power" in self.registry.caps else
                          {"host": None, "alias": None, "model": None,
                           "on": None, "age_s": None, "stale": None,
                           "reason": "power capability failed to start"}),
                # Which protocol board is installed, and therefore which
                # computer the input path is actually wired to. Unknown is a
                # first-class answer -- never a default to IBMPC.
                "board": (self.registry.caps["board"].snapshot()
                          if "board" in self.registry.caps else
                          {"id": None, "name": None, "target": None,
                           "source": None, "stale": None,
                           "reason": "board capability failed to start"}),
                # The DOS target's own hardware, as dinspect last measured
                # it -- a READING with an age, never a live poll (a scan
                # reboots the machine twice). `null` fields and `source:
                # null` mean no scan has ever been pulled, not "unknown
                # hardware" -- see SysinfoCapability.snapshot().
                "sysinfo": (self.registry.caps["sysinfo"].snapshot()
                            if "sysinfo" in self.registry.caps else
                            {"fields": None, "other": None, "source": None,
                             "age_s": None, "stale": None,
                             "reason": "sysinfo capability failed to start"}),
                # Whether this machine can be sent a file at all, and if
                # not, WHICH not -- the page greys the transfer entry with the
                # reason rather than hiding it or letting it fail on click.
                "files": (self.registry.caps["files"].snapshot()
                          if "files" in self.registry.caps else
                          {"available": False, "why": "not_configured",
                           "reason": "the files capability is not running",
                           "backend": None, "server": None, "dest": None,
                           "warn_bytes": None, "refuse_bytes": None}),
                # THE BOOT PROFILE, AS A READING AND NOT A STATUS. Null
                # whenever it has not been established or a reboot has
                # invalidated it -- absent rather than old, because a stale
                # profile is the same string in the same place with nothing on
                # screen to say the machine underneath it changed. The page
                # shows the machine's name alone when this is null; it must
                # not substitute the likely one.
                "profile": (self.registry.caps["board"]._profile(
                                {"action": "state"})["profile"]
                            if "board" in self.registry.caps else
                            {"name": None, "at": None, "how": None,
                             "reason": "board capability failed to start"}),
                # Host facts. The login banner has had these since the Pi 5
                # build and the page has not, so "is it thermally throttling
                # while I watch the stream stutter" was answerable at a shell
                # and not in the KVM. Cheap /proc and /sys reads, cached
                # briefly so a 1.5 s poll from several tabs does not re-read
                # them per tab.
                "host": self.host_facts(),
                # WHAT IS HAPPENING RIGHT NOW, in one sentence -- narration
                # a driving session (typically Claude Code, not a person)
                # sets by hand. Not derived from `inflight`/the activity log
                # above, which only ever have command names: this is the one
                # field that tells a viewer WHY, and it is the whole reason
                # the public read-only mirror's control strip has anything
                # in the slot the file/type/send controls used to occupy.
                "note": (self.registry.caps["note"].snapshot()
                         if "note" in self.registry.caps else
                         {"text": None, "at": None, "by": None,
                          "reason": "note capability failed to start"}),
                "viewers": self.clients,
                "listeners": self.listeners,
                "audio": (self.audio()._state() if self.audio()
                          else {"state": "unavailable"}),
                "ws": {"opened": self.ws_opened, "closed": self.ws_closed,
                       "dropped": self.ws_dropped,
                       "sent_frames": self.ws_sent_frames,
                       "sent_bytes": self.ws_sent_bytes,
                       "last": self.ws_last,
                       "client_ops": self.ws_client_ops,
                       "handshake": self.ws_hs,
                       "last_error": self.ws_last_error,
                       "last_agent": self.ws_last_agent},
                "caps": self.registry.report()}

    _host_cache = (0.0, None)

    def host_facts(self):
        """Model, kernel, thermals, governor, memory, disk, uptime, load.

        Thermals and the throttled word are the two that earn their place on a
        KVM rather than in a shell: a Pi that is throttling produces a stream
        that stutters, and the page is where someone is looking when it does.
        `throttled` is the firmware's own bitfield, not a derived guess.
        """
        now = time.time()
        ts, cached = WebCapability._host_cache
        if cached and now - ts < 5.0:
            return cached

        def _read(path, default=None):
            try:
                with open(path) as f:
                    return f.read().strip()
            except OSError:
                return default

        facts = {"model": None, "kernel": None, "arch": None,
                 "governor": None, "temp_c": None, "throttled": None,
                 "mem_used_mb": None, "mem_total_mb": None,
                 "disk_used_gb": None, "disk_total_gb": None,
                 "uptime_s": None, "load": None, "cores": None}
        try:
            facts["model"] = (_read("/proc/device-tree/model") or "").replace("\x00", "") or None
            uname = os.uname()
            facts["kernel"], facts["arch"] = uname.release, uname.machine
            # Load average means nothing without it. The page was about to
            # colour a load of 2.0 against a hardcoded four cores, which would
            # have been right on this Pi and wrong on the next machine.
            facts["cores"] = os.cpu_count()
            facts["governor"] = _read(
                "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
            t = _read("/sys/class/thermal/thermal_zone0/temp")
            if t and t.isdigit():
                facts["temp_c"] = round(int(t) / 1000.0, 1)
            up = _read("/proc/uptime")
            if up:
                facts["uptime_s"] = int(float(up.split()[0]))
            la = _read("/proc/loadavg")
            if la:
                facts["load"] = [float(x) for x in la.split()[:3]]
            mem = {}
            for line in (_read("/proc/meminfo") or "").splitlines():
                k, _, v = line.partition(":")
                mem[k] = v.strip().split()[0] if v.strip() else "0"
            if "MemTotal" in mem and "MemAvailable" in mem:
                tot = int(mem["MemTotal"]) // 1024
                avail = int(mem["MemAvailable"]) // 1024
                facts["mem_total_mb"], facts["mem_used_mb"] = tot, tot - avail
            # The firmware's own bitfield, not a derived guess. 0x0 is clean;
            # bit 0 is under-voltage now, bit 2 currently throttled, bit 16 an
            # under-voltage that HAS occurred since boot. That last one is the
            # useful one on a rig where the stream stutters intermittently and
            # nobody was watching at the time.
            #
            # A subprocess, so it rides the same 5 s cache as everything else
            # here -- roughly one vcgencmd per five seconds regardless of how
            # many tabs are open, which is the cost that mattered.
            try:
                out = subprocess.run(["vcgencmd", "get_throttled"],
                                     capture_output=True, text=True,
                                     timeout=2).stdout.strip()
                if "=" in out:
                    facts["throttled"] = out.split("=", 1)[1]
            except Exception:
                pass       # non-Pi host, or vcgencmd absent: stays null
            st = os.statvfs("/")
            facts["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
            facts["disk_used_gb"] = round(
                (st.f_blocks - st.f_bfree) * st.f_frsize / 1e9, 1)
        except Exception:
            pass       # partial facts are fine; every key is present regardless

        WebCapability._host_cache = (now, facts)
        return facts

    def build_id(self):
        """Short hash of the page as it is on disk right now.

        Exists because "reload" has been the answer to three reports in a row.
        A tab that has not been reloaded runs the old JavaScript indefinitely,
        and from the operator's side that is indistinguishable from a change
        that did not deploy -- so the page should notice for itself rather than
        being told.
        """
        try:
            with open(os.path.join(HERE, "kvm.html"), "rb") as f:
                return hashlib.md5(f.read()).hexdigest()[:8]
        except Exception:
            return "unknown"

    def page(self):
        with open(os.path.join(HERE, "kvm.html"), "rb") as f:
            body = f.read()
        # The served copy carries the hash of the copy on disk, so a running
        # page can compare itself against what the daemon is serving now.
        return body.replace(b"__BUILD__", self.build_id().encode())

    # -- the stream ---------------------------------------------------------

    def serve_ws(self, sock, agent=None, fps=20.0):
        """One connection, ONE THREAD, and that thread owns the socket.

        WHY THIS IS NOT TWO THREADS ANY MORE. It used to be a frame pump plus
        an input reader, with a `wlock` serialising the two writers. On the TLS
        port the socket is an SSLSocket: one OpenSSL `SSL*` shared by both.
        `wlock` covered writer-against-writer and nothing else, so SSL_read and
        SSL_write ran concurrently on that one `SSL*` as a matter of routine --
        which OpenSSL does not support without the application serialising it.
        vcctrld aborted on 2026-08-24 with "free(): invalid next size" raised
        from SSL_free, and the teardown race that produced it was only the
        visible half; read-against-write during normal operation was the rest,
        and no lock in the old design touched it.

        `select` gives both directions to one thread, so the question does not
        arise. The loop waits on the socket being readable with a timeout set
        by when the next frame is due: client input wakes it immediately, and
        the frame cadence is a deadline rather than a sleep. Nothing else ever
        holds this socket, so the close at the end cannot land under a reader.

        WHAT THIS DELIBERATELY KEEPS: the drop-rather-than-queue rule, the
        ten-second unwritable stall test, and the applied-rate echo. Those are
        described where they happen in `_ws_pump`.
        """
        with self.lock:
            self.clients += 1
            self.ws_opened += 1
            self.ws_client_ops = []
            self.ws_last_agent = agent
            self.ws_sent_frames = 0     # per connection, so the number answers
            self.ws_sent_bytes = 0      # "did THIS client get anything"
            self.ws_last = "open"
        opened_t = time.time()
        self._log_ws("open", kind="video", agent=agent)
        held = set()
        # A list because the client retunes it live from inside the same loop:
        # the client knows how it is doing far better than this side can infer.
        rate = [fps]
        try:
            self._ws_pump(sock, held, rate)
        except Exception as exc:
            with self.lock:
                self.ws_last_error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            with self.lock:
                self.clients -= 1
                self.ws_closed += 1
                self.ws_last = "closed after %d frames / %d bytes written" % (
                    self.ws_sent_frames, self.ws_sent_bytes)
                nf, nb = self.ws_sent_frames, self.ws_sent_bytes
            self._log_ws("close", kind="video", frames=nf, nbytes=nb,
                         held_s=time.time() - opened_t)
            # Release anything this viewer was holding. A dropped wifi
            # connection mid-keypress must not leave a key down at the g2k,
            # where at a DOS prompt it types until the buffer fills.
            if held:
                try:
                    self.call("release_all", {})
                except Exception:
                    pass
            # Safe unconditionally, and that is the point of the rewrite above:
            # this thread is the only one that has ever touched this socket, so
            # there is nobody inside it to free it underneath. The previous
            # design had to stop a reader, join it, and skip the close if the
            # join timed out -- leaking a descriptor rather than corrupting the
            # heap. None of that is needed when there is no second thread.
            try:
                sock.close()
            except Exception:
                pass

    def _log_ws(self, event, kind, agent=None, frames=None, nbytes=None,
                held_s=None):
        """Record one connection event. Cheap, bounded, and NOT the event bus.

        Kept separate from `events` deliberately: that log is sized for
        activity, which on this daemon means LED polls at several a second,
        and a browser connection is a few an hour. Sharing one ring meant the
        rare thing was always already gone by the time anyone looked.
        """
        row = {"t": round(time.time(), 3), "event": event, "kind": kind}
        if agent:
            row["agent"] = agent[:120]
        if frames is not None:
            row["frames"] = frames
        if nbytes is not None:
            row["bytes"] = nbytes
        if held_s is not None:
            row["held_s"] = round(held_s, 1)
        with self.lock:
            self.ws_log.append(row)

    def _ws_say(self, sock, obj):
        """Send one JSON text frame, or give up quietly.

        Used to tell a client what its request actually became. Never raises:
        a control message that cannot be delivered must not take down a
        picture that is being delivered fine.

        No lock. Called only from the thread that owns the socket -- see
        `serve_ws` for why there is only one of those now.
        """
        try:
            sock.sendall(ws_frame(json.dumps(obj).encode(), opcode=0x1))
        except Exception:
            pass

    def _ws_pump(self, sock, held, rate):
        """Both directions, one thread, `select` deciding which runs next.

        Returns when the connection is finished, for any reason. It never
        closes the socket: `serve_ws`'s `finally` does that, once, and this
        function existing on the same thread is what makes that close safe.

        SENDING DROPS RATHER THAN QUEUES. A detailed screen is ~70 KB, so 30
        fps is ~17 Mbit/s, and sendall() on a client that cannot drink that
        fast BLOCKS -- frames pile up in the kernel buffer and the stream turns
        into a backlog being replayed. For a KVM that is strictly worse than
        skipping: a late frame has no value, because the only frame anyone
        wants is the current one. So a zero-timeout `select` asks whether the
        socket can take a write right now, and if it cannot the frame is
        dropped and the next one is considered fresh. Nothing is buffered on
        this side either.

        READING IS DRAINED TO EMPTY BEFORE ANYTHING IS WRITTEN, and `pending()`
        is checked before `select`: on a TLS socket OpenSSL may already hold a
        decrypted record in its own buffer, in which case the fd is not
        readable and the data is there. Getting that wrong does not hang -- it
        stalls input until the next wake, which reads as a keyboard that
        sometimes ignores you, and is the kind of fault nobody reports
        precisely.

        THE CADENCE IS A DEADLINE, NOT A SLEEP. The old pump slept 1/rate at
        the bottom of its loop, which was fine when a separate thread was
        waiting on input. With one thread a sleep would make keystrokes wait
        for the next frame, so the wait is a `select` on the socket with the
        time until the next frame as its timeout: input wakes it immediately,
        and an idle client still gets frames on time.
        """
        vid = self.video()
        # A CLIENT THAT NEVER DRAINS IS GONE, AND THIS LOOP CANNOT SEE IT.
        #
        # The send path only writes when select() says writable, so a socket
        # whose peer has vanished -- tab closed, phone asleep, network gone --
        # fills its send buffer, never becomes writable again, and is never
        # written to. No write means no error, so nothing ever raises and the
        # loop drops a frame and waits, forever.
        #
        # Measured on the rig: two such sockets dropping 35 frames a second
        # between them, 100% of attempts, dead flat for ninety seconds, while
        # a socket opened alongside them took 20 fps cleanly. Flatness was the
        # tell -- a congested link fluctuates and the page's own rate control
        # would have halved within twelve seconds. These were not congested,
        # they were abandoned.
        #
        # Ten seconds of CONTINUOUS unwritability is the test. A live peer
        # that is merely slow drains something in that window; one that drains
        # nothing at all is not reading.
        STALL_S = 10.0
        stalled_since = None
        last_t, last_state = 0.0, None
        # Zero rather than "now": the first frame goes as soon as there is one.
        next_due = 0.0
        # THE RATE IS THIS SIDE'S NUMBER, SO THIS SIDE SAYS WHAT IT IS.
        #
        # A page reported "asking for 5 fps" beside "arriving here 9.5 fps",
        # which cannot both be true of one socket -- this loop paces to the ask
        # and measured from outside it honours it to within 2% at 5, 15 and 30.
        # So the two numbers disagreed because one of them was a BELIEF: the
        # page was displaying what it had asked for, and nothing ever told it
        # what it got. Same correction as the ring length, which the page also
        # used to remember rather than read.
        self._ws_say(sock, {"rate": rate[0]})
        while True:
            # -- CLIENT -> HERE. Drain it; several messages can arrive in one
            #    wake, and one TLS record can carry more than one ws frame.
            while True:
                try:
                    if getattr(sock, "pending", lambda: 0)():
                        ready = True
                    else:
                        ready = bool(select.select([sock], [], [], 0)[0])
                except Exception:
                    return
                if not ready:
                    break
                if not self._ws_handle_input(sock, held, rate):
                    return

            # -- HERE -> CLIENT.
            now = time.time()
            if vid is not None and now >= next_due:
                next_due = now + 1.0 / max(1.0, rate[0])
                with vid.lock:
                    state = vid.state
                    item = vid.ring[-1] if vid.ring else None
                if item is not None and item[0] > last_t:
                    try:
                        writable = bool(select.select([], [sock], [], 0)[1])
                    except Exception:
                        return
                    if writable:
                        stalled_since = None
                        last_t = item[0]
                        try:
                            sock.sendall(ws_frame(item[2], opcode=0x2))
                        except Exception:
                            return
                        with self.lock:
                            self.ws_sent_frames += 1
                            self.ws_sent_bytes += len(item[2])
                    elif stalled_since is None:
                        stalled_since = now
                        with self.lock:
                            self.ws_dropped += 1
                    elif now - stalled_since > STALL_S:
                        with self.lock:
                            self.ws_dropped += 1
                            self.ws_last = ("closed: unwritable for %.0fs after "
                                            "%d frames" % (STALL_S,
                                                           self.ws_sent_frames))
                        return
                    else:
                        with self.lock:
                            self.ws_dropped += 1
                if state != last_state:
                    last_state = state
                    try:
                        sock.sendall(ws_frame(
                            json.dumps({"state": state}).encode(), opcode=0x1))
                    except Exception:
                        return

            # -- WAIT. Wakes on client input, or when the next frame is due.
            #    Capped so that a stalled client is still noticed promptly and
            #    a very low rate does not park this thread for a whole second.
            wait = 0.5 if vid is None else max(0.0, next_due - time.time())
            try:
                select.select([sock], [], [], min(wait, 0.5))
            except Exception:
                return

    def _ws_handle_input(self, sock, held, rate):
        """Read and act on ONE client message. False means stop.

        Split out of the loop so the read side stays readable, not because it
        runs anywhere else: it is called from `_ws_pump` and nowhere else, on
        the one thread that owns this socket.
        """
        try:
            got = ws_read(sock)
        except Exception:
            return False
        if got is None:
            return False
        opcode, data = got
        with self.lock:
            if len(self.ws_client_ops) < 12:
                self.ws_client_ops.append(hex(opcode))
        if opcode == 0x8:
            with self.lock:
                self.ws_client_ops.append("close")
            return False
        if opcode == 0x9:
            try:
                sock.sendall(ws_frame(data, opcode=0xA))
            except Exception:
                return False
            return True
        if opcode != 0x1:
            return True
        try:
            msg = json.loads(data)
        except Exception:
            return True
        kind = msg.get("t")
        try:
            if kind == "down":
                held.add(msg["k"])
                self.call("keydown", {"key": msg["k"]})
            elif kind == "up":
                held.discard(msg["k"])
                self.call("keyup", {"key": msg["k"]})
            elif kind == "text":
                self.call("type", {"text": msg["s"]})
            elif kind == "combo":
                self.call("combo", {"keys": msg["k"]})
            elif kind == "rate" and rate is not None:
                rate[0] = max(1.0, min(30.0, float(msg.get("fps", 20))))
                # Echo the APPLIED value, not the requested one: the clamp
                # above is exactly where a request and a reality diverge, and
                # a client that asks for 40 should be told it is getting 30
                # rather than left to infer it.
                self._ws_say(sock, {"rate": rate[0]})
            elif kind == "release":
                held.clear()
                self.call("release_all", {})
        except Exception:
            return True
        return True

    def serve_ws_audio(self, sock, codec="pcm"):
        """Audio out, on its own socket -- raw PCM by default, Ogg Opus pages
        with `?codec=opus`.

        A separate socket rather than a channel on the video one: adding a type
        prefix to every video frame would touch the working path to add an
        optional feature, and this way "only stream when someone is listening"
        falls out of the connection lifecycle instead of needing a flag.

        The stream sends nothing until a client connects, which is the point --
        silence still costs 1.5 Mbit/s (raw PCM; the Opus encoder likewise
        does not even run until its first listener attaches).
        """
        aud = self.audio()
        if aud is None:
            try:
                sock.close()
            except Exception:
                pass
            return
        if codec == "opus":
            return self._serve_ws_audio_opus(sock, aud)
        with self.lock:
            self.listeners += 1
        opened_t = time.time()
        self._log_ws("open", kind="audio")
        last_seq = 0
        try:
            # Start from the live edge, not the ring's tail: a listener joining
            # should hear now, not a burst of the last twenty seconds.
            with aud.lock:
                last_seq = aud.seq
            while True:
                with aud.lock:
                    pending = [(sq, c) for _t, sq, c in aud.ring if sq > last_seq]
                if pending:
                    # Audio drops differently from video. A dropped frame is
                    # invisible; a dropped chunk is an audible click. But an
                    # unbounded queue is worse -- it turns into ever-growing
                    # delay, and late audio is worth nothing for monitoring.
                    # So: skip ahead if a client has fallen badly behind,
                    # rather than trying to deliver everything.
                    if len(pending) > 40:          # ~0.8 s behind
                        pending = pending[-10:]
                    try:
                        if not select.select([], [sock], [], 0.2)[1]:
                            continue
                        for sq, chunk in pending:
                            sock.sendall(ws_frame(chunk, opcode=0x2))
                            last_seq = sq
                    except Exception:
                        return
                else:
                    time.sleep(0.005)
        finally:
            with self.lock:
                self.listeners -= 1
            self._log_ws("close", kind="audio",
                         held_s=time.time() - opened_t)
            try:
                sock.close()
            except Exception:
                pass

    def _serve_ws_audio_opus(self, sock, aud):
        """Ogg Opus pages out, one whole page per WS frame.

        Same skeleton as the PCM loop above with two additions the container
        forces (see AudioCapability's own "-- the Opus side-stream --"
        comment): the stream headers are replayed to every joining listener
        before any audio page, and a generation change (the encoder was
        respawned) DROPS the connection rather than continuing -- a new
        encoder's pages cannot follow another stream's headers, and a client
        that reconnects gets the new headers by the same replay.
        """
        gen = aud.opus_attach()
        with self.lock:
            self.listeners += 1
        opened_t = time.time()
        self._log_ws("open", kind="audio-opus")
        try:
            # Wait for the header pages. Unbounded on purpose, matching the
            # PCM path's behavior when the capture is down: an open socket
            # that sends nothing until there is something to send. If the
            # encoder respawns while we wait, drop -- the client's reconnect
            # lands on the new generation.
            headers = None
            while headers is None:
                with aud.opus_lock:
                    if aud.opus_generation != gen:
                        return
                    if aud.opus_headers_done:
                        headers = list(aud.opus_headers)
                        last_seq = aud.opus_seq      # join at the live edge
                time.sleep(0.02)
            try:
                for page in headers:
                    sock.sendall(ws_frame(page, opcode=0x2))
            except Exception:
                return
            while True:
                with aud.opus_lock:
                    if aud.opus_generation != gen:
                        return
                    pending = [(sq, p) for _t, sq, p in aud.opus_ring
                               if sq > last_seq]
                if pending:
                    # Same skip-ahead rule and numbers as the PCM loop: at
                    # 20 ms pages, 40 behind is the same ~0.8 s. Skipping
                    # whole pages is safe where skipping bytes would not be.
                    if len(pending) > 40:
                        pending = pending[-10:]
                    try:
                        if not select.select([], [sock], [], 0.2)[1]:
                            continue
                        for sq, page in pending:
                            sock.sendall(ws_frame(page, opcode=0x2))
                            last_seq = sq
                    except Exception:
                        return
                else:
                    time.sleep(0.005)
        finally:
            aud.opus_detach()
            with self.lock:
                self.listeners -= 1
            self._log_ws("close", kind="audio-opus",
                         held_s=time.time() - opened_t)
            try:
                sock.close()
            except Exception:
                pass
