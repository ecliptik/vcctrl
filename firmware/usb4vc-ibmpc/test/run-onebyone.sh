#!/bin/bash
# Host-side test of patch 0003. Needs `make VARIANT=onebyone` first.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd); root=$(dirname "$here")
src=$root/build/onebyone/src/Src
[ -f "$src/main.c" ] || { echo "run: make VARIANT=onebyone first" >&2; exit 2; }
out=${TMPDIR:-/tmp}/vc-onebyone-test.$$; mkdir -p "$out"; trap 'rm -rf "$out"' EXIT
awk '/^uint8_t mouse_buf_is_full\(/{p=1} /^void mouse_buf_reset\(/{p=0} p' "$src/helpers.c" > "$out/extract_helpers.c"
awk '/^void cap_to_127\(/{p=1} /^uint32_t last_mouse_movement;/{p=0} p' "$src/main.c" > "$out/extract_main.c"
grep -q 'vc_spacing_ok' "$out/extract_main.c" && grep -q 'mouse_buf_pop' "$out/extract_helpers.c" || { echo "extract failed" >&2; exit 2; }
cc -std=gnu99 -Wall -Werror -I"$out" -o "$out/t" "$here/onebyone_test.c"
"$out/t"
