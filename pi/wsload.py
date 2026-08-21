#!/usr/bin/env python3
"""Load the websocket streamer path, and notice if the picture changes under it.

WHY THIS EXISTS. vcctrld aborted twice in one evening with glibc heap
corruption -- "double free or corruption", SIGABRT, no Python traceback,
because the fault is in a C extension and not in Python. Both aborts happened
with browser clients connected and websocket threads live in the stack. This
drives that path deliberately: N long-lived sockets, each asking for a rate,
each waking the daemon's sender on every new ring entry and pushing ring bytes
through SSL from a thread that is not the capture reader.

    pi/wsload.py 4 20        four sockets, twenty minutes
    pi/wsload.py 4 20 30     ...at 30 fps each

It exits the moment the daemon's PID changes, because a crash IS the result.
Then run pi/core.sh before anything else touches the box.

WHY IT MEASURES BYTES AND NOT ONLY FRAMES. The first version of this counted
frames, and its throughput column read a flat 111 fps straight through the
target losing mains -- because the capture stick keeps emitting at 30 fps when
the signal dies. The frames merely become uniform and tiny. ANY MEASURE OF
VOLUME SURVIVES THE SOURCE DYING: frames per second, socket liveness, spawn
count, all of them. Only a measure of CONTENT notices. That cost a run its
comparability and the boundary had to be recovered afterwards from the
daemon's own video.frozen event.

So each line carries bytes per frame and a distinct-frame count, and the two
together name the source state without asking the daemon:

    distinct ~= n     a real analog source. Sensor noise makes every frame
                      differ even on a dead-static DOS prompt -- measured at
                      90 distinct out of 90.
    distinct ~= 1     no lock: the stick's own constant, frames still arriving
    n == 0            starved pipe, nothing arriving at all

A run that straddles a change in either is two runs, and pooling them reports
a load that was never applied.
"""
import asyncio
import hashlib
import json
import ssl
import subprocess
import sys
import time

try:
    import websockets
except ImportError:
    sys.exit("needs python3-websockets (pip install websockets)")

HOST = "vcctrl-pi.example.ts.net"
URL = "wss://%s/ws" % HOST
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

N = int(sys.argv[1]) if len(sys.argv) > 1 else 4
MINS = float(sys.argv[2]) if len(sys.argv) > 2 else 20
FPS = float(sys.argv[3]) if len(sys.argv) > 3 else 30

stop = False
count = [0] * N
bytes_ = [0] * N
# Hashes of what ONE socket saw, so the distinct count is a property of the
# stream and not of how many sockets happen to be watching the same frame.
seen = set()


def daemon_pid():
    try:
        return subprocess.run(
            ["ssh", HOST.split(".")[0], "systemctl show vcctrld -p MainPID --value"],
            capture_output=True, text=True, timeout=25).stdout.strip()
    except Exception:
        return "?"


async def streamer(i, end):
    while not stop and time.time() < end:
        try:
            async with websockets.connect(URL, ssl=CTX, max_size=None,
                                          ping_interval=None) as ws:
                await ws.send(json.dumps({"t": "rate", "fps": FPS}))
                while not stop and time.time() < end:
                    m = await asyncio.wait_for(ws.recv(), timeout=20)
                    if isinstance(m, bytes):
                        count[i] += 1
                        bytes_[i] += len(m)
                        if i == 0:
                            seen.add(hashlib.md5(m).digest())
        except Exception as exc:
            if not stop:
                print("  [%d] socket ended: %s" % (i, type(exc).__name__),
                      flush=True)
                await asyncio.sleep(1)


async def watch(end, start_pid):
    global stop
    t0 = time.time()
    last_n, last_b, last_d = 0, 0, 0
    while not stop and time.time() < end:
        await asyncio.sleep(20)
        n, b, d = sum(count), sum(bytes_), len(seen)
        dt = time.time() - t0
        dn, db, dd = n - last_n, b - last_b, d - last_d
        # Per-interval, not cumulative: a cumulative average hides the moment
        # a source changes, which is the whole thing this column exists for.
        print("  %5.1f min  %6.0f fps  %5.1f KB/frame  %4d new distinct  pid %s"
              % (dt / 60, dn / 20.0, db / max(1, dn) / 1024.0, dd, daemon_pid()),
              flush=True)
        last_n, last_b, last_d = n, b, d
        p = daemon_pid()
        if p != start_pid:
            print("\n*** DAEMON PID %s -> %s AFTER %.1f MIN -- IT DIED ***\n"
                  "*** run pi/core.sh NOW, before anything else touches it ***"
                  % (start_pid, p, dt / 60), flush=True)
            stop = True
            return


async def main():
    start = daemon_pid()
    end = time.time() + MINS * 60
    print("%d streamers at %.0f fps for %.0f min; daemon pid %s"
          % (N, FPS, MINS, start), flush=True)
    await asyncio.gather(watch(end, start),
                         *[streamer(i, end) for i in range(N)])
    n, b = sum(count), sum(bytes_)
    print("done. %d frames, %.1f MB, %.1f KB/frame, %d distinct on socket 0. "
          "survived: %s" % (n, b / 1e6, b / max(1, n) / 1024.0, len(seen),
                            daemon_pid() == start), flush=True)


asyncio.run(main())
