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
import hashlib
import json
import os
import select
import socket
import struct
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS_GUID = "258EAFA5-E914-47DA-95CA-5AB0DC85B11D"

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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

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
                return self._json(self.cap.snapshot())
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
            if path == "/timeline.json":
                return self._json(self.cap.call("timeline", {}))
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


# --------------------------------------------------------------- capability

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
        "framestats",
        # Read-only. `level` is needed by both the page meter and by
        # bin/vcctrl-audio, which reaches the daemon over HTTPS now that plain
        # http is off -- it was missing here and the tool got a 403.
        "level", "powerlog",
        # scrub
        "pin", "timeline", "frame",
    ])

    def __init__(self, registry, bind, port):
        self.registry = registry
        self.bind = bind
        self.port = port
        self.httpd = None
        self.clients = 0
        self.ws_opened = 0
        self.ws_closed = 0
        self.ws_last_error = None
        self.ws_last_agent = None
        self.ws_dropped = 0
        self.listeners = 0
        self.lock = threading.Lock()

    def start(self):
        self.httpd = Server((self.bind, self.port), Handler)
        self.httpd.web = self
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

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
        try:
            leds = self.registry.devs.read_leds()
        except Exception:
            leds = None
        return {"video": vid._state() if vid else {"state": "unavailable"},
                "leds": leds,
                "inflight": act.get("inflight"),
                "lock": act.get("lock"),
                "last_event_age_s": act.get("last_event_age_s"),
                "seq": act.get("seq"),
                "viewers": self.clients,
                "listeners": self.listeners,
                "audio": (self.audio()._state() if self.audio()
                          else {"state": "unavailable"}),
                "ws": {"opened": self.ws_opened, "closed": self.ws_closed,
                       "dropped": self.ws_dropped,
                       "last_error": self.ws_last_error,
                       "last_agent": self.ws_last_agent},
                "caps": self.registry.report()}

    def page(self):
        with open(os.path.join(HERE, "kvm.html"), "rb") as f:
            return f.read()

    # -- the stream ---------------------------------------------------------

    def serve_ws(self, sock, agent=None, fps=20.0):
        with self.lock:
            self.clients += 1
            self.ws_opened += 1
            self.ws_last_agent = agent
        stop = threading.Event()
        held = set()
        # A list so the input thread can retune it live: the client knows how
        # it is doing far better than this side can infer.
        rate = [fps]
        try:
            threading.Thread(target=self._ws_input,
                             args=(sock, stop, held, rate), daemon=True).start()
            self._ws_frames(sock, stop, rate)
        except Exception as exc:
            with self.lock:
                self.ws_last_error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            stop.set()
            with self.lock:
                self.clients -= 1
                self.ws_closed += 1
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

    def _ws_frames(self, sock, stop, rate):
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
        last_t, last_state = 0.0, None
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
                    last_t = item[0]
                    try:
                        sock.sendall(ws_frame(item[2], opcode=0x2))
                    except Exception:
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
            try:
                sock.close()
            except Exception:
                pass

    def _ws_input(self, sock, stop, held, rate=None):
        while not stop.is_set():
            try:
                got = ws_read(sock)
            except Exception:
                break
            if got is None:
                break
            opcode, data = got
            if opcode == 0x8:
                break
            if opcode == 0x9:
                try:
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
                elif kind == "release":
                    held.clear()
                    self.call("release_all", {})
            except Exception:
                continue
        stop.set()
