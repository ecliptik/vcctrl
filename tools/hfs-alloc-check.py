#!/usr/bin/env python3
"""Read-only allocation check of an HFS disk image: the bitmap against every fork.

Opens the image read-only and never writes. Run it on a COPY anyway.

hfsutils' hfsck checks the volume header (MDB) and the B-trees' structure, but
it never compares the volume bitmap with the files' extents. Disk First Aid
does, and it couldn't verify the classic Mac's boot volume (an HD20 on a
Floppy Emu: "This is not an HFS disk."), so this does that part offline:

  - every data and resource fork's extents, from the catalog plus the
    extents-overflow file, and the catalog's and extents file's own extents;
  - a block claimed twice (cross-linked), past the end of the volume, or in
    use but FREE in the bitmap: a real problem, exit 1;
  - a fork with fewer blocks than its length needs: a real problem, exit 1;
  - blocks marked used that no fork owns: leaked, harmless, reported only;
  - the bitmap's free count against the header's.

Finds the volume in a raw image ('BD' at byte 1024) or one with an Apple
partition map (Apple_HFS). Written for the 512Ke's boot disk, 2026-10-16.

    tools/hfs-alloc-check.py <image>
"""
import struct, sys, collections
if len(sys.argv) != 2:
    sys.exit("usage: hfs-alloc-check.py <image>")
f = open(sys.argv[1], "rb")   # read-only, always
def rd(off, n): f.seek(off); return f.read(n)
# volume start: raw volume ('BD' at 1024) or Apple partition map
vol = None
if rd(1024, 2) == b"BD":
    vol = 0
elif rd(0, 2) == b"ER":
    blk = struct.unpack(">H", rd(2, 2))[0]
    for i in range(1, 64):
        e = rd(i * blk, 512)
        if e[:2] != b"PM": break
        start, cnt = struct.unpack(">II", e[8:16]); typ = e[48:80].split(b"\0")[0]
        if typ == b"Apple_HFS": vol = start * blk; break
if vol is None: sys.exit("no HFS volume found")
m = rd(vol + 1024, 162)
sig, crd, lsm, atrb, nmfls, vbmst, allocptr, nmalblks, alblksiz, clpsiz, alblst, nxtcnid, freebks = struct.unpack(">2sIIHHHHHIIHIH", m[:36])
xtflsize = struct.unpack(">I", m[130:134])[0]; xtext = struct.unpack(">6H", m[134:146])
ctflsize = struct.unpack(">I", m[146:150])[0]; ctext = struct.unpack(">6H", m[150:162])
print("volume at byte %d: %d blocks of %d, first at sector %d, bitmap at sector %d, header free %d" % (vol, nmalblks, alblksiz, alblst, vbmst, freebks))
bm = rd(vol + vbmst * 512, (nmalblks + 7) // 8)
def used_in_bm(b): return (bm[b >> 3] >> (7 - (b & 7))) & 1
def blk_off(b): return vol + alblst * 512 + b * alblksiz
def ext3(t): return [(t[i], t[i + 1]) for i in range(0, 6, 2) if t[i + 1]]
# read a special file (extents/catalog) given its MDB extents (+ overflow later for catalog)
def read_fork(exts, size):
    data = b"".join(rd(blk_off(s), c * alblksiz) for s, c in exts)
    return data[:size]
def leaves(data, nodesize=512):
    hdr = data[:nodesize]
    depth, root, nrecs, fnode, lnode, nsz = struct.unpack(">HIIIIH", hdr[14:34])
    n = fnode
    while n:
        node = data[n * nsz:(n + 1) * nsz]
        flink, blink, kind, height, cnt = struct.unpack(">IIbBH", node[:12])
        offs = [struct.unpack(">H", node[nsz - 2 * (i + 1):nsz - 2 * i])[0] for i in range(cnt)]
        for o in offs: yield node, o
        n = flink
# extents overflow
xt = read_fork(ext3(xtext), xtflsize)
overflow = collections.defaultdict(list)   # (fnum, fork) -> [(fabn, extents)]
for node, o in leaves(xt):
    kl = node[o]
    fork, fnum, fabn = struct.unpack(">BIH", node[o + 1:o + 8])
    d = o + 1 + kl + ((1 + kl) & 1)
    overflow[(fnum, fork)].append((fabn, ext3(struct.unpack(">6H", node[d:d + 12]))))
def all_ext(fnum, fork, first):
    out = list(first)
    for fabn, e in sorted(overflow.get((fnum, fork), [])): out += e
    return out
ct_exts = all_ext(4, 0, ext3(ctext))
ct = read_fork(ct_exts, ctflsize)
owner = {}
problems = collections.Counter(); samples = collections.defaultdict(list)
def claim(name, exts):
    for s, c in exts:
        for b in range(s, s + c):
            if b >= nmalblks:
                problems["extent past end of volume"] += 1; samples["past end"].append(name); continue
            if b in owner and owner[b] != name:
                problems["block claimed twice (cross-linked)"] += 1; samples["cross"].append((b, owner[b], name))
            owner[b] = name
            if not used_in_bm(b):
                problems["block in use but FREE in bitmap"] += 1; samples["free"].append((b, name))
claim("<extents file>", all_ext(3, 0, ext3(xtext)))
claim("<catalog file>", ct_exts)
files = dirs = 0
for node, o in leaves(ct):
    kl = node[o]
    if kl == 0: continue
    parid = struct.unpack(">I", node[o + 2:o + 6])[0]; nl = node[o + 6]
    name = node[o + 7:o + 7 + nl].decode("mac_roman", "replace")
    d = o + 1 + kl + ((1 + kl) & 1)
    typ = node[d]
    if typ == 1: dirs += 1
    elif typ == 2:
        files += 1
        r = node[d:d + 102]
        fnum, = struct.unpack(">I", r[20:24])
        dlg, dpy = struct.unpack(">II", r[26:34]); rlg, rpy = struct.unpack(">II", r[36:44])
        dext = all_ext(fnum, 0, ext3(struct.unpack(">6H", r[74:86])))
        rext = all_ext(fnum, 0xFF, ext3(struct.unpack(">6H", r[86:98])))
        for fork, lg, py, ex in (("data", dlg, dpy, dext), ("rsrc", rlg, rpy, rext)):
            have = sum(c for _, c in ex) * alblksiz
            if lg > py or py > have:
                problems["fork shorter than its length"] += 1; samples["short"].append((name, fork, lg, py, have))
            claim("%s [%s] (#%d)" % (name, fork, fnum), ex)
bm_used = sum(used_in_bm(b) for b in range(nmalblks))
leaked = [b for b in range(nmalblks) if used_in_bm(b) and b not in owner]
print("catalog: %d files, %d folders" % (files, dirs))
print("bitmap: %d used, %d free (header says %d free)%s" % (bm_used, nmalblks - bm_used, freebks,
      "" if nmalblks - bm_used == freebks else "  <-- MISMATCH"))
print("blocks owned by forks: %d; marked used but owned by nothing (leaked, harmless): %d" % (len(owner), len(leaked)))
for k, v in problems.items(): print("PROBLEM: %s: %d" % (k, v))
for k, v in samples.items(): print("  e.g. %s: %s" % (k, v[:5]))
if not problems: print("no cross-linked blocks, no in-use blocks marked free, every fork long enough")

if leaked:
    runs = []
    for b in leaked:
        if runs and runs[-1][1] == b - 1:
            runs[-1][1] = b
        else:
            runs.append([b, b])
    print("leaked runs (first, last):", runs[:10])
sys.exit(1 if problems else 0)
