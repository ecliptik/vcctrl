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
import socket
import struct
import threading
import time

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS_GUID = "258EAFA5-E914-47DA-95CA-5AB0DC85B11D"
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
            if path == "/state.json":
                return self._json(self.cap.snapshot())
            if path in ("/shot.jpg", "/lastgood.jpg"):
                cmd = "shot" if path == "/shot.jpg" else "lastgood"
                r = self.cap.call(cmd, {})
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
            if path == "/events":
                since = 0
                if "?" in self.path:
                    q = self.path.split("?", 1)[1]
                    for part in q.split("&"):
                        if part.startswith("since="):
                            since = int(part[6:] or 0)
                return self._json(self.cap.call("events", {"since": since}))
            if path == "/ws":
                return self._websocket()
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

    # -- websocket ----------------------------------------------------------

    def _websocket(self):
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
        self.cap.serve_ws(self.connection)


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
    ])

    def __init__(self, registry, bind, port):
        self.registry = registry
        self.bind = bind
        self.port = port
        self.httpd = None
        self.clients = 0
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

    def snapshot(self):
        """Everything the page needs to answer "is it stuck", in one request."""
        vid = self.video()
        act = self.call("activity", {})
        return {"video": vid._state() if vid else {"state": "unavailable"},
                "inflight": act.get("inflight"),
                "lock": act.get("lock"),
                "last_event_age_s": act.get("last_event_age_s"),
                "seq": act.get("seq"),
                "viewers": self.clients,
                "caps": self.registry.report()}

    def page(self):
        with open(os.path.join(HERE, "kvm.html"), "rb") as f:
            return f.read()

    # -- the stream ---------------------------------------------------------

    def serve_ws(self, sock):
        with self.lock:
            self.clients += 1
        stop = threading.Event()
        held = set()
        try:
            threading.Thread(target=self._ws_input, args=(sock, stop, held),
                             daemon=True).start()
            self._ws_frames(sock, stop)
        finally:
            stop.set()
            with self.lock:
                self.clients -= 1
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

    def _ws_frames(self, sock, stop):
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
                last_t = item[0]
                try:
                    sock.sendall(ws_frame(item[1], opcode=0x2))
                except Exception:
                    return
            if state != last_state:
                last_state = state
                try:
                    sock.sendall(ws_frame(
                        json.dumps({"state": state}).encode(), opcode=0x1))
                except Exception:
                    return
            # Paced just under the source rate. Sending only frames newer than
            # the last one sent means a stalled source costs nothing, and a
            # slow client simply misses frames rather than accumulating a
            # backlog -- for a KVM, skipping beats catching up.
            time.sleep(1.0 / 45.0)

    def _ws_input(self, sock, stop, held):
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
            except Exception:
                continue
        stop.set()
