#!/usr/bin/env python3
"""Build a corpus of MALFORMED frames that the daemon would accept.

vcctrl-94's hypothesis, and it is the best one on the table: a glibc "double
free or corruption" is heap METADATA damage -- characteristically a write past
the end of an allocation. In a CPython process whose only third-party C
extensions are PIL and evdev, the classic shape is a decoder overrunning a
buffer on malformed input.

The daemon's entire validation is:

    frame = buf[i:j+2]          # SOI .. EOI
    if len(frame) >= 128: push  # and that is all

So a frame with valid markers and corrupt insides goes straight to
Image.open() in four places -- and these come off a hardware capture stick
over a pipe, which is exactly where truncated and garbled frames come from.
Every decode failure is swallowed with no counter, so nobody knows whether
this happens once an hour or never.

Frames extracted from a recorded AVI are well-formed BY SELECTION: they
survived being muxed. A stress built on them cannot find an overrun on bad
input no matter how long it runs.

Every mutant here keeps SOI at the front and EOI at the back, so all of them
would pass the daemon's filter. Six kinds, in rising order of how much they
lie to the decoder:

  truncate   entropy data cut short, EOI re-appended (a partial pipe read)
  bitflip    random bits flipped after SOS (line noise on the capture path)
  splice     head of one frame, tail of another (a missed EOI boundary)
  tables     bytes corrupted inside DQT/DHT (the decoder's own lookup tables)
  dims       SOF width/height rewritten -- decoder allocates for one size and
             decodes another, which is the textbook overrun
  chop       cut inside a marker segment, so a declared length runs past the
             end of the buffer
"""
import glob
import os
import random
import struct
import sys

SRC = sys.argv[1] if len(sys.argv) > 1 else "frames"
OUT = sys.argv[2] if len(sys.argv) > 2 else "fuzzed"
N = int(sys.argv[3]) if len(sys.argv) > 3 else 1200
SEED = int(sys.argv[4]) if len(sys.argv) > 4 else 20260821

rnd = random.Random(SEED)          # reproducible: a crash must be re-runnable
os.makedirs(OUT, exist_ok=True)
good = [open(p, "rb").read() for p in sorted(glob.glob(os.path.join(SRC, "*.jpg")))]
if not good:
    sys.exit("no source frames in %s" % SRC)


def sos_at(f):
    i = f.find(b"\xff\xda")
    return i if i > 0 else len(f) // 2


def truncate(f):
    cut = rnd.randint(sos_at(f) + 8, max(sos_at(f) + 9, len(f) - 4))
    return f[:cut] + b"\xff\xd9"


def bitflip(f):
    b = bytearray(f)
    start = sos_at(f) + 2
    for _ in range(rnd.randint(1, 40)):
        i = rnd.randrange(start, len(b) - 2)
        b[i] ^= 1 << rnd.randrange(8)
    return bytes(b)


def splice(f):
    g = rnd.choice(good)
    cut1 = rnd.randint(sos_at(f), len(f) - 2)
    cut2 = rnd.randint(sos_at(g), len(g) - 2)
    return f[:cut1] + g[cut2:]


def tables(f):
    b = bytearray(f)
    hits = 0
    for mark in (b"\xff\xdb", b"\xff\xc4"):        # DQT, DHT
        i = b.find(mark)
        while i > 0 and hits < 6:
            n = struct.unpack(">H", bytes(b[i + 2:i + 4]))[0]
            for _ in range(rnd.randint(1, 6)):
                j = rnd.randrange(i + 4, min(len(b) - 2, i + 2 + n))
                b[j] = rnd.randrange(256)
            hits += 1
            i = b.find(mark, i + 2)
    return bytes(b)


def dims(f):
    """Rewrite the frame header's height/width. The decoder sizes its buffers
    from these and then decodes whatever the entropy data actually contains."""
    b = bytearray(f)
    for mark in (b"\xff\xc0", b"\xff\xc1", b"\xff\xc2"):
        i = b.find(mark)
        if i > 0:
            h, w = struct.unpack(">HH", bytes(b[i + 5:i + 9]))
            nh = rnd.choice([h + rnd.randint(1, 400), max(1, h - rnd.randint(1, 400)),
                             rnd.randint(1, 4000)])
            nw = rnd.choice([w, w + rnd.randint(1, 400), max(1, w - rnd.randint(1, 400)),
                             rnd.randint(1, 4000)])
            b[i + 5:i + 9] = struct.pack(">HH", min(65535, nh), min(65535, nw))
            break
    return bytes(b)


def chop(f):
    """Cut inside a marker segment so its declared length runs off the end."""
    i = f.find(b"\xff\xc4")
    if i < 0:
        i = f.find(b"\xff\xdb")
    if i < 0:
        return truncate(f)
    n = struct.unpack(">H", f[i + 2:i + 4])[0]
    return f[:i + 2 + rnd.randint(2, max(3, n // 2))] + b"\xff\xd9"


KINDS = [truncate, bitflip, splice, tables, dims, chop]
made = {}
for k in range(N):
    fn = KINDS[k % len(KINDS)]
    f = fn(rnd.choice(good))
    if not f.startswith(b"\xff\xd8"):
        f = b"\xff\xd8" + f
    if not f.endswith(b"\xff\xd9"):
        f = f + b"\xff\xd9"
    if len(f) < 128:                       # the daemon would reject it
        f = f + b"\x00" * (128 - len(f)) + b"\xff\xd9"
    open(os.path.join(OUT, "z%05d.jpg" % k), "wb").write(f)
    made[fn.__name__] = made.get(fn.__name__, 0) + 1

print("wrote %d mutants to %s/: %s" % (N, OUT, made))
print("all start FFD8, end FFD9, >=128 bytes -- every one would pass "
      "_read_frames' filter")
