#!/usr/bin/env bash
# Push vcctrl from the VM to the Pi and install it there.
set -euo pipefail
HOST="${VCCTRL_HOST:-usb4vc}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ssh "$HOST" 'rm -rf ~/vcctrl-src && mkdir -p ~/vcctrl-src'
tar -C "$SRC" -cf - daemon bin pi | ssh "$HOST" 'tar -C ~/vcctrl-src -xf -'
ssh "$HOST" 'bash ~/vcctrl-src/pi/install.sh'
