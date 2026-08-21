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
  dims       SOF width/height rewritten -- decoder sizes buffers for one
             image and decodes another. Measured: shrinking does not bail
             (time falls with the scanlines, pixels are real) and a x4 grow
             takes LONGER than the original while producing four times the
             rows the entropy data holds, silently.
  chop       cut inside a marker segment, so a declared length runs past the
             end of the buffer
  sampling   per-component sampling factors rewritten, so the MCU grid the
             decoder lays out is not the one the data was written against
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
    """Rewrite the frame header's height/width -- the textbook overrun shape.

    MEASURED, because the first version of this guessed and vcctrl-94 guessed
    differently. Decoding a real 640x480 frame with a rewritten SOF, timed
    against the untouched frame at 0.65 ms:

        original    declared  640x480     307,200 px   17.2% nonzero
        shrink half declared  640x240     153,600 px   20.1% nonzero
        grow x4     declared 2560x1920  4,915,200 px   94.8% nonzero

    GROWING is the direction that matters: 4.66 M pixels fabricated from
    307 K pixels of entropy data, silently, taking LONGER than the untouched
    frame. That is a decoder writing more scanlines than its input contains,
    which is the shape we are hunting.

    A NOTE ON HOW THIS WAS MEASURED, because both of us got it wrong first and
    in opposite directions. I read `extrema` -- (0,115) on a shrunk frame --
    and called it "real picture, so the entropy data is being processed".
    Extrema needs ONE pixel at 115 to say that; it is a range, not a census,
    and it cannot distinguish a full frame from a single bright speck.
    vcctrl-94 counted nonzero pixels instead, which is the right instrument,
    and got 0.2% on their frame -- but theirs was a DARK frame, so their
    number was an artefact of content rather than of the mutation.
    Counting on a normal frame gives 20.1%, close to the original's 17.2%.

    Both readings were wrong and both pointed at the same answer. Shrinking
    asks the decoder to write LESS than the input provides, so it stops early
    and nothing runs past anything. Weight this toward grow.

    The one thing to avoid is the EXTREME grow: Pillow's decompression-bomb
    check refuses anything past ~179 M pixels before the decoder is reached,
    so a random 4000x4000 sometimes just bounces off the guard and burns a
    cycle. Everything here stays under it deliberately.

    Note also that these mutants do not RAISE, so they never appear in an
    error count. A mutant that raises is one that stopped early; the silent
    ones ran the decoder furthest.
    """
    b = bytearray(f)
    for mark in (b"\xff\xc0", b"\xff\xc1", b"\xff\xc2"):
        i = b.find(mark)
        if i > 0:
            h, w = struct.unpack(">HH", bytes(b[i + 5:i + 9]))
            # GROW-DOMINANT, and this took two people measuring it wrong
            # in opposite directions before it was settled.
            #
            # The decoder sizes its output from the DECLARED dimensions and
            # then fills what it can. So shrinking asks it to write LESS than
            # the entropy stream provides -- it stops early, and nothing is
            # asked to run past anything. Growing asks it to write MORE than
            # the input can supply, and that is the direction that exercises
            # input exhaustion and the fake-EOI recovery. Measured on a real
            # frame with no draft:
            #
            #   original      declared  640x480     307,200 px   17.2% nonzero
            #   shrink half   declared  640x240     153,600 px   20.1% nonzero
            #   grow x4       declared 2560x1920  4,915,200 px   94.8% nonzero
            #
            # 4.66 M pixels fabricated from 307 K pixels of input, silently.
            kind = rnd.random()
            if kind < 0.75:
                nh = int(h * rnd.uniform(2.0, 8.0))
                nw = w if rnd.random() < 0.5 else int(w * rnd.uniform(2.0, 6.0))
            elif kind < 0.9:
                nh, nw = h + rnd.choice([1, 7, 15, -1, -7]), w
            else:
                nh = max(1, int(h * rnd.choice([0.5, 0.25])))
                nw = w
            # Stay under Pillow's decompression-bomb guard, which refuses
            # before the decoder runs and would test nothing.
            while nh * nw > 150_000_000:
                nh //= 2
            b[i + 5:i + 9] = struct.pack(">HH", max(1, min(65535, nh)),
                                         max(1, min(65535, nw)))
            break
    return bytes(b)


def sampling(f):
    """Corrupt the per-component sampling factors in SOF.

    These set the MCU geometry -- how many blocks of each component make up
    one minimum coded unit -- so a wrong value makes the decoder lay out rows
    on a grid the entropy data was not written against. Same family as `dims`
    and it reaches a different part of the decoder's arithmetic.
    """
    b = bytearray(f)
    for mark in (b"\xff\xc0", b"\xff\xc1", b"\xff\xc2"):
        i = b.find(mark)
        if i > 0:
            ncomp = b[i + 9]
            for c in range(min(ncomp, 4)):
                off = i + 10 + c * 3 + 1          # id, HV, Tq
                if off < len(b) - 2 and rnd.random() < 0.7:
                    b[off] = rnd.choice([0x11, 0x21, 0x22, 0x12, 0x41, 0x14,
                                         0x44, 0x00])
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


# dims twice, because it is the only shape either of us has
# MEASURED running the decoder past what its input can supply.
KINDS = [truncate, bitflip, splice, tables, dims, chop, sampling, dims]
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
