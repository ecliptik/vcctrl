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


class _NullLock(object):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_NULLLOCK = _NullLock()

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

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj), "application/json", extra)

    # -- routes -------------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        try:
            if path == "/":
                return self._send(200, self.cap.page(), "text/html; charset=utf-8")
            if path == "/themes.css":
                with open(os.path.join(HERE, "themes.css"), "rb") as f:
                    return self._send(200, f.read(), "text/css; charset=utf-8")
            if path == "/state.json":
                # CORS on this endpoint only. The page is served from :443 and
                # must be able to ask whether :8443 is reachable from THIS
                # device before trying to open a socket there -- otherwise an
                # unreachable port and a broken WebSocket are the same 1006.
                # Read-only, tailnet-only, no secrets.
                return self._json(self.cap.snapshot(),
                                  extra={"Access-Control-Allow-Origin": "*"})
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
        try:
            while True:
                with vid.lock:
                    item = vid.ring[-1] if vid.ring else None
                if item is not None and item[0] > last_t:
                    last_t = item[0]
                    self.wfile.write(
                        ("--%s\r\nContent-Type: image/jpeg\r\n"
                         "Content-Length: %d\r\n\r\n"
                         % (boundary, len(item[2]))).encode())
                    self.wfile.write(item[2])
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
            return self.cap.serve_ws_audio(self.connection)
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
        except Exception:
            # A TLS handshake failure is one client's problem, not the
            # server's; without this the accept loop dies on the first probe.
            # DO NOT CLOSE WHILE THE READER IS STILL INSIDE THE SOCKET.
            #
            # vcctrld aborted on 2026-08-24 with "free(): invalid next size",
            # raised from SSL_free reached through a Python attribute rebind --
            # an SSLSocket being deallocated. This is where that happened: the
            # reader loops in ws_read() until `stop` is set, so at teardown it
            # is typically sitting inside SSL_read on the very object about to
            # be closed and freed. One OpenSSL SSL* used by two threads, with
            # a close racing a read, produces exactly that.
            #
            # `wlock` did not cover it. It serialises WRITERS against each
            # other, and the reader is neither a writer nor joined.
            #
            # So: stop, join, then close. And if the join times out, DO NOT
            # CLOSE -- leaking a file descriptor on a rare path is enormously
            # better than corrupting the heap of a process that is driving a
            # measurement. The leak is recorded so it cannot be silent.
            reader.join(timeout=2.0)
            if reader.is_alive():
                with self.lock:
                    self.ws_reader_stuck += 1
                    self.ws_last_error = (
                        "reader thread did not exit in 2s; socket left open "
                        "rather than freed underneath it")
            else:
                try:
                    sock.close()
                except Exception:
                    pass
            raise


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
        "level", "powerlog",
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
        # Times a websocket teardown left a socket open because its reader
        # would not exit. Non-zero means fds are leaking and the TLS thread
        # model needs the rewrite noted in serve_ws, not another timeout.
        self.ws_reader_stuck = 0

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
                # Host facts. The login banner has had these since the Pi 5
                # build and the page has not, so "is it thermally throttling
                # while I watch the stream stutter" was answerable at a shell
                # and not in the KVM. Cheap /proc and /sys reads, cached
                # briefly so a 1.5 s poll from several tabs does not re-read
                # them per tab.
                "host": self.host_facts(),
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
        # ONE writer at a time. Two threads share this socket -- the frame
        # pump and the input reader, which answers pings with pongs -- and on
        # the TLS port that socket is an SSLSocket. Concurrent writes to one
        # SSL connection interleave inside the record layer and produce a
        # corrupt record, which the peer reports as a connection error rather
        # than as anything diagnosable.
        #
        # This is the only behavioural difference between the probes that
        # stream 455 KB happily and a browser that dies after two frames: the
        # probes never make the daemon write from both threads at once.
        wlock = threading.Lock()
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
        stop = threading.Event()
        held = set()
        # A list so the input thread can retune it live: the client knows how
        # it is doing far better than this side can infer.
        rate = [fps]
        # KEEP THE HANDLE. See the teardown in `finally`: this thread must be
        # joined before the socket is closed, and a fire-and-forget daemon
        # thread cannot be.
        reader = threading.Thread(target=self._ws_input,
                                  args=(sock, stop, held, rate, wlock),
                                  daemon=True)
        try:
            reader.start()
            self._ws_frames(sock, stop, rate, wlock)
        except Exception as exc:
            with self.lock:
                self.ws_last_error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            stop.set()
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

    def _ws_say(self, sock, wlock, obj):
        """Send one JSON text frame, or give up quietly.

        Used to tell a client what its request actually became. Never raises:
        a control message that cannot be delivered must not take down a
        picture that is being delivered fine.
        """
        try:
            with (wlock or _NULLLOCK):
                sock.sendall(ws_frame(json.dumps(obj).encode(), opcode=0x1))
        except Exception:
            pass

    def _ws_frames(self, sock, stop, rate, wlock=None):
        """Send frames, dropping rather than queueing when the client is slow.

        This is the backpressure rule the plan called for and the first version
        did not implement, which is very likely why an iPhone kept dropping the
        socket: a detailed screen is ~70 KB, so 30 fps is ~17 Mbit/s, and
        sendall() on a client that cannot drink that fast BLOCKS -- frames pile
        up in the kernel buffer and the stream turns into a backlog being
        replayed. For a KVM that is strictly worse than skipping: a late frame
        has no value, because the only frame anyone wants is the current one.

        select() with a zero timeout asks the socket whether it can take a
        write right now. If it cannot, the frame is dropped and the next one is
        considered fresh. Nothing is buffered on this side either.
        """
        vid = self.video()
        # A CLIENT THAT NEVER DRAINS IS GONE, AND THIS LOOP CANNOT SEE IT.
        #
        # The send path only writes when select() says writable, so a socket
        # whose peer has vanished -- tab closed, phone asleep, network gone --
        # fills its send buffer, never becomes writable again, and is never
        # written to. No write means no error, so nothing ever raises and the
        # loop drops a frame and sleeps, forever.
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
        # THE RATE IS THIS SIDE'S NUMBER, SO THIS SIDE SAYS WHAT IT IS.
        #
        # A page reported "asking for 5 fps" beside "arriving here 9.5 fps",
        # which cannot both be true of one socket -- the loop below sleeps
        # 1/rate between frames, and measured from outside it honours the ask
        # to within 2% at 5, 15 and 30. So the two numbers disagreed because
        # one of them was a BELIEF: the page was displaying what it had asked
        # for, and nothing ever told it what it got. Same correction as the
        # ring length, which the page also used to remember rather than read.
        self._ws_say(sock, wlock, {"rate": rate[0]})
        while not stop.is_set():
            if vid is None:
                time.sleep(0.5)
                continue
            with vid.lock:
                state = vid.state
                item = vid.ring[-1] if vid.ring else None
            if item is not None and item[0] > last_t:
                try:
                    writable = select.select([], [sock], [], 0)[1]
                except Exception:
                    return
                if writable:
                    stalled_since = None
                    last_t = item[0]
                    try:
                        with (wlock or _NULLLOCK):
                            sock.sendall(ws_frame(item[2], opcode=0x2))
                    except Exception:
                        return
                    with self.lock:
                        self.ws_sent_frames += 1
                        self.ws_sent_bytes += len(item[2])
                elif stalled_since is None:
                    stalled_since = time.time()
                    with self.lock:
                        self.ws_dropped += 1
                elif time.time() - stalled_since > STALL_S:
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
                    with (wlock or _NULLLOCK):
                        sock.sendall(ws_frame(
                            json.dumps({"state": state}).encode(), opcode=0x1))
                except Exception:
                    return
            time.sleep(1.0 / max(1.0, rate[0]))

    def serve_ws_audio(self, sock):
        """Raw PCM out, on its own socket.

        A separate socket rather than a channel on the video one: adding a type
        prefix to every video frame would touch the working path to add an
        optional feature, and this way "only stream when someone is listening"
        falls out of the connection lifecycle instead of needing a flag.

        The stream sends nothing until a client connects, which is the point --
        silence still costs 1.5 Mbit/s.
        """
        aud = self.audio()
        if aud is None:
            try:
                sock.close()
            except Exception:
                pass
            return
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

    def _ws_input(self, sock, stop, held, rate=None, wlock=None):
        """Read frames from the client until `stop`.

        WAKES UP REGULARLY, which is what makes the join in serve_ws possible.
        ws_read() blocks indefinitely, so a reader that only checks `stop` at
        the top of the loop can sit in SSL_read forever while teardown waits --
        and the previous code did not wait at all, which is what let the socket
        be freed underneath it.

        `select` on the fd is not sufficient on its own for a TLS socket:
        OpenSSL may already hold a decrypted record in its own buffer, in which
        case the fd is not readable and the data is there. `pending()` is
        checked first for that reason. Getting this wrong does not hang -- it
        stalls input for up to the poll interval, which reads as a keyboard
        that sometimes ignores you, and is the kind of fault nobody reports
        precisely.
        """
        import select
        while not stop.is_set():
            try:
                if not getattr(sock, "pending", lambda: 0)():
                    r, _w, _x = select.select([sock], [], [], 0.5)
                    if not r:
                        continue
                got = ws_read(sock)
            except Exception:
                break
            if got is None:
                break
            opcode, data = got
            with self.lock:
                if len(self.ws_client_ops) < 12:
                    self.ws_client_ops.append(hex(opcode))
            if opcode == 0x8:
                with self.lock:
                    self.ws_client_ops.append("close")
                break
            if opcode == 0x9:
                try:
                    with (wlock or _NULLLOCK):
                        sock.sendall(ws_frame(data, opcode=0xA))
                except Exception:
                    break
                continue
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(data)
            except Exception:
                continue
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
                    # above is exactly where a request and a reality diverge,
                    # and a client that asks for 40 should be told it is
                    # getting 30 rather than left to infer it.
                    self._ws_say(sock, wlock, {"rate": rate[0]})
                elif kind == "release":
                    held.clear()
                    self.call("release_all", {})
            except Exception:
                continue
        stop.set()
