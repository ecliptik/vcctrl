#!/usr/bin/env bash
# Fetch the two upstream inputs this build needs, pinned, into build/.
#
#   build/upstream/  dekuNukem/USB4VC firmware/ibmpc at UPSTREAM_REV
#   build/startup_stm32f072xb.s  ST's GCC startup for the part
#
# UPSTREAM_REV IS ae3813d, NOT MASTER. The stock release
# (firmware/releases/PBFW_IBMPC_PBID1_V0_5_7.hex) is byte-identical to Keil's
# own output checked in at ae3813d (2023-07-02). Master adds aa5f90b
# (2023-08-30, extended codes in PS/2 keyboard set 1), which no release ever
# shipped. Building from master would change the keyboard as a side effect of
# a mouse fix, on the one board the harness types through.
#
# Idempotent: an existing checkout at the right revision is left alone.
set -euo pipefail

UPSTREAM_URL=https://github.com/dekuNukem/USB4VC.git
UPSTREAM_REV=ae3813d27488ff92103f02041105c848ff7cbbf4
ST_REV=cbb5da5d48b4b5f2efacdc2f033be30f9d29889f
ST_URL=https://raw.githubusercontent.com/STMicroelectronics/cmsis-device-f0/$ST_REV/Source/Templates/gcc/startup_stm32f072xb.s
ST_SHA256=a4d44425f88296539e94903275a93490ddd695975003f8069137047d080e891c

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B="$HERE/build"
mkdir -p "$B"

if [ "$(git -C "$B/upstream" rev-parse HEAD 2>/dev/null || true)" != "$UPSTREAM_REV" ]; then
  rm -rf "$B/upstream"
  git clone -q --filter=blob:none --no-checkout "$UPSTREAM_URL" "$B/upstream"
  git -C "$B/upstream" sparse-checkout set --no-cone /firmware/ibmpc/ /LICENSE
  git -C "$B/upstream" checkout -q "$UPSTREAM_REV"
fi
echo "upstream: $(git -C "$B/upstream" log -1 --format='%h %cd %s' --date=short)"

if ! echo "$ST_SHA256  $B/startup_stm32f072xb.s" | sha256sum -c --status 2>/dev/null; then
  curl -fsSL "$ST_URL" -o "$B/startup_stm32f072xb.s.tmp"
  echo "$ST_SHA256  $B/startup_stm32f072xb.s.tmp" | sha256sum -c --status || {
    echo "fetch: ST startup file does not match its pinned hash" >&2
    rm -f "$B/startup_stm32f072xb.s.tmp"; exit 1; }
  mv "$B/startup_stm32f072xb.s.tmp" "$B/startup_stm32f072xb.s"
fi
echo "startup: ST cmsis-device-f0 ${ST_REV:0:7}, sha256 verified"
