#!/bin/bash
# Host-side test of patch 0002. Needs `make VARIANT=resume` first (it reads the
# PATCHED source). Extracts vc_pend_drop/vc_pend_service and the pending state
# verbatim, so the test runs the code that is flashed, not a re-typing of it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); root=$(dirname "$here")
variant=${1:-resume}   # resume (0.5.108) or onebyone (0.5.109): same state machine
src=$root/build/$variant/src/Src/main.c
[ -f "$src" ] || { echo "run: make VARIANT=$variant first" >&2; exit 2; }
echo "variant: $variant"
out=${TMPDIR:-/tmp}/vc-resume-test.$$; mkdir -p "$out"; trap 'rm -rf "$out"' EXIT
awk '/^#define VC_RESUME_BYTE_WAIT_MS/{p=1} /^void ps2mouse_update\(void\)/{p=0} p' "$src" > "$out/pend_extract.c"
grep -q 'void vc_pend_service' "$out/pend_extract.c" || { echo "extract failed" >&2; exit 2; }
cc -std=gnu99 -Wall -Werror -I"$out" -o "$out/t" "$here/resume_test.c"
"$out/t"
