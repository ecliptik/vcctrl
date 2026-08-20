#!/usr/bin/env bash
# Push vcctrl from the VM to the Pi and install it there.
set -euo pipefail
HOST="${VCCTRL_HOST:-usb4vc}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------------------------------------------------------------------------
# REFUSE TO DEPLOY WHILE THE TARGET IS BUSY.
#
# Installing restarts vcctrld, which drops both uinput devices and the unix
# socket. To this side that is four seconds of nobody looking. To a running
# measurement cell it is fatal: the cell is 150 s of the target's time plus a
# reboot either side, it is not resumable, and a restart does not degrade it --
# it destroys it. That happened at 18:54 on 2026-08-19 and cost a real run.
#
# The social protocol ("ask for a window") had already failed twice by then, so
# this is the mechanism that replaces it. The input lock the daemon already has
# is exactly the right signal: whoever is driving the machine holds it, and a
# deploy that would interrupt them is refused rather than merely discouraged.
#
# Override with VCCTRL_FORCE=1 when you genuinely need to deploy anyway --
# deliberately awkward, and it prints who you are interrupting.
guard_busy() {
  local state owner inflight
  state="$(curl -fsS --max-time 5 \
      "${VCCTRL_WEB:-https://vcctrl-pi.example.ts.net}/state.json" 2>/dev/null || true)"
  [ -n "$state" ] || return 0        # daemon down: nothing to interrupt
  owner="$(printf '%s' "$state" | python3 -c \
      'import json,sys; print((json.load(sys.stdin).get("lock") or {}).get("owner") or "")' 2>/dev/null || true)"
  inflight="$(printf '%s' "$state" | python3 -c \
      'import json,sys; print(len(json.load(sys.stdin).get("inflight") or []))' 2>/dev/null || echo 0)"
  if [ -n "$owner" ]; then
    echo "REFUSING TO DEPLOY: the input lock is held by '$owner'." >&2
    echo "A restart would kill whatever they are running. Ask them, or set" >&2
    echo "VCCTRL_FORCE=1 if you know it is safe." >&2
    return 1
  fi
  if [ "${inflight:-0}" -gt 0 ]; then
    echo "REFUSING TO DEPLOY: $inflight command(s) in flight on the daemon." >&2
    echo "Wait for them, or set VCCTRL_FORCE=1." >&2
    return 1
  fi
  return 0
}

if [ "${VCCTRL_FORCE:-0}" != "1" ]; then
  guard_busy || exit 1
else
  echo "VCCTRL_FORCE=1: deploying without checking whether anyone is mid-run." >&2
fi

ssh "$HOST" 'rm -rf ~/vcctrl-src && mkdir -p ~/vcctrl-src'
tar -C "$SRC" -cf - daemon bin pi | ssh "$HOST" 'tar -C ~/vcctrl-src -xf -'
ssh "$HOST" 'bash ~/vcctrl-src/pi/install.sh'
