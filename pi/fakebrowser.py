#!/usr/bin/env python3
"""Simulate KVM browser tabs, because four bare streamers are not a browser.

WHY. vcctrld aborted twice with glibc heap corruption. The three windows we
have say the same thing three ways:

    32.2 min  ABORT   cells running, browsers connected
    28.2 min  ABORT   cells running, browsers connected
    73.6 min  CLEAN   cells running, NO browsers -- and MORE Pillow work

A first stress used four websocket video streamers and survived. But a tab is
not a video socket. Every open tab also:

  - polls /state.json every 1.5 s (host facts: /proc and /sys reads)
  - opens a SECOND websocket for audio, streaming PCM continuously, off a
    different capability with its own ring, subprocess and reader thread --
    a path no stress had ever touched
  - fetches /shot.jpg and /lastgood.jpg, which decode JPEGs through Pillow
    IN A REQUEST THREAD rather than on the watchdog
  - fetches /timeline.json while reviewing, which decodes and differences the
    WHOLE ring per request
  - fetches /frame.jpg per scrub step
  - long-polls /events

Testing the video socket alone and calling it "browser load" is the same
mistake as counting frames and calling it "picture" -- the instrument covered
a fraction of the thing it was named after.

CORRECTION, recorded rather than quietly edited: an earlier version of this
file said `_read_pcm` was "in both crash stacks". Wrong twice. There is only
ONE stack -- faulthandler was armed AFTER the first abort, so 23:09 logged
nothing but the glibc line, and everything we know about thread state at a
crash comes from a single sample. And `_read_pcm` is a PERMANENT thread: the
daemon owns the ALSA device for its lifetime and the reader runs whether or
not anyone is listening, so it sits in that stack the way `_watchdog` does,
and sits in a healthy daemon's stack right now. It is not evidence about
browsers. The audio websocket is still worth stressing because that half IS
browser-driven and untested -- but the evidence it was present at the aborts
is the operator saying sound was connected, and that is now the only evidence.

    pi/fakebrowser.py 2 45          two tabs, forty-five minutes
    pi/fakebrowser.py 2 45 --review one of them also scrubs the timeline

Exits the instant the daemon's PID changes. Then run pi/core.sh before
anything else touches the box.
"""
import asyncio
import json
import ssl
import subprocess
import sys
import time
import urllib.request

try:
    import websockets
except ImportError:
    sys.exit("needs python3-websockets")

HOST = "vcctrl-pi.example.ts.net"
BASE = "https://" + HOST
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

TABS = int(sys.argv[1]) if len(sys.argv) > 1 else 2
MINS = float(sys.argv[2]) if len(sys.argv) > 2 else 45
REVIEW = "--review" in sys.argv

stop = False
tally = {}


def bump(k, n=1):
    tally[k] = tally.get(k, 0) + n


def pid():
    try:
        return subprocess.run(
            ["ssh", HOST.split(".")[0], "systemctl show vcctrld -p MainPID --value"],
            capture_output=True, text=True, timeout=25).stdout.strip()
    except Exception:
        return "?"


def get(path):
    with urllib.request.urlopen(BASE + path, context=CTX, timeout=30) as r:
        return r.read()


async def video(tab, end):
    """The picture. Asks a rate the way the page does, then drinks."""
    while not stop and time.time() < end:
        try:
            async with websockets.connect("wss://%s/ws" % HOST, ssl=CTX,
                                          max_size=None, ping_interval=None) as ws:
                await ws.send(json.dumps({"t": "rate", "fps": 14 if tab else 8}))
                while not stop and time.time() < end:
                    m = await asyncio.wait_for(ws.recv(), timeout=20)
                    if isinstance(m, bytes):
                        bump("video-frame")
        except Exception:
            if not stop:
                bump("video-reconnect")
                await asyncio.sleep(1)


async def audio(tab, end):
    """The sound. A whole second capability, never stressed before."""
    while not stop and time.time() < end:
        try:
            async with websockets.connect("wss://%s/wsaudio" % HOST, ssl=CTX,
                                          max_size=None, ping_interval=None) as ws:
                while not stop and time.time() < end:
                    m = await asyncio.wait_for(ws.recv(), timeout=20)
                    if isinstance(m, (bytes, bytearray)):
                        bump("audio-chunk")
        except Exception:
            if not stop:
                bump("audio-reconnect")
                await asyncio.sleep(1)


async def polls(tab, end):
    """Everything the page fetches over HTTP while it is simply open."""
    loop = asyncio.get_running_loop()
    n = 0
    while not stop and time.time() < end:
        n += 1
        try:
            await loop.run_in_executor(None, get, "/state.json")
            bump("state")
            if n % 4 == 0:                    # the veil's stills: PIL in a
                await loop.run_in_executor(None, get, "/shot.jpg")
                bump("shot")                  # request thread, not the watchdog
                await loop.run_in_executor(None, get, "/lastgood.jpg")
                bump("lastgood")
            if REVIEW and tab == 0 and n % 8 == 0:
                raw = await loop.run_in_executor(None, get, "/timeline.json")
                bump("timeline")              # decodes the WHOLE ring
                fr = json.loads(raw).get("frames") or []
                for k in fr[::max(1, len(fr) // 8)][:8]:
                    await loop.run_in_executor(
                        None, get, "/frame.jpg?seq=%d" % k["seq"])
                    bump("frame")
        except Exception:
            bump("http-err")
        await asyncio.sleep(1.5)


async def watch(end, start):
    global stop
    t0 = time.time()
    last = {}
    while not stop and time.time() < end:
        await asyncio.sleep(30)
        now = dict(tally)
        d = {k: now.get(k, 0) - last.get(k, 0) for k in now}
        p = pid()
        print("  %5.1f min  %s  pid %s"
              % ((time.time() - t0) / 60,
                 "  ".join("%s +%d" % (k, v) for k, v in sorted(d.items())), p),
              flush=True)
        last = now
        if p != start:
            print("\n*** DAEMON PID %s -> %s AFTER %.1f MIN -- IT DIED ***\n"
                  "*** run pi/core.sh NOW, before anything else touches it ***"
                  % (start, p, (time.time() - t0) / 60), flush=True)
            stop = True
            return


async def main():
    start = pid()
    end = time.time() + MINS * 60
    print("%d tabs (video + audio + polls%s) for %.0f min; daemon pid %s"
          % (TABS, " + review" if REVIEW else "", MINS, start), flush=True)
    jobs = [watch(end, start)]
    for t in range(TABS):
        jobs += [video(t, end), audio(t, end), polls(t, end)]
    await asyncio.gather(*jobs)
    print("done:", dict(tally), "survived:", pid() == start, flush=True)


asyncio.run(main())
