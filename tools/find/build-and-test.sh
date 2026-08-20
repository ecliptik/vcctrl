#!/usr/bin/env bash
# Assemble FIND.COM and run it against a DOS emulator. Nothing goes to the g2k
# without passing this first -- a bad binary on that machine costs a reboot and
# a physical trip, and the whole point of the rig is that it is a controlled
# environment.
set -euo pipefail
cd "$(dirname "$0")"

nasm -f bin FIND.ASM -o FIND.COM
echo "assembled: $(stat -c%s FIND.COM) bytes"

mkdir -p testdir
cp FIND.COM T.BAT testdir/
printf 'alpha one\r\nBETA two\r\nbeta three\r\ngamma four\r\ndelta five\r\n' > testdir/TEST.TXT

# Bigger than the 2048-byte read buffer, so lines straddling a block boundary
# are exercised. That is where a chunked scanner breaks, and it is exactly the
# shape of a real doskutsu log.
python3 - <<'PY'
lines = ["tick=%d state=running pad=%s" % (i, "x"*20) for i in range(1, 4001)]
lines.insert(2500, "avg_fps=27.71 frames=8313 NEEDLE")
open("testdir/BIG.LOG", "w", newline="").write("\r\n".join(lines) + "\r\n")
PY

cat > dbx.conf <<CONF
[sdl]
autolock=false
[dosbox]
memsize=16
[cpu]
cycles=max
[autoexec]
mount c $(pwd)/testdir
c:
T.BAT
exit
CONF

rm -f testdir/RESULT.TXT
xvfb-run -a timeout 180 dosbox-x -conf dbx.conf -nolog -exit >/dev/null 2>&1 || true

echo "--- results ---"
cat testdir/RESULT.TXT

# Mechanical pass/fail, rather than a human glancing at the transcript.
python3 - <<'PY'
import sys
t = open("testdir/RESULT.TXT").read()
checks = [
    ("case-sensitive excludes BETA", "beta three" in t and "[2]BETA" in t),
    ("match sets errorlevel 0",      "RC=0-FOUND" in t),
    ("no match sets errorlevel 1",   "RC=1-NOTFOUND" in t),
    ("/C counts correctly",          "count: 2" in t),
    ("/N numbers lines",             "[3]beta three" in t),
    ("/V inverts",                   "alpha one" in t and "gamma four" in t),
    ("finds across block boundary",  "[2501]avg_fps=27.71" in t),
    ("reads stdin from a pipe",      "gamma four" in t),
    ("missing file sets errorlevel 2", "RC=2-ERROR" in t),
    ("suite ran to completion",      "=== DONE ===" in t),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print("  %-34s %s" % (n, "ok" if ok else "FAIL"))
sys.exit(1 if bad else 0)
PY
echo "ALL TESTS PASSED"
