#!/usr/bin/env bash
# Install vcctrld on the USB4VC Pi. Run from the repo root on the Pi, or let
# pi/deploy.sh push it from the VM.
set -euo pipefail

PREFIX=/opt/vcctrl
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sudo mkdir -p "$PREFIX"
sudo install -m 0755 "$SRC/daemon/vcctrld.py"    "$PREFIX/vcctrld.py"
sudo install -m 0755 "$SRC/bin/vcctrl-client"    /usr/local/bin/vcctrl

sudo tee /etc/systemd/system/vcctrld.service >/dev/null <<'UNIT'
[Unit]
Description=vcctrl virtual PS/2 input server
# USB4VC must be up first: it only discovers input devices on its 0.75s scan,
# and we want it running when our uinput devices appear.
After=multi-user.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -u /opt/vcctrl/vcctrld.py
Restart=always
RestartSec=2
# Needs root for /dev/uinput and for reading the USB4VC debug log.
User=root

[Install]
WantedBy=multi-user.target
UNIT

# The virtual keyboard is a keyboard to THIS Pi as well as to the DOS box, so
# systemd would act on chords meant for the target. Ctrl-Alt-Del is the one
# that bites: without masking, `vcctrl combo ctrl alt delete` reboots the Pi.
# Found the hard way -- see docs/FINDINGS.md.
sudo systemctl mask ctrl-alt-del.target

sudo systemctl daemon-reload
sudo systemctl enable vcctrld
sudo systemctl restart vcctrld
sleep 3
sudo systemctl --no-pager --lines=15 status vcctrld || true
