#!/usr/bin/env bash
# Install vcctrld on the USB4VC Pi. Run from the repo root on the Pi, or let
# pi/deploy.sh push it from the VM.
set -euo pipefail

PREFIX=/opt/vcctrl
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

sudo mkdir -p "$PREFIX"
sudo install -m 0755 "$SRC/daemon/vcctrld.py"    "$PREFIX/vcctrld.py"
sudo install -m 0644 "$SRC/daemon/vcweb.py"      "$PREFIX/vcweb.py"
sudo install -m 0644 "$SRC/daemon/kvm.html"      "$PREFIX/kvm.html"
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
# vcweb lives beside vcctrld; the daemon imports it by name.
Environment=PYTHONPATH=/opt/vcctrl
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

# Config. Written only if absent, so a local edit survives re-installs.
# NOTE the plug's Kasa alias is "retro-rig-plug" -- it is not renamed, so anyone
# looking at the Kasa app will not obviously connect it to the g2k. That is
# recorded here rather than fixed because the operator wants the name kept.
if [ ! -f "$PREFIX/config.json" ]; then
  sudo tee "$PREFIX/config.json" >/dev/null <<'CONF'
{
  "kasa_host": "192.0.2.46",
  "_kasa_note": "TP-Link EP10, alias 'retro-rig-plug', MAC 00:00:5E:00:53:01. Legacy port-9999 protocol; if a firmware update moves it to KLAP on port 80 this stops working and needs python-kasa."
}
CONF
  echo "wrote default $PREFIX/config.json"
fi

# HTTPS over the tailnet, via Tailscale's own cert.
#
# Three features need a SECURE CONTEXT and simply do not exist on a plain-http
# origin, in any browser: AudioWorklet, navigator.clipboard.write, and
# RTCPeerConnection. Each of those reads as "broken in Safari" when the real
# cause is the scheme, which is a debugging session nobody should have to have.
#
# `tailscale serve` terminates TLS with a real Let's Encrypt cert for the
# machine's MagicDNS name and proxies to the daemon. Tailnet-only: it is not
# reachable from the internet, and Tailscale remains the authentication
# boundary. The config lives in tailscaled's state, so it survives reboots.
#
# NOTE this publishes the machine's MagicDNS name to public Certificate
# Transparency logs -- that is inherent to any publicly-trusted cert, not
# something this script chooses. The operator approved it.
#
# Idempotent: re-running only re-asserts the same mapping.
if command -v tailscale >/dev/null 2>&1; then
  TS_NAME="$(tailscale status --json 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  if [ -n "${TS_NAME:-}" ]; then
    if sudo tailscale serve --bg --https=443 "http://${WEB_BIND:-100.64.0.1}:8080" >/dev/null 2>&1; then
      echo "https://${TS_NAME}/  -> proxying to the daemon"
    else
      # Not fatal. Plain http on the tailnet still works; only the
      # secure-context features are unavailable.
      echo "note: could not configure tailscale serve (HTTPS certs enabled for this tailnet?)"
    fi
  fi
fi

sudo systemctl daemon-reload
sudo systemctl enable vcctrld
sudo systemctl restart vcctrld
sleep 3
sudo systemctl --no-pager --lines=15 status vcctrld || true
